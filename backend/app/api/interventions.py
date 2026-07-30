"""Activation & Impact API (thin v1).

The operational loop on top of GEO recommendations: create an owned intervention, track it
through publication, and measure the before/after change in AI answers. Every mutation is
recorded on the intervention's immutable timeline and mirrored to the audit log.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.schemas import (
    InterventionCreate,
    InterventionPublish,
    InterventionTransition,
    InterventionUpdate,
)
from app.services import intervention_service as svc

router = APIRouter(prefix="/interventions", tags=["interventions"])


@router.post("/from-recommendation/{rec_id}", status_code=201)
async def create_from_recommendation(
    rec_id: str, data: InterventionCreate, db: AsyncSession = Depends(get_db)
):
    """Create an intervention from a GEO recommendation (seeds cohort + discovery baseline)."""
    return await svc.create_from_recommendation(db, rec_id, data)


@router.get("")
async def list_interventions(
    status: str | None = Query(None), db: AsyncSession = Depends(get_db)
):
    """All interventions (newest-updated first), optionally filtered by workflow status."""
    return await svc.list_interventions(db, status=status)


@router.get("/{intervention_id}")
async def get_intervention(intervention_id: str, db: AsyncSession = Depends(get_db)):
    """Full detail: fields, cohort, snapshots, latest result, metric definitions."""
    return await svc.get_intervention(db, intervention_id)


@router.patch("/{intervention_id}")
async def update_intervention(
    intervention_id: str, data: InterventionUpdate, db: AsyncSession = Depends(get_db)
):
    """Patch ownership/detail fields (cohort fields are frozen after publish)."""
    return await svc.update_intervention(db, intervention_id, data)


@router.post("/{intervention_id}/transition")
async def transition(
    intervention_id: str, data: InterventionTransition, db: AsyncSession = Depends(get_db)
):
    """Manual workflow move (PROPOSED/IN_PROGRESS/DEFERRED/CANCELLED)."""
    return await svc.transition(db, intervention_id, data)


@router.post("/{intervention_id}/publish")
async def publish(
    intervention_id: str, data: InterventionPublish, db: AsyncSession = Depends(get_db)
):
    """Record publication + launch the official pre-publication baseline runs."""
    return await svc.publish(db, intervention_id, data)


@router.post("/{intervention_id}/measure")
async def measure_now(intervention_id: str, db: AsyncSession = Depends(get_db)):
    """Force the measurement state machine forward one step (demo/dev; the sweep is automatic)."""
    return await svc.measure_now(db, intervention_id)


@router.get("/{intervention_id}/result")
async def get_result(intervention_id: str, db: AsyncSession = Depends(get_db)):
    """The before/after result + baseline/post snapshots for this intervention."""
    return await svc.get_result(db, intervention_id)


@router.get("/{intervention_id}/timeline")
async def get_timeline(intervention_id: str, db: AsyncSession = Depends(get_db)):
    """The immutable event timeline for this intervention."""
    return await svc.get_timeline(db, intervention_id)
