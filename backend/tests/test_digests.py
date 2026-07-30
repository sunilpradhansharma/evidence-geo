"""BR-008a Stakeholder-Differentiated Digest tests.

Covers: profile CRUD with rules, role-differentiated alert selection (PV vs Brand),
severity ranking + top-N cap, and end-to-end generation (offline summary fallback,
in-app storage, delivery bookkeeping, and the immutable audit record).
"""
import json
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.alert import Alert
from app.models.audit_log import AuditLog
from app.models.database import Base
from app.models.question import Question
from app.models.response import Response
from app.models.scoring import ScoringRecord
from app.models.source_domain import SourceDomain
from app.schemas import DigestProfileCreate, DigestProfileUpdate, DigestRuleIn
from app.services import digest_service as svc


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from app.models import digest, workshop_summary  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _make_alert(session, *, rule, domain, ta="Obesity", persona="Provider",
                      llm="Claude", sentiment=-0.5, position="NOT_RECOMMENDED",
                      brand="Rinvoq", when=None):
    rid = f"r-{uuid.uuid4().hex[:8]}"
    sid = f"s-{uuid.uuid4().hex[:8]}"
    session.add(Response(
        response_id=rid, run_id="run-1", llm_name=llm, persona=persona,
        question_id=f"Q-{rid}", question_text=f"question about {domain}",
        therapeutic_area=ta, brand_focus=brand, domain=domain,
        response_text="…", status="SUCCESS",
    ))
    session.add(ScoringRecord(
        score_id=sid, response_id=rid, sentiment_score=sentiment,
        competitive_position=position,
    ))
    session.add(Alert(
        alert_id=str(uuid.uuid4()), score_id=sid, response_id=rid,
        rule_triggered=rule, detail=f"{rule} on {domain}",
        created_at=when or datetime.now(timezone.utc),
    ))
    await session.commit()


# The curated Workshop Questions set (Rhem.csv) is identified by matching the question
# TEXT, so a workshop question here is just a Question whose text is one of the curated
# prompts. Its designation (Persona + indication) comes from the CSV mapping, not the row.
WQ_PATIENT_RA = "Who is a good candidate for RINVOQ?"          # designation: Patient RA
WQ_HCP_PSA = "Which PsA drug is best by disease domain?"       # designation: HCP PsA


async def _add_workshop_question(session, *, prompt, question_id,
                                 persona="Patient", ta="Immunology"):
    session.add(Question(
        question_id=question_id, question_text=prompt, persona=persona,
        therapeutic_area=ta, domain="Efficacy",
    ))
    await session.commit()


async def _add_workshop_response(session, *, question_id, prompt, llm="Gemini",
                                 sentiment=0.3, position="AMONG_OPTIONS",
                                 key_claims=None, rationale=None, sources=None,
                                 brand="Rinvoq", persona="Patient",
                                 ta="Immunology", when=None):
    rid = f"r-{uuid.uuid4().hex[:8]}"
    sid = f"s-{uuid.uuid4().hex[:8]}"
    session.add(Response(
        response_id=rid, run_id="run-w", llm_name=llm, persona=persona,
        question_id=question_id, question_text=prompt,
        therapeutic_area=ta, brand_focus=brand, domain="Efficacy",
        response_text="…", status="SUCCESS",
        sources=json.dumps(sources) if sources else None,
        timestamp_utc=when or datetime.now(timezone.utc),
    ))
    session.add(ScoringRecord(
        score_id=sid, response_id=rid, sentiment_score=sentiment,
        competitive_position=position, key_claims=json.dumps(key_claims or []),
        scoring_rationale=rationale,
    ))
    await session.commit()
    return rid


# --- Profile CRUD (BR-008a.1/2/4) -----------------------------------------------
async def test_profile_crud_with_rules(session):
    profile = await svc.create_profile(session, DigestProfileCreate(
        role="PV", description="safety", cron="0 8 * * 1", timezone="America/Chicago",
        recipients=["pv@example.com"], delivery_methods=["in_app"],
        rules=[DigestRuleIn(alert_categories=["LOW_SENTIMENT"], domains=["Safety"])],
    ))
    assert profile.id is not None
    assert len(profile.rules) == 1

    updated = await svc.update_profile(session, profile.id, DigestProfileUpdate(enabled=False))
    assert updated.enabled is False

    profiles = await svc.list_profiles(session)
    assert len(profiles) == 1

    assert await svc.delete_profile(session, profile.id) is True
    assert await svc.list_profiles(session) == []


