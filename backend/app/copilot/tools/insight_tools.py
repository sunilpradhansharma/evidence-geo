"""Insight + warehouse action tools: rebuild taxonomy, Snowflake sync, GEO generate."""
from __future__ import annotations

import asyncio

from app.copilot.tools.schemas import ToolInput, ToolResultData, ToolSpec
from app.models.database import AsyncSessionLocal


class RebuildInsightsInput(ToolInput):
    target_themes: int = 12
    sample_cap: int = 300


async def rebuild_insights(payload: RebuildInsightsInput) -> ToolResultData:
    from app.api import insights as insights_api

    if insights_api._REBUILD.get("running"):
        return ToolResultData(tool_name="rebuild_insights", ok=False, summary="A rebuild is already running.", error="already_running")
    target = max(4, min(payload.target_themes, 30))
    sample = max(20, min(payload.sample_cap, 2000))
    # Mark running synchronously so completion polling can't read a stale result.
    insights_api._REBUILD.update(running=True, finished_at=None, error=None, last_result=None)
    asyncio.create_task(insights_api._rebuild_task(target, sample))
    return ToolResultData(
        tool_name="rebuild_insights",
        ok=True,
        summary=f"Started rebuilding the theme taxonomy (~{target} themes).",
        data={"target_themes": target, "sample_cap": sample},
        nav_target="/dashboard/insights",
        job={"kind": "insights"},
    )


class SnowflakeSyncInput(ToolInput):
    pass


async def snowflake_sync(payload: SnowflakeSyncInput) -> ToolResultData:
    from app.snowflake import client, mirror

    if not client.is_enabled():
        return ToolResultData(tool_name="snowflake_sync", ok=True, summary="Snowflake is disabled; nothing to sync.", data={"enabled": False})
    asyncio.create_task(mirror.run_mirror_safe())
    return ToolResultData(
        tool_name="snowflake_sync",
        ok=True,
        summary="Started a Snowflake mirror sync.",
        data={"enabled": True},
        nav_target="/dashboard/cortex",
    )


class GenerateRecommendationsInput(ToolInput):
    persona: str | None = None
    therapeutic_area: str | None = None
    model: str | None = None  # scope to one AI model's gaps (llm_name)


async def generate_recommendations(payload: GenerateRecommendationsInput) -> ToolResultData:
    """(Re)build the GEO Intervention recommendation batch from the latest scored gaps.

    Billed: enriches each gap with SEMrush SEO metrics + an LLM-authored content action, so
    this is a mutating tool the executor gates behind a Confirm card. Every recommendation is a
    STRATEGIC SUGGESTION ONLY (never MLR-approved content)."""
    from app.services import recommendation_service as svc

    async with AsyncSessionLocal() as db:
        try:
            result = await svc.generate(
                db,
                persona=payload.persona,
                therapeutic_area=payload.therapeutic_area,
                llm_name=payload.model,
            )
        except Exception as exc:  # noqa: BLE001 — never raise out of a tool
            return ToolResultData(
                tool_name="generate_recommendations", ok=False,
                summary="Could not generate GEO intervention recommendations.", error=str(exc),
            )
    n_gen = int(result.get("generated", 0) or 0)
    n_gap = int(result.get("gaps_found", 0) or 0)
    if n_gap == 0:
        summary = ("No competitive-position gaps (SECOND_LINE / NOT_RECOMMENDED) were found, so no "
                   "recommendations were generated. Run and score analyses first.")
    else:
        summary = (f"Generated {n_gen} GEO intervention recommendation(s) from {n_gap} "
                   f"competitive-position gap(s) (SEMrush: {result.get('semrush_source', 'stub')}). "
                   "These are strategic suggestions only — not MLR-approved content.")
    return ToolResultData(
        tool_name="generate_recommendations", ok=True, summary=summary, data=result,
        nav_target="/dashboard/recommendations",
    )


SPECS: list[ToolSpec] = [
    ToolSpec("rebuild_insights", "Discover a fresh theme taxonomy and tag all responses (runs in the background). Optional target_themes and sample_cap.", RebuildInsightsInput, rebuild_insights, mutating=True, nav_target="/dashboard/insights"),
    ToolSpec("snowflake_sync", "Trigger an incremental mirror of operational data into Snowflake (no-op when Snowflake is disabled).", SnowflakeSyncInput, snowflake_sync, mutating=True, nav_target="/dashboard/cortex"),
    ToolSpec("generate_recommendations", "Generate (or regenerate) the GEO Intervention recommendation batch: turn the latest weak competitive positions into ranked, plain-language content actions enriched with SEMrush SEO metrics. BILLED (SEMrush + LLM) and mutating, so the user confirms first. Optional filters: persona, therapeutic_area, model (llm_name). Output is a strategic suggestion list only — never MLR-approved content. Use get_recommendations to READ the existing list.", GenerateRecommendationsInput, generate_recommendations, mutating=True, nav_target="/dashboard/recommendations"),
]
