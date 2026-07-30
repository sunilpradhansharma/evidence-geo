"""Analytics API for the dashboard (FR-602, FR-606).

Each endpoint queries Snowflake views first. If Snowflake is disabled or the
query fails for any reason, the endpoint transparently falls back to the local
SQLite database. The ``X-Data-Source`` response header indicates which path
served the request (``snowflake`` or ``sqlite``).
"""
import json

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.config.labels import HIDDEN_LLM_NAMES, PRELAUNCH_LABEL
from app.models.alert import Alert
from app.models.consensus import ConsensusRecord
from app.models.database import get_db
from app.models.response import Response
from app.models.scoring import ScoringRecord
from app.config.taxonomy import keys_for_area
from app.snowflake import analytics as sf_analytics
from app.snowflake.fallback import with_snowflake_fallback

router = APIRouter(prefix="/analytics", tags=["analytics"])


def _json_with_source(data: object, source: str) -> JSONResponse:
    """Wrap *data* in a JSONResponse with an ``X-Data-Source`` header."""
    return JSONResponse(content=data, headers={"X-Data-Source": source})


def _apply_ta_filters(
    stmt,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    disease: str | None = None,
    brand: str | None = None,
    monitoring_mode: str | None = None,
):
    """Apply optional TA/indication/disease/brand WHERE clauses to a SQLAlchemy statement.

    ``therapeutic_area`` may be a broad area display name (e.g. "Women's Health")
    or a stored key (e.g. "Endometriosis"). Broad areas are expanded to their
    child keys so an area selection matches every indication beneath it.
    """
    if therapeutic_area:
        child_keys = keys_for_area(therapeutic_area)
        if child_keys:
            stmt = stmt.where(Response.therapeutic_area.in_(child_keys))
        else:
            stmt = stmt.where(Response.therapeutic_area == therapeutic_area)
    if indication:
        stmt = stmt.where(Response.indication == indication)
    if disease:
        stmt = stmt.where(Response.disease == disease)
    if brand:
        stmt = stmt.where(Response.brand_focus == brand)
    # FR-108a: scope to one monitoring mode so brand-centric analytics never mix in
    # brand-less DISEASE_STATE (landscape) rows, and the landscape view sees only its own.
    if monitoring_mode:
        stmt = stmt.where(Response.monitoring_mode == monitoring_mode)
    return stmt


async def _latest_score_subquery():
    return (
        select(
            ScoringRecord.response_id,
            func.max(ScoringRecord.score_version).label("maxv"),
        )
        .group_by(ScoringRecord.response_id)
        .subquery()
    )


async def _latest_scores_join(
    db: AsyncSession,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    disease: str | None = None,
    brand: str | None = None,
    monitoring_mode: str | None = None,
):
    subq = await _latest_score_subquery()
    stmt = (
        select(Response, ScoringRecord)
        .join(ScoringRecord, ScoringRecord.response_id == Response.response_id)
        .join(
            subq,
            and_(
                ScoringRecord.response_id == subq.c.response_id,
                ScoringRecord.score_version == subq.c.maxv,
            ),
        )
    )
    stmt = _apply_ta_filters(stmt, therapeutic_area=therapeutic_area, indication=indication, disease=disease, brand=brand, monitoring_mode=monitoring_mode)
    if HIDDEN_LLM_NAMES:
        stmt = stmt.where(Response.llm_name.notin_(HIDDEN_LLM_NAMES))
    return (await db.execute(stmt)).all()


async def _sqlite_sentiment_distribution(
    db: AsyncSession,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    disease: str | None = None,
    brand: str | None = None,
    monitoring_mode: str | None = None,
) -> dict:
    rows = await _latest_scores_join(db, therapeutic_area=therapeutic_area, indication=indication, disease=disease, brand=brand, monitoring_mode=monitoring_mode)
    by_llm: dict[str, list[float]] = {}
    by_ta: dict[str, list[float]] = {}
    buckets = {"positive": 0, "neutral": 0, "negative": 0}
    for resp, score in rows:
        s = score.sentiment_score
        if s is None:
            continue
        by_llm.setdefault(resp.llm_name, []).append(s)
        by_ta.setdefault(resp.therapeutic_area, []).append(s)
        if s > 0.2:
            buckets["positive"] += 1
        elif s < -0.2:
            buckets["negative"] += 1
        else:
            buckets["neutral"] += 1

    def summarize(d):
        return [
            {"key": k, "avg_sentiment": round(sum(v) / len(v), 3), "count": len(v)}
            for k, v in d.items()
        ]

    return {
        "by_llm": summarize(by_llm),
        "by_therapeutic_area": summarize(by_ta),
        "buckets": buckets,
    }


