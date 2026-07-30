"""Tests for the Influence-Graph "build content actions for not-mentioned answers" flow.

The one-click action now ONLY generates GEO Intervention recommendations (content actions)
scoped to a cohort of responses — it does NOT create interventions. Coverage:
  1. NOT_MENTIONED answers are treated as GEO gaps, so they generate a recommendation.
  2. Generation is scoped to exactly the requested response_ids, and (unlike the old
     intervention loop) Provider-persona answers still get a content action.
  3. A response with no scored gap yields nothing.

External calls (LLM + SEMrush) and the best-effort placement lookup are monkeypatched so the
tests are hermetic.
"""
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.database import Base

# Register every model touched by generation before create_all.
from app.models import (  # noqa: F401
    audit_log,
    recommendation as _rec_mod,
    recommendation_review as _review_mod,
    response as _response_mod,
    scoring as _scoring_mod,
)
from app.models.recommendation import Recommendation
from app.models.response import Response
from app.models.scoring import ScoringRecord
from app.services import recommendation_service as rsvc


@pytest.fixture
async def session():
    engine_ = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine_.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine_, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine_.dispose()


async def _ok_chat_json(system, user, **kwargs):
    return {
        "content_type": "FAQ",
        "recommended_action": "Publish an FAQ establishing the brand's place in therapy.",
        "rationale": "Creates a citable, brand-owned presence to earn a mention.",
    }


def _patch(monkeypatch):
    """Stub the network + best-effort lookups so generation is hermetic and fast."""
    monkeypatch.setattr("app.remediation.engine.chat_json", _ok_chat_json)

    async def _fake_enrich(domain, *, keyword):
        return {"search_volume": 1000, "domain_authority": 50, "source": "stub"}

    monkeypatch.setattr("app.remediation.semrush.enrich", _fake_enrich)

    async def _no_placement(db, **kwargs):
        return {}

    monkeypatch.setattr("app.remediation.citations.placement_guidance", _no_placement)


async def _seed_not_mentioned(session, *, response_id, persona, ta="Immunology", brand="Humira", llm="gpt-4o"):
    """A grounded answer where the focus brand is entirely absent (NOT_MENTIONED)."""
    session.add(Response(
        response_id=response_id, run_id="run1", llm_name=llm, persona=persona,
        question_id=f"q-{response_id}", question_text="Best biologic for RA?",
        therapeutic_area=ta, brand_focus=brand, domain="Comparative",
        response_text="Competitors are discussed; the brand is absent.", status="SUCCESS",
        sources=None,
    ))
    session.add(ScoringRecord(
        score_id=f"s-{response_id}", response_id=response_id, score_version=1,
        competitive_position="NOT_MENTIONED", sentiment_score=0.0,
        brand_mentions=json.dumps([{"brand": "Stelara", "is_competitor": True, "sentiment": 0.5}]),
        key_claims=json.dumps([]), scoring_rationale="x", scored_by="AI",
    ))
    await session.commit()


# --- 1. NOT_MENTIONED is a GEO gap -> a content action -------------------------------
async def test_not_mentioned_generates_content_action(session, monkeypatch):
    _patch(monkeypatch)
    await _seed_not_mentioned(session, response_id="r1", persona="Patient")

    summary = await rsvc.generate(session, response_ids=["r1"])
    assert summary["gaps_found"] == 1
    assert summary["generated"] == 1
    assert summary["rec_ids"]

    item = (await rsvc.list_recommendations(session))["items"][0]
    assert item["competitive_position"] == "NOT_MENTIONED"
    assert item["gap_severity"] == 1.5  # ranked between SECOND_LINE (1.0) and NOT_RECOMMENDED (2.0)


# --- 2. Scoped to the requested cohort; Provider answers still get a content action ---
async def test_generation_is_scoped_to_requested_responses(session, monkeypatch):
    _patch(monkeypatch)
    await _seed_not_mentioned(session, response_id="r1", persona="Patient")
    await _seed_not_mentioned(session, response_id="r2", persona="Provider")

    # Only the requested response becomes a content action — and Provider is NOT skipped
    # (there's no intervention/measurement step here, so every persona is fair game).
    gen = await rsvc.generate(session, response_ids=["r2"])
    assert gen["gaps_found"] == 1
    assert gen["generated"] == 1
    rows = (
        await session.execute(select(Recommendation).where(Recommendation.batch_id == gen["batch_id"]))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].source_response_id == "r2"
    assert rows[0].persona == "Provider"

    # Passing the whole cohort covers every answer in it.
    gen_all = await rsvc.generate(session, response_ids=["r1", "r2"])
    assert gen_all["generated"] == 2


# --- 3. No scored gap -> nothing generated -------------------------------------------
async def test_unscored_response_yields_no_content_action(session, monkeypatch):
    _patch(monkeypatch)
    session.add(Response(
        response_id="rx", run_id="run1", llm_name="gpt-4o", persona="Patient",
        question_id="q-rx", question_text="?", therapeutic_area="Immunology",
        brand_focus="Humira", domain="Comparative", response_text="...", status="SUCCESS",
    ))
    await session.commit()

    gen = await rsvc.generate(session, response_ids=["rx"])
    assert gen["gaps_found"] == 0
    assert gen["generated"] == 0
