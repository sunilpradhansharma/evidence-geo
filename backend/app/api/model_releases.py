"""Model Release Event Correlation API (FR-707a).

Model updates are AUTO-DETECTED from response-drift spikes (no manual logging); the
differ correlates material drift against them. Exposes the timeline overlay, the
operational correlation ratio, and the before/after response drift list + detail."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.schemas import (
    LiveVersionItem,
    ModelReleaseOut,
    ResponseDriftDetail,
    ResponseDriftItem,
    VersionImpactItem,
)
from app.services import model_release_service as svc

router = APIRouter(prefix="/model-releases", tags=["model-releases"])


@router.get("", response_model=list[ModelReleaseOut])
async def list_releases(target_platform: str | None = None, db: AsyncSession = Depends(get_db)):
    """List detected model updates, optionally filtered by platform."""
    return await svc.list_releases(db, target_platform=target_platform)


@router.get("/correlation-ratio")
async def correlation_ratio(db: AsyncSession = Depends(get_db)):
    """FR-707a.7: correlated material drifts / total material drifts."""
    return await svc.correlation_ratio(db)


@router.get("/drift-timeline")
async def drift_timeline(target_platform: str | None = None, db: AsyncSession = Depends(get_db)):
    """FR-707a.5: material-drift counts per day + release markers for the timeline overlay."""
    return await svc.drift_timeline(db, target_platform=target_platform)


@router.get("/drifts", response_model=list[ResponseDriftItem])
async def list_drifts(
    target_platform: str | None = None, limit: int = 100, db: AsyncSession = Depends(get_db),
):
    """Material response drifts (question + platform + before/after snippets) for the
    AI Update Impact list. Each row links to the actual model responses."""
    return await svc.list_drifts(db, target_platform=target_platform, limit=limit)


@router.get("/drifts/{diff_id}", response_model=ResponseDriftDetail)
async def get_drift_detail(diff_id: int, db: AsyncSession = Depends(get_db)):
    """Full before/after model responses for one drift (the 'View responses' drawer)."""
    detail = await svc.get_drift_detail(db, diff_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Drift not found")
    return detail


@router.post("/detect")
async def detect(db: AsyncSession = Depends(get_db)):
    """Re-run auto-detection of model updates from drift spikes (idempotent). Normally
    runs automatically after scoring; exposed for on-demand backfill/refresh."""
    return await svc.detect_model_updates(db)


@router.post("/sync")
async def sync(db: AsyncSession = Depends(get_db)):
    """Capture REAL vendor versions + changelogs (FR-707a): refresh version observations
    from our own traffic, turn new versions into api-sourced events, and (when the opt-in
    sync is enabled) fetch vendor changelogs to enrich each event with 'what changed'.
    Idempotent; also runnable daily via the scheduler."""
    from app.model_updates import sync_model_updates

    return await sync_model_updates(db)


@router.get("/sync-status")
async def sync_status_endpoint():
    """Config snapshot: whether changelog sync is enabled + the configured vendor sources."""
    from app.model_updates import sync_status

    return sync_status()


@router.get("/versions", response_model=list[LiveVersionItem])
async def live_versions(db: AsyncSession = Depends(get_db)):
    """Current live vendor version per target (from Response.llm_model_version), with when
    it first appeared and how many distinct versions we've observed."""
    from app.model_updates.versions import list_current_versions

    return await list_current_versions(db)


@router.get("/version-impact", response_model=list[VersionImpactItem])
async def version_impact(target_platform: str | None = None, db: AsyncSession = Depends(get_db)):
    """Per model-update event: how many tracked answers changed across it + net brand
    sentiment shift + competitive-position changes ('how vX→vY affected our answers')."""
    return await svc.version_impact(db, target_platform=target_platform)


@router.get("/high-impact", response_model=list[VersionImpactItem])
async def high_impact(target_platform: str | None = None, db: AsyncSession = Depends(get_db)):
    """Model updates that materially moved our tracked answers (many answers changed or a
    sizeable brand-sentiment drop) — the alert feed also surfaced in the stakeholder digest."""
    return await svc.high_impact_updates(db, target_platform=target_platform)
