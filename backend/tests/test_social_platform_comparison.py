"""Per-platform "AbbVie vs each competitor brand" social-listening feature.

Covers the three moving parts added for the Social Listening lead section:
  - brand OWNERSHIP resolution from brands.yaml ``company`` (taxonomy.is_abbvie_brand),
    including the two wrinkles that make focus-vs-competitor the wrong signal:
      * Orilissa/Oriahnn are listed as competitors but are AbbVie-owned, and
      * Obesity has no AbbVie focus brand at all;
  - the deterministic per-channel aggregation in services.social_service.insights(); and
  - the best-effort AI gist generator in social.narrative.generate_platform_summaries().

All LLM boundaries are monkeypatched; the DB is in-memory SQLite.
"""
import json
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import taxonomy
from app.models import social_brief, social_comment, social_post  # noqa: F401 — register tables
from app.models.database import Base
from app.models.social_brief import SocialBrief
from app.models.social_post import SocialPost
from app.services import social_service as svc
from app.social import narrative


@pytest.fixture
async def session():
    engine_ = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine_.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine_, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine_.dispose()


def _post(channel: str, brand: str | None, sentiment: float | None, label: str | None,
          *, ta: str = "Rheumatology") -> SocialPost:
    return SocialPost(
        channel=channel,
        text=f"post about {brand or 'the condition'}",
        dedupe_hash=uuid.uuid4().hex,
        brand_focus=brand,
        therapeutic_area=ta,
        sentiment=sentiment,
        sentiment_label=label,
    )


# --- Ownership: company-based, not focus-vs-competitor -----------------------------------

def test_is_abbvie_brand_keys_off_company_not_focus_list():
    # AbbVie focus brands.
    assert taxonomy.is_abbvie_brand("Skyrizi") is True
    assert taxonomy.is_abbvie_brand("Humira") is True
    # Listed under `competitors` in Women's Health but company: AbbVie -> still AbbVie.
    assert taxonomy.is_abbvie_brand("Orilissa") is True
    assert taxonomy.is_abbvie_brand("Oriahnn") is True
    # Generic alias resolves too, and matching is case-insensitive.
    assert taxonomy.is_abbvie_brand("upadacitinib") is True
    assert taxonomy.is_abbvie_brand("skyrizi") is True
    # Real competitors owned by other companies.
    assert taxonomy.is_abbvie_brand("Cosentyx") is False
    assert taxonomy.is_abbvie_brand("Stelara") is False
    # Obesity focus brands are NOT AbbVie (no marketed AbbVie asset in the category).
    assert taxonomy.is_abbvie_brand("Wegovy") is False
    assert taxonomy.is_abbvie_brand("Zepbound") is False
    # Unknown / empty is safe.
    assert taxonomy.is_abbvie_brand(None) is False
    assert taxonomy.is_abbvie_brand("Nonexistent") is False


def test_company_for_maps_brands_to_owner():
    assert taxonomy.company_for("Cosentyx") == "Novartis"
    assert taxonomy.company_for("Enbrel") == "Amgen"
    assert taxonomy.company_for("Rinvoq") == "AbbVie"
    assert taxonomy.company_for("Wegovy") == "Novo Nordisk"
    assert taxonomy.company_for(None) is None


# --- Deterministic aggregation -----------------------------------------------------------

