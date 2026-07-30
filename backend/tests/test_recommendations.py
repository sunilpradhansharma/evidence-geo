"""Tests for the GEO Intervention Recommendation engine (BR-012).

Covers: generation + approved content-type enforcement (BR-012.1/.2), impact ranking +
CSV export (BR-012.3), MLR labelling constant (BR-012.4 data side), supporting evidence
arrays (BR-012.5), filtering by TA/persona/model (BR-012.6), and the deterministic SEMrush
stub fallback. External calls (LLM + SEMrush) are monkeypatched so tests are hermetic.
"""
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.database import Base

# Import models so they register on Base.metadata before create_all.
from app.models import (  # noqa: F401
    audit_log,
    preferred_source as _pref_mod,
    preferred_source_observation as _prefobs_mod,
    recommendation,
    recommendation_review as _review_mod,
    response as _response_mod,
    response_citation as _rc_mod,
    scoring as _scoring_mod,
    source_domain as _sd_mod,
)
from app.models.preferred_source import PreferredSource
from app.models.preferred_source_observation import PreferredSourceObservation
from app.models.response import Response
from app.models.response_citation import ResponseCitation
from app.models.scoring import ScoringRecord
from app.models.source_domain import (
    CONTROL_ABBVIE,
    CONTROL_COMPETITOR,
    CONTROL_INDEPENDENT,
    CONTROL_UNKNOWN,
    SourceDomain,
)
from app.remediation import semrush
from app.remediation.prompts import APPROVED_CONTENT_TYPES
from app.services import recommendation_service as svc


@pytest.fixture
async def session():
    engine_ = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine_.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine_, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine_.dispose()


# --- helpers ---------------------------------------------------------------------
async def _seed_gap(
    session,
    *,
    response_id,
    position="SECOND_LINE",
    persona="Provider",
    ta="Immunology",
    brand="Humira",
    llm="gpt-4o",
    competitor="Stelara",
    competitor_sent=0.6,
    sources=None,
):
    session.add(Response(
        response_id=response_id, run_id="run1", llm_name=llm, persona=persona,
        question_id=f"q-{response_id}", question_text="Best biologic for RA?",
        therapeutic_area=ta, brand_focus=brand, domain="Comparative",
        response_text="The competitor is generally preferred here.", status="SUCCESS",
        sources=json.dumps(sources) if sources is not None else None,
    ))
    session.add(ScoringRecord(
        score_id=f"s-{response_id}", response_id=response_id, score_version=1,
        competitive_position=position, sentiment_score=-0.2,
        brand_mentions=json.dumps([
            {"brand": brand, "is_competitor": False, "sentiment": -0.2},
            {"brand": competitor, "is_competitor": True, "sentiment": competitor_sent},
        ]),
        key_claims=json.dumps([]), scoring_rationale="x", scored_by="AI",
    ))
    await session.commit()


async def _ensure_domain(session, domain, control, authority="OTHER"):
    """Get-or-create a classified SourceDomain row (curated-style, no enrichment)."""
    existing = (await session.execute(
        select(SourceDomain).where(SourceDomain.authority_domain == domain)
    )).scalar_one_or_none()
    if existing:
        return existing
    display = {
        CONTROL_ABBVIE: "ABBVIE_CONTROLLED",
        CONTROL_COMPETITOR: "COMPETITOR_CONTROLLED",
    }.get(control, authority)
    d = SourceDomain(
        domain_id=str(uuid.uuid4()), authority_domain=domain, registrable_domain=domain,
        publisher_name=domain, control_type=control, authority_type=authority,
        display_category=display, verification="UNKNOWN",
    )
    session.add(d)
    await session.flush()
    return d


async def _seed_citation(
    session, *, response_id, domain, control, count=1,
    ta="Immunology", brand="Humira", llm="gpt-4o", persona="Provider",
):
    """Seed a classified citation (SourceDomain + ResponseCitation) — the fused A/C graph."""
    d = await _ensure_domain(session, domain, control)
    session.add(ResponseCitation(
        citation_id=str(uuid.uuid4()), response_id=response_id, run_id="run1",
        domain_id=d.domain_id, authority_domain=domain, llm_name=llm, persona=persona,
        therapeutic_area=ta, brand_focus=brand, citation_count=count,
        citation_urls=json.dumps([f"https://{domain}/x"]), first_citation_position=0,
    ))
    await session.commit()


