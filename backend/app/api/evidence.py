"""Evidence store read API (X2).

The entry point the evidence programme was missing. ``/comparisons/resolve`` and
``/evidence-review/networks/{id}`` both take a ``network_id``, and until this router there
was no way to discover one — networks are assembled by ``scripts/ingest_evidence.py``, so
every existing route was reachable only by someone who already knew the id.

**Read-only by design.** Every lifecycle transition stays on ``/evidence-review``. A GET
surface that could also verify a study or ratify a network would put a governance decision
one accidental click from a browse action.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.services import evidence_read_service as svc

router = APIRouter(prefix="/evidence", tags=["evidence"])


@router.get("/overview")
async def overview(db: AsyncSession = Depends(get_db)):
    """Headline counts, led by canonical endpoint coverage rather than raw ingest volume."""
    return await svc.overview(db)


@router.get("/networks")
async def list_networks(
    indication: str | None = None,
    ratification_status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Every assembled network — the list ``/comparisons`` needs to be usable."""
    return await svc.list_networks(
        db,
        indication=indication,
        ratification_status=ratification_status,
        limit=limit,
    )


@router.get("/networks/{network_id}")
async def get_network(network_id: str, db: AsyncSession = Depends(get_db)):
    """One network, with both topologies reported side by side.

    ``protocol_scope`` is derived on every request and never stored: a window is one
    protocol's judgement and can be re-approved without re-harvesting, so a cached scope
    would go stale in silence.
    """
    try:
        return await svc.get_network(db, network_id)
    except svc.EvidenceNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/studies")
async def list_studies(
    indication: str | None = None,
    verification_status: str | None = None,
    treatment: str | None = Query(
        default=None, description='Studies with an arm on this treatment, e.g. "Rinvoq".'
    ),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Ingested studies with arm, outcome and canonical-endpoint counts."""
    return await svc.list_studies(
        db,
        indication=indication,
        verification_status=verification_status,
        treatment=treatment,
        limit=limit,
    )


@router.get("/studies/{study_id}")
async def get_study(study_id: str, db: AsyncSession = Depends(get_db)):
    """One study with its arms and every outcome row, mismatch flags included."""
    try:
        return await svc.get_study(db, study_id)
    except svc.EvidenceNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/drug-facts")
async def list_drug_facts(
    brand: str | None = None,
    verification_status: str | None = None,
    current_only: bool = True,
    limit: int = Query(default=200, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Label-derived drug facts. Independent of the NMA stack, and valuable without it."""
    return await svc.list_drug_facts(
        db,
        brand=brand,
        verification_status=verification_status,
        current_only=current_only,
        limit=limit,
    )


@router.get("/drug-facts/{brand}")
async def get_drug_fact(brand: str, db: AsyncSession = Depends(get_db)):
    """The current label version for one brand, plus its superseded history."""
    try:
        return await svc.get_drug_fact(db, brand)
    except svc.EvidenceNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
