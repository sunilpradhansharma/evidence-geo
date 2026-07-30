"""Activation & Impact — measurement sweep (thin v1).

A single recurring job (NOT one job per intervention — respects the single-worker scheduler
constraint) that advances every in-flight intervention's measurement state machine:

    BASELINE_RUNNING --(runs ready)--> MEASURING (official baseline finalized)
    MEASURING --(post_due reached)--> POST_RUNNING (post runs launched)
    POST_RUNNING --(runs ready)--> DONE (post snapshot + before/after result; COMPLETED)

Each intervention is advanced independently and best-effort, so one failure never blocks the
others. ``advance_intervention`` is also called directly (force=True) by the "measure now"
endpoint for demos.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.activation import measurement
from app.models.intervention import Intervention
from app.models.measurement_snapshot import MeasurementSnapshot
from app.services import intervention_service as svc
from app.utils.logging import get_logger

logger = get_logger("activation.sweep")

_ACTIVE_MEASUREMENT_STATUSES = ["BASELINE_RUNNING", "MEASURING", "POST_RUNNING"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _personas_models(interv: Intervention) -> tuple[list[str] | None, list[str] | None]:
    personas = svc._jl(interv.target_personas_json) or None
    models = svc._jl(interv.target_models_json) or None
    return personas, models


async def advance_intervention(db: AsyncSession, interv: Intervention, *, force: bool = False) -> str:
    """Advance one intervention's measurement state machine by at most one step. Commits."""
    ms = interv.measurement_status
    personas, models = _personas_models(interv)

    if ms == "BASELINE_RUNNING":
        snap = await db.get(MeasurementSnapshot, interv.official_baseline_snapshot_id) \
            if interv.official_baseline_snapshot_id else None
        if snap is None:
            return "baseline_missing"
        run_ids = svc._jl(snap.run_ids_json)
        if not await measurement.runs_ready(db, run_ids):
            return "baseline_pending"
        await measurement.finalize_snapshot(db, snap, personas=personas, models=models, commit=False)
        interv.measurement_status = "MEASURING"
        if interv.status == "PUBLISHED":
            interv.status = "MEASURING"
        await svc._record_event(
            db, interv, "BASELINE_CAPTURED", new_status=interv.status,
            metadata={"snapshot_id": snap.id, "response_count": snap.response_count},
        )
        await db.commit()
        return "baseline_captured"

    if ms == "MEASURING":
        due = interv.post_due_at
        ready_to_start = force or (due is not None and _utcnow() >= due)
        if not ready_to_start:
            return "waiting_post_window"
        question_ids = svc._jl(interv.target_question_ids_json)
        if not question_ids:
            return "no_questions"
        run_ids = await svc._launch_measurement_runs(
            db, question_ids=question_ids, monitoring_mode=interv.monitoring_mode,
            reps=interv.repetitions_per_question,
        )
        snap = measurement.create_pending_snapshot(
            intervention_id=interv.id, snapshot_type="POST",
            run_ids=run_ids, question_ids=question_ids,
        )
        db.add(snap)
        interv.post_snapshot_id = snap.id
        interv.measurement_status = "POST_RUNNING"
        await svc._record_event(
            db, interv, "MEASUREMENT_STARTED", metadata={"post_runs": run_ids},
        )
        await db.commit()
        return "post_started"

    if ms == "POST_RUNNING":
        post = await db.get(MeasurementSnapshot, interv.post_snapshot_id) \
            if interv.post_snapshot_id else None
        if post is None:
            return "post_missing"
        run_ids = svc._jl(post.run_ids_json)
        if not await measurement.runs_ready(db, run_ids):
            return "post_pending"
        await measurement.finalize_snapshot(db, post, personas=personas, models=models, commit=False)
        baseline = await db.get(MeasurementSnapshot, interv.official_baseline_snapshot_id) \
            if interv.official_baseline_snapshot_id else None
        if baseline is None:
            interv.measurement_status = "ERROR"
            await svc._record_event(db, interv, "MEASUREMENT_ERROR",
                                    notes="Official baseline snapshot missing at result time.")
            await db.commit()
            return "error_no_baseline"
        result = await measurement.compute_result(
            db, intervention=interv, baseline=baseline, post=post, commit=False,
        )
        interv.outcome_status = result.outcome_status
        interv.measurement_status = "DONE"
        prev_status = interv.status
        interv.status = "COMPLETED"
        await svc._record_event(
            db, interv, "MEASUREMENT_COMPLETED", previous_status=prev_status,
            new_status="COMPLETED",
            metadata={"outcome": result.outcome_status, "confidence": result.confidence,
                      "post_response_count": post.response_count},
        )
        await db.commit()
        logger.info("Intervention %s measured: %s (%s confidence)",
                    interv.id, result.outcome_status, result.confidence)
        return "completed"

    return "noop"


async def run_sweep() -> dict:
    """Advance every in-flight intervention. Safe to call repeatedly (idempotent per step)."""
    from app.models.database import AsyncSessionLocal

    actions: dict[str, str] = {}
    async with AsyncSessionLocal() as db:
        rows = list((await db.execute(
            select(Intervention).where(
                Intervention.measurement_status.in_(_ACTIVE_MEASUREMENT_STATUSES)
            )
        )).scalars().all())
        for interv in rows:
            try:
                actions[interv.id] = await advance_intervention(db, interv)
            except Exception as e:  # noqa: BLE001 — one bad intervention never blocks the rest
                logger.warning("Sweep failed for intervention %s: %s", interv.id, e)
                actions[interv.id] = f"error: {e}"
    if actions:
        logger.info("Intervention sweep processed %d intervention(s)", len(actions))
    return {"processed": len(actions), "actions": actions}