# --- Role-differentiated selection (BR-008a.2/3) --------------------------------
async def test_pv_and_brand_get_different_findings(session):
    # A safety low-sentiment alert (PV-relevant) and a commercial competitor alert (Brand).
    await _make_alert(session, rule="LOW_SENTIMENT", domain="Safety", sentiment=-0.6)
    await _make_alert(session, rule="COMPETITOR_ADVANTAGE", domain="Comparative", sentiment=-0.1)

    pv = await svc.create_profile(session, DigestProfileCreate(
        role="PV", rules=[DigestRuleIn(alert_categories=["LOW_SENTIMENT", "NOT_RECOMMENDED"], domains=["Safety"])],
    ))
    brand = await svc.create_profile(session, DigestProfileCreate(
        role="Brand", rules=[DigestRuleIn(alert_categories=["COMPETITOR_ADVANTAGE"])],
    ))

    since = datetime.now(timezone.utc) - timedelta(days=7)
    pv_findings = await svc._select_findings(session, pv, since)
    brand_findings = await svc._select_findings(session, brand, since)

    assert [f["rule"] for f in pv_findings] == ["LOW_SENTIMENT"]
    assert [f["rule"] for f in brand_findings] == ["COMPETITOR_ADVANTAGE"]


# --- Severity ranking + top-N cap (BR-008a.5) -----------------------------------
async def test_ranking_and_cap(session):
    # 6 alerts of mixed severity; expect top 5, worst-first (NOT_RECOMMENDED before others).
    await _make_alert(session, rule="LOW_SENTIMENT", domain="Efficacy", sentiment=-0.4)
    await _make_alert(session, rule="LOW_SENTIMENT", domain="Efficacy", sentiment=-0.9)
    await _make_alert(session, rule="COMPETITOR_ADVANTAGE", domain="Comparative", sentiment=-0.2)
    await _make_alert(session, rule="NOT_RECOMMENDED", domain="Efficacy", sentiment=-0.3)
    await _make_alert(session, rule="NOT_RECOMMENDED", domain="Access", sentiment=-0.7)
    await _make_alert(session, rule="LOW_SENTIMENT", domain="General", sentiment=0.0)

    profile = await svc.create_profile(session, DigestProfileCreate(role="Medical Affairs", rules=[]))
    since = datetime.now(timezone.utc) - timedelta(days=7)
    findings = await svc._select_findings(session, profile, since)

    assert len(findings) == svc.MAX_FINDINGS  # capped at 5
    assert findings[0]["rule"] == "NOT_RECOMMENDED"  # worst severity first
    # within NOT_RECOMMENDED, lower sentiment first
    assert findings[0]["sentiment_score"] == pytest.approx(-0.7)


# --- End-to-end generation (BR-008a.5/6/7) --------------------------------------
async def test_generate_digest_stores_and_audits(session, monkeypatch):
    # Force the offline summary path so the test never calls AWS.
    monkeypatch.setattr(svc, "_render_pdf", lambda *a, **k: None)

    await _make_alert(session, rule="NOT_RECOMMENDED", domain="Safety", sentiment=-0.8, brand="Rinvoq")
    profile = await svc.create_profile(session, DigestProfileCreate(
        role="PV", delivery_methods=["in_app"],
        rules=[DigestRuleIn(alert_categories=["NOT_RECOMMENDED"], domains=["Safety"])],
    ))

    run = await svc.generate_digest(session, profile, deliver=True)

    assert run.id is not None
    assert run.findings_count == 1
    assert run.summary and len(run.summary) > 0
    assert run.html and "PV" in run.html
    assert run.delivered_email is False  # SES disabled -> no-op

    # Immutable audit record maps recipient role -> digest reference (BR-008a.7).
    audits = (await session.execute(
        select(AuditLog).where(AuditLog.event == "DIGEST_DELIVERED")
    )).scalars().all()
    assert len(audits) == 1
    ctx = json.loads(audits[0].context)
    assert ctx["digest_role"] == "PV"
    assert ctx["digest_run_id"] == run.id


async def test_generate_digest_empty_findings(session, monkeypatch):
    monkeypatch.setattr(svc, "_render_pdf", lambda *a, **k: None)
    profile = await svc.create_profile(session, DigestProfileCreate(role="Brand", rules=[]))
    run = await svc.generate_digest(session, profile, deliver=True)
    assert run.findings_count == 0
    assert "No priority findings" in run.summary