async def _ok_chat_json(system, user, **kwargs):
    return {
        "content_type": "FAQ",
        "recommended_action": "Publish an FAQ comparing Humira and Stelara for RA.",
        "rationale": "Closes the citation gap the AI relied on.",
    }


def _patch(monkeypatch, *, chat=_ok_chat_json, volume=1000, authority=50, source="stub"):
    monkeypatch.setattr("app.remediation.engine.chat_json", chat)

    async def _fake_enrich(domain, *, keyword):
        return {"search_volume": volume, "domain_authority": authority, "source": source}

    monkeypatch.setattr("app.remediation.semrush.enrich", _fake_enrich)


# --- BR-012.1 / .2 : generation + approved content type ---------------------------
async def test_generate_produces_approved_content_type(session, monkeypatch):
    _patch(monkeypatch)
    await _seed_gap(session, response_id="r1")

    summary = await svc.generate(session, limit=10)
    assert summary["generated"] == 1
    assert summary["gaps_found"] == 1

    result = await svc.list_recommendations(session)
    assert result["count"] == 1
    rec = result["items"][0]
    assert rec["recommended_action"]  # non-empty (BR-012.1)
    assert rec["content_type"] in APPROVED_CONTENT_TYPES  # BR-012.2
    assert rec["mlr_status"] == "UNAPPROVED_SUGGESTION"  # BR-012.4 (data side)


async def test_offlist_content_type_falls_back_to_approved(session, monkeypatch):
    async def _bad(system, user, **kwargs):
        return {"content_type": "Blog Post", "recommended_action": "Do something", "rationale": "y"}

    _patch(monkeypatch, chat=_bad)
    await _seed_gap(session, response_id="r1")
    await svc.generate(session)

    rec = (await svc.list_recommendations(session))["items"][0]
    assert rec["content_type"] in APPROVED_CONTENT_TYPES
    assert rec["content_type"] == "FAQ"  # fallback


# --- BR-012.3 : ranking by impact_score + evidence (BR-012.5) ---------------------
async def test_ranking_by_volume(session, monkeypatch):
    async def _enrich(domain, *, keyword):
        vol = 50000 if keyword == "Stelara" else 300
        return {"search_volume": vol, "domain_authority": 60, "source": "stub"}

    monkeypatch.setattr("app.remediation.engine.chat_json", _ok_chat_json)
    monkeypatch.setattr("app.remediation.semrush.enrich", _enrich)

    await _seed_gap(session, response_id="r1", competitor="Stelara")
    await _seed_gap(session, response_id="r2", competitor="Enbrel")
    await svc.generate(session)

    items = (await svc.list_recommendations(session))["items"]
    assert len(items) == 2
    assert items[0]["impact_score"] >= items[1]["impact_score"]  # sorted desc
    assert items[0]["outperforming_competitor"] == "Stelara"  # higher volume wins
    for it in items:
        assert it["outperforming_competitor"]  # BR-012.5
        assert isinstance(it["missing_citations"], list)  # BR-012.5


async def test_severity_weights_ranking(session, monkeypatch):
    _patch(monkeypatch, volume=1000)  # equal volume -> severity decides
    await _seed_gap(session, response_id="r1", position="SECOND_LINE")
    await _seed_gap(session, response_id="r2", position="NOT_RECOMMENDED")
    await svc.generate(session)

    items = (await svc.list_recommendations(session))["items"]
    assert items[0]["competitive_position"] == "NOT_RECOMMENDED"  # severity 2.0 > 1.0


