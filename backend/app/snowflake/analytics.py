"""Snowflake-view query functions for the /analytics/* dashboard endpoints.

Each function queries the pre-built Snowflake views (see ``snowflake_views.sql``)
and reshapes the result into the **exact** JSON structure the frontend expects,
so the API response contracts remain identical regardless of the data source.
"""
from __future__ import annotations

from app.config.labels import HIDDEN_LLM_NAMES
from app.snowflake import client
from app.utils.logging import get_logger

logger = get_logger("snowflake.analytics")


# ---------------------------------------------------------------------------
# /analytics/sentiment-distribution
# ---------------------------------------------------------------------------
async def sentiment_distribution() -> dict:
    by_llm_rows = await client.execute("SELECT * FROM VW_SENTIMENT_BY_LLM")
    by_ta_rows = await client.execute("SELECT * FROM VW_SENTIMENT_BY_THERAPEUTIC_AREA")
    bucket_rows = await client.execute("SELECT * FROM VW_SENTIMENT_BUCKETS")

    by_llm = [
        {"key": r["LLM"], "avg_sentiment": float(r["AVG_SENTIMENT"]), "count": int(r["SCORED"])}
        for r in by_llm_rows
        if r["LLM"] not in HIDDEN_LLM_NAMES
    ]
    by_ta = [
        {"key": r["THERAPEUTIC_AREA"], "avg_sentiment": float(r["AVG_SENTIMENT"]), "count": int(r["SCORED"])}
        for r in by_ta_rows
    ]
    b = bucket_rows[0] if bucket_rows else {}
    buckets = {
        "positive": int(b.get("POSITIVE", 0)),
        "neutral": int(b.get("NEUTRAL", 0)),
        "negative": int(b.get("NEGATIVE", 0)),
    }
    return {"by_llm": by_llm, "by_therapeutic_area": by_ta, "buckets": buckets}


# ---------------------------------------------------------------------------
# /analytics/positioning
# ---------------------------------------------------------------------------
async def positioning() -> dict:
    rows = await client.execute("SELECT * FROM VW_POSITIONING_BY_LLM")
    result: dict[str, dict[str, int]] = {}
    for r in rows:
        llm = r["LLM"]
        if llm in HIDDEN_LLM_NAMES:
            continue
        pos = r["POSITION"]
        result.setdefault(llm, {})
        result[llm][pos] = int(r["N"])
    return result


# ---------------------------------------------------------------------------
# /analytics/volume
# ---------------------------------------------------------------------------
async def volume() -> dict:
    rows = await client.execute("SELECT * FROM VW_VOLUME_BY_DAY")
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        day = str(r["DAY"])
        status = r["STATUS"]
        out.setdefault(day, {})
        out[day][status] = int(r["N"])
    return out


# ---------------------------------------------------------------------------
# /analytics/alerts-summary
# ---------------------------------------------------------------------------
async def alerts_summary() -> dict:
    rows = await client.execute("SELECT * FROM VW_ALERTS_BY_RULE")
    by_rule = {r["RULE_TRIGGERED"]: int(r["N"]) for r in rows}
    total = sum(by_rule.values())
    return {"total_alerts": total, "by_rule": by_rule}


# ---------------------------------------------------------------------------
# /analytics/consensus-summary
# ---------------------------------------------------------------------------
async def consensus_summary() -> dict:
    level_rows = await client.execute("SELECT * FROM VW_CONSENSUS_BY_LEVEL")
    llm_rows = await client.execute("SELECT * FROM VW_CONSENSUS_BY_LLM")
    geo_rows = await client.execute(
        "SELECT COUNT(*) AS N FROM CONSENSUS_RECORDS WHERE GEO_FALLBACK_USED = TRUE"
    )

    by_level = {r["CONSENSUS_LEVEL"]: int(r["N"]) for r in level_rows}

    by_llm: dict[str, dict[str, int]] = {}
    for r in llm_rows:
        llm = r["LLM"]
        if llm in HIDDEN_LLM_NAMES:
            continue
        by_llm.setdefault(llm, {})
        by_llm[llm][r["CONSENSUS_LEVEL"]] = int(r["N"])

    geo_fallback = int(geo_rows[0]["N"]) if geo_rows else 0

    return {
        "by_level": by_level,
        "by_llm": by_llm,
        "geo_fallback_count": geo_fallback,
        "total_evaluations": sum(by_level.values()),
    }


# ---------------------------------------------------------------------------
# /analytics/intent-distribution
# ---------------------------------------------------------------------------
async def intent_distribution() -> dict:
    intent_rows = await client.execute("SELECT * FROM VW_INTENT_DISTRIBUTION")
    persona_rows = await client.execute("SELECT * FROM VW_INTENT_BY_PERSONA")

    by_intent = {r["INTENT_TYPE"]: int(r["N"]) for r in intent_rows}

    by_persona: dict[str, dict[str, int]] = {}
    for r in persona_rows:
        persona = r["PERSONA"]
        by_persona.setdefault(persona, {})
        by_persona[persona][r["INTENT_TYPE"]] = int(r["N"])

    return {
        "by_intent": by_intent,
        "by_persona": by_persona,
        "total": sum(by_intent.values()),
    }


