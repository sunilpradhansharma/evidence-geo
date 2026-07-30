"""Competitor discovery API (Phase 5, Tier A).

Six routes under ``/competitor-discovery``. The sweep and the queue are separate calls because
they are separate acts: one reads evidence, the other records human judgement.

**No route writes ``brands.yaml``.** ``GET /config-proposal`` renders the fragment a human
commits by hand, and ``POST /config-applied`` records that they did. Accepting a candidate is
a decision about a molecule; committing the config is a change to the taxonomy; the two are
tracked separately so the queue cannot claim a change nobody made.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.evidence import discovery
from app.models.competitor_candidate import REVIEW_STATES
from app.models.database import get_db
from app.services import competitor_discovery_service as svc

router = APIRouter(prefix="/competitor-discovery", tags=["competitor-discovery"])


class ReviewIn(BaseModel):
    decision: str = Field(description=f"One of {', '.join(REVIEW_STATES)}")
    reviewer: str = Field(min_length=1, description="Recorded, NOT authenticated.")
    note: str | None = None


class ConfigAppliedIn(BaseModel):
    candidate_ids: list[str] = Field(min_length=1)
    applied_by: str = Field(min_length=1)


@router.get("/reasons")
async def reasons():
    """The Tier A reason vocabulary and its weights, served from one place.

    Exposed so a queue UI does not hardcode an enum or invent its own labels, and so the
    ranking is inspectable: ``discovery_confidence`` is the sum of these weights and nothing
    else.
    """
    return {
        "reasons": [
            {
                "code": code,
                "label": discovery.REASON_LABELS[code],
                "weight": discovery.REASON_WEIGHTS[code],
            }
            for code in discovery.DISCOVERY_REASONS
        ],
        "review_states": list(REVIEW_STATES),
        "newly_active_days": discovery.NEWLY_ACTIVE_DAYS,
        "tier_b2_out_of_scope": (
            "Class relationships are never inferred. An uncurated molecule carries no class "
            "until a human characterises it."
        ),
    }


@router.post("/sweep")
async def sweep(
    indication: str | None = None,
    commit: bool = Query(
        default=True,
        description="False reports what it would store without persisting anything.",
    ),
    db: AsyncSession = Depends(get_db),
):
    """Run a Tier A discovery sweep. Decided candidates are left untouched."""
    try:
        return await svc.discover(db, indication=indication, commit=commit)
    except svc.DiscoveryError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/candidates")
async def candidates(
    indication: str | None = None,
    review_status: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """The review queue, strongest signal first."""
    if review_status and review_status not in REVIEW_STATES:
        raise HTTPException(
            status_code=400,
            detail=f"review_status must be one of {', '.join(REVIEW_STATES)}",
        )
    return await svc.list_candidates(
        db, indication=indication, review_status=review_status, limit=limit
    )


@router.post("/candidates/{candidate_id}/review")
async def review(
    candidate_id: str, body: ReviewIn, db: AsyncSession = Depends(get_db)
):
    """Record a decision. Accepting does not add the drug to any competitor list."""
    try:
        return await svc.review_candidate(
            db,
            candidate_id,
            decision=body.decision,
            reviewer=body.reviewer,
            note=body.note,
        )
    except svc.DiscoveryError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/config-proposal")
async def config_proposal(
    indication: str | None = None, db: AsyncSession = Depends(get_db)
):
    """The YAML fragment for accepted candidates awaiting a human commit."""
    return await svc.config_proposal(db, indication=indication)


@router.post("/config-applied")
async def config_applied(body: ConfigAppliedIn, db: AsyncSession = Depends(get_db)):
    """Record that the proposed config has been committed for these candidates."""
    try:
        return await svc.mark_config_applied(
            db, body.candidate_ids, applied_by=body.applied_by
        )
    except svc.DiscoveryError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/class-map")
async def class_map(indication: str, db: AsyncSession = Depends(get_db)):
    """Cross-class view over this indication's trial arms, from the curated table only.

    ``uncharacterised`` is reported alongside the groups because a class map that dropped
    every uncurated molecule would look complete while hiding most of the network.
    """
    return await svc.class_map(db, indication=indication)
