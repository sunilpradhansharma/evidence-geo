"""Data-maintenance / backfill action tools.

One-time (idempotent) sweeps that fold existing history into a newly enabled
capability. Both are mutating, so the executor intercepts them for a Confirm
card. Re-redaction is regex-based over already-stored text (no LLM). The Source
Authority backfill classifies from the curated taxonomy first; only UNCURATED
domains fall through to the evidence-based LLM classifier (when enabled), so a
backfill that encounters new domains may make some billed LLM calls.
"""
from __future__ import annotations

from app.copilot.tools.schemas import ToolInput, ToolResultData, ToolSpec
from app.models.database import AsyncSessionLocal


class SourceAuthorityBackfillInput(ToolInput):
    pass


async def source_authority_backfill(payload: SourceAuthorityBackfillInput) -> ToolResultData:
    from app.source_authority import service as sa

    processed = failed = 0
    remaining = 0
    # Each pass drains up to 200 unclassified responses; classified rows drop out
    # of the next pass, so we loop until nothing new is processed (bounded for safety).
    async with AsyncSessionLocal() as db:
        for _ in range(50):
            res = await sa.classify_unclassified_sweep(db, limit=200, offset=0)
            processed += int(res.get("processed", 0) or 0)
            failed += int(res.get("failed", 0) or 0)
            remaining = int(res.get("remaining", 0) or 0)
            if not res.get("processed"):
                break

    summary = f"Classified sources for {processed} historical response(s)."
    if remaining:
        summary += f" {remaining} still pending — run me again to continue."
    else:
        summary += " Source Authority history is fully backfilled."
    return ToolResultData(
        tool_name="source_authority_backfill",
        ok=True,
        summary=summary,
        data={"processed": processed, "failed": failed, "remaining": remaining},
        nav_target="/dashboard/source-authority",
    )


class RedactBackfillInput(ToolInput):
    therapeutic_area: str | None = None


async def redact_backfill(payload: RedactBackfillInput) -> ToolResultData:
    from app.compliance import backfill as compliance_backfill

    ta = (payload.therapeutic_area or "").strip() or None
    async with AsyncSessionLocal() as db:
        s = await compliance_backfill.redact_backfill(db, therapeutic_area=ta)

    scope = f" ({ta})" if ta else ""
    summary = (
        f"Re-redacted stored text{scope}: updated "
        f"{int(s.get('harvested_updated', 0) or 0)} question(s), "
        f"{int(s.get('posts_updated', 0) or 0)} post(s), and "
        f"{int(s.get('comments_updated', 0) or 0)} comment(s)."
    )
    return ToolResultData(
        tool_name="redact_backfill",
        ok=True,
        summary=summary,
        data=s,
    )


SPECS: list[ToolSpec] = [
    ToolSpec(
        "source_authority_backfill",
        "Backfill Source Authority: classify the web-domain citations of historical responses that were "
        "captured before Source Authority existed, so the Source Authority dashboard includes them. "
        "Idempotent and safe to re-run (drains all pending). Takes no arguments.",
        SourceAuthorityBackfillInput,
        source_authority_backfill,
        mutating=True,
        nav_target="/dashboard/source-authority",
    ),
    ToolSpec(
        "redact_backfill",
        "Re-run the PHI/PII redactor over already-stored harvested questions and social posts/comments "
        "(defense in depth after a detector upgrade). Idempotent. Optionally scope the social sweep to one "
        "therapeutic_area; unset = all.",
        RedactBackfillInput,
        redact_backfill,
        mutating=True,
    ),
]
