"""Advanced Analytics API — theme discovery, trends, and signal extraction.

`POST /insights/rebuild` runs the (potentially slow) taxonomy discovery + tagging in the
background; the UI polls `GET /insights/status` for progress. The read endpoints serve the
theme overview, per-theme detail, time-series, and extracted signals.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights import pipeline, trends
from app.models.database import AsyncSessionLocal, get_db
from app.utils.logging import get_logger

logger = get_logger("api.insights")

router = APIRouter(prefix="/insights", tags=["insights"])

# In-memory rebuild state (single-process POC). Surfaced via /insights/status.
_REBUILD: dict = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "last_result": None,
    "error": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _rebuild_task(target_themes: int, sample_cap: int) -> None:
    _REBUILD.update(running=True, started_at=_now(), finished_at=None, error=None)
    try:
        async with AsyncSessionLocal() as db:
            result = await pipeline.rebuild(db, target_themes=target_themes, sample_cap=sample_cap)
        _REBUILD["last_result"] = result
        logger.info("insights rebuild result: %s", result)
    except Exception as e:  # noqa: BLE001
        _REBUILD["error"] = str(e)
        logger.exception("insights rebuild failed: %s", e)
    finally:
        _REBUILD.update(running=False, finished_at=_now())


@router.post("/rebuild", status_code=202)
async def rebuild(
    background_tasks: BackgroundTasks,
    target_themes: int = Query(12, ge=4, le=30),
    sample_cap: int = Query(300, ge=20, le=2000),
):
    """Discover a fresh theme taxonomy and tag all responses (runs in the background)."""
    if _REBUILD["running"]:
        raise HTTPException(409, "A rebuild is already running")
    background_tasks.add_task(_rebuild_task, target_themes, sample_cap)
    return {"status": "started", "target_themes": target_themes, "sample_cap": sample_cap}


@router.get("/status")
async def status(db: AsyncSession = Depends(get_db)):
    base = await pipeline.status(db)
    base["rebuild"] = {
        "running": _REBUILD["running"],
        "started_at": _REBUILD["started_at"],
        "finished_at": _REBUILD["finished_at"],
        "last_result": _REBUILD["last_result"],
        "error": _REBUILD["error"],
    }
    return base


@router.get("/themes")
async def themes(
    persona: str | None = Query(None),
    therapeutic_area: str | None = Query(None),
    indication: str | None = Query(None),
    disease: str | None = Query(None),
    brand: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    return await trends.theme_overview(
        db, persona=persona,
        therapeutic_area=therapeutic_area, indication=indication, disease=disease, brand=brand,
    )


@router.get("/trends")
async def trend_series(
    top: int = Query(8, ge=1, le=20),
    persona: str | None = Query(None),
    therapeutic_area: str | None = Query(None),
    indication: str | None = Query(None),
    disease: str | None = Query(None),
    brand: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    return await trends.theme_timeseries(
        db, top=top, persona=persona,
        therapeutic_area=therapeutic_area, indication=indication, disease=disease, brand=brand,
    )


@router.get("/signals")
async def signals(
    persona: str | None = Query(None),
    therapeutic_area: str | None = Query(None),
    indication: str | None = Query(None),
    disease: str | None = Query(None),
    brand: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    return await trends.signals(
        db, persona=persona,
        therapeutic_area=therapeutic_area, indication=indication, disease=disease, brand=brand,
    )


@router.get("/themes/{theme_id}")
async def theme_detail(theme_id: str, db: AsyncSession = Depends(get_db)):
    detail = await trends.theme_detail(db, theme_id)
    if detail is None:
        raise HTTPException(404, "Theme not found")
    return detail
