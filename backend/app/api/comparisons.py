"""Comparison resolution API (Phase 6).

``GET /comparisons/resolve`` walks the evidence hierarchy and **always** returns a named
status — an unanswerable comparison is a 200 carrying a structured gap, not a 4xx. A gap is
a product output: *"Rinvoq versus Tremfya is not estimable in Crohn's because no shared
comparator links them"* is exactly what a reviewer needs, and returning an error code would
push callers into inventing their own explanation.

Two things every response makes explicit:

* **``is_releasable``** — the single predicate downstream consumers must obey. An
  exploratory result is a success and is *not* releasable.
* **``considered``** — every level tried and why it was rejected, so the answer explains
  why it sits where it does rather than only where it stopped.
"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.models.nma_result import EXECUTION_MODES, EXPLORATORY
from app.services import comparison_service as svc

router = APIRouter(prefix="/comparisons", tags=["comparisons"])


@router.get("/resolve")
async def resolve(
    network_id: str,
    treatment_a: str,
    treatment_b: str,
    execution_mode: str = Query(
        default=EXPLORATORY,
        description="GOVERNED asks the governance gate for permission. Refusal downgrades "
                    "to EXPLORATORY with the blocking status recorded — it is not an error.",
    ),
    requested_dose: str | None = None,
    max_published_age_years: int | None = Query(
        default=None,
        description="Recency gate for Level 2. Omit to use the suitability default.",
    ),
    persist: bool = Query(
        default=False,
        description="Store a computed result as an NMAResult row with full provenance.",
    ),
    as_of: date | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Resolve one comparison. A gap is a 200 with a named status, never a 4xx."""
    if execution_mode not in EXECUTION_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"execution_mode must be one of {', '.join(EXECUTION_MODES)}",
        )
    try:
        return await svc.resolve_comparison(
            db,
            network_id=network_id,
            treatment_a=treatment_a,
            treatment_b=treatment_b,
            execution_mode=execution_mode,
            requested_dose=requested_dose,
            max_published_age_years=max_published_age_years,
            persist=persist,
            as_of=as_of,
        )
    except svc.ComparisonError as e:
        # The scope itself is incoherent (no such network). That is a client error, unlike
        # a comparison that is simply not estimable.
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/matrix")
async def matrix(
    network_id: str,
    execution_mode: str = EXPLORATORY,
    db: AsyncSession = Depends(get_db),
):
    """Every pair in the network, resolved — which comparisons it can actually support."""
    try:
        return await svc.resolve_all_pairs(
            db, network_id=network_id, execution_mode=execution_mode
        )
    except svc.ComparisonError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/evidence")
async def evidence(
    network_id: str,
    treatment_a: str,
    treatment_b: str,
    db: AsyncSession = Depends(get_db),
):
    """What the resolver would see, and what scoping excluded.

    Exposed on its own because "why was my trial not used?" is the most common question a
    curator asks, and answering it should not require running a computation.
    """
    try:
        network = await svc._network(db, network_id)
    except svc.ComparisonError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    request = svc.ComparisonRequest(
        indication=network.indication,
        treatment_a=treatment_a,
        treatment_b=treatment_b,
        canonical_outcome_id=network.canonical_outcome_id,
        population_stratum=network.population_stratum,
        treatment_phase=network.treatment_phase,
        protocol_id=network.protocol_id,
    )
    gathered, report = await svc.gather_evidence(db, network, request)
    graph = gathered.topology()

    return {
        "network_id": network_id,
        "treatment": request.nodes[0],
        "comparator": request.nodes[1],
        "topology": graph.summary(),
        "shared_comparators": list(graph.shared_comparators(*request.nodes)),
        "has_direct_evidence": graph.has_direct_evidence(*request.nodes),
        "contrast_count": len(gathered.contrasts),
        "administration_routes": dict(gathered.administration_routes),
        "unsuitable_direct": [
            {"study_id": s, "reason": r} for s, r in gathered.unsuitable_direct
        ],
        "insufficient_data": [
            {"study_id": s, "reason": r} for s, r in gathered.insufficient_data
        ],
        "ambiguous_arms": [
            {"study_id": s, "reason": r} for s, r in gathered.ambiguous_arms
        ],
        "scoping": {k: v for k, v in report.items() if k != "sidecar_studies"},
    }