async def test_platform_comparison_splits_abbvie_vs_each_competitor(session):
    session.add_all([
        # reddit: 2 AbbVie (Rinvoq, Humira), 2 Cosentyx, 1 Enbrel, 1 unattributed
        _post("reddit", "Rinvoq", 0.5, "positive"),
        _post("reddit", "Humira", -0.4, "negative"),
        _post("reddit", "Cosentyx", 0.3, "positive"),
        _post("reddit", "Cosentyx", 0.0, "neutral"),
        _post("reddit", "Enbrel", -0.2, "negative"),
        _post("reddit", None, 0.0, "neutral"),
        # x: 1 AbbVie (Rinvoq), 1 Cosentyx
        _post("x", "Rinvoq", 0.4, "positive"),
        _post("x", "Cosentyx", -0.3, "negative"),
    ])
    await session.commit()

    ins = await svc.insights(session, therapeutic_area="Rheumatology")
    pc = ins["platform_comparison"]
    assert pc["abbvie_present"] is True

    # Channels ordered by captured volume (reddit=6 leads x=2).
    assert [c["channel"] for c in pc["channels"]] == ["reddit", "x"]
    reddit = pc["channels"][0]
    assert reddit["total_posts"] == 6
    assert reddit["attributed_posts"] == 5
    assert reddit["unattributed_posts"] == 1

    # AbbVie is one aggregated bucket with its brands named; share is over attributed posts.
    assert reddit["abbvie"]["posts"] == 2
    assert set(reddit["abbvie"]["brands"]) == {"Rinvoq", "Humira"}
    assert reddit["abbvie"]["post_share"] == pytest.approx(0.4)

    # Each competitor brand is listed individually, ranked by volume, with its owner.
    comp = reddit["competitors"]
    assert [r["brand"] for r in comp] == ["Cosentyx", "Enbrel"]
    assert comp[0]["posts"] == 2 and comp[0]["company"] == "Novartis"
    assert comp[0]["post_share"] == pytest.approx(0.4)
    assert comp[1]["brand"] == "Enbrel" and comp[1]["company"] == "Amgen"

    x = pc["channels"][1]
    assert x["abbvie"]["posts"] == 1 and x["competitors"][0]["brand"] == "Cosentyx"


async def test_platform_comparison_reports_no_abbvie_asset_for_obesity(session):
    session.add_all([
        _post("reddit", "Wegovy", 0.6, "positive", ta="Obesity"),
        _post("reddit", "Zepbound", -0.2, "negative", ta="Obesity"),
        _post("reddit", "Ozempic", 0.1, "neutral", ta="Obesity"),
    ])
    await session.commit()

    ins = await svc.insights(session, therapeutic_area="Obesity")
    pc = ins["platform_comparison"]
    assert pc["abbvie_present"] is False

    reddit = pc["channels"][0]
    assert reddit["abbvie"]["posts"] == 0
    assert reddit["abbvie"]["brands"] == []
    brands = {r["brand"] for r in reddit["competitors"]}
    assert brands == {"Wegovy", "Zepbound", "Ozempic"}
    companies = {r["brand"]: r["company"] for r in reddit["competitors"]}
    assert companies["Wegovy"] == "Novo Nordisk"
    assert companies["Zepbound"] == "Eli Lilly"


# --- AI gist generation (best-effort) ----------------------------------------------------

async def test_generate_platform_summaries_persists_and_insights_surfaces_gist(session, monkeypatch):
    session.add_all([
        _post("reddit", "Rinvoq", 0.5, "positive"),
        _post("reddit", "Cosentyx", -0.3, "negative"),
        _post("x", "Humira", 0.2, "positive"),
    ])
    await session.commit()

    # Model returns a gist per platform, plus a channel we never sent (must be dropped).
    monkeypatch.setattr(narrative, "chat_json", AsyncMock(return_value={
        "platforms": {
            "reddit": "On Reddit, AbbVie's Rinvoq is praised while Cosentyx draws cost complaints.",
            "x": "On X, Humira sentiment is mildly positive.",
            "tiktok": "Hallucinated platform that was never in the prompt.",
        }
    }))

    out = await narrative.generate_platform_summaries(session, therapeutic_area="Rheumatology")
    assert out["status"] == "ok"

    brief = await session.get(SocialBrief, "Rheumatology")
    stored = json.loads(brief.platform_summaries)
    assert set(stored.keys()) == {"reddit", "x"}          # tiktok dropped (anti-hallucination)
    assert stored["reddit"].startswith("On Reddit")

    # insights() merges the persisted gist onto the matching channel.
    ins = await svc.insights(session, therapeutic_area="Rheumatology")
    gists = {c["channel"]: c["gist"] for c in ins["platform_comparison"]["channels"]}
    assert gists["reddit"].startswith("On Reddit")
    assert gists["x"].startswith("On X")


