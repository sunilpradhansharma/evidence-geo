"""Scoring API (FR-406..408)."""
import json
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import AsyncSessionLocal, get_db
from app.models.response import Response
from app.models.scoring import ScoringRecord
from app.schemas import ScoreOverride
from app.scoring.scorer import VALID_POSITIONS, score_response, score_unscored_sweep

router = APIRouter(prefix="/scores", tags=["scores"])


async def _rescore_task(prompt_version: str, run_id: str | None) -> None:
    async with AsyncSessionLocal() as db:
        stmt = select(Response).where(Response.status.in_(["SUCCESS", "TRUNCATED"]))
        if run_id:
            stmt = stmt.where(Response.run_id == run_id)
        responses = list((await db.execute(stmt)).scalars().all())
        for r in responses:
            await score_response(db, r, prompt_version=prompt_version, commit=True)


@router.post("/sweep")
async def sweep(db: AsyncSession = Depends(get_db)):
    """Score any unscored responses now (FR-406)."""
    return await score_unscored_sweep(db)


@router.post("/rescore", status_code=202)
async def rescore(
    background_tasks: BackgroundTasks,
    prompt_version: str = Query("v2"),
    run_id: str | None = None,
):
    """Re-score historical responses as NEW versioned records (FR-407)."""
    background_tasks.add_task(_rescore_task, prompt_version, run_id)
    return {"status": "rescoring_started", "prompt_version": prompt_version, "run_id": run_id}


@router.get("/response/{response_id}")
async def scores_for_response(response_id: str, db: AsyncSession = Depends(get_db)):
    stmt = (
        select(ScoringRecord)
        .where(ScoringRecord.response_id == response_id)
        .order_by(ScoringRecord.score_version.desc())
    )
    records = list((await db.execute(stmt)).scalars().all())
    return [
        {
            "score_id": s.score_id,
            "score_version": s.score_version,
            "prompt_version": s.prompt_version,
            "sentiment_score": s.sentiment_score,
            "competitive_position": s.competitive_position,
            "brand_mentions": json.loads(s.brand_mentions) if s.brand_mentions else [],
            "key_claims": json.loads(s.key_claims) if s.key_claims else [],
            "scoring_rationale": s.scoring_rationale,
            "scored_by": s.scored_by,
            "override_rationale": s.override_rationale,
            "created_at": s.created_at,
        }
        for s in records
    ]


@router.post("/{response_id}/override", status_code=201)
async def override_score(
    response_id: str, data: ScoreOverride, db: AsyncSession = Depends(get_db)
):
    """Human override (FR-408) — records a new HUMAN-scored version without deleting AI score."""
    if data.competitive_position not in VALID_POSITIONS:
        raise HTTPException(422, f"Invalid position. Allowed: {sorted(VALID_POSITIONS)}")

    existing = await db.execute(
        select(ScoringRecord).where(ScoringRecord.response_id == response_id)
    )
    versions = [s.score_version for s in existing.scalars().all()]
    if not versions:
        raise HTTPException(404, "No existing score for this response")

    record = ScoringRecord(
        score_id=str(uuid.uuid4()),
        response_id=response_id,
        score_version=max(versions) + 1,
        prompt_version="human-override",
        sentiment_score=data.sentiment_score,
        competitive_position=data.competitive_position,
        scoring_rationale=data.rationale,
        scored_by=f"HUMAN:{data.reviewer_name}",
        override_rationale=data.rationale,
    )
    db.add(record)
    await db.commit()
    return {"score_id": record.score_id, "score_version": record.score_version}