# --- Regression: a profile WITH generated digests can still be deleted ----------
async def test_delete_profile_with_runs(session, monkeypatch):
    """SQLite runs with PRAGMA foreign_keys=ON and digest_runs references the profile;
    deleting a profile that has generated digests must succeed (child rows cascade)."""
    monkeypatch.setattr(svc, "_render_pdf", lambda *a, **k: None)
    await _make_alert(session, rule="NOT_RECOMMENDED", domain="Safety", sentiment=-0.8)
    profile = await svc.create_profile(session, DigestProfileCreate(
        role="PV", delivery_methods=["in_app"],
        rules=[DigestRuleIn(alert_categories=["NOT_RECOMMENDED"], domains=["Safety"])],
    ))
    run = await svc.generate_digest(session, profile, deliver=True)
    assert run.id is not None  # a DigestRun now references the profile

    assert await svc.delete_profile(session, profile.id) is True
    assert await svc.list_profiles(session) == []
    # The generated run is gone too (cascade), not orphaned.
    from app.models.digest import DigestRun
    remaining = (await session.execute(select(DigestRun))).scalars().all()
    assert remaining == []


# --- Workshop Questions insights section (marketing/business snapshot) -----------
async def test_workshop_insights_summary_aggregates(session):
    """Latest answer per (workshop question, platform): positioning by designation, the
    per-platform scorecard, the needs-attention rollup, and coverage counts."""
    await _add_workshop_question(session, prompt=WQ_PATIENT_RA, question_id="WQ1", persona="Patient")
    await _add_workshop_question(session, prompt=WQ_HCP_PSA, question_id="WQ2", persona="Provider")
    await _add_workshop_response(session, question_id="WQ1", prompt=WQ_PATIENT_RA, llm="gemini",
                                 sentiment=0.4, position="AMONG_OPTIONS",
                                 rationale="Lists RINVOQ among reasonable oral options for RA.",
                                 key_claims=["RINVOQ is an oral JAK inhibitor"])
    await _add_workshop_response(session, question_id="WQ1", prompt=WQ_PATIENT_RA, llm="gpt-4o",
                                 sentiment=-0.6, position="NOT_RECOMMENDED",
                                 key_claims=["RINVOQ carries a boxed warning"])
    await _add_workshop_response(session, question_id="WQ2", prompt=WQ_HCP_PSA, llm="gemini",
                                 sentiment=0.1, position="SECOND_LINE",
                                 key_claims=["Skyrizi shows strong skin outcomes"])

    out = await svc._workshop_insights_summary(session)
    assert out is not None
    assert out["questions_covered"] == 2
    assert out["responses"] == 3
    # Raw platform ids (gemini / gpt-4o) surface as marketer-friendly labels.
    assert set(out["models"]) == {"Gemini", "GPT-4o"}

    # Designation (Persona + indication) is derived from the Rhem.csv text mapping.
    desigs = {d["designation"] for d in out["by_designation"]}
    assert "Patient RA" in desigs and "HCP PsA" in desigs

    assert out["positioning"].get("NOT_RECOMMENDED") == 1

    # Per-platform breakdown (a general summary replaces the old per-question list).
    by_model = {m["llm"]: m for m in out["by_model"]}
    assert set(by_model) == {"Gemini", "GPT-4o"}
    assert by_model["GPT-4o"]["responses"] == 1 and by_model["GPT-4o"]["weak"] == 1
    assert by_model["Gemini"]["responses"] == 2 and by_model["Gemini"]["favorable"] == 1
    assert "answers" not in by_model["Gemini"]  # the flat Q&A list is gone
    # No cached LLM summary yet -> summary is None and a background refresh is requested.
    assert by_model["Gemini"]["summary"] is None
    assert out["needs_summary_refresh"] is True
    # Parametric answers (no Response.sources) -> no sources, flagged answered-from-knowledge.
    assert by_model["Gemini"]["sources"] is None
    assert by_model["Gemini"]["answered_from_knowledge"] is True
    assert out["citations"] is None
    # by_model is sorted worst-first; ties break toward the platform with more answers.
    assert out["by_model"][0]["llm"] == "Gemini"

    # Needs-attention surfaces the weak/negative answers worst-first for escalation.
    assert out["needs_attention_count"] == 2
    na = out["needs_attention"]
    assert na[0]["platform"] == "GPT-4o"
    assert na[0]["competitive_position"] == "NOT_RECOMMENDED"
    assert na[0]["summary"] == "RINVOQ carries a boxed warning"


