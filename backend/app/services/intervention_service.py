"""Service layer for Activation & Impact interventions (thin v1).

Owns the intervention lifecycle: create from a GEO recommendation, lightweight ownership,
manual transitions, publish (which locks the cohort + launches the official baseline), and
read helpers. Every mutation appends an immutable ``InterventionEvent`` and mirrors a compact
record to the general ``audit_log``. Measurement snapshots + the before/after result are
computed by ``app.activation.measurement``; the daily ``app.activation.sweep`` advances the
measurement state machine.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.activation import measurement
from app.models.intervention import (
    DEFAULT_PRIMARY_METRIC,
    Intervention,
    SOURCE_GEO_RECOMMENDATION,
)
from app.models.intervention_event import InterventionEvent
from app.models.intervention_result import InterventionResult
from app.models.measurement_snapshot import MeasurementSnapshot
from app.models.recommendation import Recommendation
from app.models.recommendation_review import RecommendationReview
from app.remediation import citations
from app.schemas import (
    InterventionCreate,
    InterventionPublish,
    InterventionTransition,
    InterventionUpdate,
)
from app.services import run_service
from app.utils.audit import write_audit
from app.utils.logging import get_logger

logger = get_logger("intervention_service")

PROVIDER_PERSONA = "Provider"

# Manual transitions allowed via the /transition endpoint. Publish + the measurement sweep
# own PUBLISHED / MEASURING / COMPLETED; COMPLETED is terminal.
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "PROPOSED": {"IN_PROGRESS", "DEFERRED", "CANCELLED"},
    "IN_PROGRESS": {"PROPOSED", "DEFERRED", "CANCELLED"},
    "DEFERRED": {"PROPOSED", "IN_PROGRESS", "CANCELLED"},
}
_PRE_PUBLISH_STATUSES = {"PROPOSED", "IN_PROGRESS", "DEFERRED"}

# Strong refs to fire-and-forget measurement run tasks so the loop can't GC them.
_bg_tasks: set[asyncio.Task] = set()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _launch(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _jl(raw: str | None) -> list:
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except (ValueError, TypeError):
        return []


# --------------------------------------------------------------- events + audit
async def _record_event(
    db: AsyncSession, interv: Intervention, event_type: str, *,
    previous_status: str | None = None, new_status: str | None = None,
    actor: str | None = None, notes: str | None = None, metadata: dict | None = None,
    commit: bool = False,
) -> None:
    """Append an immutable timeline event and mirror a compact record to the audit log."""
    db.add(InterventionEvent(
        intervention_id=interv.id,
        event_type=event_type,
        previous_status=previous_status,
        new_status=new_status,
        actor_name=actor,
        notes=notes,
        metadata_json=json.dumps(metadata, default=str) if metadata else None,
    ))
    await write_audit(
        db, role="SYSTEM", event=f"INTERVENTION_{event_type}",
        context={"intervention_id": interv.id, "from": previous_status,
                 "to": new_status, "actor": actor, **(metadata or {})},
        commit=False,
    )
    if commit:
        await db.commit()


# --------------------------------------------------------------- serialization
def _serialize(i: Intervention) -> dict:
    return {
        "id": i.id,
        "recommendation_id": i.recommendation_id,
        "source_type": i.source_type,
        "source_id": i.source_id,
        "title": i.title,
        "description": i.description,
        "status": i.status,
        "priority": i.priority,
        "owner_name": i.owner_name,
        "reviewer_name": i.reviewer_name,
        "review_required": i.review_required,
        "review_status": i.review_status,
        "therapeutic_area": i.therapeutic_area,
        "indication": i.indication,
        "brand_focus": i.brand_focus,
        "publication_url": i.publication_url,
        "publication_date": _iso(i.publication_date),
        "due_date": _iso(i.due_date),
        "monitoring_mode": i.monitoring_mode,
        "target_question_ids": _jl(i.target_question_ids_json),
        "target_personas": _jl(i.target_personas_json),
        "target_models": _jl(i.target_models_json),
        "target_metrics": _jl(i.target_metrics_json),
        "primary_metric": i.primary_metric,
        "measurement_wait_days": i.measurement_wait_days,
        "repetitions_per_question": i.repetitions_per_question,
        "measurement_status": i.measurement_status,
        "post_due_at": _iso(i.post_due_at),
        "outcome_status": i.outcome_status,
        "evidence": json.loads(i.evidence_snapshot_json) if i.evidence_snapshot_json else None,
        "created_at": _iso(i.created_at),
        "updated_at": _iso(i.updated_at),
    }


async def _snapshot(db: AsyncSession, snapshot_id: str | None) -> MeasurementSnapshot | None:
    if not snapshot_id:
        return None
    return await db.get(MeasurementSnapshot, snapshot_id)


async def _latest_result(db: AsyncSession, intervention_id: str) -> InterventionResult | None:
    row = await db.execute(
        select(InterventionResult)
        .where(InterventionResult.intervention_id == intervention_id)
        .order_by(InterventionResult.measured_at.desc())
        .limit(1)
    )
    return row.scalars().first()


async def _detail(db: AsyncSession, interv: Intervention) -> dict:
    data = _serialize(interv)
    data["snapshots"] = {
        "discovery": measurement.serialize_snapshot(
            await _snapshot(db, interv.discovery_baseline_snapshot_id)),
        "official_baseline": measurement.serialize_snapshot(
            await _snapshot(db, interv.official_baseline_snapshot_id)),
        "post": measurement.serialize_snapshot(
            await _snapshot(db, interv.post_snapshot_id)),
    }
    data["result"] = measurement.serialize_result(await _latest_result(db, interv.id))
    data["metric_defs"] = {k: {"label": lbl, "kind": kind}
                           for k, (lbl, kind) in measurement.METRIC_DEFS.items()}
    return data


# --------------------------------------------------------------- reads
async def list_interventions(db: AsyncSession, *, status: str | None = None) -> dict:
    stmt = select(Intervention)
    if status:
        stmt = stmt.where(Intervention.status == status)
    stmt = stmt.order_by(Intervention.updated_at.desc())
    rows = list((await db.execute(stmt)).scalars().all())
    return {"count": len(rows), "items": [_serialize(r) for r in rows]}


async def get_intervention(db: AsyncSession, intervention_id: str) -> dict:
    interv = await db.get(Intervention, intervention_id)
    if interv is None:
        raise HTTPException(status_code=404, detail="Intervention not found")
    return await _detail(db, interv)


async def get_timeline(db: AsyncSession, intervention_id: str) -> dict:
    interv = await db.get(Intervention, intervention_id)
    if interv is None:
        raise HTTPException(status_code=404, detail="Intervention not found")
    rows = list((await db.execute(
        select(InterventionEvent)
        .where(InterventionEvent.intervention_id == intervention_id)
        .order_by(InterventionEvent.created_at.asc(), InterventionEvent.id.asc())
    )).scalars().all())
    items = [{
        "id": e.id,
        "event_type": e.event_type,
        "previous_status": e.previous_status,
        "new_status": e.new_status,
        "actor_name": e.actor_name,
        "notes": e.notes,
        "metadata": json.loads(e.metadata_json) if e.metadata_json else None,
        "created_at": _iso(e.created_at),
    } for e in rows]
    return {"count": len(items), "items": items}


async def get_result(db: AsyncSession, intervention_id: str) -> dict:
    interv = await db.get(Intervention, intervention_id)
    if interv is None:
        raise HTTPException(status_code=404, detail="Intervention not found")
    return {
        "intervention_id": intervention_id,
        "measurement_status": interv.measurement_status,
        "outcome_status": interv.outcome_status,
        "primary_metric": interv.primary_metric,
        "result": measurement.serialize_result(await _latest_result(db, intervention_id)),
        "discovery": measurement.serialize_snapshot(
            await _snapshot(db, interv.discovery_baseline_snapshot_id)),
        "official_baseline": measurement.serialize_snapshot(
            await _snapshot(db, interv.official_baseline_snapshot_id)),
        "post": measurement.serialize_snapshot(await _snapshot(db, interv.post_snapshot_id)),
    }


# --------------------------------------------------------------- create
async def _mark_recommendation_actioned(db: AsyncSession, rec_id: str, actor: str | None) -> None:
    review = await db.get(RecommendationReview, rec_id)
    if review is None:
        review = RecommendationReview(rec_id=rec_id)
        db.add(review)
    review.status = "ACTIONED"
    review.updated_by = actor or "Activation & Impact"


async def create_from_recommendation(
    db: AsyncSession, rec_id: str, data: InterventionCreate
) -> dict:
    rec = await db.get(Recommendation, rec_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    # v1: Provider cohorts park in AWAITING_OPENEVIDENCE (manual capture) and can't be
    # auto-measured — reject up front so an intervention can't get stuck later.
    if (rec.persona or "") == PROVIDER_PERSONA:
        raise HTTPException(
            status_code=400,
            detail="Provider-persona recommendations are not supported for measurement in v1 "
                   "(Provider runs await manual OpenEvidence capture). Use a Patient/Prospect "
                   "recommendation.",
        )

    question_ids: list[str] = []
    if rec.question_id:
        question_ids.append(rec.question_id)
    for qid in (data.extra_question_ids or []):
        if qid and qid not in question_ids:
            question_ids.append(qid)
    if not question_ids:
        raise HTTPException(
            status_code=400,
            detail="This recommendation has no linked question to measure. Add extra_question_ids.",
        )

    personas = [rec.persona] if rec.persona else []
    evidence = {
        "recommended_action": rec.recommended_action,
        "rationale": rec.rationale,
        "content_type": rec.content_type,
        "competitive_position": rec.competitive_position,
        "outperforming_competitor": rec.outperforming_competitor,
        "competitor_domain": rec.competitor_domain,
        "missing_citations": _jl(rec.missing_citations),
        "search_volume": rec.search_volume,
        "domain_authority": rec.domain_authority,
        "impact_score": rec.impact_score,
        "llm_name": rec.llm_name,
    }
    # Freeze "where to publish / earn a citation" guidance alongside the "what" so the
    # Activation & Impact page carries the same placement suggestion the recommendation showed.
    try:
        evidence["placement"] = await citations.placement_guidance(
            db, persona=rec.persona, therapeutic_area=rec.therapeutic_area, brand=rec.brand_focus
        )
    except Exception as e:  # noqa: BLE001 — placement is additive context, never blocks create
        logger.warning("Placement guidance skipped for rec %s: %s", rec.rec_id, e)

    default_title = (rec.recommended_action or f"{rec.content_type} for {rec.brand_focus}")
    interv = Intervention(
        id=str(uuid.uuid4()),
        recommendation_id=rec.rec_id,
        source_type=SOURCE_GEO_RECOMMENDATION,
        source_id=rec.source_response_id,
        evidence_snapshot_json=json.dumps(evidence, default=str),
        therapeutic_area=rec.therapeutic_area,
        indication=rec.indication,
        brand_focus=rec.brand_focus,
        title=(data.title or default_title)[:500],
        description=data.description or rec.recommended_action,
        priority=data.priority,
        owner_name=data.owner_name,
        reviewer_name=data.reviewer_name,
        review_required=bool(data.review_required),
        review_status="PENDING" if data.review_required else None,
        status="PROPOSED",
        monitoring_mode="BRAND",
        target_question_ids_json=json.dumps(question_ids),
        target_personas_json=json.dumps(personas),
        target_models_json=json.dumps(data.target_models) if data.target_models else None,
        target_metrics_json=json.dumps(list(measurement.METRIC_DEFS.keys())),
        primary_metric=data.primary_metric or DEFAULT_PRIMARY_METRIC,
        measurement_wait_days=data.measurement_wait_days if data.measurement_wait_days is not None else 14,
        repetitions_per_question=data.repetitions_per_question or 3,
        measurement_status="PLANNED",
        due_date=data.due_date,
    )
    db.add(interv)
    await db.flush()

    # Link back: mark the source recommendation ACTIONED (shared triage state).
    await _mark_recommendation_actioned(db, rec.rec_id, data.owner_name)

    # Discovery baseline from existing history (free; explains "why"). Best-effort.
    try:
        snap = await measurement.build_discovery_snapshot(
            db, intervention_id=interv.id, question_ids=question_ids,
            personas=personas or None, models=data.target_models, commit=False,
        )
        interv.discovery_baseline_snapshot_id = snap.id
    except Exception as e:  # noqa: BLE001 — discovery baseline is context, never blocks create
        logger.warning("Discovery baseline skipped for %s: %s", interv.id, e)

    await _record_event(
        db, interv, "CREATED", new_status="PROPOSED", actor=data.owner_name,
        metadata={"recommendation_id": rec.rec_id, "question_ids": question_ids},
    )
    await db.commit()
    return await _detail(db, interv)


# --------------------------------------------------------------- update / transition
async def update_intervention(db: AsyncSession, intervention_id: str, data: InterventionUpdate) -> dict:
    interv = await db.get(Intervention, intervention_id)
    if interv is None:
        raise HTTPException(status_code=404, detail="Intervention not found")

    cohort_locked = interv.status not in _PRE_PUBLISH_STATUSES
    cohort_fields = {
        "target_question_ids", "target_models", "target_metrics",
        "primary_metric", "measurement_wait_days", "repetitions_per_question",
    }
    payload = data.model_dump(exclude_unset=True)
    if cohort_locked and (cohort_fields & set(payload)):
        raise HTTPException(
            status_code=409,
            detail="Cohort/measurement fields are frozen once the intervention is published.",
        )

    # Ownership + descriptive fields.
    for field in ("title", "description", "owner_name", "reviewer_name",
                  "review_status", "priority", "due_date"):
        if field in payload:
            setattr(interv, field, payload[field])
    if "review_required" in payload:
        interv.review_required = bool(payload["review_required"])
    # Cohort/measurement fields (pre-publish only).
    if "target_question_ids" in payload:
        interv.target_question_ids_json = json.dumps(payload["target_question_ids"] or [])
    if "target_models" in payload:
        interv.target_models_json = (
            json.dumps(payload["target_models"]) if payload["target_models"] else None
        )
    if "target_metrics" in payload:
        interv.target_metrics_json = json.dumps(payload["target_metrics"] or [])
    if "primary_metric" in payload and payload["primary_metric"]:
        interv.primary_metric = payload["primary_metric"]
    if "measurement_wait_days" in payload and payload["measurement_wait_days"] is not None:
        interv.measurement_wait_days = payload["measurement_wait_days"]
    if "repetitions_per_question" in payload and payload["repetitions_per_question"]:
        interv.repetitions_per_question = payload["repetitions_per_question"]

    await _record_event(db, interv, "UPDATED", metadata={"fields": sorted(payload.keys())})
    await db.commit()
    return await _detail(db, interv)


async def transition(db: AsyncSession, intervention_id: str, data: InterventionTransition) -> dict:
    interv = await db.get(Intervention, intervention_id)
    if interv is None:
        raise HTTPException(status_code=404, detail="Intervention not found")

    current = interv.status
    to = data.to_status
    if to == current:
        return await _detail(db, interv)
    allowed = _ALLOWED_TRANSITIONS.get(current, set())
    if to not in allowed:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot move an intervention from {current} to {to}. "
                   f"Allowed: {sorted(allowed) or 'none (terminal)'}.",
        )
    interv.status = to
    await _record_event(
        db, interv, "STATUS_CHANGED", previous_status=current, new_status=to,
        actor=data.actor_name, notes=data.notes,
    )
    await db.commit()
    return await _detail(db, interv)


# --------------------------------------------------------------- publish + measure
async def _launch_measurement_runs(
    db: AsyncSession, *, question_ids: list[str], monitoring_mode: str, reps: int
) -> list[str]:
    """Create + background-launch ``reps`` ADHOC runs over exactly ``question_ids``.

    Each run re-asks the frozen cohort across all enabled targets and auto-scores. N runs =
    N repeated samples per (question, target) — the ``responses`` UNIQUE(run,question,llm)
    constraint means repetition must come from separate runs, not one run.
    """
    from app.schemas import RunCreate  # local import avoids a heavy import at module load

    run_ids: list[str] = []
    for _ in range(max(1, reps)):
        run_data = RunCreate(
            trigger="ADHOC", monitoring_mode=monitoring_mode or "BRAND",
            question_ids=question_ids,
        )
        run = await run_service.create_run(db, run_data)
        run_ids.append(run.run_id)
        _launch(run_service.run_in_background(run.run_id, run_data))
    return run_ids


async def publish(db: AsyncSession, intervention_id: str, data: InterventionPublish) -> dict:
    interv = await db.get(Intervention, intervention_id)
    if interv is None:
        raise HTTPException(status_code=404, detail="Intervention not found")
    if interv.status not in _PRE_PUBLISH_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Only an unpublished intervention can be published (current: {interv.status}).",
        )
    question_ids = _jl(interv.target_question_ids_json)
    if not question_ids:
        raise HTTPException(status_code=400, detail="No target questions to measure.")

    prev = interv.status
    pub_date = data.publication_date or utcnow()
    interv.publication_url = data.publication_url
    interv.publication_date = pub_date
    interv.status = "PUBLISHED"
    interv.measurement_status = "BASELINE_RUNNING"
    interv.post_due_at = pub_date + timedelta(days=interv.measurement_wait_days or 0)

    # Launch the official pre-publication baseline (same method as the post measurement).
    run_ids = await _launch_measurement_runs(
        db, question_ids=question_ids, monitoring_mode=interv.monitoring_mode,
        reps=interv.repetitions_per_question,
    )
    snap = measurement.create_pending_snapshot(
        intervention_id=interv.id, snapshot_type="OFFICIAL_BASELINE",
        run_ids=run_ids, question_ids=question_ids,
    )
    db.add(snap)
    interv.official_baseline_snapshot_id = snap.id

    await _record_event(
        db, interv, "PUBLISHED", previous_status=prev, new_status="PUBLISHED",
        actor=data.actor_name,
        metadata={"publication_url": data.publication_url,
                  "baseline_runs": run_ids, "post_due_at": _iso(interv.post_due_at)},
    )
    await db.commit()
    return await _detail(db, interv)


async def measure_now(db: AsyncSession, intervention_id: str) -> dict:
    """Force the measurement state machine to advance one step (dev/demo; the sweep does this
    automatically, ignoring the wait period)."""
    from app.activation import sweep  # local import: sweep imports this module at top level

    interv = await db.get(Intervention, intervention_id)
    if interv is None:
        raise HTTPException(status_code=404, detail="Intervention not found")
    action = await sweep.advance_intervention(db, interv, force=True)
    detail = await _detail(db, interv)
    detail["measure_action"] = action
    return detail
