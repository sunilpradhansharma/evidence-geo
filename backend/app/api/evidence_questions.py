"""Evidence-driven question generation API (Phase 7).

Three routes and one refusal. Generation stages into the shared review queue; it never
creates a ``Question`` and never approves one, so there is no route here that can put a
monitored question into the corpus. Promotion stays on ``/harvest/items/{id}/promote``,
which already owns the PII, injection and adverse-event guards — a second promotion path
would be a second place for those to be forgotten.

``POST /generate`` is **dry-run by default**, matching ``scripts/ingest_evidence.py`` and
``POST /competitor-discovery/sweep``: a call that writes should be the one you asked for.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.evidence import question_generation as qg
from app.models.database import get_db
from app.models.question import Question
from app.services import comparison_service, evidence_question_service as svc
from app.services import evidence_synthesis_service as synthesis_service

router = APIRouter(prefix="/evidence-questions", tags=["evidence-questions"])


@router.get("/vocabulary")
async def vocabulary():
    """The closed vocabularies a reviewer UI must render. Read from the module, not copied.

    A second list of role names in the frontend is how a role gets rendered that the
    service will not accept.
    """
    return {
        "categories": list(qg.CATEGORIES),
        "relationship_roles": list(qg.RELATIONSHIP_ROLES),
        "evidence_priorities": list(qg.EVIDENCE_PRIORITIES),
        "evidence_types": list(qg.EVIDENCE_TYPES),
        "expected_evidence_types": list(qg.EXPECTED_EVIDENCE_TYPES),
        "gap_attributions": [
            qg.ATTRIBUTION_EVIDENCE, qg.ATTRIBUTION_CURATION, qg.ATTRIBUTION_PROTOCOL
        ],
    }


@router.post("/generate")
async def generate(
    network_id: str,
    commit: bool = Query(
        default=False,
        description="Dry run by default: reports what would be staged and writes nothing.",
    ),
    db: AsyncSession = Depends(get_db),
):
    """Generate every question this network's evidence supports, plus every refusal.

    Refusals are part of the answer. ``gaps_attributable_to_curation`` in particular names
    comparisons that look like evidence gaps and are actually our own verification backlog
    — a number that belongs in front of a curator, not buried as a shorter question list.
    """
    try:
        return await svc.generate_for_network(db, network_id=network_id, commit=commit)
    except comparison_service.ComparisonError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/synthesis")
async def synthesis(
    indication: str,
    network_id: str | None = None,
    change_window_days: int = Query(default=90, ge=1, le=730),
    db: AsyncSession = Depends(get_db),
):
    """Phase 9 synthesis for one indication: what the evidence shows, and what it implies.

    Read **limitations first**. On a corpus where nothing is verified and no network is
    ratified, the limitations are the finding — a page that led with six estimates and
    footnoted the governance state would read as settled evidence.
    """
    return await synthesis_service.synthesise(
        db, indication=indication, network_id=network_id,
        change_window_days=change_window_days,
    )


@router.get("/{question_id}/evidence")
async def evidence_for_question(question_id: str, db: AsyncSession = Depends(get_db)):
    """Every association behind a question, with its evidence's **live** review state.

    ``verification_state_at_link`` and ``verification_state_now`` are both reported. They
    diverge when evidence was verified after the association was made — or rejected after
    it — and only showing the snapshot would let an approved question look backed by
    evidence that has since been withdrawn.
    """
    exists = (await db.execute(
        select(Question.id).where(Question.question_id == question_id).limit(1)
    )).first()
    if exists is None:
        raise HTTPException(status_code=404, detail=f"question {question_id!r} does not exist")
    links = await svc.associations(db, question_id)
    return {
        "question_id": question_id,
        "association_count": len(links),
        "verified_count": sum(1 for link in links if link["is_verified"]),
        "associations": links,
    }


@router.get("/{question_id}/approval-blockers")
async def approval_blockers(question_id: str, db: AsyncSession = Depends(get_db)):
    """Why this question may not be approved yet. ``[]`` means it may.

    A GET rather than a failed PATCH, because a reviewer deciding what to work on should
    not have to attempt an approval to find out it is refused.
    """
    question = (await db.execute(
        select(Question).where(
            Question.question_id == question_id,
            Question.deleted_at.is_(None),
            Question.superseded_by.is_(None),
        )
    )).scalars().first()
    if question is None:
        raise HTTPException(status_code=404, detail=f"question {question_id!r} does not exist")
    blockers = await svc.approval_blockers(db, question)
    return {
        "question_id": question_id,
        "generation_method": question.generation_method,
        "approval_status": question.approval_status,
        "may_approve": not blockers,
        "blockers": blockers,
    }