@router.get("/sentiment-distribution")
async def sentiment_distribution(
    therapeutic_area: str | None = None,
    indication: str | None = None,
    disease: str | None = None,
    brand: str | None = None,
    monitoring_mode: str = "BRAND",
    db: AsyncSession = Depends(get_db),
):
    """(a) Sentiment by LLM and therapy (FR-602a). Defaults to BRAND mode so landscape
    (DISEASE_STATE) rows never leak into brand-focused sentiment."""
    data, source = await with_snowflake_fallback(
        sf_analytics.sentiment_distribution,
        _sqlite_sentiment_distribution, db,
        therapeutic_area=therapeutic_area, indication=indication, disease=disease, brand=brand,
        monitoring_mode=monitoring_mode,
    )
    return _json_with_source(data, source)


async def _sqlite_positioning(
    db: AsyncSession,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    disease: str | None = None,
    brand: str | None = None,
    monitoring_mode: str | None = None,
) -> dict:
    rows = await _latest_scores_join(db, therapeutic_area=therapeutic_area, indication=indication, disease=disease, brand=brand, monitoring_mode=monitoring_mode)
    result: dict[str, dict[str, int]] = {}
    for resp, score in rows:
        pos = score.competitive_position or "NOT_MENTIONED"
        result.setdefault(resp.llm_name, {})
        result[resp.llm_name][pos] = result[resp.llm_name].get(pos, 0) + 1
    return result


@router.get("/positioning")
async def positioning(
    therapeutic_area: str | None = None,
    indication: str | None = None,
    disease: str | None = None,
    brand: str | None = None,
    monitoring_mode: str = "BRAND",
    db: AsyncSession = Depends(get_db),
):
    """(b) Competitive positioning breakdown by LLM (FR-602b). BRAND-only by default so the
    disease-state LANDSCAPE marker never appears as a brand positioning bucket."""
    data, source = await with_snowflake_fallback(
        sf_analytics.positioning,
        _sqlite_positioning, db,
        therapeutic_area=therapeutic_area, indication=indication, disease=disease, brand=brand,
        monitoring_mode=monitoring_mode,
    )
    return _json_with_source(data, source)


async def _sqlite_volume(db: AsyncSession) -> dict:
    stmt = select(
        func.date(Response.timestamp_utc).label("day"),
        Response.status,
        func.count().label("n"),
    ).group_by("day", Response.status)
    rows = (await db.execute(stmt)).all()
    out: dict[str, dict[str, int]] = {}
    for day, status, n in rows:
        out.setdefault(str(day), {})
        out[str(day)][status] = n
    return out


@router.get("/volume")
async def volume(db: AsyncSession = Depends(get_db)):
    """(d) Response volume over time (FR-602d)."""
    data, source = await with_snowflake_fallback(
        sf_analytics.volume,
        _sqlite_volume, db,
    )
    return _json_with_source(data, source)


async def _sqlite_alerts_summary(db: AsyncSession) -> dict:
    total = (await db.execute(select(func.count()).select_from(Alert))).scalar() or 0
    by_rule = (
        await db.execute(select(Alert.rule_triggered, func.count()).group_by(Alert.rule_triggered))
    ).all()
    return {
        "total_alerts": total,
        "by_rule": {rule: n for rule, n in by_rule},
    }


@router.get("/alerts-summary")
async def alerts_summary(db: AsyncSession = Depends(get_db)):
    """(c) Alert count + breakdown (FR-602c)."""
    data, source = await with_snowflake_fallback(
        sf_analytics.alerts_summary,
        _sqlite_alerts_summary, db,
    )
    return _json_with_source(data, source)


