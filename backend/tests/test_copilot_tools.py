"""Copilot (Ema) tool backfill tests.

Covers the new read-tool dispatchers wired into ``read_tools.py`` (Source Authority, GEO
Interventions, Prompt Volume, Workshop Insights, Stakeholder Digests, Model Releases /
AI Update Impact, Question Variations), the ``generate_recommendations`` mutating tool in
``insight_tools.py``, and the Social Listening fixes (no Obesity default + ask-which-area).

The read tools open their OWN ``AsyncSessionLocal`` (so behaviour matches the REST API), so
the fixture monkeypatches that factory to a shared in-memory SQLite DB (StaticPool keeps the
seeding session and the tool's own session on the same connection).
"""
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.database import Base

# Import every model so the full schema registers on Base.metadata before create_all
# (mirrors app.models.database.init_db).
from app.models import (  # noqa: F401
    alert,
    audit_log,
    consensus,
    digest,
    harvested_question,
    model_release,
    preferred_source,
    preferred_source_observation,
    prompt_volume,
    prompt_volume_alert,
    question,
    question_variation,
    recommendation,
    recommendation_review,
    response,
    response_citation,
    response_diff,
    run,
    schedule,
    scoring,
    social_brief,
    social_comment,
    social_post,
    source_domain,
    theme,
    workshop_summary,
)
from app.copilot.tools import insight_tools, read_tools, social_tools
from app.copilot.tools.registry import TOOLS, anthropic_tool_schemas
from app.models.digest import DigestProfile
from app.models.model_release import ModelReleaseLog
from app.models.social_comment import SocialComment
from app.models.social_post import SocialPost

# Every tool this backfill added (read + one mutating). Kept explicit so the registry test
# fails loudly if a spec is dropped.
NEW_READ_TOOLS = [
    "get_source_authority",
    "get_recommendations",
    "get_prompt_volume",
    "get_workshop_insights",
    "get_digests",
    "get_model_releases",
    "get_variations",
]
NEW_TOOLS = [*NEW_READ_TOOLS, "generate_recommendations"]