# ---------------------------------------------------------------------------
# /analytics/persona-summary
# ---------------------------------------------------------------------------
async def persona_summary() -> dict:
    """Per-persona KPI bundle (sentiment, positioning, consensus, alert rate).

    Reshapes the persona-level views into the SAME structure as the SQLite
    implementation (``_sqlite_persona_summary``) so the response contract is
    identical regardless of the data source.
    """
    PERSONAS = ["Prospect", "Patient", "Provider"]

    sent_rows = await client.execute("SELECT * FROM VW_PERSONA_SENTIMENT")
    pos_rows = await client.execute("SELECT * FROM VW_PERSONA_POSITIONING")
    cons_rows = await client.execute("SELECT * FROM VW_PERSONA_CONSENSUS")
    alert_rows = await client.execute("SELECT * FROM VW_PERSONA_ALERTS")

    sent_by = {r["PERSONA"]: r for r in sent_rows}

    pos_by: dict[str, dict[str, int]] = {}
    for r in pos_rows:
        pos_by.setdefault(r["PERSONA"], {})[r["POSITION"]] = int(r["N"])

    cons_by: dict[str, dict[str, int]] = {}
    for r in cons_rows:
        cons_by.setdefault(r["PERSONA"], {})[r["CONSENSUS_LEVEL"]] = int(r["N"])

    alert_by = {r["PERSONA"]: int(r["N"]) for r in alert_rows}

    result: dict[str, dict] = {}
    for p in PERSONAS:
        s = sent_by.get(p, {})
        total = int(s.get("RESPONSE_COUNT", 0) or 0)
        cons = cons_by.get(p, {})
        total_cons = sum(cons.values()) or 1
        alerts = alert_by.get(p, 0)
        avg = s.get("AVG_SENTIMENT")
        result[p] = {
            "response_count": total,
            "sentiment": {
                "avg": round(float(avg), 3) if avg is not None else None,
                "positive": int(s.get("POSITIVE", 0) or 0),
                "neutral": int(s.get("NEUTRAL", 0) or 0),
                "negative": int(s.get("NEGATIVE", 0) or 0),
                "scored": int(s.get("SCORED", 0) or 0),
            },
            "positioning": pos_by.get(p, {}),
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


# ---------------------------------------------------------------------------
# /analytics/llm-comparison
# ---------------------------------------------------------------------------
async def llm_comparison() -> list[dict]:
    comp_rows = await client.execute("SELECT * FROM VW_LLM_COMPARISON")
    count_rows = await client.execute("SELECT * FROM VW_LLM_RESPONSE_COUNTS")

    counts_by_llm: dict[str, dict[str, int]] = {}
    for r in count_rows:
        llm = r["LLM"]
        counts_by_llm.setdefault(llm, {})
        counts_by_llm[llm][r["STATUS"]] = int(r["N"])

    result = []
    for r in comp_rows:
        llm = r["LLM"]
        if llm in HIDDEN_LLM_NAMES:
            continue
        result.append({
            "llm_name": llm,
            "counts": counts_by_llm.get(llm, {}),
            "avg_sentiment": float(r["AVG_SENTIMENT"]) if r.get("AVG_SENTIMENT") is not None else None,
            "scored": int(r["SCORED"]) if r.get("SCORED") is not None else 0,
        })
    return result


# ---------------------------------------------------------------------------
# /analytics/run-summary
# ---------------------------------------------------------------------------
async def run_summary(run_id: str) -> dict | None:
    """Per-run analytics bundle. Returns None if the run doesn't exist in Snowflake."""
    # Run metadata
    run_rows = await client.execute(
        "SELECT * FROM VW_RUN_KPIS WHERE RUN_ID = %s", (run_id,)
    )
    if not run_rows:
        return None

    run = run_rows[0]

    # Sentiment by LLM for this run
    sent_rows = await client.execute(
        "SELECT * FROM VW_RUN_SENTIMENT_BY_LLM WHERE RUN_ID = %s", (run_id,)
    )
    sentiment_by_llm = [
        {"key": r["LLM"], "avg_sentiment": float(r["AVG_SENTIMENT"]), "count": int(r["SCORED"])}
        for r in sent_rows
        if r["LLM"] not in HIDDEN_LLM_NAMES
    ]

    # Positioning by LLM for this run
    pos_rows = await client.execute(
        "SELECT * FROM VW_RUN_POSITIONING_BY_LLM WHERE RUN_ID = %s", (run_id,)
    )
    positioning_by_llm: dict[str, dict[str, int]] = {}
    for r in pos_rows:
        llm = r["LLM"]
        if llm in HIDDEN_LLM_NAMES:
            continue
        positioning_by_llm.setdefault(llm, {})
        positioning_by_llm[llm][r["POSITION"]] = int(r["N"])

    # Responses by LLM + status for this run
    resp_rows = await client.execute(
        "SELECT LLM_NAME AS LLM, STATUS, COUNT(*) AS N FROM RESPONSES "
        "WHERE RUN_ID = %s GROUP BY LLM_NAME, STATUS",
        (run_id,),
    )
    responses_by_llm: dict[str, dict[str, int]] = {}
    for r in resp_rows:
        llm = r["LLM"]
        if llm in HIDDEN_LLM_NAMES:
            continue
        responses_by_llm.setdefault(llm, {})
        responses_by_llm[llm][r["STATUS"]] = int(r["N"])

    # Consensus for this run
    cons_rows = await client.execute(
        "SELECT * FROM VW_RUN_CONSENSUS WHERE RUN_ID = %s", (run_id,)
    )
    consensus_by_level = {r["CONSENSUS_LEVEL"]: int(r["N"]) for r in cons_rows}
    geo_fallback = sum(int(r.get("GEO_FALLBACK_COUNT", 0)) for r in cons_rows)

    # Consensus overall (sentiment + position from consensus_records)
    overall_rows = await client.execute(
        "SELECT OVERALL_SENTIMENT, OVERALL_POSITION FROM CONSENSUS_RECORDS "
        "WHERE RUN_ID = %s",
        (run_id,),
    )
    overall_sents = [float(r["OVERALL_SENTIMENT"]) for r in overall_rows if r.get("OVERALL_SENTIMENT") is not None]
    consensus_position_dist: dict[str, int] = {}
    for r in overall_rows:
        pos = r.get("OVERALL_POSITION")
        if pos:
            consensus_position_dist[pos] = consensus_position_dist.get(pos, 0) + 1
    consensus_overall = {
        "avg_sentiment": round(sum(overall_sents) / len(overall_sents), 3) if overall_sents else None,
        "questions_scored": len(overall_sents),
        "position_distribution": consensus_position_dist,
    }

    # Intent for this run
    intent_rows = await client.execute(
        "SELECT * FROM VW_RUN_INTENT WHERE RUN_ID = %s", (run_id,)
    )
    intent_by_type = {r["INTENT_TYPE"]: int(r["N"]) for r in intent_rows}

    # Alerts for this run
    alert_rows = await client.execute(
        "SELECT a.RULE_TRIGGERED, COUNT(*) AS N FROM ALERTS a "
        "JOIN RESPONSES r ON r.RESPONSE_ID = a.RESPONSE_ID "
        "WHERE r.RUN_ID = %s GROUP BY a.RULE_TRIGGERED",
        (run_id,),
    )
    alert_by_rule = {r["RULE_TRIGGERED"]: int(r["N"]) for r in alert_rows}
    alert_total = sum(alert_by_rule.values())

    def _safe_int(v: object) -> int:
        return int(v) if v is not None else 0

    def _safe_float(v: object) -> float:
        return float(v) if v is not None else 0.0

    def _safe_str(v: object) -> str | None:
        return str(v) if v is not None else None

    return {
        "run": {
            "run_id": run["RUN_ID"],
            "trigger": run.get("TRIGGER_TYPE"),
            "status": run.get("STATUS"),
            "started_at": _safe_str(run.get("STARTED_AT")),
            "ended_at": _safe_str(run.get("ENDED_AT")),
            "questions_attempted": _safe_int(run.get("QUESTIONS_ATTEMPTED")),
            "responses_success": _safe_int(run.get("RESPONSES_SUCCESS")),
            "responses_failed": _safe_int(run.get("RESPONSES_FAILED")),
            "responses_truncated": _safe_int(run.get("RESPONSES_TRUNCATED")),
            "responses_blocked": _safe_int(run.get("RESPONSES_BLOCKED")),
            "total_tokens": _safe_int(run.get("TOTAL_TOKENS")),
            "estimated_cost_usd": _safe_float(run.get("ESTIMATED_COST_USD")),
            "alerts_triggered": _safe_int(run.get("ALERTS_TRIGGERED")),
            "consensus_full": _safe_int(run.get("CONSENSUS_FULL")),
            "consensus_partial": _safe_int(run.get("CONSENSUS_PARTIAL")),
            "consensus_missing": _safe_int(run.get("CONSENSUS_MISSING")),
        },
        "sentiment_by_llm": sentiment_by_llm,
        "positioning_by_llm": positioning_by_llm,
        "consensus_by_level": consensus_by_level,
        "consensus_overall": consensus_overall,
        "geo_fallback_count": geo_fallback,
        "intent_by_type": intent_by_type,
        "alerts": {"total": alert_total, "by_rule": alert_by_rule},
        "responses_by_llm": responses_by_llm,
    }