async def _sqlite_consensus_summary(db: AsyncSession) -> dict:
    # By consensus level
    level_rows = (
        await db.execute(
            select(ConsensusRecord.consensus_level, func.count())
            .group_by(ConsensusRecord.consensus_level)
        )
    ).all()
    by_level = {level: n for level, n in level_rows}

    # By LLM (from responses)
    llm_stmt = (
        select(Response.llm_name, Response.consensus_level, func.count())
        .where(Response.consensus_level.isnot(None))
        .group_by(Response.llm_name, Response.consensus_level)
    )
    if HIDDEN_LLM_NAMES:
        llm_stmt = llm_stmt.where(Response.llm_name.notin_(HIDDEN_LLM_NAMES))
    llm_rows = (await db.execute(llm_stmt)).all()
    by_llm: dict[str, dict[str, int]] = {}
    for llm, level, n in llm_rows:
        by_llm.setdefault(llm, {})
        by_llm[llm][level] = n

    # GEO fallback usage
    geo_used = (
        await db.execute(
            select(func.count()).select_from(ConsensusRecord)
            .where(ConsensusRecord.geo_fallback_used.is_(True))
        )
    ).scalar() or 0

    return {
        "by_level": by_level,
        "by_llm": by_llm,
        "geo_fallback_count": geo_used,
        "total_evaluations": sum(by_level.values()),
    }


@router.get("/consensus-summary")
async def consensus_summary(db: AsyncSession = Depends(get_db)):
    """Consensus distribution by LLM and question."""
    data, source = await with_snowflake_fallback(
        sf_analytics.consensus_summary,
        _sqlite_consensus_summary, db,
    )
    return _json_with_source(data, source)


async def _sqlite_intent_distribution(db: AsyncSession) -> dict:
    rows = (
        await db.execute(
            select(Response.intent_type, func.count())
            .where(Response.intent_type.isnot(None))
            .group_by(Response.intent_type)
        )
    ).all()
    by_intent = {intent: n for intent, n in rows}

    # By persona
    persona_rows = (
        await db.execute(
            select(Response.persona, Response.intent_type, func.count())
            .where(Response.intent_type.isnot(None))
            .group_by(Response.persona, Response.intent_type)
        )
    ).all()
    by_persona: dict[str, dict[str, int]] = {}
    for persona, intent, n in persona_rows:
        by_persona.setdefault(persona, {})
        by_persona[persona][intent] = n

    return {
        "by_intent": by_intent,
        "by_persona": by_persona,
        "total": sum(by_intent.values()),
    }


@router.get("/intent-distribution")
async def intent_distribution(db: AsyncSession = Depends(get_db)):
    """Intent type breakdown across responses."""
    data, source = await with_snowflake_fallback(
        sf_analytics.intent_distribution,
        _sqlite_intent_distribution, db,
    )
    return _json_with_source(data, source)