async def test_workshop_platform_context_grounded(session):
    """gather_workshop_platform_context builds the per-platform LLM input: answers worst-first,
    a synopsis that prefers the scorer rationale (else the leading key claim), and a stable
    signature so a cached summary is only regenerated when the answers change."""
    await _add_workshop_question(session, prompt=WQ_PATIENT_RA, question_id="WQ1", persona="Patient")
    await _add_workshop_question(session, prompt=WQ_HCP_PSA, question_id="WQ2", persona="Provider")
    await _add_workshop_response(session, question_id="WQ1", prompt=WQ_PATIENT_RA, llm="gemini",
                                 sentiment=0.4, position="AMONG_OPTIONS",
                                 rationale="Lists RINVOQ among reasonable oral options for RA.",
                                 key_claims=["RINVOQ is an oral JAK inhibitor"])
    await _add_workshop_response(session, question_id="WQ2", prompt=WQ_HCP_PSA, llm="gemini",
                                 sentiment=0.1, position="SECOND_LINE",
                                 key_claims=["Skyrizi shows strong skin outcomes"])

    ctx = await svc.gather_workshop_platform_context(session)
    assert set(ctx) == {"gemini"}
    g = ctx["gemini"]
    assert g["label"] == "Gemini" and g["signature"]
    # Worst-position first: SECOND_LINE before AMONG_OPTIONS.
    assert g["answers"][0]["competitive_position"] == "SECOND_LINE"
    # Synopsis prefers the scorer rationale, else the leading key claim.
    assert g["answers"][-1]["summary"] == "Lists RINVOQ among reasonable oral options for RA."
    assert g["answers"][0]["summary"] == "Skyrizi shows strong skin outcomes"
    # Signature is stable across calls when nothing changed.
    ctx2 = await svc.gather_workshop_platform_context(session)
    assert ctx2["gemini"]["signature"] == g["signature"]


async def test_workshop_insights_none_without_workshop(session):
    """No curated workshop questions in the bank -> section is omitted (None)."""
    await _make_alert(session, rule="LOW_SENTIMENT", domain="Safety")  # non-workshop noise
    assert await svc._workshop_insights_summary(session) is None


async def test_workshop_citations_share_of_voice(session):
    """Citations roll up from each answer's RAW provenance (Response.sources), tagged via the
    cached SourceDomain table (no dependency on the async classification pass), into
    AbbVie/Competitor/Independent share plus the competitor domains + pages AI leaned on."""
    await _add_workshop_question(session, prompt=WQ_PATIENT_RA, question_id="WQ1", persona="Patient")
    session.add(SourceDomain(
        domain_id="d-comp", authority_domain="cosentyx.com", registrable_domain="cosentyx.com",
        publisher_name="Novartis", control_type="COMPETITOR", display_category="COMPETITOR_CONTROLLED",
    ))
    await session.commit()
    await _add_workshop_response(
        session, question_id="WQ1", prompt=WQ_PATIENT_RA, llm="Gemini", key_claims=["x"],
        sources=[{"url": "https://www.cosentyx.com/psa"}, {"url": "https://www.cosentyx.com/ra"}],
    )

    out = await svc._workshop_insights_summary(session)
    assert out is not None
    cit = out["citations"]
    assert cit is not None
    assert cit["total_citations"] == 2
    assert cit["competitor_share_pct"] == 100.0
    assert cit["top_competitors"][0]["authority_domain"] == "cosentyx.com"
    assert cit["top_competitors"][0]["publisher_name"] == "Novartis"
    assert len(cit["top_competitor_pages"]) == 2

    # Per-platform sources: Gemini's citations roll up under its own card.
    gem = next(m for m in out["by_model"] if m["llm"] == "Gemini")
    assert gem["sources"] is not None
    assert gem["sources"]["competitor"] == 2
    assert gem["sources"]["domains"][0]["authority_domain"] == "cosentyx.com"
    assert gem["sources"]["domains"][0]["control_type"] == "COMPETITOR"
    assert gem["answered_from_knowledge"] is False


async def test_workshop_insights_public_accessor(session):
    """The public workshop_insights() (what the /digests/workshop-insights endpoint calls)
    returns the same snapshot payload as the internal summary."""
    await _add_workshop_question(session, prompt=WQ_PATIENT_RA, question_id="WQ1", persona="Patient")
    await _add_workshop_response(session, question_id="WQ1", prompt=WQ_PATIENT_RA, llm="Gemini",
                                 key_claims=["RINVOQ is an oral JAK inhibitor"])
    out = await svc.workshop_insights(session)
    assert out is not None
    assert out["responses"] == 1
    assert out["questions_covered"] == 1


