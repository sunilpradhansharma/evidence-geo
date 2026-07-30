"""Response query service — projects latest score onto immutable response rows.

Implements FR-302 (full response fields) and FR-304 (immutable response + versioned
score) simultaneously by joining the latest scoring record at read time.
"""
import json

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.labels import HIDDEN_LLM_NAMES
from app.models.alert import Alert
from app.models.consensus import ConsensusRecord
from app.models.question import Question
from app.models.response import Response
from app.models.response_diff import ResponseDiff
from app.models.scoring import ScoringRecord


async def _latest_scores_map(db: AsyncSession, response_ids: list[str]) -> dict[str, ScoringRecord]:
    if not response_ids:
        return {}
    # max version per response
    subq = (
        select(
            ScoringRecord.response_id,
            func.max(ScoringRecord.score_version).label("maxv"),
        )
        .where(ScoringRecord.response_id.in_(response_ids))
        .group_by(ScoringRecord.response_id)
        .subquery()
    )
    stmt = select(ScoringRecord).join(
        subq,
        and_(
            ScoringRecord.response_id == subq.c.response_id,
            ScoringRecord.score_version == subq.c.maxv,
        ),
    )
    rows = (await db.execute(stmt)).scalars().all()
    return {r.response_id: r for r in rows}


def _serialize(r: Response, score: ScoringRecord | None) -> dict:
    return {
        "response_id": r.response_id,
        "run_id": r.run_id,
        "timestamp_utc": r.timestamp_utc,
        "llm_name": r.llm_name,
        "llm_model_version": r.llm_model_version,
        "persona": r.persona,
        "question_id": r.question_id,
        "question_text": r.question_text,
        "therapeutic_area": r.therapeutic_area,
        "indication": r.indication,
        "brand_focus": r.brand_focus,
        "monitoring_mode": r.monitoring_mode,
        "competitor_focus": json.loads(r.competitor_focus) if r.competitor_focus else None,
        "domain": r.domain,
        "intent_type": r.intent_type,
        "consensus_level": r.consensus_level,
        "response_text": r.response_text,
        "prompt_tokens": r.prompt_tokens,
        "response_tokens": r.response_tokens,
        "finish_reason": r.finish_reason,
        "status": r.status,
        "sources": json.loads(r.sources) if r.sources else [],  # real retrieval provenance (Type B)
        "grounding_supports": json.loads(r.grounding_supports) if r.grounding_supports else [],
        "search_queries": json.loads(r.search_queries) if r.search_queries else [],
        # projected derived fields (FR-302 nullable until scored)
        "sentiment_score": score.sentiment_score if score else None,
        "competitive_position": score.competitive_position if score else None,
        "scoring_rationale": score.scoring_rationale if score else None,
        "brand_mentions": json.loads(score.brand_mentions) if score and score.brand_mentions else [],
        "key_claims": json.loads(score.key_claims) if score and score.key_claims else [],
        "scored_by": score.scored_by if score else None,
        "alert_triggered": False,  # set below
    }