@pytest.fixture
async def maker(monkeypatch):
    """In-memory DB shared across sessions + the tools' own AsyncSessionLocal."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # The tools call AsyncSessionLocal() themselves — point it at the test DB.
    monkeypatch.setattr(read_tools, "AsyncSessionLocal", factory)
    monkeypatch.setattr(insight_tools, "AsyncSessionLocal", factory)
    yield factory
    await engine.dispose()


# --- seed helpers -----------------------------------------------------------------
async def _add_post(factory, *, channel="reddit", ta="Obesity", brand="Wegovy",
                    sentiment=0.3, label="positive", ae=False, dedupe="h"):
    async with factory() as db:
        p = SocialPost(
            channel=channel, text=f"a post about {brand}", dedupe_hash=dedupe,
            therapeutic_area=ta, brand_focus=brand, topic="efficacy",
            sentiment=sentiment, sentiment_label=label, engagement_score=10, ae_flag=ae,
        )
        db.add(p)
        await db.commit()
        return p.id


async def _add_comment(factory, *, post_id, channel="reddit", dedupe="c", ae=False):
    async with factory() as db:
        db.add(SocialComment(
            post_id=post_id, channel=channel, text="a reply", dedupe_hash=dedupe,
            sentiment=0.1, sentiment_label="neutral", ae_flag=ae,
        ))
        await db.commit()


# =================================================================================
# Registry / schema
# =================================================================================
def test_registry_exposes_new_tools():
    for name in NEW_TOOLS:
        assert name in TOOLS, f"{name} not registered"


def test_anthropic_tool_schemas_build():
    schemas = anthropic_tool_schemas()
    names = {s["name"] for s in schemas}
    for name in NEW_TOOLS:
        assert name in names
    # Every schema is a well-formed {name, description, input_schema} triple.
    for s in schemas:
        assert s["name"] and s["description"]
        assert isinstance(s["input_schema"], dict)


# =================================================================================
# Social Listening: no Obesity default + ask-which-area
# =================================================================================
def test_social_inputs_have_no_obesity_default():
    assert read_tools.SocialInsightsInput().therapeutic_area is None
    assert read_tools.ListSocialPostsInput().therapeutic_area is None
    assert read_tools.ListSocialCommentsInput().therapeutic_area is None
    assert social_tools.RunSocialIngestInput().therapeutic_area is None


async def test_social_insights_asks_which_area_when_multiple(maker):
    await _add_post(maker, ta="Obesity", dedupe="o1")
    await _add_post(maker, ta="Immunology", brand="Rinvoq", dedupe="i1")

    res = await read_tools.social_insights(read_tools.SocialInsightsInput())
    assert res.ok is True
    assert res.data["needs_area"] is True
    areas = {a["therapeutic_area"] for a in res.data["available_areas"]}
    assert areas == {"Obesity", "Immunology"}
    # The user-facing chip summary must be a neutral status — the internal
    # "ask the user…" steer belongs in LLM-only data, not the summary.
    assert "ask the user" not in res.summary.lower()
    assert "which area" in res.data["assistant_guidance"].lower()
    # A structured dropdown of areas is surfaced so the user can click one.
    assert res.prompt_options is not None
    assert res.prompt_options["param"] == "therapeutic_area"
    assert {o["value"] for o in res.prompt_options["options"]} == {"Obesity", "Immunology"}
    assert "{value}" in res.prompt_options["send_template"]


async def test_social_insights_uses_single_area(maker):
    await _add_post(maker, ta="Obesity", dedupe="o1")
    await _add_post(maker, ta="Obesity", brand="Zepbound", dedupe="o2")

    res = await read_tools.social_insights(read_tools.SocialInsightsInput())
    assert res.ok is True
    assert res.data.get("needs_area") is None  # went straight to insights
    assert res.data["therapeutic_area"] == "Obesity"
    assert res.data["total_posts"] == 2


async def test_social_insights_no_data_is_graceful(maker):
    res = await read_tools.social_insights(read_tools.SocialInsightsInput())
    assert res.ok is True
    assert res.data["needs_area"] is False
    assert res.data["available_areas"] == []


async def test_social_insights_explicit_area(maker):
    await _add_post(maker, ta="Obesity", dedupe="o1")
    await _add_post(maker, ta="Immunology", brand="Rinvoq", dedupe="i1")

    res = await read_tools.social_insights(read_tools.SocialInsightsInput(therapeutic_area="Immunology"))
    assert res.ok is True
    assert res.data["therapeutic_area"] == "Immunology"
    assert res.data["total_posts"] == 1
    # platform_comparison is surfaced for Ema.
    assert "platform_comparison" in res.data


async def test_list_social_posts_defaults_to_all_areas(maker):
    await _add_post(maker, ta="Obesity", dedupe="o1")
    await _add_post(maker, ta="Immunology", brand="Rinvoq", dedupe="i1")

    res = await read_tools.list_social_posts(read_tools.ListSocialPostsInput())
    assert res.ok is True
    tas = {p["therapeutic_area"] for p in res.data["posts"]}
    assert tas == {"Obesity", "Immunology"}  # both areas, not just Obesity


async def test_list_social_comments_defaults_to_all_areas(maker):
    o = await _add_post(maker, ta="Obesity", dedupe="o1")
    i = await _add_post(maker, ta="Immunology", brand="Rinvoq", dedupe="i1")
    await _add_comment(maker, post_id=o, dedupe="co")
    await _add_comment(maker, post_id=i, dedupe="ci")

    res = await read_tools.list_social_comments(read_tools.ListSocialCommentsInput())
    assert res.ok is True
    assert len(res.data["comments"]) == 2  # spans both areas


# =================================================================================
# get_source_authority dispatcher
# =================================================================================
async def test_source_authority_known_view_ok(maker):
    res = await read_tools.get_source_authority(read_tools.GetSourceAuthorityInput(view="distribution"))
    assert res.ok is True
    assert res.data["view"] == "distribution"
    assert isinstance(res.data["result"], dict)


async def test_source_authority_unknown_view_errors(maker):
    res = await read_tools.get_source_authority(read_tools.GetSourceAuthorityInput(view="nope"))
    assert res.ok is False
    assert "view must be one of" in (res.error or "")


async def test_source_authority_domain_requires_authority_domain(maker):
    res = await read_tools.get_source_authority(read_tools.GetSourceAuthorityInput(view="domain"))
    assert res.ok is False
    assert "authority_domain" in (res.error or "")


async def test_source_authority_provenance_requires_response_id(maker):
    res = await read_tools.get_source_authority(read_tools.GetSourceAuthorityInput(view="provenance"))
    assert res.ok is False
    assert "response_id" in (res.error or "")


# =================================================================================
# get_recommendations dispatcher
# =================================================================================
async def test_recommendations_list_ok(maker):
    res = await read_tools.get_recommendations(read_tools.GetRecommendationsInput(view="list"))
    assert res.ok is True
    assert res.data["view"] == "list"


async def test_recommendations_content_types_ok(maker):
    res = await read_tools.get_recommendations(read_tools.GetRecommendationsInput(view="content_types"))
    assert res.ok is True
    assert res.data["result"]["content_types"]  # non-empty enum
    assert "semrush_configured" in res.data["result"]


async def test_recommendations_unknown_view_errors(maker):
    res = await read_tools.get_recommendations(read_tools.GetRecommendationsInput(view="bogus"))
    assert res.ok is False
    assert "view must be one of" in (res.error or "")


# =================================================================================
# get_prompt_volume dispatcher
# =================================================================================
async def test_prompt_volume_intelligence_ok(maker):
    res = await read_tools.get_prompt_volume(read_tools.GetPromptVolumeInput(view="intelligence"))
    assert res.ok is True
    assert res.data["view"] == "intelligence"


async def test_prompt_volume_gap_alerts_summary_ok(maker):
    res = await read_tools.get_prompt_volume(read_tools.GetPromptVolumeInput(view="gap_alerts_summary"))
    assert res.ok is True


async def test_prompt_volume_unknown_view_errors(maker):
    res = await read_tools.get_prompt_volume(read_tools.GetPromptVolumeInput(view="???"))
    assert res.ok is False
    assert "view must be one of" in (res.error or "")


# =================================================================================
# get_workshop_insights
# =================================================================================
async def test_workshop_insights_empty_available_false(maker):
    res = await read_tools.get_workshop_insights(read_tools.GetWorkshopInsightsInput(scope="workshop"))
    assert res.ok is True
    assert res.data["scope"] == "workshop"
    assert res.data["available"] is False


async def test_workshop_insights_bad_scope_errors(maker):
    res = await read_tools.get_workshop_insights(read_tools.GetWorkshopInsightsInput(scope="nope"))
    assert res.ok is False
    assert "workshop|all" in (res.error or "")


# =================================================================================
# get_digests
# =================================================================================
async def test_digests_profiles_ok(maker):
    async with maker() as db:
        db.add(DigestProfile(role="PV", description="safety"))
        await db.commit()

    res = await read_tools.get_digests(read_tools.GetDigestsInput(view="profiles"))
    assert res.ok is True
    assert len(res.data["profiles"]) == 1
    assert res.data["profiles"][0]["role"] == "PV"


async def test_digests_runs_ok_empty(maker):
    res = await read_tools.get_digests(read_tools.GetDigestsInput(view="runs"))
    assert res.ok is True
    assert res.data["runs"] == []


async def test_digests_run_requires_run_id(maker):
    res = await read_tools.get_digests(read_tools.GetDigestsInput(view="run"))
    assert res.ok is False
    assert "run_id" in (res.error or "")


async def test_digests_run_not_found(maker):
    res = await read_tools.get_digests(read_tools.GetDigestsInput(view="run", run_id=999))
    assert res.ok is False
    assert "999" in (res.error or "")


async def test_digests_unknown_view_errors(maker):
    res = await read_tools.get_digests(read_tools.GetDigestsInput(view="zzz"))
    assert res.ok is False
    assert "profiles|runs|run" in (res.error or "")


# =================================================================================
# get_model_releases
# =================================================================================
async def test_model_releases_list_ok(maker):
    async with maker() as db:
        db.add(ModelReleaseLog(target_platform="Gemini", release_date=date(2026, 1, 15), version="2.5"))
        await db.commit()

    res = await read_tools.get_model_releases(read_tools.GetModelReleasesInput(view="list"))
    assert res.ok is True
    releases = res.data["result"]["releases"]
    assert len(releases) == 1
    assert releases[0]["target_platform"] == "Gemini"


async def test_model_releases_correlation_ratio_ok(maker):
    res = await read_tools.get_model_releases(read_tools.GetModelReleasesInput(view="correlation_ratio"))
    assert res.ok is True


async def test_model_releases_drift_detail_requires_diff_id(maker):
    res = await read_tools.get_model_releases(read_tools.GetModelReleasesInput(view="drift_detail"))
    assert res.ok is False
    assert "diff_id" in (res.error or "")


async def test_model_releases_unknown_view_errors(maker):
    res = await read_tools.get_model_releases(read_tools.GetModelReleasesInput(view="foo"))
    assert res.ok is False
    assert "view must be one of" in (res.error or "")


# =================================================================================
# get_variations
# =================================================================================
async def test_variations_groups_ok_empty(maker):
    res = await read_tools.get_variations(read_tools.GetVariationsInput(view="groups"))
    assert res.ok is True
    assert res.data["view"] == "groups"


async def test_variations_group_requires_group_id(maker):
    res = await read_tools.get_variations(read_tools.GetVariationsInput(view="group"))
    assert res.ok is False
    assert "group_id" in (res.error or "")


async def test_variations_group_results_requires_group_id(maker):
    res = await read_tools.get_variations(read_tools.GetVariationsInput(view="group_results"))
    assert res.ok is False
    assert "group_id" in (res.error or "")


async def test_variations_unknown_view_errors(maker):
    res = await read_tools.get_variations(read_tools.GetVariationsInput(view="bad"))
    assert res.ok is False
    assert "groups|group|group_results" in (res.error or "")


# =================================================================================
# All no-id views execute cleanly on an empty DB (catches kwarg/signature drift)
# =================================================================================
@pytest.mark.parametrize("view", [
    "distribution", "top_domains", "coverage", "share_of_voice", "pages",
    "trends", "sentiment_correlation", "preferred", "preferred_observations",
])
async def test_source_authority_all_simple_views_ok(maker, view):
    res = await read_tools.get_source_authority(read_tools.GetSourceAuthorityInput(view=view))
    assert res.ok is True, f"{view}: {res.error}"


@pytest.mark.parametrize("view", [
    "list", "citation_opportunities", "share_of_citation", "preferred_source_gaps",
    "query_fanouts", "citation_trend", "reviews", "content_types",
])
async def test_recommendations_all_views_ok(maker, view):
    res = await read_tools.get_recommendations(read_tools.GetRecommendationsInput(view=view))
    assert res.ok is True, f"{view}: {res.error}"


@pytest.mark.parametrize("view", [
    "intelligence", "gaps", "gap_alerts", "gap_alerts_summary", "trend", "batches", "prioritized",
])
async def test_prompt_volume_all_views_ok(maker, view):
    res = await read_tools.get_prompt_volume(read_tools.GetPromptVolumeInput(view=view))
    assert res.ok is True, f"{view}: {res.error}"


@pytest.mark.parametrize("view", [
    "list", "drifts", "drift_timeline", "version_impact", "high_impact",
    "correlation_ratio", "versions", "sync_status",
])
async def test_model_releases_all_simple_views_ok(maker, view):
    res = await read_tools.get_model_releases(read_tools.GetModelReleasesInput(view=view))
    assert res.ok is True, f"{view}: {res.error}"


# =================================================================================
# generate_recommendations (mutating)
# =================================================================================
def test_generate_recommendations_spec_is_mutating():
    spec = TOOLS["generate_recommendations"]
    assert spec.mutating is True
    assert spec.nav_target == "/dashboard/recommendations"


async def test_generate_recommendations_runs(maker, monkeypatch):
    async def _fake_generate(db, *, persona=None, therapeutic_area=None,
                             indication=None, brand=None, llm_name=None, limit=25):
        return {"batch_id": "b1", "gaps_found": 3, "generated": 3,
                "semrush_source": "stub", "semrush_live": False}

    # Mirror test_recommendations.py: monkeypatch the billed generate path.
    monkeypatch.setattr("app.services.recommendation_service.generate", _fake_generate)

    res = await insight_tools.generate_recommendations(
        insight_tools.GenerateRecommendationsInput(persona="Provider", model="gpt-4o")
    )
    assert res.ok is True
    assert res.nav_target == "/dashboard/recommendations"
    assert res.data["generated"] == 3 and res.data["gaps_found"] == 3
    assert "MLR" in res.summary  # not-MLR-approved caveat surfaced


async def test_generate_recommendations_no_gaps_is_ok(maker, monkeypatch):
    async def _no_gaps(db, **kwargs):
        return {"batch_id": None, "gaps_found": 0, "generated": 0,
                "semrush_source": "stub", "semrush_live": False}

    monkeypatch.setattr("app.services.recommendation_service.generate", _no_gaps)
    res = await insight_tools.generate_recommendations(insight_tools.GenerateRecommendationsInput())
    assert res.ok is True
    assert "No competitive-position gaps" in res.summary
