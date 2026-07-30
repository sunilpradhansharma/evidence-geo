"""OpenEvidence manual-capture bridge API.

Provider-persona only. OpenEvidence has no automatable API, so answers are pasted in
by a human and ingested here; scoring + consensus refresh run in the background.
"""
from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.schemas import OpenEvidenceCapture
from app.services import openevidence_service as svc

router = APIRouter(prefix="/openevidence", tags=["openevidence"])


@router.get("/runs")
async def runs(db: AsyncSession = Depends(get_db)):
    """Recent runs with Provider-persona questions + OpenEvidence capture progress."""
    return await svc.list_runs_with_provider(db)


@router.get("/worklist")
async def worklist(run_id: str, db: AsyncSession = Depends(get_db)):
    """Provider questions in a run and which still need an OpenEvidence answer."""
    return await svc.worklist(db, run_id)


@router.post("/capture", status_code=201)
async def capture(
    data: OpenEvidenceCapture,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Ingest a pasted OpenEvidence answer as an `open-evidence` Response, then score +
    re-run Chairman consensus for that question in the background. If this was the last
    pending Provider question, the run auto-completes out of AWAITING_OPENEVIDENCE."""
    result = await svc.capture(db, data)
    background_tasks.add_task(svc.finalize_capture, result["response_id"])
    return result


@router.post("/runs/{run_id}/finalize")
async def finalize_run(run_id: str, db: AsyncSession = Depends(get_db)):
    """Escape hatch: close an AWAITING_OPENEVIDENCE run WITHOUT OpenEvidence, computing
    Provider consensus from the automated targets only for any still-pending questions."""
    return await svc.finalize_without_oe(db, run_id)