async def _sqlite_run_summary(run_id: str, db: AsyncSession) -> dict | None:
    from app.models.run import Run as RunModel

    # Fetch run metadata
    run_row = (await db.execute(select(RunModel).where(RunModel.run_id == run_id))).scalar_one_or_none()
    if run_row is None:
        return None

    # Responses + latest scores for this run
    subq = await _latest_score_subquery()
    stmt = (
        select(Response, ScoringRecord)
        .join(ScoringRecord, ScoringRecord.response_id == Response.response_id)
        .join(subq, and_(ScoringRecord.response_id == subq.c.response_id, ScoringRecord.score_version == subq.c.maxv))
        .where(Response.run_id == run_id)
    )
    if HIDDEN_LLM_NAMES:
        stmt = stmt.where(Response.llm_name.notin_(HIDDEN_LLM_NAMES))
    scored_rows = (await db.execute(stmt)).all()

    # Sentiment by LLM
    sent_by_llm: dict[str, list[float]] = {}
    for resp, score in scored_rows:
        if score.sentiment_score is not None:
            sent_by_llm.setdefault(resp.llm_name, []).append(score.sentiment_score)
    sentiment_by_llm = [
        {"key": k, "avg_sentiment": round(sum(v) / len(v), 3), "count": len(v)}
        for k, v in sent_by_llm.items()
    ]

    # Positioning by LLM
    pos_by_llm: dict[str, dict[str, int]] = {}
    for resp, score in scored_rows:
        pos = score.competitive_position or "NOT_MENTIONED"
        pos_by_llm.setdefault(resp.llm_name, {})
        pos_by_llm[resp.llm_name][pos] = pos_by_llm[resp.llm_name].get(pos, 0) + 1

    # Responses by LLM + status
    resp_by_llm: dict[str, dict[str, int]] = {}
    all_resps_stmt = select(Response).where(Response.run_id == run_id)
    if HIDDEN_LLM_NAMES:
        all_resps_stmt = all_resps_stmt.where(Response.llm_name.notin_(HIDDEN_LLM_NAMES))
    all_resps = (await db.execute(all_resps_stmt)).scalars().all()
    for r in all_resps:
        resp_by_llm.setdefault(r.llm_name, {})
        resp_by_llm[r.llm_name][r.status] = resp_by_llm[r.llm_name].get(r.status, 0) + 1

    # Consensus
    cons_rows = (
        await db.execute(
            select(ConsensusRecord.consensus_level, func.count())
            .where(ConsensusRecord.run_id == run_id)
            .group_by(ConsensusRecord.consensus_level)
        )
    ).all()
    consensus_by_level = {level: n for level, n in cons_rows}
    geo_used = (
        await db.execute(
            select(func.count()).select_from(ConsensusRecord)
            .where(ConsensusRecord.run_id == run_id, ConsensusRecord.geo_fallback_used.is_(True))
        )
    ).scalar() or 0

    # Synthesized "council" aggregate: mean overall sentiment + modal-position spread per run
    overall_rows = (
        await db.execute(
            select(ConsensusRecord.overall_sentiment, ConsensusRecord.overall_position)
            .where(ConsensusRecord.run_id == run_id)
        )
    ).all()
    overall_sents = [s for s, _ in overall_rows if s is not None]
    consensus_position_dist: dict[str, int] = {}
    for _, pos in overall_rows:
        if pos:
            consensus_position_dist[pos] = consensus_position_dist.get(pos, 0) + 1
    consensus_overall = {
        "avg_sentiment": round(sum(overall_sents) / len(overall_sents), 3) if overall_sents else None,
        "questions_scored": len(overall_sents),
        "position_distribution": consensus_position_dist,
    }

    # Intent distribution
    intent_rows = (
        await db.execute(
            select(Response.intent_type, func.count())
            .where(Response.run_id == run_id, Response.intent_type.isnot(None))
            .group_by(Response.intent_type)
        )
    ).all()
    intent_by_type = {intent: n for intent, n in intent_rows}

    # Alerts for this run (join via response_id)
    run_response_ids = [r.response_id for r in all_resps]
    alert_total = 0
    alert_by_rule: dict[str, int] = {}
    if run_response_ids:
        alert_rows = (
            await db.execute(
                select(Alert.rule_triggered, func.count())
                .where(Alert.response_id.in_(run_response_ids))
                .group_by(Alert.rule_triggered)
            )
        ).all()
        alert_by_rule = {rule: n for rule, n in alert_rows}
        alert_total = sum(alert_by_rule.values())

    return {
        "run": {
            "run_id": run_row.run_id,
            "trigger": run_row.trigger,
            "status": run_row.status,
            "started_at": run_row.started_at.isoformat() if run_row.started_at else None,
            "ended_at": run_row.ended_at.isoformat() if run_row.ended_at else None,
            "questions_attempted": run_row.questions_attempted,
            "responses_success": run_row.responses_success,
            "responses_failed": run_row.responses_failed,
            "responses_truncated": run_row.responses_truncated,
            "responses_blocked": run_row.responses_blocked,
            "total_tokens": run_row.total_tokens,
            "estimated_cost_usd": float(run_row.estimated_cost_usd),
            "alerts_triggered": run_row.alerts_triggered,
            "consensus_full": run_row.consensus_full,
            "consensus_partial": run_row.consensus_partial,
            "consensus_missing": run_row.consensus_missing,
        },
        "sentiment_by_llm": sentiment_by_llm,
        "positioning_by_llm": pos_by_llm,
        "consensus_by_level": consensus_by_level,
        "consensus_overall": consensus_overall,
        "geo_fallback_count": geo_used,
        "intent_by_type": intent_by_type,
        "alerts": {"total": alert_total, "by_rule": alert_by_rule},
        "responses_by_llm": resp_by_llm,
    }


@router.get("/run-summary")
async def run_summary(run_id: str, db: AsyncSession = Depends(get_db)):
    """Per-run analytics bundle: sentiment, positioning, consensus, intent, alerts."""
    from fastapi import HTTPException

    async def _sf_run() -> dict | None:
        return await sf_analytics.run_summary(run_id)

    data, source = await with_snowflake_fallback(
        _sf_run, _sqlite_run_summary, run_id, db,
    )
    if data is None:
        raise HTTPException(404, "Run not found")
    return _json_with_source(data, source)


