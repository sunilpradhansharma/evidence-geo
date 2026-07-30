"""Run management API (FR-501..506, FR-209/210)."""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.cancellation import request_cancel
from app.models.database import get_db
from app.schemas import RunCreate, RunOut
from app.services import run_service as svc
from app.services.deploy_lock import is_deploying

router = APIRouter(prefix="/runs", tags=["runs"])


def _reject_if_deploying() -> None:
    """Refuse to START work while a deploy is staging on this host.

    The deploy ends by replacing the container, so anything started now is killed
    mid-flight. The window is minutes long (prune + image build) and the old container
    serves normally throughout it, so without this an operator gets no warning at all.
    Only run STARTS are blocked — reads, cancels and every other endpoint stay live."""
    if is_deploying():
        raise HTTPException(
            503,
            "A deployment is in progress on this server. A run started now would be "
            "killed when the new container swaps in. Try again in a few minutes.",
        )


@router.get("", response_model=list[RunOut])
async def list_runs(limit: int = 100, db: AsyncSession = Depends(get_db)):
    return await svc.list_runs(db, limit=limit)


# Declared BEFORE /{run_id} on purpose: FastAPI matches routes in definition order, and
# a literal path that looks like an id would otherwise be swallowed by the id route.
@router.get("/deploy-status")
async def deploy_status():
    """Whether a deploy is staging on this host, so the UI can warn before a run is lost."""
    return {"deploying": is_deploying()}


@router.get("/{run_id}", response_model=RunOut)
async def get_run(run_id: str, db: AsyncSession = Depends(get_db)):
    run = await svc.get_run(db, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return run


@router.get("/{run_id}/progress")
async def get_run_progress(run_id: str, db: AsyncSession = Depends(get_db)):
    """Live per-model progress derived from stored responses (real status, no simulation)."""
    progress = await svc.run_progress(db, run_id)
    if progress is None:
        raise HTTPException(404, "Run not found")
    return progress


@router.post("", response_model=RunOut, status_code=202)
async def create_run(
    data: RunCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Trigger an ad-hoc run (FR-506). Supports persona/TA/domain filters (FR-210)
    and dry-run mode (FR-209). Executes asynchronously in the background."""
    _reject_if_deploying()
    run = await svc.create_run(db, data)
    background_tasks.add_task(svc.run_in_background, run.run_id, data)
    return run


@router.post("/{run_id}/cancel", response_model=RunOut)
async def cancel_run(run_id: str, db: AsyncSession = Depends(get_db)):
    """Request cancellation of an in-flight run. The orchestrator stops cleanly
    between questions; responses already captured are preserved (NF-005)."""
    run = await svc.get_run(db, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    if run.status != "RUNNING":
        raise HTTPException(409, f"Run is not running (status={run.status})")
    request_cancel(run_id)
    return run


@router.post("/{run_id}/rerun", response_model=RunOut, status_code=202)
async def rerun_run(
    run_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Re-run a previous run's exact question set and filters as a fresh ad-hoc run, so the
    operator doesn't have to reselect questions. Reconstructs the request from the original
    run's config_snapshot (FR-506). Runs asynchronously in the background like create_run.

    This starts a FRESH run and re-pays for every response. To continue an interrupted run
    from where it stopped, use /resume instead."""
    _reject_if_deploying()
    original = await svc.get_run(db, run_id)
    if original is None:
        raise HTTPException(404, "Run not found")
    data = svc.build_rerun_data(original)
    run = await svc.create_run(db, data)
    background_tasks.add_task(svc.run_in_background, run.run_id, data)
    return run


@router.post("/{run_id}/resume", response_model=RunOut, status_code=202)
async def resume_run(
    run_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Continue an interrupted run IN PLACE, keeping the same run_id (FR-504).

    The difference from /rerun is the run_id: because it is unchanged, execute_run skips
    every (question, target) pair that already has a stored response and seeds the token
    budget from the run's existing total, so only the remaining work is dispatched and
    nothing already paid for is bought twice. Resumable statuses are FAILED (which is what
    a deploy or crash leaves behind), PAUSED_BUDGET, and CANCELLED (an operator stop is a
    pause in practice: the alternative would re-pay for every response already bought)."""
    _reject_if_deploying()
    run = await svc.get_run(db, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    blocker = svc.resume_blocker(run)
    if blocker:
        raise HTTPException(409, blocker)
    data = svc.build_rerun_data(run)  # same snapshot reconstruction; question set is identical
    await svc.mark_resuming(db, run)
    background_tasks.add_task(svc.run_in_background, run_id, data)
    return run


@router.post("/{run_id}/retry-failed", response_model=RunOut, status_code=202)
async def retry_failed_responses(
    run_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Re-attempt only this run's FAILED responses, in place under the same run_id.

    Separate from /resume on purpose. Resume dispatches the pairs that were never
    attempted and leaves a failure alone; this dispatches the pairs that WERE attempted
    and errored (timeout, rate limit, provider fault), which is a different decision with
    a different cost. A run that finished COMPLETED with failures can be retried too.

    The failed rows are deleted first: a response is unique per (run, question, target)
    and execute_run treats any stored row as done, so the row both blocks the re-insert
    and marks the pair complete. Nothing derived is lost (scoring and insights only ever
    look at SUCCESS/TRUNCATED) and each failure stays in audit_log as its LLM_CALL event.
    BLOCKED responses are deliberately left alone: a safety refusal refuses again."""
    _reject_if_deploying()
    run = await svc.get_run(db, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    blocker = svc.retry_failed_blocker(run)
    if blocker:
        raise HTTPException(409, blocker)
    # Counted from the rows, not from run.responses_failed: refusing on a stale counter
    # would either reopen a run with nothing to do or block a retry that has work.
    failed = await svc.count_failed_responses(db, run_id)
    if failed == 0:
        raise HTTPException(409, "This run has no failed responses to retry.")
    data = svc.build_rerun_data(run)  # same snapshot reconstruction; question set is identical
    await svc.mark_retrying_failed(db, run)
    background_tasks.add_task(svc.run_in_background, run_id, data)
    return run


@router.post("/dry-run", response_model=RunOut, status_code=202)
async def dry_run(
    data: RunCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    _reject_if_deploying()
    data.dry_run = True
    run = await svc.create_run(db, data)
    background_tasks.add_task(svc.run_in_background, run.run_id, data)
    return run