async def query_responses(
    db: AsyncSession,
    *,
    llm_name: str | None = None,
    persona: str | None = None,
    therapeutic_area: str | None = None,
    therapeutic_areas: list[str] | None = None,
    indication: str | None = None,
    brand_focus: str | None = None,
    competitor: str | None = None,
    domain: str | None = None,
    status: str | None = None,
    run_id: str | None = None,
    intent_type: str | None = None,
    consensus_level: str | None = None,
    sentiment_min: float | None = None,
    sentiment_max: float | None = None,
    alert_only: bool = False,
    analyst: bool = False,
    designations: list[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    stmt = select(Response)
    if llm_name:
        stmt = stmt.where(Response.llm_name == llm_name)
    if persona:
        stmt = stmt.where(Response.persona == persona)
    # Therapeutic area: multi-select (in_) takes precedence over the single-value form,
    # so callers can scope the list/export to several areas at once.
    if therapeutic_areas:
        stmt = stmt.where(Response.therapeutic_area.in_(therapeutic_areas))
    elif therapeutic_area:
        stmt = stmt.where(Response.therapeutic_area == therapeutic_area)
    if indication:
        stmt = stmt.where(Response.indication == indication)
    if brand_focus:
        stmt = stmt.where(Response.brand_focus == brand_focus)
    # Competitor is NOT brand_focus. That column holds the monitored AbbVie brand, so a
    # rival's name can never match it; who else an answer named lives in the scoring
    # record's mention payload. Resolved to ids BEFORE the count so `total` describes the
    # same rows as the page — the sentiment filters below are applied post-projection and
    # deliberately do not have that property.
    if competitor:
        from app.competitive import mentions as mentions_mod  # local import avoids a cycle

        named_ids = await mentions_mod.matching_response_ids(db, competitor)
        if not named_ids:
            return {"total": 0, "count": 0, "items": []}
        stmt = stmt.where(Response.response_id.in_(named_ids))
    if domain:
        stmt = stmt.where(Response.domain == domain)
    if status:
        stmt = stmt.where(Response.status == status)
    if run_id:
        stmt = stmt.where(Response.run_id == run_id)
    if intent_type:
        stmt = stmt.where(Response.intent_type == intent_type)
    if consensus_level:
        stmt = stmt.where(Response.consensus_level == consensus_level)
    # Hide de-listed targets (e.g. legacy open-evidence) from the response list.
    if HIDDEN_LLM_NAMES:
        stmt = stmt.where(Response.llm_name.notin_(HIDDEN_LLM_NAMES))
    designation_map: dict[str, str] = {}
    # Designation is a workshop-only concept, so either the Workshop Questions toggle
    # (analyst) or an explicit designation filter scopes to the curated set (Rhem.csv)
    # and tags each row with its Persona+indication designation (e.g. "Patient RA").
    want_designation = analyst or bool(designations)
    if want_designation:
        # Responses whose stable question_id is a workshop base question or one of its
        # variations. Resolved via the questions bank (local import avoids any import
        # cycle). The designation map's keys ARE that id set (base + variations), so it
        # doubles as the scope filter and the tag source for the CSV export. When explicit
        # `designations` are supplied, narrow the scope to just those labels. Empty -> no rows.
        from app.services import question_service

        designation_map = await question_service.analyst_designation_map(db)
        if not designation_map:
            return {"total": 0, "count": 0, "items": []}
        allowed_qids = list(designation_map.keys())
        if designations:
            wanted = set(designations)
            allowed_qids = [qid for qid, label in designation_map.items() if label in wanted]
            if not allowed_qids:
                return {"total": 0, "count": 0, "items": []}
        stmt = stmt.where(Response.question_id.in_(allowed_qids))

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar() or 0

    stmt = stmt.order_by(Response.timestamp_utc.desc()).limit(limit).offset(offset)
    responses = list((await db.execute(stmt)).scalars().all())

    ids = [r.response_id for r in responses]
    scores = await _latest_scores_map(db, ids)

    # alerts present per response
    alert_ids = set()
    if ids:
        arows = await db.execute(select(Alert.response_id).where(Alert.response_id.in_(ids)))
        alert_ids = {a for (a,) in arows.all()}

    items = []
    for r in responses:
        score = scores.get(r.response_id)
        # sentiment range filter (applied post-projection)
        if sentiment_min is not None and (score is None or (score.sentiment_score or 0) < sentiment_min):
            continue
        if sentiment_max is not None and (score is None or (score.sentiment_score or 0) > sentiment_max):
            continue
        if alert_only and r.response_id not in alert_ids:
            continue
        d = _serialize(r, score)
        d["alert_triggered"] = r.response_id in alert_ids
        if competitor:
            # The named agent's OWN sentiment in this answer, beside our brand's, so the
            # two are never confused for each other.
            from app.competitive import mentions as mentions_mod

            d["competitor"] = mentions_mod.canonical_agent(competitor)
            d["competitor_sentiment"] = mentions_mod.mention_sentiment(
                d["brand_mentions"], competitor
            )
        if want_designation:
            # Workshop-scoped: tag with the Persona+indication designation (Rhem.csv).
            d["designation"] = designation_map.get(r.question_id)
        items.append(d)

    return {"total": total, "count": len(items), "items": items}


async def get_response_detail(db: AsyncSession, response_id: str) -> dict | None:
    r = await db.get(Response, response_id)
    if r is None:
        return None
    scores = await _latest_scores_map(db, [response_id])
    d = _serialize(r, scores.get(response_id))

    arows = await db.execute(select(Alert).where(Alert.response_id == response_id))
    alerts = arows.scalars().all()
    d["alert_triggered"] = len(alerts) > 0
    d["alerts"] = [{"rule": a.rule_triggered, "detail": a.detail} for a in alerts]

    drow = await db.execute(
        select(ResponseDiff).where(ResponseDiff.current_response_id == response_id)
    )
    diff = drow.scalars().first()
    if diff:
        d["diff"] = {
            "similarity_ratio": diff.similarity_ratio,
            "material_change": diff.material_change,
            "diff_text": diff.diff_text,
            "previous_response_id": diff.previous_response_id,
        }
    else:
        d["diff"] = None

    # Consensus record for this question/run
    if r.run_id and r.question_id:
        crow = await db.execute(
            select(ConsensusRecord).where(
                ConsensusRecord.run_id == r.run_id,
                ConsensusRecord.question_id == r.question_id,
            )
        )
        consensus = crow.scalars().first()
        if consensus:
            d["consensus"] = {
                "consensus_level": consensus.consensus_level,
                "agreed_recommendation": consensus.agreed_recommendation,
                "divergence_points": json.loads(consensus.divergence_points) if consensus.divergence_points else [],
                "confidence": consensus.confidence,
                "final_answer": consensus.final_answer,
                "overall_sentiment": consensus.overall_sentiment,
                "overall_position": consensus.overall_position,
                "sentiment_min": consensus.sentiment_min,
                "sentiment_max": consensus.sentiment_max,
                "position_distribution": json.loads(consensus.position_distribution) if consensus.position_distribution else {},
                "models_scored": consensus.models_scored,
                "geo_fallback_used": consensus.geo_fallback_used,
                "geo_context": json.loads(consensus.geo_context) if consensus.geo_context else None,
            }
        else:
            d["consensus"] = None
    else:
        d["consensus"] = None

    # Variation lineage (forward): if this response's question is a variation, surface the
    # source question it was created from (resolved to current text). Single-item view only.
    from app.services.question_service import attach_variation_lineage  # local import avoids cycle
    d["is_variation"] = False
    d["variation_of"] = None
    d["variation_of_text"] = None
    d["generation_method"] = None
    if r.question_id:
        qrow = (await db.execute(
            select(Question)
            .where(Question.question_id == r.question_id, Question.deleted_at.is_(None))
            .order_by(Question.superseded_by.is_(None).desc(), Question.version.desc())
        )).scalars().first()
        if qrow is not None:
            await attach_variation_lineage(db, [qrow])
            d["is_variation"] = bool(qrow.is_variation)
            d["variation_of"] = qrow.variation_of
            d["variation_of_text"] = qrow.variation_of_text
            d["generation_method"] = qrow.generation_method

    return d


async def compare_question(db: AsyncSession, question_id: str, run_id: str | None = None) -> dict:
    """Side-by-side: latest response per LLM for a question (FR-606)."""
    stmt = select(Response).where(Response.question_id == question_id)
    if run_id:
        stmt = stmt.where(Response.run_id == run_id)
    if HIDDEN_LLM_NAMES:
        stmt = stmt.where(Response.llm_name.notin_(HIDDEN_LLM_NAMES))
    stmt = stmt.order_by(Response.timestamp_utc.desc())
    responses = list((await db.execute(stmt)).scalars().all())

    latest_by_llm: dict[str, Response] = {}
    for r in responses:
        if r.llm_name not in latest_by_llm:
            latest_by_llm[r.llm_name] = r

    ids = [r.response_id for r in latest_by_llm.values()]
    scores = await _latest_scores_map(db, ids)
    question_text = responses[0].question_text if responses else ""

    return {
        "question_id": question_id,
        "question_text": question_text,
        "answers": [_serialize(r, scores.get(r.response_id)) for r in latest_by_llm.values()],
    }