async def test_all_questions_insights_scope(session):
    """scope="all" summarizes EVERY tracked question (not just the curated workshop set), with an
    audience-by-area designation derived from the answers themselves; workshop scope ignores it."""
    await _add_workshop_question(session, prompt="What is the dosing for RINVOQ?",
                                 question_id="Q-GEN", persona="Provider", ta="Immunology")
    await _add_workshop_response(session, question_id="Q-GEN",
                                 prompt="What is the dosing for RINVOQ?", llm="gpt-4o",
                                 sentiment=0.2, position="AMONG_OPTIONS", persona="Provider",
                                 ta="Immunology", key_claims=["RINVOQ 15 mg once daily"])

    # The curated workshop set is not present, so the workshop scope has nothing to show...
    assert await svc.workshop_insights(session, scope="workshop") is None
    # ...but the all-questions scope includes this non-workshop answer.
    out = await svc.workshop_insights(session, scope="all")
    assert out is not None
    assert out["scope"] == "all"
    assert out["responses"] == 1 and out["questions_covered"] == 1
    assert set(out["models"]) == {"GPT-4o"}
    desigs = {d["designation"] for d in out["by_designation"]}
    assert "Provider \u00b7 Immunology" in desigs


async def test_workshop_summary_scope_migration(tmp_path):
    """An older single-key workshop_platform_summaries table (v3, pre-scope) is transparently
    rebuilt with the composite (scope, llm_name) key on startup — the cache is regenerable."""
    from sqlalchemy import inspect as _inspect, text as _text

    from app.models import database as dbmod
    # Register every model so create_all builds the tables _migrate_sqlite_schema touches.
    from app.models import (  # noqa: F401
        alert, audit_log, consensus, digest, harvested_question, model_release,
        preferred_source, preferred_source_observation, prompt_volume, prompt_volume_alert,
        question, question_variation, recommendation, recommendation_review, response,
        response_citation, response_diff, run, schedule, scoring, social_brief,
        social_comment, social_post, source_domain, theme, workshop_summary,
    )

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mig.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Simulate the pre-scope schema: drop the current table, recreate the old single-key one.
            await conn.execute(_text("DROP TABLE workshop_platform_summaries"))
            await conn.execute(_text(
                "CREATE TABLE workshop_platform_summaries ("
                "llm_name VARCHAR(64) PRIMARY KEY, summary TEXT, input_signature VARCHAR(64), "
                "responses_analyzed INTEGER, model VARCHAR(64), updated_at DATETIME)"
            ))
            await conn.execute(_text(
                "INSERT INTO workshop_platform_summaries (llm_name, summary) VALUES ('gpt-4o', 'stale')"
            ))
        async with engine.begin() as conn:
            await dbmod._migrate_sqlite_schema(conn)
            cols = await conn.run_sync(
                lambda c: {col["name"] for col in _inspect(c).get_columns("workshop_platform_summaries")}
            )
        assert "scope" in cols  # rebuilt with the new composite key
    finally:
        await engine.dispose()


async def test_workshop_platform_summary_cached(session, monkeypatch):
    """The per-platform 'general summary' is LLM-generated once, cached by input signature,
    surfaced on the by_model card, and not regenerated while the answers are unchanged."""
    from app.services import workshop_narrative

    async def _fake_chat_json(system, user, *, max_tokens=2000):
        return {"platforms": {"gemini": "Gemini positions RINVOQ cautiously, leading with safety."}}
    monkeypatch.setattr(workshop_narrative, "chat_json", _fake_chat_json)

    await _add_workshop_question(session, prompt=WQ_PATIENT_RA, question_id="WQ1", persona="Patient")
    await _add_workshop_response(session, question_id="WQ1", prompt=WQ_PATIENT_RA, llm="gemini",
                                 sentiment=-0.4, position="NOT_RECOMMENDED",
                                 rationale="Focuses on the boxed warning.")

    res = await workshop_narrative.refresh_workshop_platform_summaries(session)
    assert res["status"] == "ok" and res["platforms"] == 1

    out = await svc._workshop_insights_summary(session)
    gem = next(m for m in out["by_model"] if m["llm"] == "Gemini")
    assert gem["summary"] == "Gemini positions RINVOQ cautiously, leading with safety."
    assert out["needs_summary_refresh"] is False

    # A second refresh is a no-op while the underlying answers (signature) are unchanged.
    res2 = await workshop_narrative.refresh_workshop_platform_summaries(session)
    assert res2["status"] == "fresh"


