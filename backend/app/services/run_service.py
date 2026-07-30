"""Run lifecycle service — creation, background execution, post-run scoring."""
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.cancellation import clear_cancel
from app.agent.orchestrator import execute_run
from app.models.database import AsyncSessionLocal
from app.models.response import Response
from app.models.run import Run
from app.schemas import RunCreate
from app.utils.audit import write_audit
from app.utils.logging import get_logger

logger = get_logger("run_service")

# Statuses a run can be continued from. A run stops for three recoverable reasons: the
# process went away (deploy/crash sweep -> FAILED), it hit the token ceiling
# (PAUSED_BUDGET), or an operator stopped it (CANCELLED). In all three the responses
# already captured are intact and execute_run can pick up exactly where it left off.
# CANCELLED is included because stopping a run and later wanting the rest of it is a
# normal operator sequence, and the alternative (Re-run) re-pays for every response that
# was already bought. mark_resuming clears the cancel flag, so the resumed run is not
# aborted by its own earlier stop request.
RESUMABLE_STATUSES = ("FAILED", "PAUSED_BUDGET", "CANCELLED")

INTERRUPTED_NOTE = (
    "Interrupted by server restart (deploy or crash). Resume to continue from the "
    "responses already captured."
)


async def fail_interrupted_runs() -> int:
    """Reconcile runs left RUNNING by a previous (crashed/restarted) process.

    Background run tasks do not survive a server restart, so a lingering RUNNING row is
    stale — it makes the UI show a phantom "in progress" run that never advances. On
    startup we flip any such run to FAILED, preserving the partial responses already
    captured so it stays resumable. Returns the number of runs reconciled."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Run).where(Run.status == "RUNNING"))
        stale = list(result.scalars().all())
        for run in stale:
            run.status = "FAILED"
            run.ended_at = datetime.now(timezone.utc)
            run.notes = INTERRUPTED_NOTE
            clear_cancel(run.run_id)
        if stale:
            await db.commit()
        return len(stale)


async def create_run(db: AsyncSession, data: RunCreate) -> Run:
    run = Run(
        run_id=str(uuid.uuid4()),
        trigger=data.trigger,
        monitoring_mode=data.monitoring_mode,
        status="RUNNING",
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


def build_rerun_data(run: Run) -> RunCreate:
    """Reconstruct a RunCreate from a prior run so it can be executed again without the
    operator reselecting questions. Prefers the original run's config_snapshot (its exact
    question_ids + persona/TA/domain filters); falls back to the run's monitoring_mode when
    no snapshot exists (older runs). Always a real ADHOC run — never a dry-run."""
    monitoring_mode = run.monitoring_mode or "BRAND"
    filters: dict = {}
    if run.config_snapshot:
        try:
            snapshot = json.loads(run.config_snapshot)
            monitoring_mode = snapshot.get("monitoring_mode", monitoring_mode)
            filters = snapshot.get("filters") or {}
        except (ValueError, TypeError):
            filters = {}
    return RunCreate(
        trigger="ADHOC",
        monitoring_mode=monitoring_mode,
        persona=filters.get("persona"),
        therapeutic_area=filters.get("therapeutic_area"),
        domain=filters.get("domain"),
        question_ids=filters.get("question_ids"),
        dry_run=False,
    )


def resume_blocker(run: Run) -> str | None:
    """Why this run cannot be resumed, or None when it can be.

    Returned verbatim to the operator, so each message says what to do instead."""
    if run.status == "RUNNING":
        return "Run is already in progress."
    if run.status not in RESUMABLE_STATUSES:
        return (
            f"A {run.status} run cannot be resumed. Use Re-run to start a fresh run with "
            "the same questions."
        )
    if not run.config_snapshot:
        return (
            "This run has no config snapshot, so the original question set cannot be "
            "reconstructed. Use Re-run instead."
        )
    try:
        json.loads(run.config_snapshot)
    except (ValueError, TypeError):
        return "This run's config snapshot is unreadable. Use Re-run instead."
    return None


async def mark_resuming(db: AsyncSession, run: Run) -> None:
    """Flip a resumable run back to RUNNING so execute_run continues it IN PLACE.

    Same run_id on purpose — that is what makes this a resume rather than a re-run:
    execute_run skips every (question, target) pair that already has a response and seeds
    the budget from run.total_tokens, so the work already paid for is kept and only the
    remainder is dispatched."""
    done = (run.responses_success + run.responses_failed
            + run.responses_truncated + run.responses_blocked)
    previous = (run.notes or "").strip()
    # A cancel flag left over from an earlier attempt would abort the resume on its first
    # check, before a single question dispatched.
    clear_cancel(run.run_id)
    run.status = "RUNNING"
    run.ended_at = None
    run.notes = f"Resumed with {done} response(s) already captured."
    if previous:
        run.notes += f" Previously: {previous}"
    await db.commit()
    await write_audit(
        db, role="OPERATOR", event="RUN_RESUMED", run_id=run.run_id,
        context={"responses_already_captured": done, "previous_notes": previous},
    )
    logger.info("Resuming run %s (%d response(s) already captured)", run.run_id, done)


async def count_failed_responses(db: AsyncSession, run_id: str) -> int:
    """How many FAILED response rows this run actually holds.

    Counted from the rows rather than read off run.responses_failed: the counter is a
    running tally that a retry mutates, and the decision to spend money on a retry has to
    be made against what is really stored."""
    result = await db.execute(
        select(func.count())
        .select_from(Response)
        .where(Response.run_id == run_id, Response.status == "FAILED")
    )
    return int(result.scalar() or 0)


def retry_failed_blocker(run: Run) -> str | None:
    """Why this run's failed responses cannot be retried, or None when they can be.

    Returned verbatim to the operator, so each message says what to do instead."""
    if run.status == "RUNNING":
        return "Run is already in progress."
    if run.status == "AWAITING_OPENEVIDENCE":
        return (
            "This run is paused for OpenEvidence capture. Finish or finalize that capture "
            "first, then retry the failed responses."
        )
    if not run.config_snapshot:
        return (
            "This run has no config snapshot, so the original question set cannot be "
            "reconstructed. Use Re-run instead."
        )
    try:
        json.loads(run.config_snapshot)
    except (ValueError, TypeError):
        return "This run's config snapshot is unreadable. Use Re-run instead."
    return None


async def mark_retrying_failed(db: AsyncSession, run: Run) -> int:
    """Drop this run's FAILED responses and reopen it so those pairs are dispatched again.

    The delete is what makes the retry possible: responses carry a UNIQUE
    (run_id, question_id, llm_name), and execute_run treats ANY stored row for a pair as
    done, so a FAILED row both blocks a re-insert and marks the pair complete. Deleting it
    loses nothing: a FAILED row's response_text is the error string, never a model answer,
    no scoring/insights record can reference it (both filter to SUCCESS/TRUNCATED), and the
    failure itself is kept permanently in audit_log as that pair's LLM_CALL event.

    total_tokens and estimated_cost_usd are deliberately left alone: that spend happened.
    Returns the number of responses cleared for retry."""
    result = await db.execute(
        select(Response.response_id).where(
            Response.run_id == run.run_id, Response.status == "FAILED"
        )
    )
    failed_ids = list(result.scalars().all())
    if failed_ids:
        await db.execute(
            delete(Response).where(Response.response_id.in_(failed_ids))
        )
    cleared = len(failed_ids)
    run.responses_failed = max(0, run.responses_failed - cleared)
    previous = (run.notes or "").strip()
    # A cancel flag left over from an earlier attempt would abort the retry on its first
    # check, before a single question dispatched.
    clear_cancel(run.run_id)
    run.status = "RUNNING"
    run.ended_at = None
    run.notes = f"Retrying {cleared} failed response(s)."
    if previous:
        run.notes += f" Previously: {previous}"
    await db.commit()
    await write_audit(
        db, role="OPERATOR", event="RUN_RETRY_FAILED", run_id=run.run_id,
        context={"responses_cleared": cleared, "previous_notes": previous},
    )
    logger.info("Retrying %d failed response(s) for run %s", cleared, run.run_id)
    return cleared


async def run_in_background(run_id: str, data: RunCreate) -> None:
    """Execute a run end-to-end in its own session, then trigger scoring.

    Used for both a fresh run and a resume — execute_run is idempotent per
    (run, question, target), so passing an existing run_id continues that run."""
    from app.scoring.scorer import score_run  # late import to avoid cycle

    async with AsyncSessionLocal() as db:
        try:
            run = await execute_run(
                db, run_id,
                persona=data.persona,
                therapeutic_area=data.therapeutic_area,
                domain=data.domain,
                question_ids=data.question_ids,
                dry_run=data.dry_run,
                monitoring_mode=data.monitoring_mode,
            )
            # Stop here for dry/paused/cancelled runs — no scoring or insights passes,
            # so a cancelled run truly halts all work immediately. A cancelled run keeps
            # the partial responses already captured (they just stay unscored).
            if data.dry_run or run.status in ("PAUSED_BUDGET", "CANCELLED"):
                return
        except Exception as e:  # noqa: BLE001
            logger.exception("Run failed: %s", e)
            await db.rollback()  # the failure may have left the session mid-transaction
            run = await db.get(Run, run_id)
            if run:
                run.status = "FAILED"
                # Set ended_at here too, so a run that died on an exception is
                # distinguishable from one swept by the restart reconciler.
                run.ended_at = datetime.now(timezone.utc)
                run.notes = str(e)[:2000]
                await db.commit()
            clear_cancel(run_id)
            return

    # Scoring pass (FR-406) in a fresh session
    async with AsyncSessionLocal() as db:
        try:
            await score_run(db, run_id)
        except Exception as e:  # noqa: BLE001
            logger.exception("Scoring failed for run %s: %s", run_id, e)

    # Advanced analytics: tag the new responses against the current theme taxonomy.
    # Best-effort — never fail a run because insights tagging hiccuped, and no-op until
    # a taxonomy has been built at least once via POST /insights/rebuild.
    try:
        from app.insights import pipeline as insights_pipeline  # late import to avoid cycle

        async with AsyncSessionLocal() as db:
            await insights_pipeline.tag_new(db)
    except Exception as e:  # noqa: BLE001
        logger.warning("Insights tagging skipped for run %s: %s", run_id, e)

    # Source Authority Mapping (FR-706a): classify the cited sources of this run's grounded
    # responses, record preferred-source observations, and raise source alerts. Best-effort —
    # a classification hiccup must never fail the run (backfill via POST /source-authority/classify/sweep).
    try:
        from app.source_authority import service as source_authority_service

        async with AsyncSessionLocal() as db:
            summary = await source_authority_service.classify_run(db, run_id)
        logger.info("Source authority classified for run %s: %s", run_id, summary)
    except Exception as e:  # noqa: BLE001
        logger.warning("Source authority classification skipped for run %s: %s", run_id, e)

    # Mirror the run's new data into Snowflake (best-effort; no-op when disabled).
    try:
        from app.snowflake import mirror as sf_mirror  # late import to avoid cycle

        await sf_mirror.run_mirror_safe()
    except Exception as e:  # noqa: BLE001
        logger.warning("Snowflake mirror skipped for run %s: %s", run_id, e)


async def list_runs(db: AsyncSession, limit: int = 100) -> list[Run]:
    result = await db.execute(select(Run).order_by(Run.started_at.desc()).limit(limit))
    return list(result.scalars().all())


async def get_run(db: AsyncSession, run_id: str) -> Run | None:
    return await db.get(Run, run_id)


async def run_progress(db: AsyncSession, run_id: str) -> dict | None:
    """Live per-model progress for a run, derived from actually-stored responses.

    Powers the Pipeline execution view with real status (no client-side simulation):
    each model's `done` count is the number of responses persisted so far for that run.
    """
    run = await db.get(Run, run_id)
    if run is None:
        return None

    rows = await db.execute(
        select(
            Response.llm_name,
            func.count().label("total"),
            func.sum(case((Response.status == "SUCCESS", 1), else_=0)).label("success"),
            func.sum(case((Response.status == "TRUNCATED", 1), else_=0)).label("truncated"),
            func.sum(case((Response.status == "BLOCKED", 1), else_=0)).label("blocked"),
            func.sum(case((Response.status == "FAILED", 1), else_=0)).label("failed"),
        )
        .where(Response.run_id == run_id)
        .group_by(Response.llm_name)
    )
    by_model = {
        r.llm_name: {
            "done": int(r.total or 0),
            "success": int(r.success or 0),
            "truncated": int(r.truncated or 0),
            "blocked": int(r.blocked or 0),
            "failed": int(r.failed or 0),
        }
        for r in rows.all()
    }
    return {
        "run_id": run.run_id,
        "status": run.status,
        "questions_attempted": run.questions_attempted,
        "responses_done": run.responses_success + run.responses_failed
        + run.responses_truncated + run.responses_blocked,
        "by_model": by_model,
    }
