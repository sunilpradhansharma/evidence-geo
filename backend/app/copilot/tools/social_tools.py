"""Social Listening tools (confirmed write — background ingest).

Mirrors the Discovery ``run_harvest`` tool: the copilot proposes the ingest, the
executor intercepts it for confirmation, and on confirm we mark the in-memory
state running synchronously (so completion polling can't read a previous run's
stale result) and schedule the (slow, network-bound) Apify fetch + classify
pipeline as a background task. The UI polls /copilot/job?kind=social.
"""
from __future__ import annotations

import asyncio

from app.copilot.tools.schemas import ToolInput, ToolResultData, ToolSpec

# Channels supported by social_sources.yaml (Apify actors). ``myrateam``/``bezzy`` are
# Rheumatology-only, opt-in web-crawl channels (public RA community content).
_VALID_CHANNELS = {"reddit", "tiktok", "instagram", "facebook", "x", "myrateam", "bezzy"}


class RunSocialIngestInput(ToolInput):
    channels: str | None = None  # comma-separated (reddit,tiktok,...); empty = all enabled
    therapeutic_area: str | None = None  # required — Social Listening is per-area (no default)
    terms: str | None = None  # comma-separated custom seed terms for an ad-hoc free-text search


async def run_social_ingest(payload: RunSocialIngestInput) -> ToolResultData:
    from app.api import social as social_api
    from app.social import pipeline

    if not pipeline.is_configured():
        return ToolResultData(
            tool_name="run_social_ingest",
            ok=False,
            summary="Social listening is not configured.",
            error="APIFY_API_TOKEN is not set (or APIFY_ENABLED is false). Add it to .env and restart the backend.",
        )
    if social_api._SOCIAL.get("running"):
        return ToolResultData(
            tool_name="run_social_ingest",
            ok=False,
            summary="A social-listening ingest is already in progress.",
            error="already_running",
        )

    ta = (payload.therapeutic_area or "").strip()
    if not ta:
        return ToolResultData(
            tool_name="run_social_ingest",
            ok=False,
            summary="Which therapeutic area should I ingest? Social Listening runs are per area.",
            error="therapeutic_area_required",
        )

    channels = [c.strip().lower() for c in (payload.channels.split(",") if payload.channels else []) if c.strip()]
    bad = [c for c in channels if c not in _VALID_CHANNELS]
    if bad:
        return ToolResultData(
            tool_name="run_social_ingest",
            ok=False,
            summary=f"Unknown channel(s): {', '.join(bad)}.",
            error=f"channels must be from: {sorted(_VALID_CHANNELS)}",
        )
    tl = [t.strip() for t in (payload.terms.split(",") if payload.terms else []) if t.strip()]

    # Ad-hoc free-text search: gate the query to pharma/medical topics before spending Apify
    # credits (mirrors the POST /social/ingest guard). Fails open on LLM error.
    if tl:
        from app.social.guard import is_pharma_relevant
        allowed, reason = await is_pharma_relevant(", ".join(tl))
        if not allowed:
            return ToolResultData(
                tool_name="run_social_ingest",
                ok=False,
                summary="That search isn't related to pharma or a therapeutic area, so no ingest was started.",
                error=reason,
            )

    # Mark running synchronously (before scheduling the task) so the first
    # completion poll can't read a previous ingest's stale finished_at/result.
    social_api._SOCIAL.update(running=True, finished_at=None, error=None, last_result=None)
    asyncio.create_task(social_api._ingest_task(channels or None, ta, tl or None))

    scope = ", ".join(channels) if channels else "all enabled channels"
    label = f"'{ta}' (ad-hoc terms: {', '.join(tl)})" if tl else ta
    return ToolResultData(
        tool_name="run_social_ingest",
        ok=True,
        summary=f"Started a social-listening ingest ({label}) across {scope}.",
        data={"channels": channels or None, "therapeutic_area": ta, "terms": tl or None},
        nav_target="/social-listening",
        job={"kind": "social"},
    )


SPECS: list[ToolSpec] = [
    ToolSpec(
        "run_social_ingest",
        "Start a Social Listening ingest (background job): scrape public social posts AND their "
        "comments/replies via Apify, scrub PII, screen for adverse events, auto-translate non-English "
        "text to English, and classify them (post and comment sentiment are tracked separately). "
        "therapeutic_area is REQUIRED (ingests are per-area, no default) — if the user has not named "
        "one, ASK which area to ingest before calling. Optionally scope by channels (comma-separated: "
        "reddit,tiktok,instagram,facebook,x, plus the Rheumatology-only community sites myrateam,bezzy). "
        "For an ad-hoc free-text search, pass comma-separated `terms` (any indication/topic/brand) and "
        "set therapeutic_area to a label for the captured posts. Unset channels = all enabled channels "
        "(myrateam/bezzy are opt-in: they run only when named explicitly AND the area is Rheumatology).",
        RunSocialIngestInput,
        run_social_ingest,
        mutating=True,
        nav_target="/social-listening",
    ),
]
