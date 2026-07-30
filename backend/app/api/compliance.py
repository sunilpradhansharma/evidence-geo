"""Compliance / PII maintenance API.

``POST /compliance/redact-sweep`` re-runs the central redactor over already-stored harvest
and social free text (defense in depth after a detector upgrade). It is idempotent and, on
success, refreshes the social narrative brief so verbatim quotes reflect the cleaned text.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance import backfill
from app.models.database import get_db
from app.utils.logging import get_logger

logger = get_logger("api.compliance")

router = APIRouter(prefix="/compliance", tags=["compliance"])


@router.post("/redact-sweep")
async def redact_sweep(
    therapeutic_area: str | None = Query(
        None, description="Optional: scope the social sweep to one therapeutic area."
    ),
    db: AsyncSession = Depends(get_db),
):
    result = await backfill.redact_backfill(db, therapeutic_area=therapeutic_area)
    # Verbatim quotes are pulled from the stored text, so regenerate the brief best-effort.
    try:
        from app.social import narrative

        await narrative.generate_social_brief(db, therapeutic_area=therapeutic_area or "Obesity")
        result["brief_regenerated"] = True
    except Exception as e:  # noqa: BLE001 — maintenance must not fail on the brief
        logger.warning("brief regeneration after redact sweep skipped: %s", e)
        result["brief_regenerated"] = False
    return result