def test_to_csv_is_valid():
    items = [{
        "rec_id": "1", "created_at": "2026-07-10T00:00:00", "impact_score": 5.7,
        "content_type": "FAQ", "recommended_action": "Publish FAQ", "rationale": "why",
        "competitive_position": "SECOND_LINE", "outperforming_competitor": "Stelara",
        "competitor_domain": "stelara.com",
        "missing_citations": ["nih.gov — Study", "jama.org — Trial"],
        "therapeutic_area": "Immunology", "indication": None, "persona": "Provider",
        "brand_focus": "Humira", "llm_name": "gpt-4o", "search_volume": 50000,
        "domain_authority": 60, "metrics_source": "stub", "volume_multiplier": 5.7,
        "mlr_status": "UNAPPROVED_SUGGESTION",
    }]
    csv_text = svc.to_csv(items)
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("rec_id,created_at,impact_score")
    assert "Stelara" in csv_text
    assert "nih.gov — Study | jama.org — Trial" in csv_text  # list joined for CSV


# --- BR-012.5 : competitor domain + missing citations from response sources -------
async def test_competitor_domain_and_missing_citations_from_sources(session, monkeypatch):
    _patch(monkeypatch)
    sources = [
        {"url": "https://www.stelara.com/psoriasis", "title": "Stelara efficacy", "domain": "stelara.com"},
        {"url": "https://humira.com/ra", "title": "Humira info", "domain": "humira.com"},
    ]
    await _seed_gap(session, response_id="r1", competitor="Stelara", brand="Humira", sources=sources)
    await svc.generate(session)

    rec = (await svc.list_recommendations(session))["items"][0]
    assert rec["competitor_domain"] == "stelara.com"
    joined = " ".join(rec["missing_citations"]).lower()
    assert "stelara.com" in joined          # competitor citation surfaced
    assert "humira.com" not in joined       # brand's own citation excluded ("missing" = brand absent)


# --- BR-012.6 : filtering by persona / model / therapeutic area -------------------
async def test_filter_by_persona_model_and_ta(session, monkeypatch):
    _patch(monkeypatch)
    await _seed_gap(session, response_id="r1", persona="Provider", llm="gpt-4o", ta="Immunology")
    await _seed_gap(session, response_id="r2", persona="Patient", llm="gemini", ta="Oncology")
    await svc.generate(session)

    prov = await svc.list_recommendations(session, persona="Provider")
    assert prov["count"] == 1 and prov["items"][0]["persona"] == "Provider"

    gem = await svc.list_recommendations(session, llm_name="gemini")
    assert gem["count"] == 1 and gem["items"][0]["llm_name"] == "gemini"

    onc = await svc.list_recommendations(session, therapeutic_area="Oncology")
    assert onc["count"] == 1 and onc["items"][0]["therapeutic_area"] == "Oncology"


# --- SEMrush deterministic stub fallback ------------------------------------------
async def test_semrush_stub_deterministic(monkeypatch):
    monkeypatch.setattr(
        "app.remediation.semrush.get_settings",
        lambda: SimpleNamespace(
            semrush_api_key="", semrush_base_url="https://api.semrush.com", semrush_database="us"
        ),
    )
    a = await semrush.enrich("stelara.com", keyword="Stelara")
    b = await semrush.enrich("stelara.com", keyword="Stelara")
    assert a["source"] == "stub"
    assert a == b  # deterministic
    assert 200 <= a["search_volume"] <= 50000
    assert 20 <= a["domain_authority"] <= 90


async def test_generate_with_no_gaps_is_empty(session, monkeypatch):
    _patch(monkeypatch)
    summary = await svc.generate(session)
    assert summary["generated"] == 0
    assert summary["gaps_found"] == 0
    result = await svc.list_recommendations(session)
    assert result["count"] == 0 and result["items"] == []


# --- BR-005 : citation-gap multiplier (B) + content brief / suggested questions (D/E) ---
async def test_citation_gap_multiplier_and_brief_questions(session, monkeypatch):
    _patch(monkeypatch, volume=1000)
    sources = [
        {"url": "https://www.stelara.com/psoriasis", "title": "Stelara efficacy", "domain": "stelara.com"},
        {"url": "https://nih.gov/study", "title": "RA biologics review", "domain": "nih.gov"},
    ]
    await _seed_gap(session, response_id="r1", competitor="Stelara", brand="Humira", sources=sources)
    await svc.generate(session)

    rec = (await svc.list_recommendations(session))["items"][0]
    # 2 trusted sources omit Humira (stelara.com + nih.gov) + 1 references the competitor.
    assert rec["citation_gap_score"] >= 2.0
    assert rec["citation_multiplier"] > 1.0
    # Brief + questions are always populated (LLM output or deterministic fallback).
    assert isinstance(rec["content_brief"], list) and rec["content_brief"]
    assert isinstance(rec["suggested_questions"], list) and rec["suggested_questions"]


