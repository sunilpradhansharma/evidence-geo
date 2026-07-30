"""Question<->variation lineage tagging tests (bidirectional traceability).

Covers the computed lineage surfaced on QuestionOut and in the response detail:
  * forward  — a variation resolves ``variation_of_text`` to the CURRENT source text
  * reverse  — an original's ``variation_count`` totals ALL staged variations
               (approved + pending + rejected)
  * edits    — ``update_question`` carries variation lineage onto the new version
  * results  — ``get_response_detail`` surfaces the source for a variation's response
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.database import Base
from app.models.question import Question
from app.models.response import Response
from app.schemas import QuestionUpdate
from app.services import question_service as qsvc
from app.services import response_service as rsvc
from app.services import variation_service as svc


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    # Register every table the exercised services touch (get_response_detail joins
    # alerts, consensus, and response diffs alongside questions/variations/responses).
    from app.models import (  # noqa: F401
        alert,
        audit_log,
        consensus,
        harvested_question,
        prompt_volume,
        question,
        question_variation,
        response,
        response_diff,
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
    async def _gen(**_kw):
        return list(texts), model_id
    return _gen


# --- forward (variation -> source text) + reverse (original -> count) -------------
async def test_lineage_forward_text_and_reverse_count(session, monkeypatch):
    base = await _seed_base(session)
    monkeypatch.setattr(svc, "scan_for_pii", lambda t: [])
    monkeypatch.setattr(
        svc.generator, "generate_variations",
        _fake_gen(["What is the price of Rinvoq?", "How much is Rinvoq monthly?"]),
    )
    gen = await svc.generate_for_question(session, base.id, n=2)
    await svc.approve_variation(session, gen["variations"][0]["id"], reviewer_name="Dr. A")
    await svc.reject_variation(session, gen["variations"][1]["id"], reviewer_name="Dr. A")

    # Reverse: the original counts ALL staged variations (approved + rejected).
    await session.refresh(base)
    await qsvc.attach_variation_lineage(session, [base])
    assert base.variation_count == 2
    assert base.variation_of_text is None  # an original is not itself a variation

    # Forward: the promoted variation resolves to the CURRENT source question text.
    promoted = (await session.execute(
        select(Question).where(Question.is_variation.is_(True))
    )).scalars().first()
    await qsvc.attach_variation_lineage(session, [promoted])
    assert promoted.variation_of == base.question_id
    assert promoted.variation_of_text == base.question_text
    assert promoted.variation_count == 0


async def test_lineage_defaults_when_no_variations(session):
    base = await _seed_base(session)
    await qsvc.attach_variation_lineage(session, [base])
    assert base.variation_count == 0
    assert base.variation_of_text is None


# --- edits keep lineage (regression for the dropped-on-version-bump bug) ----------
async def test_update_question_preserves_variation_lineage(session, monkeypatch):
    base = await _seed_base(session)
    monkeypatch.setattr(svc, "scan_for_pii", lambda t: [])
    monkeypatch.setattr(svc.generator, "generate_variations",
                        _fake_gen(["What is the price of Rinvoq?"]))
    gen = await svc.generate_for_question(session, base.id, n=1)
    out = await svc.approve_variation(session, gen["variations"][0]["id"], reviewer_name="Dr. A")
    promoted = (await session.execute(
        select(Question).where(Question.question_id == out["promoted_question_id"])
    )).scalars().first()
    assert promoted.is_variation is True

    updated = await qsvc.update_question(
        session, promoted.id, QuestionUpdate(question_text="What's the monthly price of Rinvoq?")
    )
    assert updated is not None
    assert updated.is_variation is True
    assert updated.variation_of == base.question_id
    assert updated.variation_group_id == base.question_id
    assert updated.generation_method == promoted.generation_method


# --- response detail surfaces the forward source ---------------------------------
async def test_get_response_detail_surfaces_variation_source(session, monkeypatch):
    base = await _seed_base(session)
    monkeypatch.setattr(svc, "scan_for_pii", lambda t: [])
    monkeypatch.setattr(svc.generator, "generate_variations",
                        _fake_gen(["What is the price of Rinvoq?"]))
    gen = await svc.generate_for_question(session, base.id, n=1)
    out = await svc.approve_variation(session, gen["variations"][0]["id"], reviewer_name="Dr. A")

    session.add(Response(
        response_id="R-1", run_id="RUN-1", llm_name="chatgpt",
        persona=base.persona, question_id=out["promoted_question_id"],
        question_text="What is the price of Rinvoq?",
        therapeutic_area=base.therapeutic_area, brand_focus=base.brand_focus,
        domain=base.domain, response_text="Around $X per month.", status="SUCCESS",
    ))
    await session.commit()

    detail = await rsvc.get_response_detail(session, "R-1")
    assert detail is not None
    assert detail["is_variation"] is True
    assert detail["variation_of"] == base.question_id
    assert detail["variation_of_text"] == base.question_text


# --- source derivation (Manual / Prompt Volume / Discover / Variation) ------------
async def test_attach_question_source_derives_buckets(session, monkeypatch):
    from app.models.harvested_question import HarvestedQuestion

    # MANUAL: a plain analyst-authored question (also the base we vary below).
    manual = await _seed_base(session, question_id="Q-MANUAL", question_text="Manual question?")

    # PROMPT_VOLUME: carries a demand_origin from the prompt/keyword importer.
    session.add(Question(
        question_id="Q-PV", question_text="From prompt volume?", persona="Patient",
        therapeutic_area="Rheumatology", brand_focus="Rinvoq", domain="Access",
        monitoring_mode="BRAND", approval_status="APPROVED", active=True, demand_origin="PROMPT",
    ))
    # DISCOVER: promoted from a harvested question (linked by promoted_question_id).
    session.add(Question(
        question_id="Q-DISC", question_text="Harvested question?", persona="Patient",
        therapeutic_area="Rheumatology", brand_focus="Rinvoq", domain="Access",
        monitoring_mode="BRAND", approval_status="APPROVED", active=True,
    ))
    session.add(HarvestedQuestion(
        source="tavily", question_text="Harvested question?", dedupe_hash="h1",
        status="PROMOTED", promoted_question_id="Q-DISC",
    ))
    await session.commit()

    # VARIATION: generate + approve one so a promoted is_variation row exists.
    monkeypatch.setattr(svc, "scan_for_pii", lambda t: [])
    monkeypatch.setattr(svc.generator, "generate_variations", _fake_gen(["A rephrased question?"]))
    gen = await svc.generate_for_question(session, manual.id, n=1)
    out = await svc.approve_variation(session, gen["variations"][0]["id"], reviewer_name="Dr. A")

    rows = (await session.execute(select(Question))).scalars().all()
    await qsvc.attach_question_source(session, rows)
    by_qid = {q.question_id: q.source for q in rows}

    assert by_qid["Q-MANUAL"] == "MANUAL"
    assert by_qid["Q-PV"] == "PROMPT_VOLUME"
    assert by_qid["Q-DISC"] == "DISCOVER"
    assert by_qid[out["promoted_question_id"]] == "VARIATION"


# --- source: exact staged match recovers prompt-volume questions lacking demand_origin ----
async def test_attach_question_source_prompt_volume_exact_match(session):
    from app.models.prompt_volume import PromptVolumeStaging

    exact = await _seed_base(session, question_id="Q-PVX", question_text="How much does Rinvoq cost?")
    similar = await _seed_base(session, question_id="Q-SIM", question_text="A totally manual question?")
    # Exact staged match (score 1.0) -> PROMPT_VOLUME; a sub-threshold coverage match -> MANUAL.
    session.add(PromptVolumeStaging(
        batch_id="PV-1", query_text="how much does rinvoq cost", normalized_query="cost rinvoq",
        matched_question_id="Q-PVX", match_score=1.0,
    ))
    session.add(PromptVolumeStaging(
        batch_id="PV-1", query_text="rinvoq", normalized_query="rinvoq",
        matched_question_id="Q-SIM", match_score=0.6,
    ))
    await session.commit()

    await qsvc.attach_question_source(session, [exact, similar])
    assert exact.source == "PROMPT_VOLUME"   # ingested verbatim as a staged prompt
    assert similar.source == "MANUAL"        # only a coverage similarity, not created from PV
