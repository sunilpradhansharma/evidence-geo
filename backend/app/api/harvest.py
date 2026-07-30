"""Discovery API — harvest real user questions, review staged candidates, and promote
approved ones into the Question Repository (as PENDING for Medical-Affairs approval).

`POST /harvest/run` runs the (slow, network-bound) search + classify pipeline in the
background; the UI polls `GET /harvest/status` for progress, mirroring /insights.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.harvest import pipeline
from app.models.database import AsyncSessionLocal, get_db
from app.schemas import HarvestPromote, HarvestReject, HarvestRunToPipeline, QuestionOut, RunCreate
from app.services import harvest_service as svc
from app.services import run_service
from app.utils.audit import write_audit
from app.utils.logging import get_logger

logger = get_logger("api.harvest")

router = APIRouter(prefix="/harvest", tags=["harvest"])

# In-memory harvest state (single-process POC). Surfaced via /harvest/status.
_HARVEST: dict = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "last_result": None,
    "error": None,
    "progress": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _harvest_task(max_queries: int | None, max_items: int | None,
                        persona: str | None = None, therapeutic_area: str | None = None,
                        brand_focus: str | None = None, monitoring_mode: str | None = None) -> None:
    # Mutated in place by pipeline.harvest and surfaced live via /harvest/status.
    progress: dict = {"phase": "starting", "queries_total": 0, "queries_done": 0,
                      "raw_results": 0, "candidates": 0, "staged": 0, "quarantined_ae": 0}
    _HARVEST.update(running=True, started_at=_now(), finished_at=None, error=None,
                    last_result=None, progress=progress,
                    scope={"persona": persona, "therapeutic_area": therapeutic_area,
                           "monitoring_mode": monitoring_mode})
    try:
        async with AsyncSessionLocal() as db:
            result = await pipeline.harvest(db, max_queries=max_queries,
                                            max_items=max_items, progress=progress,
                                            persona=persona, therapeutic_area=therapeutic_area,
                                            brand_focus=brand_focus, monitoring_mode=monitoring_mode)
        _HARVEST["last_result"] = result
        logger.info("harvest result: %s", result)
    except Exception as e:  # noqa: BLE001
        _HARVEST["error"] = str(e)
        logger.exception("harvest failed: %s", e)
    finally:
        progress["phase"] = "done"
        _HARVEST.update(running=False, finished_at=_now())


@router.post("/run", status_code=202)
async def run_harvest(
    background_tasks: BackgroundTasks,
    max_queries: int | None = Query(None, ge=1, le=200),
    max_items: int | None = Query(None, ge=1, le=1000),
    persona: str | None = Query(None, description="Scope discovery to Prospect/Provider/Patient"),
    therapeutic_area: str | None = Query(None, description="Scope discovery to one therapeutic area"),
    monitoring_mode: str | None = Query(
        None,
        pattern="^(BRAND|DISEASE_STATE)$",
        description="BRAND (AbbVie focus brands) or DISEASE_STATE (whole-landscape / All Brands)",
    ),
):
    """Search public communities for real questions and stage them (runs in background).

    Optional persona / therapeutic_area scope which audience + area to target;
    monitoring_mode selects AbbVie (focus brands) vs All Brands (full landscape) discovery."""
    if _HARVEST["running"]:
        raise HTTPException(409, "A harvest is already running")
    background_tasks.add_task(_harvest_task, max_queries, max_items, persona,
                              therapeutic_area, None, monitoring_mode)
    return {"status": "started"}


@router.get("/status")
async def status(db: AsyncSession = Depends(get_db)):
    base = await pipeline.stats(db)
    base["harvest"] = dict(_HARVEST)
    return base


@router.get("/items")
async def items(
    status: str | None = None,
    source: str | None = None,
    persona: str | None = None,
    therapeutic_area: str | None = None,
    domain: str | None = None,
    ae_only: bool = False,
    limit: int = Query(200, le=1000),
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    return await svc.list_items(
        db,
        status=status,
        source=source,
        persona=persona,
        therapeutic_area=therapeutic_area,
        domain=domain,
        ae_only=ae_only,
        limit=limit,
        offset=offset,
    )


@router.post("/items/{item_id}/promote", response_model=QuestionOut)
async def promote(item_id: int, data: HarvestPromote, db: AsyncSession = Depends(get_db)):
    return await svc.promote(db, item_id, data)


@router.post("/items/{item_id}/reject")
async def reject(item_id: int, data: HarvestReject, db: AsyncSession = Depends(get_db)):
    return await svc.reject(db, item_id, data.reason)


@router.post("/run-to-pipeline", status_code=202)
async def run_to_pipeline(
    data: HarvestRunToPipeline,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """One-click Discover action: promote + APPROVE the selected harvested items, then launch
    an ad-hoc run scoped to exactly those questions. Adverse-event / PII / injection / incomplete
    items are skipped (and reported back), never run. Returns the run_id (null if nothing
    qualified) plus what was promoted and skipped."""
    result = await svc.promote_and_approve_batch(
        db, data.item_ids, reviewer_name=data.reviewer_name
    )
    question_ids = result["question_ids"]
    run_id = None
    if question_ids:
        run_data = RunCreate(
            trigger="ADHOC",
            monitoring_mode=data.monitoring_mode,
            question_ids=question_ids,
        )
        run = await run_service.create_run(db, run_data)
        run_id = run.run_id
        await write_audit(
            db,
            role="REVIEWER",
            event="HARVEST_RUN_TO_PIPELINE",
            run_id=run_id,
            context={
                "item_ids": data.item_ids,
                "question_ids": question_ids,
                "reviewer_name": data.reviewer_name,
                "skipped_item_ids": [s["id"] for s in result["skipped"]],
            },
        )
        background_tasks.add_task(run_service.run_in_background, run_id, run_data)
    return {
        "run_id": run_id,
        "ran_count": len(question_ids),
        "promoted": result["promoted"],
        "skipped": result["skipped"],
    }
