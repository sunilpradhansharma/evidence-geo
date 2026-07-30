"""Question Variations tests.

Covers the phrasing-robustness feature end to end (minus the network):
  * generator pure helpers — prompt safety rails, tolerant JSON parsing, dedupe/cap
  * staging + compliance — drafts stay DRAFT, dedupe by hash, PII flagged
  * promotion — approve creates a runnable APPROVED Question in the group; PII blocks it
  * group run scoping — only the base + APPROVED variations run (review gate preserved)
  * results rollup + divergence math
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.database import Base
from app.models.question import Question
from app.models.response import Response
from app.models.scoring import ScoringRecord
from app.services import variation_service as svc
from app.variations import generator


# --- generator pure helpers (no network, no DB) ---------------------------------
def test_build_prompt_carries_context_and_safety_rules():
    system, user = generator.build_prompt(
        question_text="How much does Rinvoq cost per month?",
        persona="Patient", therapeutic_area="Rheumatology", brand_focus="Rinvoq",
        domain="Access", n=3,
    )
    assert "Do NOT introduce" in system          # no new claims/brands/doses
    assert "PRESERVE" in system
    assert "Rinvoq" in user                        # base question echoed
    assert "Brand focus: Rinvoq" in user
    assert "3" in user                             # requested count threaded through


def test_build_prompt_disease_state_is_brandless_and_lists_competitors():
    _system, user = generator.build_prompt(
        question_text="What treatments exist for this condition?",
        monitoring_mode="DISEASE_STATE", competitor_focus=["Wegovy", "Zepbound"],
    )
    assert "Wegovy" in user and "Zepbound" in user
    assert "Brand focus" not in user               # brand-less by construction


def test_parse_variations_handles_json_shapes():
    assert generator.parse_variations('{"variations": ["a?", "b?"]}') == ["a?", "b?"]
    assert generator.parse_variations("```json\n[\"x?\", \"y?\"]\n```") == ["x?", "y?"]
    assert generator.parse_variations('{"variations": [{"question": "q1?"}]}') == ["q1?"]


def test_parse_variations_line_fallback_when_no_json():
    raw = "Here you go:\n1. What is the dose?\n2. How is it dosed?\nthanks"
    out = generator.parse_variations(raw)
    assert "What is the dose?" in out
    assert "How is it dosed?" in out
    assert "thanks" not in out                     # not a question -> dropped


def test_postprocess_dedupes_drops_base_echo_and_caps():
    base = "How much does it cost?"
    cands = [
        "How much does it cost?",       # echo of base -> dropped
        "What is the price?",
        "what is the price?!",          # normalized duplicate -> dropped
        "  Whats the monthly cost?  ",  # trimmed -> kept
        "x",                            # too short -> dropped
        "Is it expensive to buy?",      # never reached (cap)
    ]
    out = generator.postprocess(base, cands, n=2)
    assert out == ["What is the price?", "Whats the monthly cost?"]


# --- DB fixture ------------------------------------------------------------------
@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    # Register every table the service touches (audit_log for the write_audit calls).
    from app.models import (  # noqa: F401
        audit_log,
        question,
        question_variation,
        response,
        scoring,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _seed_base(session, **kw) -> Question:
    q = Question(
        question_id=kw.get("question_id", "Q-BASE"),
        question_text=kw.get("question_text", "How much does Rinvoq cost?"),
        persona=kw.get("persona", "Patient"),
        therapeutic_area=kw.get("therapeutic_area", "Rheumatology"),
        brand_focus=kw.get("brand_focus", "Rinvoq"),
        domain=kw.get("domain", "Access"),
        monitoring_mode=kw.get("monitoring_mode", "BRAND"),
        approval_status="APPROVED", active=True,
    )
    session.add(q)
    await session.commit()
    await session.refresh(q)
    return q


def _fake_gen(texts, model_id="fake-claude"):
    """A stand-in for the networked generator.generate_variations coroutine."""
    async def _gen(**_kw):
        return list(texts), model_id
    return _gen


# --- generate + staging + dedupe + PII flag -------------------------------------
async def test_generate_stages_drafts_dedupes_and_flags_pii(session, monkeypatch):
    base = await _seed_base(session)
    monkeypatch.setattr(
        svc.generator, "generate_variations",
        _fake_gen([
            "What is the price of Rinvoq?",
            "what is the price of rinvoq?!",   # normalized duplicate -> dropped by hash
            "Email me at a@b.com for pricing?",
        ]),
    )
    monkeypatch.setattr(svc, "scan_for_pii", lambda t: ["EMAIL"] if "@" in t else [])

    result = await svc.generate_for_question(session, base.id, n=3)

    assert result["created"] == 2                  # duplicate collapsed
    drafts = result["variations"]
    assert all(d["status"] == "DRAFT" for d in drafts)
    pii_row = next(d for d in drafts if "@" in d["variation_text"])
    assert pii_row["pii_flags"] == ["EMAIL"]
    clean_row = next(d for d in drafts if "@" not in d["variation_text"])
    assert clean_row["pii_flags"] is None
    # base is stamped into its own group so it lists alongside its variations
    await session.refresh(base)
    assert base.variation_group_id == base.question_id
    # nothing promoted / run yet
    promoted = (await session.execute(
        select(Question).where(Question.is_variation.is_(True))
    )).scalars().all()
    assert promoted == []


async def test_generate_missing_base_returns_not_found(session):
    assert (await svc.generate_for_question(session, 99999, n=2))["error"] == "not_found"


# --- promotion (approve) --------------------------------------------------------
async def test_approve_promotes_to_runnable_question(session, monkeypatch):
    base = await _seed_base(session)
    monkeypatch.setattr(svc, "scan_for_pii", lambda t: [])
    monkeypatch.setattr(svc.generator, "generate_variations",
                        _fake_gen(["What is the price of Rinvoq?"]))
    gen = await svc.generate_for_question(session, base.id, n=1)

    out = await svc.approve_variation(
        session, gen["variations"][0]["id"], reviewer_name="Dr. A", note="reads well"
    )
    assert out["status"] == "APPROVED"
    assert out["promoted_question_id"]

    promoted = (await session.execute(
        select(Question).where(Question.question_id == out["promoted_question_id"])
    )).scalars().first()
    assert promoted.is_variation is True
    assert promoted.approval_status == "APPROVED"
    assert promoted.active is True
    assert promoted.variation_group_id == base.question_id
    assert promoted.variation_of == base.question_id
    assert promoted.generation_method == "CLAUDE"

    _run, ids = await svc.build_group_run(session, base.question_id)
    assert base.question_id in ids and promoted.question_id in ids


async def test_approve_blocked_by_pii(session, monkeypatch):
    base = await _seed_base(session)
    monkeypatch.setattr(svc, "scan_for_pii", lambda t: [])
    monkeypatch.setattr(svc.generator, "generate_variations",
                        _fake_gen(["Call 555-123-4567 for the price?"]))
    gen = await svc.generate_for_question(session, base.id, n=1)

    monkeypatch.setattr(svc, "scan_for_pii", lambda t: ["PHONE"])   # re-lint at approval flags it
    out = await svc.approve_variation(session, gen["variations"][0]["id"])
    assert out["error"] == "pii_detected"
    assert out["pii_flags"] == ["PHONE"]
    promoted = (await session.execute(
        select(Question).where(Question.is_variation.is_(True))
    )).scalars().all()
    assert promoted == []                          # gate held — nothing promoted


async def test_approve_non_draft_rejected(session, monkeypatch):
    base = await _seed_base(session)
    monkeypatch.setattr(svc, "scan_for_pii", lambda t: [])
    monkeypatch.setattr(svc.generator, "generate_variations", _fake_gen(["Rinvoq price?"]))
    gen = await svc.generate_for_question(session, base.id, n=1)
    var_id = gen["variations"][0]["id"]
    await svc.approve_variation(session, var_id)
    again = await svc.approve_variation(session, var_id)
    assert again["error"] == "not_draft"


# --- edit + reject --------------------------------------------------------------
async def test_edit_updates_text_and_relints(session, monkeypatch):
    base = await _seed_base(session)
    monkeypatch.setattr(svc, "scan_for_pii", lambda t: ["EMAIL"] if "@" in t else [])
    monkeypatch.setattr(svc.generator, "generate_variations", _fake_gen(["Rinvoq price?"]))
    gen = await svc.generate_for_question(session, base.id, n=1)

    out = await svc.edit_variation(session, gen["variations"][0]["id"],
                                   "Reach me at a@b.com about price?")
    assert out["edited"] is True
    assert out["pii_flags"] == ["EMAIL"]
    assert out["variation_text"] == "Reach me at a@b.com about price?"


async def test_reject_marks_rejected_and_excludes_from_run(session, monkeypatch):
    base = await _seed_base(session)
    monkeypatch.setattr(svc, "scan_for_pii", lambda t: [])
    monkeypatch.setattr(svc.generator, "generate_variations", _fake_gen(["Rinvoq price monthly?"]))
    gen = await svc.generate_for_question(session, base.id, n=1)

    out = await svc.reject_variation(session, gen["variations"][0]["id"], note="off-intent")
    assert out["status"] == "REJECTED"
    _run, ids = await svc.build_group_run(session, base.question_id, include_base=False)
    assert ids == []                               # rejected never runs


# --- group run scoping ----------------------------------------------------------
async def test_build_group_run_scopes_base_and_approved_only(session, monkeypatch):
    base = await _seed_base(session)
    monkeypatch.setattr(svc, "scan_for_pii", lambda t: [])
    monkeypatch.setattr(svc.generator, "generate_variations",
                        _fake_gen(["Price of Rinvoq per month?",
                                   "Rinvoq monthly cost?",
                                   "Cost of Rinvoq therapy?"]))
    gen = await svc.generate_for_question(session, base.id, n=3)
    ids_created = [v["id"] for v in gen["variations"]]

    approved = await svc.approve_variation(session, ids_created[0])
    await svc.reject_variation(session, ids_created[1])         # leave ids_created[2] a DRAFT

    run_create, ids = await svc.build_group_run(session, base.question_id)
    assert ids == [base.question_id, approved["promoted_question_id"]]
    assert run_create.trigger == "ADHOC"
    assert run_create.question_ids == ids

    _, ids_no_base = await svc.build_group_run(session, base.question_id, include_base=False)
    assert ids_no_base == [approved["promoted_question_id"]]


async def test_build_group_run_no_runnable_returns_none(session):
    base = await _seed_base(session)
    run_create, ids = await svc.build_group_run(session, base.question_id, include_base=False)
    assert run_create is None and ids == []


# --- bank-selection expansion ("Run with Variations") ---------------------------
async def _seed_group(session, monkeypatch, qid: str, texts: list[str]) -> Question:
    """A base question with one staged draft per text (all still DRAFT)."""
    base = await _seed_base(session, question_id=qid, question_text=f"Base {qid}?")
    monkeypatch.setattr(svc, "scan_for_pii", lambda t: [])
    monkeypatch.setattr(svc.generator, "generate_variations", _fake_gen(texts))
    gen = await svc.generate_for_question(session, base.id, n=len(texts))
    base._draft_ids = [v["id"] for v in gen["variations"]]  # test-only handle
    return base


async def test_expand_adds_approved_variations_and_counts_the_rest(session, monkeypatch):
    base = await _seed_group(session, monkeypatch, "Q-A", ["A one?", "A two?", "A three?"])
    approved = await svc.approve_variation(session, base._draft_ids[0])
    await svc.reject_variation(session, base._draft_ids[1])   # third stays a DRAFT

    out = await svc.expand_with_variations(session, [base.question_id])

    assert out["question_ids"] == [base.question_id, approved["promoted_question_id"]]
    assert (out["base_count"], out["variation_count"], out["total"]) == (1, 1, 2)
    grp = out["groups"][0]
    assert grp["approved_count"] == 1
    assert grp["pending_count"] == 1        # surfaced, never run
    assert grp["rejected_count"] == 1
    assert grp["question_text"] == base.question_text


async def test_expand_keeps_a_question_with_no_approved_variations(session, monkeypatch):
    """Three staged drafts, none approved: the original still runs, alone."""
    base = await _seed_group(session, monkeypatch, "Q-B", ["B one?", "B two?"])

    out = await svc.expand_with_variations(session, [base.question_id])

    assert out["question_ids"] == [base.question_id]
    assert (out["base_count"], out["variation_count"]) == (1, 0)
    assert out["groups"][0]["pending_count"] == 2


async def test_expand_skips_unknown_and_soft_deleted_ids(session, monkeypatch):
    from datetime import datetime, timezone

    base = await _seed_group(session, monkeypatch, "Q-C", ["C one?"])
    gone = await _seed_base(session, question_id="Q-GONE")
    gone.deleted_at = datetime.now(timezone.utc)
    await session.commit()

    out = await svc.expand_with_variations(
        session, [base.question_id, "Q-GONE", "Q-NEVER-EXISTED"]
    )

    assert out["question_ids"] == [base.question_id]
    assert set(out["missing"]) == {"Q-GONE", "Q-NEVER-EXISTED"}


async def test_expand_never_asks_the_same_question_twice(session, monkeypatch):
    """A base selected twice, and passed alongside its own variation, still runs once each."""
    base = await _seed_group(session, monkeypatch, "Q-D", ["D one?"])
    approved = await svc.approve_variation(session, base._draft_ids[0])
    var_qid = approved["promoted_question_id"]

    out = await svc.expand_with_variations(
        session, [base.question_id, base.question_id, var_qid]
    )

    assert out["question_ids"] == [base.question_id, var_qid]
    # the headline may never overstate the run: base + variations == total
    assert out["base_count"] + out["variation_count"] == out["total"] == 2


async def test_expand_agrees_with_build_group_run(session, monkeypatch):
    """Both paths read the same membership rule, so they cannot drift apart."""
    base = await _seed_group(session, monkeypatch, "Q-E", ["E one?", "E two?"])
    await svc.approve_variation(session, base._draft_ids[0])
    await svc.approve_variation(session, base._draft_ids[1])

    _run, group_ids = await svc.build_group_run(session, base.question_id)
    out = await svc.expand_with_variations(session, [base.question_id])
    assert out["question_ids"] == group_ids


async def test_expand_covers_a_multi_question_selection(session, monkeypatch):
    a = await _seed_group(session, monkeypatch, "Q-F", ["F one?"])
    approved_a = await svc.approve_variation(session, a._draft_ids[0])
    b = await _seed_group(session, monkeypatch, "Q-G", ["G one?", "G two?"])
    approved_b1 = await svc.approve_variation(session, b._draft_ids[0])
    approved_b2 = await svc.approve_variation(session, b._draft_ids[1])
    plain = await _seed_base(session, question_id="Q-H")   # never had variations

    out = await svc.expand_with_variations(
        session, [a.question_id, b.question_id, plain.question_id]
    )

    assert out["question_ids"] == [
        a.question_id, approved_a["promoted_question_id"],
        b.question_id, approved_b1["promoted_question_id"], approved_b2["promoted_question_id"],
        plain.question_id,
    ]
    assert (out["base_count"], out["variation_count"], out["total"]) == (3, 3, 6)
    assert [g["approved_count"] for g in out["groups"]] == [1, 2, 0]


async def test_expand_api_contract(session, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from httpx import ASGITransport

    from app.api import variations as variations_api
    from app.models.database import get_db

    base = await _seed_group(session, monkeypatch, "Q-I", ["I one?"])
    approved = await svc.approve_variation(session, base._draft_ids[0])

    async def _override():
        yield session

    app = FastAPI()
    app.include_router(variations_api.router)
    app.dependency_overrides[get_db] = _override
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ok = await client.post("/variations/expand",
                               json={"question_ids": [base.question_id]})
        assert ok.status_code == 200
        body = ok.json()
        assert body["question_ids"] == [base.question_id, approved["promoted_question_id"]]
        assert body["total"] == 2

        empty = await client.post("/variations/expand", json={"question_ids": []})
        assert empty.status_code == 422        # an empty selection is a caller bug, not a run


# --- listing --------------------------------------------------------------------
async def test_list_group_and_groups(session, monkeypatch):
    base = await _seed_base(session)
    monkeypatch.setattr(svc, "scan_for_pii", lambda t: [])
    monkeypatch.setattr(svc.generator, "generate_variations",
                        _fake_gen(["Rinvoq price A?", "Rinvoq price B?"]))
    await svc.generate_for_question(session, base.id, n=2)

    grp = await svc.list_group(session, base.question_id)
    assert grp["base"]["question_id"] == base.question_id
    assert grp["counts"]["draft"] == 2

    groups = await svc.list_groups(session)
    assert groups["count"] == 1
    assert groups["groups"][0]["draft_count"] == 2


# --- divergence math (pure) -----------------------------------------------------
def test_summarize_divergence_consistent_group():
    stats = [
        {"question_id": "b", "is_base": True, "mean_sentiment": 0.60,
         "modal_position": "AMONG_OPTIONS", "mention_rate": 1.0},
        {"question_id": "v1", "is_base": False, "mean_sentiment": 0.62,
         "modal_position": "AMONG_OPTIONS", "mention_rate": 1.0},
        {"question_id": "v2", "is_base": False, "mean_sentiment": 0.58,
         "modal_position": "AMONG_OPTIONS", "mention_rate": 1.0},
    ]
    s = svc.summarize_divergence(stats)
    assert s["position_agreement"] == 1.0
    assert s["group_modal_position"] == "AMONG_OPTIONS"
    assert s["outliers"] == []
    assert s["consistency_score"] > 0.9


def test_summarize_divergence_flags_outlier():
    stats = [
        {"question_id": "b", "is_base": True, "mean_sentiment": 0.60,
         "modal_position": "FIRST_LINE_RECOMMENDED", "mention_rate": 1.0},
        {"question_id": "v1", "is_base": False, "mean_sentiment": 0.55,
         "modal_position": "FIRST_LINE_RECOMMENDED", "mention_rate": 1.0},
        {"question_id": "v2", "is_base": False, "mean_sentiment": -0.50,
         "modal_position": "NOT_RECOMMENDED", "mention_rate": 0.0},
    ]
    s = svc.summarize_divergence(stats)
    outliers = {o["question_id"]: set(o["reasons"]) for o in s["outliers"]}
    assert outliers == {"v2": {"sentiment", "position"}}
    assert s["consistency_score"] < 0.9


def test_summarize_divergence_empty():
    s = svc.summarize_divergence([])
    assert s["consistency_score"] is None
    assert s["variations_scored"] == 0


# --- results rollup -------------------------------------------------------------
async def test_group_results_rollup(session, monkeypatch):
    base = await _seed_base(session)
    monkeypatch.setattr(svc, "scan_for_pii", lambda t: [])
    monkeypatch.setattr(svc.generator, "generate_variations", _fake_gen(["Rinvoq monthly price?"]))
    gen = await svc.generate_for_question(session, base.id, n=1)
    approved = await svc.approve_variation(session, gen["variations"][0]["id"])
    var_qid = approved["promoted_question_id"]

    run_id = "run-var-1"
    rows = [
        ("r1", base.question_id, "Claude", 0.6, "AMONG_OPTIONS", '["Rinvoq"]'),
        ("r2", base.question_id, "GPT-4", 0.5, "AMONG_OPTIONS", '["Rinvoq"]'),
        ("r3", var_qid, "Claude", 0.55, "AMONG_OPTIONS", '["Rinvoq"]'),
        ("r4", var_qid, "GPT-4", -0.40, "NOT_RECOMMENDED", None),
    ]
    for rid, qid, llm, sent, pos, mentions in rows:
        session.add(Response(
            response_id=rid, run_id=run_id, llm_name=llm, persona="Patient", question_id=qid,
            question_text="q", therapeutic_area="Rheumatology", domain="Access",
            response_text="...", status="SUCCESS",
        ))
        session.add(ScoringRecord(
            score_id=f"s-{rid}", response_id=rid, score_version=1, sentiment_score=sent,
            competitive_position=pos, brand_mentions=mentions,
        ))
    await session.commit()

    res = await svc.group_results(session, base.question_id)
    assert res["run_id"] == run_id
    assert len(res["variations"]) == 2
    base_row = next(v for v in res["variations"] if v["is_base"])
    assert base_row["mean_sentiment"] == pytest.approx(0.55)
    assert base_row["modal_position"] == "AMONG_OPTIONS"
    assert len(base_row["answers"]) == 2
    assert res["summary"]["variations_scored"] == 2
    assert res["summary"]["group_modal_position"] == "AMONG_OPTIONS"