async def test_citation_multiplier_neutral_without_sources(session, monkeypatch):
    _patch(monkeypatch, volume=1000)
    await _seed_gap(session, response_id="r1", sources=None)  # no grounding -> no citation gap
    await svc.generate(session)
    rec = (await svc.list_recommendations(session))["items"][0]
    assert rec["citation_gap_score"] == 0.0
    assert rec["citation_multiplier"] == 1.0  # neutral -> ranking unchanged


# --- A : citation opportunities from the classified graph (fused with Source Authority) ---
async def test_citation_opportunities_ranks_brand_absent_domains(session, monkeypatch):
    _patch(monkeypatch)
    # r1 is a weak-position (SECOND_LINE) gap; classify its citations by control tier.
    await _seed_gap(session, response_id="r1", brand="Humira", competitor="Stelara")
    await _seed_citation(session, response_id="r1", domain="nih.gov", control=CONTROL_INDEPENDENT)
    await _seed_citation(session, response_id="r1", domain="humira.com", control=CONTROL_ABBVIE)
    await _seed_citation(session, response_id="r1", domain="stelara.com", control=CONTROL_COMPETITOR)

    result = await svc.citation_opportunities(session)
    domains = [it["domain"] for it in result["items"]]
    assert "nih.gov" in domains          # independent authority -> earnable opportunity
    assert "humira.com" not in domains   # AbbVie-controlled -> already ours
    assert "stelara.com" not in domains  # competitor-controlled -> shown in share of voice
    assert result["responses_with_citations"] == 1
    nih = next(it for it in result["items"] if it["domain"] == "nih.gov")
    assert nih["control_type"] == CONTROL_INDEPENDENT
    assert nih["weak_position_count"] == 1  # cited in a SECOND_LINE gap


async def test_citation_opportunities_flags_preferred_sources_first(session, monkeypatch):
    _patch(monkeypatch)
    await _seed_gap(session, response_id="r1", brand="Humira", competitor="Stelara")
    await _seed_citation(session, response_id="r1", domain="nih.gov", control=CONTROL_INDEPENDENT)
    await _seed_citation(session, response_id="r1", domain="mayoclinic.org", control=CONTROL_INDEPENDENT)
    # Medical Affairs designates nih.gov as preferred for the TA.
    session.add(PreferredSource(
        pref_id="p1", therapeutic_area="Immunology", authority_domain="nih.gov", active=True,
    ))
    await session.commit()

    result = await svc.citation_opportunities(session, therapeutic_area="Immunology")
    assert result["items"][0]["domain"] == "nih.gov"       # preferred floats to the top
    assert result["items"][0]["is_preferred"] is True


# --- C : share-of-citation benchmark (control-based, consistent with Source Authority) ---
async def test_share_of_citation_benchmark(session, monkeypatch):
    _patch(monkeypatch)
    await _seed_gap(session, response_id="r1", brand="Humira", competitor="Stelara")
    await _seed_citation(session, response_id="r1", domain="humira.com", control=CONTROL_ABBVIE)
    await _seed_citation(session, response_id="r1", domain="stelara.com", control=CONTROL_COMPETITOR)
    await _seed_citation(session, response_id="r1", domain="nih.gov", control=CONTROL_INDEPENDENT)
    await _seed_citation(session, response_id="r1", domain="example.org", control=CONTROL_UNKNOWN)

    result = await svc.share_of_citation(session)
    assert result["total_citations"] == 4
    assert result["response_count"] == 1
    assert result["abbvie_share_pct"] == pytest.approx(25.0, abs=0.2)
    assert result["competitor_share_pct"] == pytest.approx(25.0, abs=0.2)
    assert result["independent_share_pct"] == pytest.approx(25.0, abs=0.2)
    assert next(v for v in result["voice"] if v["control_type"] == CONTROL_UNKNOWN)["share_pct"] == pytest.approx(25.0, abs=0.2)
    assert "stelara.com" in [c["authority_domain"] for c in result["competitors"]]