async def _sqlite_persona_summary(
    db: AsyncSession,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    disease: str | None = None,
    brand: str | None = None,
    monitoring_mode: str | None = None,
) -> dict:
    PERSONAS = ["Prospect", "Patient", "Provider"]

    # --- Responses + latest scores per persona ---
    rows = await _latest_scores_join(db, therapeutic_area=therapeutic_area, indication=indication, disease=disease, brand=brand, monitoring_mode=monitoring_mode)
    sent_by_persona: dict[str, list[float]] = {p: [] for p in PERSONAS}
    pos_by_persona: dict[str, dict[str, int]] = {p: {} for p in PERSONAS}
    resp_count: dict[str, int] = {p: 0 for p in PERSONAS}

    for resp, score in rows:
        p = resp.persona
        if p not in PERSONAS:
            continue
        resp_count[p] += 1
        pos = score.competitive_position or "NOT_MENTIONED"
        pos_by_persona[p][pos] = pos_by_persona[p].get(pos, 0) + 1
        if score.sentiment_score is not None:
            sent_by_persona[p].append(score.sentiment_score)

    # --- Consensus per persona (via responses) ---
    cons_rows = (
        await db.execute(
            select(Response.persona, Response.consensus_level, func.count())
            .where(Response.consensus_level.isnot(None))
            .group_by(Response.persona, Response.consensus_level)
        )
    ).all()
    cons_by_persona: dict[str, dict[str, int]] = {p: {} for p in PERSONAS}
    for persona, level, n in cons_rows:
        if persona in PERSONAS:
            cons_by_persona[persona][level] = n

    # --- Alerts per persona (join via response) ---
    alert_rows = (
        await db.execute(
            select(Response.persona, func.count())
            .join(Alert, Alert.response_id == Response.response_id)
            .group_by(Response.persona)
        )
    ).all()
    alerts_by_persona: dict[str, int] = {p: 0 for p in PERSONAS}
    for persona, n in alert_rows:
        if persona in PERSONAS:
            alerts_by_persona[persona] = n

    def _buckets(scores: list[float]) -> dict:
        pos = sum(1 for s in scores if s > 0.2)
        neg = sum(1 for s in scores if s < -0.2)
        return {"positive": pos, "neutral": len(scores) - pos - neg, "negative": neg}

    result: dict[str, dict] = {}
    for p in PERSONAS:
        sents = sent_by_persona[p]
        total = resp_count[p]
        alerts = alerts_by_persona[p]
        cons = cons_by_persona[p]
        total_cons = sum(cons.values()) or 1
        result[p] = {
            "response_count": total,
            "sentiment": {
                "avg": round(sum(sents) / len(sents), 3) if sents else None,
                **_buckets(sents),
                "scored": len(sents),
            },
            "positioning": pos_by_persona[p],
            "consensus": {
                "FULL": cons.get("FULL", 0),
                "PARTIAL": cons.get("PARTIAL", 0),
                "MISSING": cons.get("MISSING", 0),
                "full_pct": round(cons.get("FULL", 0) / total_cons * 100, 1),
            },
            "alert_rate": {
                "alerts": alerts,
                "responses": total,
                "rate": round(alerts / total, 3) if total else 0.0,
            },
        }
    return result


@router.get("/persona-summary")
async def persona_summary(
    persona: str | None = None,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    disease: str | None = None,
    brand: str | None = None,
    monitoring_mode: str = "BRAND",
    db: AsyncSession = Depends(get_db),
):
    """Per-persona KPI bundle: sentiment, positioning, consensus quality, alert rate.

    Without ``persona`` the full per-persona mapping is returned (used by the
    comparison chart). When ``persona`` is supplied (Prospect/Patient/Provider)
    only that persona's bundle is returned, for the per-tab targeted KPIs.
    """
    data, source = await with_snowflake_fallback(
        sf_analytics.persona_summary,
        _sqlite_persona_summary, db,
        therapeutic_area=therapeutic_area, indication=indication, disease=disease, brand=brand,
        monitoring_mode=monitoring_mode,
    )
    if persona:
        data = data.get(persona, {}) if isinstance(data, dict) else {}
    return _json_with_source(data, source)