async def test_generate_platform_summaries_is_best_effort_and_keeps_prior(session, monkeypatch):
    session.add(SocialBrief(therapeutic_area="Rheumatology",
                            platform_summaries=json.dumps({"reddit": "old gist"})))
    session.add(_post("reddit", "Rinvoq", 0.5, "positive"))
    await session.commit()

    # LLM failure must not raise and must leave the prior summaries untouched.
    monkeypatch.setattr(narrative, "chat_json", AsyncMock(side_effect=RuntimeError("boom")))
    out = await narrative.generate_platform_summaries(session, therapeutic_area="Rheumatology")
    assert out["status"] == "error"

    brief = await session.get(SocialBrief, "Rheumatology")
    await session.refresh(brief)
    assert json.loads(brief.platform_summaries) == {"reddit": "old gist"}


async def test_generate_platform_summaries_general_gist_for_unattributed_channel(session, monkeypatch):
    # A crawl-style channel with posts but NO brand attribution (e.g. myRAteam / Bezzy RA)
    # now gets a general "what this community is discussing" gist instead of being skipped.
    session.add_all([
        _post("myrateam", None, 0.1, "neutral"),
        _post("myrateam", None, -0.2, "negative"),
    ])
    await session.commit()

    monkeypatch.setattr(narrative, "chat_json", AsyncMock(return_value={
        "platforms": {"myrateam": "The myRAteam community discusses daily life with RA, flares, and peer support."}
    }))

    out = await narrative.generate_platform_summaries(session, therapeutic_area="Rheumatology")
    assert out["status"] == "ok"
    assert out["attributed"] == 0 and out["general"] == 1

    brief = await session.get(SocialBrief, "Rheumatology")
    stored = json.loads(brief.platform_summaries)
    assert set(stored.keys()) == {"myrateam"}
    assert stored["myrateam"].startswith("The myRAteam community")

    # Surfaced on the channel card even though it has no brand-attributed posts.
    ins = await svc.insights(session, therapeutic_area="Rheumatology")
    gists = {c["channel"]: c["gist"] for c in ins["platform_comparison"]["channels"]}
    assert gists["myrateam"].startswith("The myRAteam community")


async def test_generate_platform_summaries_mixes_brand_and_general(session, monkeypatch):
    # Brand-attributed (reddit) and general-community (myrateam, bezzy) channels in one pass.
    session.add_all([
        _post("reddit", "Rinvoq", 0.5, "positive"),
        _post("reddit", "Cosentyx", -0.3, "negative"),
        _post("myrateam", None, 0.0, "neutral"),
        _post("bezzy", None, -0.1, "negative"),
    ])
    await session.commit()

    monkeypatch.setattr(narrative, "chat_json", AsyncMock(return_value={
        "platforms": {
            "reddit": "On Reddit, Rinvoq is praised over Cosentyx.",
            "myrateam": "The myRAteam community focuses on flare management and emotional support.",
            "bezzy": "Bezzy RA members share living-well tips and lifestyle stories.",
            "ghost": "Never sent to the model.",
        }
    }))

    out = await narrative.generate_platform_summaries(session, therapeutic_area="Rheumatology")
    assert out["status"] == "ok"
    assert out["attributed"] == 1 and out["general"] == 2

    brief = await session.get(SocialBrief, "Rheumatology")
    stored = json.loads(brief.platform_summaries)
    assert set(stored.keys()) == {"reddit", "myrateam", "bezzy"}   # ghost dropped (anti-hallucination)
    assert stored["reddit"].startswith("On Reddit")
    assert stored["myrateam"].startswith("The myRAteam")
    assert stored["bezzy"].startswith("Bezzy RA")


async def test_generate_platform_summaries_empty_when_no_posts(session, monkeypatch):
    chat = AsyncMock()
    monkeypatch.setattr(narrative, "chat_json", chat)
    out = await narrative.generate_platform_summaries(session, therapeutic_area="Rheumatology")
    assert out["status"] == "empty"
    chat.assert_not_awaited()  # no LLM spend when there are no posts to summarize
