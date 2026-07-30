"""Claim-level AI-vs-evidence evaluation API (Phase 8).

Evaluation is **triggered, never automatic**. It spends one model call per response on top
of scoring, so a scheduled full-bank run would double the post-run bill without anyone
asking. Keeping it behind an explicit POST also means a failed extraction can never fail a
monitoring run — the same isolation insights tagging and source-authority classification
already have.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.evidence import claims as cl
from app.models.database import get_db
from app.models.evaluation_claim import EvaluationClaim
from app.models.response import Response
from app.services import claim_evaluation_service as svc

router = APIRouter(prefix="/claim-evaluation", tags=["claim-evaluation"])


@router.get("/vocabulary")
async def vocabulary():
    """The closed vocabularies, the routing table and the dimension map.

    Served from the module rather than duplicated in the frontend. A second copy of the
    routing table in TypeScript is how a claim type gets rendered against an authority the
    grader would refuse.
    """
    return {
        "claim_types": list(cl.CLAIM_TYPES),
        "classifications": list(cl.CLASSIFICATIONS),
        "adverse_classifications": list(cl.ADVERSE_CLASSIFICATIONS),
        "dimensions": list(cl.DIMENSIONS),
        "certainty_levels": list(cl.CERTAINTY_LEVELS),
        "certainty_verdicts": list(cl.CERTAINTY_VERDICTS),
        "directions": list(cl.DIRECTIONS),
        "policy": {
            claim_type: {
                "authoritative_evidence": list(cl.authoritative_evidence_for(claim_type)),
                "description": cl.POLICY_DESCRIPTION[claim_type],
                "dimensions": list(cl.dimensions_for(claim_type)),
            }
            for claim_type in cl.CLAIM_TYPES
        },
    }


@router.post("/responses/{response_id}")
async def evaluate_response(
    response_id: str,
    commit: bool = Query(
        default=True,
        description="Set false to grade without persisting — useful for prompt iteration.",
    ),
    db: AsyncSession = Depends(get_db),
):
    """Decompose one response into claims and grade each against its own authority."""
    response = (await db.execute(
        select(Response).where(Response.response_id == response_id)
    )).scalars().first()
    if response is None:
        raise HTTPException(404, f"response {response_id!r} does not exist")
    return await svc.evaluate_response(db, response, commit=commit)


@router.post("/runs/{run_id}")
async def evaluate_run(
    run_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
):
    """Evaluate every successful response in a run. One model call per response."""
    return await svc.evaluate_run(db, run_id, limit=limit)


@router.get("/alignment")
async def alignment(
    run_id: str | None = None,
    llm_name: str | None = None,
    indication: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """The alignment dashboard: overall, by model, by claim type.

    ``coverage`` is reported beside every score and should be read first. A model scoring
    1.0 on three checkable claims out of forty is unmeasured, not aligned.
    """
    return await svc.alignment_report(
        db, run_id=run_id, llm_name=llm_name, indication=indication
    )


@router.get("/responses/{response_id}/claims")
async def claims_for_response(response_id: str, db: AsyncSession = Depends(get_db)):
    """Every stored claim for a response, with its verdict and the evidence behind it."""
    rows = (await db.execute(
        select(EvaluationClaim)
        .where(EvaluationClaim.response_id == response_id)
        .order_by(EvaluationClaim.claim_index, EvaluationClaim.claim_id)
    )).scalars().all()
    return {
        "response_id": response_id,
        "claim_count": len(rows),
        "claims": [
            {
                "claim_id": row.claim_id,
                "claim_text": row.claim_text,
                "claim_type": row.claim_type,
                "subject": row.subject,
                "comparator": row.comparator,
                "indication": row.indication,
                "outcome": row.outcome,
                "direction": row.direction,
                "polarity": row.polarity,
                "certainty": row.certainty,
                "magnitude": row.magnitude,
                "magnitude_unit": row.magnitude_unit,
                "cited_identifiers": svc._json_list(row.cited_identifiers),
                "expected_evidence_policy": svc._json_list(row.expected_evidence_policy),
                "classification": row.classification,
                "reason": row.reason,
                "dimensions": svc._json_list(row.dimensions),
                "certainty_verdict": row.certainty_verdict,
                "flags": svc._json_list(row.flags),
                "is_adverse": row.is_adverse,
                "extracted_by": row.extracted_by,
                "extraction_version": row.extraction_version,
            }
            for row in rows
        ],
    }