async def _sqlite_llm_comparison(db: AsyncSession, monitoring_mode: str | None = None) -> list[dict]:
    status_stmt = select(Response.llm_name, Response.status, func.count()).group_by(
        Response.llm_name, Response.status
    )
    if monitoring_mode:
        status_stmt = status_stmt.where(Response.monitoring_mode == monitoring_mode)
    if HIDDEN_LLM_NAMES:
        status_stmt = status_stmt.where(Response.llm_name.notin_(HIDDEN_LLM_NAMES))
    status_rows = (await db.execute(status_stmt)).all()
    scored = await _latest_scores_join(db, monitoring_mode=monitoring_mode)

    out: dict[str, dict] = {}
    for llm, status, n in status_rows:
        out.setdefault(llm, {"counts": {}, "sentiments": []})
        out[llm]["counts"][status] = n
    for resp, score in scored:
        if score.sentiment_score is not None:
            out.setdefault(resp.llm_name, {"counts": {}, "sentiments": []})
            out[resp.llm_name]["sentiments"].append(score.sentiment_score)

    result = []
    for llm, data in out.items():
        sents = data["sentiments"]
        result.append({
            "llm_name": llm,
            "counts": data["counts"],
            "avg_sentiment": round(sum(sents) / len(sents), 3) if sents else None,
            "scored": len(sents),
        })
    return result


@router.get("/llm-comparison")
async def llm_comparison(monitoring_mode: str = "BRAND", db: AsyncSession = Depends(get_db)):
    """Per-LLM rollup: response counts, avg sentiment, alert counts. BRAND-only by default."""
    data, source = await with_snowflake_fallback(
        sf_analytics.llm_comparison,
        _sqlite_llm_comparison, db, monitoring_mode=monitoring_mode,
    )
    return _json_with_source(data, source)


async def _sqlite_worst_questions(
    limit: int,
    persona: str | None,
    db: AsyncSession,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    disease: str | None = None,
    brand: str | None = None,
    monitoring_mode: str | None = None,
) -> list[dict]:
    """Compute a composite 'needs attention' score per question and return the worst N."""
    rows = await _latest_scores_join(db, therapeutic_area=therapeutic_area, indication=indication, disease=disease, brand=brand, monitoring_mode=monitoring_mode)

    # Aggregate per question_id
    agg: dict[str, dict] = {}
    for resp, score in rows:
        if persona and resp.persona != persona:
            continue
        qid = resp.question_id
        if qid not in agg:
            agg[qid] = {
                "question_text": resp.question_text,
                "brand_focus": resp.brand_focus,
                "persona": resp.persona,
                "therapeutic_area": resp.therapeutic_area,
                "sentiments": [],
                "positions": [],
                "consensus_levels": [],
                "alert_count": 0,
            }
        if score.sentiment_score is not None:
            agg[qid]["sentiments"].append(score.sentiment_score)
        pos = score.competitive_position or "NOT_MENTIONED"
        agg[qid]["positions"].append(pos)
        if resp.consensus_level:
            agg[qid]["consensus_levels"].append(resp.consensus_level)

    # Attach alert counts
    if agg:
        alert_rows = (
            await db.execute(
                select(Response.question_id, func.count())
                .join(Alert, Alert.response_id == Response.response_id)
                .where(Response.question_id.in_(list(agg.keys())))
                .group_by(Response.question_id)
            )
        ).all()
        for qid, count in alert_rows:
            if qid in agg:
                agg[qid]["alert_count"] = count

    # Compute composite score
    results = []
    for qid, d in agg.items():
        sents = d["sentiments"]
        positions = d["positions"]
        cons = d["consensus_levels"]
        total_pos = len(positions) or 1
        total_cons = len(cons) or 1

        avg_sent = sum(sents) / len(sents) if sents else 0.0
        # Normalize sentiment -1..1 → 0..1 where 1 = worst
        neg_sent_score = (1 - avg_sent) / 2

        bad_pos = sum(1 for p in positions if p in ("NOT_MENTIONED", "NOT_RECOMMENDED"))
        bad_pos_score = bad_pos / total_pos

        missing_cons = sum(1 for c in cons if c == "MISSING")
        missing_cons_score = missing_cons / total_cons

        alert_score = min(d["alert_count"] / 5, 1.0)

        composite = neg_sent_score + bad_pos_score + missing_cons_score + alert_score

        dominant_pos = max(set(positions), key=positions.count) if positions else "NOT_MENTIONED"
        dominant_cons = max(set(cons), key=cons.count) if cons else "MISSING"

        results.append({
            "question_id": qid,
            "question_text": d["question_text"],
            "brand_focus": d["brand_focus"],
            "persona": d["persona"],
            "therapeutic_area": d["therapeutic_area"],
            "avg_sentiment": round(avg_sent, 3),
            "dominant_position": dominant_pos,
            "consensus_level": dominant_cons,
            "alert_count": d["alert_count"],
            "composite_score": round(composite, 3),
        })

    results.sort(key=lambda x: x["composite_score"], reverse=True)
    return results[:limit]