async def test_generate_digest_includes_workshop_section(session, monkeypatch):
    """The rendered digest HTML carries the Workshop Questions section end-to-end."""
    monkeypatch.setattr(svc, "_render_pdf", lambda *a, **k: None)
    await _add_workshop_question(session, prompt=WQ_PATIENT_RA, question_id="WQ1", persona="Patient")
    await _add_workshop_response(session, question_id="WQ1", prompt=WQ_PATIENT_RA, llm="Gemini",
                                 sentiment=-0.6, position="NOT_RECOMMENDED",
                                 key_claims=["RINVOQ carries a boxed warning"])
    profile = await svc.create_profile(session, DigestProfileCreate(role="Brand", rules=[]))
    run = await svc.generate_digest(session, profile, deliver=True)

    assert run.html and "Workshop Questions" in run.html
    assert "Patient RA" in run.html
    assert "boxed warning" in run.html


# --- SES readiness diagnostics (refinement 3.1 — no per-recipient verification) ------
class _FakeSes:
    """Minimal stand-in for the boto3 SES client used by ses_status()."""

    def __init__(self, *, verified=None, max_24h=50000, raise_authz=False):
        self._verified = verified or {}        # identity -> "Success"/"Pending"
        self._max_24h = max_24h
        self._raise_authz = raise_authz

    def get_identity_verification_attributes(self, Identities):  # noqa: N803
        if self._raise_authz:
            raise Exception("AccessDenied: not authorized to perform ses:GetIdentity...")
        return {
            "VerificationAttributes": {
                i: {"VerificationStatus": self._verified.get(i, "NotStarted")}
                for i in Identities
            }
        }

    def get_send_quota(self):
        return {"Max24HourSend": self._max_24h}

    def list_verified_email_addresses(self):
        return {"VerifiedEmailAddresses": [k for k, v in self._verified.items() if "@" in k and v == "Success"]}


def _patch_ses(monkeypatch, *, sender="ema@evidence.example.com", enabled=True, client=None):
    settings = types.SimpleNamespace(
        ses_enabled=enabled, ses_sender=sender, ses_region="us-east-2", aws_region="us-east-2",
    )
    monkeypatch.setattr(svc, "get_settings", lambda: settings)
    if client is not None:
        monkeypatch.setattr(svc, "_ses_client", lambda s: client)


def test_ses_status_disabled(monkeypatch):
    _patch_ses(monkeypatch, enabled=False)
    out = svc.ses_status()
    assert out["enabled"] is False
    assert "turned off" in out["reason"]


def test_ses_status_domain_verified_production(monkeypatch):
    """Domain verified + out of sandbox => live, no per-recipient verification, no reason."""
    client = _FakeSes(verified={"evidence.example.com": "Success"}, max_24h=50000)
    _patch_ses(monkeypatch, client=client)
    out = svc.ses_status()
    assert out["sender_domain_verified"] is True
    assert out["sender_verified"] is False        # the exact address isn't itself verified
    assert out["mode"] == "production"
    assert out["reason"] is None                  # domain verification satisfies the sender


def test_ses_status_domain_verified_but_sandbox(monkeypatch):
    """Domain verified but still in sandbox => must verify recipients (reason set)."""
    client = _FakeSes(verified={"evidence.example.com": "Success"}, max_24h=200)
    _patch_ses(monkeypatch, client=client)
    out = svc.ses_status()
    assert out["mode"] == "sandbox"
    assert "sandbox" in out["reason"]


def test_ses_status_nothing_verified(monkeypatch):
    """Neither address nor domain verified => blocking reason about verification."""
    client = _FakeSes(verified={}, max_24h=200)
    _patch_ses(monkeypatch, client=client)
    out = svc.ses_status()
    assert out["sender_verified"] is False
    assert out["sender_domain_verified"] is False
    assert "verified SES identity" in out["reason"]


def test_ses_status_authz_error_is_not_blocking(monkeypatch):
    """A missing introspection IAM permission is informational, not a blocking reason."""
    client = _FakeSes(raise_authz=True, max_24h=50000)
    _patch_ses(monkeypatch, client=client)
    out = svc.ses_status()
    assert out["reason"] is None
    assert out["note"] is not None
    assert out["mode"] == "production"
