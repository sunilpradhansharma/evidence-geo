"""Question Variations API — generate, review, run, and roll up phrasing-robustness groups.

Compliance gate (SE-001/SE-002): Claude-drafted paraphrases land as DRAFT rows and are NEVER
sent to a monitored model until a human approves them. Approval promotes a draft to an
APPROVED ``Question`` in the base question's variation group, so the existing run -> score ->
consensus pipeline runs them unchanged. A group run is scoped to explicit question_ids
(base + approved variations only), preserving the review gate.
"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.schemas import (
    VariationEdit,
    VariationExpandRequest,
    VariationGenerateRequest,
    VariationGroupRunRequest,
    VariationReview,
)
from app.services import run_service as run_svc
from app.services import variation_service as svc

router = APIRouter(prefix="/variations", tags=["variations"])

# Service error code -> HTTP status. The service returns {"error": <code>, ...} for expected
# business failures so the API layer owns the status mapping in one place.
_ERROR_STATUS = {
    "not_found": 404,
    "base_not_found": 409,
    "not_draft": 409,
    "already_approved": 409,
    "pii_detected": 422,
}


def _raise_for_error(result: dict | None) -> dict:
    """404 on a missing row (None); map a service ``{"error": ...}`` dict to its HTTP status."""
    if result is None:
        raise HTTPException(404, "Variation not found")
    if isinstance(result, dict) and result.get("error"):
        code = result["error"]
        detail = {"error": code, **{k: v for k, v in result.items() if k != "error"}}
        raise HTTPException(_ERROR_STATUS.get(code, 400), detail)
    return result


# ---------- listing ----------
@router.get("/groups")
async def list_groups(db: AsyncSession = Depends(get_db)):
    """All variation groups (base questions that have generated drafts) with status counts."""
    return await svc.list_groups(db)


@router.get("/groups/{group_id}")
async def get_group(group_id: str, db: AsyncSession = Depends(get_db)):
    """A group's base question, staged drafts, and approved-variation counts."""
    return await svc.list_group(db, group_id)


@router.get("/groups/{group_id}/results")
async def group_results(
    group_id: str,
    run_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Variation x model response matrix plus a group-level divergence summary. Uses the
    latest covering run unless ``run_id`` is pinned."""
    return await svc.group_results(db, group_id, run_id=run_id)


# ---------- 1. generate + stage ----------
@router.post("/generate/{row_id}", status_code=201)
async def generate(
    row_id: int,
    data: VariationGenerateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Ask Claude for N intent-preserving paraphrases of a base question (by table row id).

    Drafts are staged for review — nothing runs against a monitored model here.
    """
    try:
        result = await svc.generate_for_question(
            db, row_id, n=data.n, reviewer_name=data.reviewer_name
        )
    except RuntimeError as e:  # generation model unavailable / returned nothing usable
        raise HTTPException(502, f"Variation generation failed: {e}") from e
    return _raise_for_error(result)


# ---------- 2. review: edit / approve / reject ----------
@router.patch("/{var_id}")
async def edit_variation(var_id: int, data: VariationEdit, db: AsyncSession = Depends(get_db)):
    """Reviewer edits a DRAFT variation's wording (re-runs the PII lint + dedupe hash)."""
    return _raise_for_error(await svc.edit_variation(db, var_id, data.variation_text))


@router.post("/{var_id}/approve")
async def approve_variation(var_id: int, data: VariationReview, db: AsyncSession = Depends(get_db)):
    """Approve a DRAFT — promotes it to an APPROVED Question in the base question's group.

    Blocked (422) if the PII lint flags the text, mirroring the manual-question gate.
    """
    return _raise_for_error(
        await svc.approve_variation(db, var_id, reviewer_name=data.reviewer_name, note=data.note)
    )


@router.post("/{var_id}/reject")
async def reject_variation(var_id: int, data: VariationReview, db: AsyncSession = Depends(get_db)):
    """Reject a DRAFT (kept for audit, never runs)."""
    return _raise_for_error(
        await svc.reject_variation(db, var_id, reviewer_name=data.reviewer_name, note=data.note)
    )


# ---------- 3. group run ----------
@router.post("/expand")
async def expand(data: VariationExpandRequest, db: AsyncSession = Depends(get_db)):
    """Preview what a bank selection runs as: each question plus its APPROVED variations.

    POST only because the selection is a list in a body — this creates nothing, approves
    nothing, and starts no run. The caller shows these counts for confirmation and then runs
    the returned ``question_ids`` verbatim via ``POST /runs``, so the size shown before the
    click is the size charged after it. Pending and rejected drafts are reported per question
    but never included: approval stays the only door into a run.
    """
    return await svc.expand_with_variations(db, data.question_ids)


@router.post("/groups/{group_id}/run", status_code=202)
async def run_group(
    group_id: str,
    data: VariationGroupRunRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Run the base question + its approved variations together so their model answers can be
    compared. Returns 400 when the group has nothing runnable (no approved variations)."""
    run_create, ids = await svc.build_group_run(
        db, group_id, include_base=data.include_base, dry_run=data.dry_run
    )
    if run_create is None:
        raise HTTPException(400, "Nothing runnable in this group (no base or approved variations).")
    run = await run_svc.create_run(db, run_create)
    background_tasks.add_task(run_svc.run_in_background, run.run_id, run_create)
    return {"run_id": run.run_id, "status": run.status, "question_ids": ids, "count": len(ids)}