@router.get("/worst-questions")
async def worst_questions(
    limit: int = 3,
    persona: str | None = None,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    disease: str | None = None,
    brand: str | None = None,
    monitoring_mode: str = "BRAND",
    db: AsyncSession = Depends(get_db),
):
    """Top N questions by composite 'needs attention' score (sentiment + positioning + consensus + alerts)."""
    data = await _sqlite_worst_questions(limit, persona, db, therapeutic_area=therapeutic_area, indication=indication, disease=disease, brand=brand, monitoring_mode=monitoring_mode)
    return JSONResponse(content=data, headers={"X-Data-Source": "sqlite"})


# ---------------------------------------------------------------------------
# /analytics/landscape (FR-108a.4/.6) — disease-state multi-competitor matrix
# ---------------------------------------------------------------------------
async def _sqlite_landscape(
    db: AsyncSession,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    disease: str | None = None,
) -> dict:
    """Aggregate the per-response landscape matrices (stored in ScoringRecord.brand_mentions
    for DISEASE_STATE responses) into a single per-competitor view: share-of-voice, mean
    sentiment, and a position distribution for every agent named across the field."""
    rows = await _latest_scores_join(
        db, therapeutic_area=therapeutic_area, indication=indication,
        disease=disease, monitoring_mode="DISEASE_STATE",
    )

    agg: dict[str, dict] = {}
    response_ids: set[str] = set()
    question_ids: set[str] = set()
    llms: set[str] = set()

    for resp, score in rows:
        response_ids.add(resp.response_id)
        question_ids.add(resp.question_id)
        llms.add(resp.llm_name)
        try:
            matrix = json.loads(score.brand_mentions) if score.brand_mentions else []
        except (TypeError, ValueError):
            matrix = []
        if not isinstance(matrix, list):
            continue
        for m in matrix:
            if not isinstance(m, dict):
                continue
            brand = (m.get("brand") or "").strip()
            if not brand:
                continue
            entry = agg.setdefault(brand, {
                "brand": brand, "is_competitor": True,
                "mentions": 0, "sentiments": [], "positions": {},
            })
            if m.get("is_competitor") is not None:
                entry["is_competitor"] = bool(m.get("is_competitor"))
            if not m.get("mentioned", True):
                continue
            entry["mentions"] += 1
            sent = m.get("sentiment")
            if sent is not None:
                try:
                    entry["sentiments"].append(float(sent))
                except (TypeError, ValueError):
                    pass
            pos = m.get("position") or "NOT_MENTIONED"
            entry["positions"][pos] = entry["positions"].get(pos, 0) + 1

    total_resp = len(response_ids) or 1
    matrix_out = []
    for brand, d in agg.items():
        sents = d["sentiments"]
        positions = d["positions"]
        dominant = max(positions, key=positions.get) if positions else "NOT_MENTIONED"
        matrix_out.append({
            "brand": brand,
            "is_competitor": d["is_competitor"],
            "mentions": d["mentions"],
            "share_of_voice": round(d["mentions"] / total_resp, 3),
            "avg_sentiment": round(sum(sents) / len(sents), 3) if sents else None,
            "positions": positions,
            "dominant_position": dominant,
        })
    # Rank by how often each agent surfaces (share of voice), then sentiment.
    matrix_out.sort(key=lambda x: (x["mentions"], x["avg_sentiment"] or 0), reverse=True)

    return {
        "pre_launch_notice": PRELAUNCH_LABEL,
        "responses_analyzed": len(response_ids),
        "questions": len(question_ids),
        "llms": sorted(llms),
        "matrix": matrix_out,
    }


@router.get("/landscape")
async def landscape(
    therapeutic_area: str | None = None,
    indication: str | None = None,
    disease: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Disease-state / pre-launch multi-competitor landscape matrix (FR-108a.4/.6).

    Brand-less: aggregates every agent named across DISEASE_STATE responses. Not served
    from Snowflake (the landscape matrix is derived from the local scoring records).
    """
    data = await _sqlite_landscape(db, therapeutic_area=therapeutic_area, indication=indication, disease=disease)
    return _json_with_source(data, "sqlite")