# --- Fusion: preferred-source gaps rank by how often AI omits MA-preferred domains ---
async def test_preferred_source_gaps_ranks_by_absence(session, monkeypatch):
    session.add(PreferredSource(
        pref_id="p1", therapeutic_area="Immunology", authority_domain="nih.gov",
        active=True, note="Key guideline source",
    ))
    for i, present in enumerate([False, False, True]):  # 2 absent, 1 present -> 66.7%
        session.add(PreferredSourceObservation(
            observation_id=f"o{i}", preferred_source_id="p1", run_id="run1",
            response_id=f"r{i}", llm_name="gpt-4o", therapeutic_area="Immunology",
            authority_domain="nih.gov", was_present=present,
        ))
    await session.commit()

    result = await svc.preferred_source_gaps(session, therapeutic_area="Immunology")
    assert result["configured"] == 1
    item = result["items"][0]
    assert item["authority_domain"] == "nih.gov"
    assert item["observations"] == 3 and item["absent"] == 2
    assert item["absence_pct"] == pytest.approx(66.7, abs=0.2)


# --- Query fanouts: the real search terms grounded models ran ---------------------
async def test_query_fanouts_aggregates_search_queries(session, monkeypatch):
    for rid, queries in [
        ("r1", ["humira vs stelara", "best biologic RA"]),
        ("r2", ["Humira vs Stelara"]),  # case-variant of r1's first query
    ]:
        session.add(Response(
            response_id=rid, run_id="run1", llm_name="gpt-4o", persona="Provider",
            question_id=f"q-{rid}", question_text="RA biologics?", therapeutic_area="Immunology",
            brand_focus="Humira", domain="Comparative", response_text="...", status="SUCCESS",
            search_queries=json.dumps(queries),
        ))
    await session.commit()

    result = await svc.query_fanouts(session)
    assert result["responses_with_queries"] == 2
    top = result["items"][0]
    assert top["query"].lower() == "humira vs stelara"
    assert top["count"] == 2  # case-insensitive merge across r1 + r2


# --- Citation trend over time -----------------------------------------------------
async def test_citation_trend_computes_daily_shares(session, monkeypatch):
    await _seed_citation(session, response_id="r1", domain="humira.com", control=CONTROL_ABBVIE)
    await _seed_citation(session, response_id="r1", domain="stelara.com", control=CONTROL_COMPETITOR)
    await _seed_citation(session, response_id="r1", domain="example.org", control=CONTROL_UNKNOWN)

    result = await svc.citation_trend(session)
    assert result["periods"]
    p = result["periods"][0]
    assert p["total"] == 3
    assert p["abbvie_share_pct"] == pytest.approx(33.3, abs=0.2)
    assert p["competitor_share_pct"] == pytest.approx(33.3, abs=0.2)
    assert p["unknown_share_pct"] == pytest.approx(33.3, abs=0.2)


# --- Persisted recommendation triage workflow (BR-010) ----------------------------
async def test_recommendation_review_workflow(session, monkeypatch):
    _patch(monkeypatch)
    await _seed_gap(session, response_id="r1")
    await svc.generate(session)
    rec = (await svc.list_recommendations(session))["items"][0]

    assert (await svc.list_reviews(session))["count"] == 0  # nothing triaged yet

    saved = await svc.set_review(
        session, rec_id=rec["rec_id"], status="ACTIONED", owner="Jane",
        note="Briefed MLR", updated_by="jane@abbvie.com",
    )
    assert saved["status"] == "ACTIONED" and saved["owner"] == "Jane"

    listed = await svc.list_reviews(session, batch_id=rec["batch_id"])
    assert listed["count"] == 1 and listed["items"][0]["status"] == "ACTIONED"

    again = await svc.set_review(session, rec_id=rec["rec_id"], status="DISMISSED")
    assert again["status"] == "DISMISSED"
    assert (await svc.list_reviews(session))["count"] == 1  # upsert, not insert
