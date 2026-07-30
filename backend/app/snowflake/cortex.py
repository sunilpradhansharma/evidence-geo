"""Snowflake Cortex-powered insights over the mirrored data.

Adds a second, warehouse-native insight layer ALONGSIDE the existing Bedrock pipeline:
- sentiment rollups by brand and over time (SQL over mirrored SCORING_RECORDS/RESPONSES),
- an LLM executive summary of recent signals via SNOWFLAKE.CORTEX.COMPLETE,
- per-brand narrative summaries.

Results are cached briefly (Cortex calls cost credits) and computed on demand. Safe no-op
when Snowflake is disabled.
"""
from __future__ import annotations

import time

from app.config.settings import get_settings
from app.snowflake import client
from app.utils.logging import get_logger

logger = get_logger("snowflake.cortex")

_CACHE: dict[str, tuple[float, object]] = {}
_TTL = 300  # seconds


def _cached(key: str):
    hit = _CACHE.get(key)
    if hit and (time.time() - hit[0]) < _TTL:
        return hit[1]
    return None


def _store(key: str, value):
    _CACHE[key] = (time.time(), value)
    return value


class CortexLLMUnavailable(RuntimeError):
    """Raised when the Cortex COMPLETE LLM function can't be used (e.g. trial accounts)."""


def _is_llm_unavailable(err: Exception) -> bool:
    msg = str(err).lower()
    return "not available for trial" in msg or "complete is not available" in msg


async def cortex_complete(prompt: str, *, max_tokens: int = 1200) -> str:
    """Call SNOWFLAKE.CORTEX.COMPLETE with the configured model. Returns text.

    Uses the 2-argument (model, prompt) form which returns a plain string — the options
    object can't be reliably bound through the connector's parameter style. Raises
    CortexLLMUnavailable when the account can't run COMPLETE (e.g. Snowflake trial).
    """
    s = get_settings()
    try:
        rows = await client.execute(
            "SELECT SNOWFLAKE.CORTEX.COMPLETE(%s, %s) AS RESPONSE",
            (s.snowflake_cortex_model, prompt),
        )
    except Exception as e:  # noqa: BLE001
        if _is_llm_unavailable(e):
            raise CortexLLMUnavailable(str(e)) from e
        raise
    return (rows[0].get("RESPONSE") if rows else "") or ""


async def sentiment_by_brand() -> list[dict]:
    return await client.execute(
        "SELECT r.BRAND_FOCUS AS BRAND, "
        "COUNT(*) AS SCORED, "
        "ROUND(AVG(sc.SENTIMENT_SCORE), 3) AS AVG_SENTIMENT, "
        "ROUND(MIN(sc.SENTIMENT_SCORE), 3) AS MIN_SENTIMENT, "
        "ROUND(MAX(sc.SENTIMENT_SCORE), 3) AS MAX_SENTIMENT "
        "FROM SCORING_RECORDS sc JOIN RESPONSES r ON r.RESPONSE_ID = sc.RESPONSE_ID "
        "WHERE sc.SENTIMENT_SCORE IS NOT NULL "
        "GROUP BY r.BRAND_FOCUS ORDER BY AVG_SENTIMENT ASC"
    )


async def sentiment_trend() -> list[dict]:
    return await client.execute(
        "SELECT TO_VARCHAR(DATE_TRUNC('day', r.TIMESTAMP_UTC)) AS DAY, "
        "r.BRAND_FOCUS AS BRAND, "
        "ROUND(AVG(sc.SENTIMENT_SCORE), 3) AS AVG_SENTIMENT, COUNT(*) AS N "
        "FROM SCORING_RECORDS sc JOIN RESPONSES r ON r.RESPONSE_ID = sc.RESPONSE_ID "
        "WHERE sc.SENTIMENT_SCORE IS NOT NULL AND r.TIMESTAMP_UTC IS NOT NULL "
        "GROUP BY 1, 2 ORDER BY 1 ASC"
    )


async def positioning_by_brand() -> list[dict]:
    return await client.execute(
        "SELECT r.BRAND_FOCUS AS BRAND, sc.COMPETITIVE_POSITION AS POSITION, COUNT(*) AS N "
        "FROM SCORING_RECORDS sc JOIN RESPONSES r ON r.RESPONSE_ID = sc.RESPONSE_ID "
        "WHERE sc.COMPETITIVE_POSITION IS NOT NULL "
        "GROUP BY 1, 2 ORDER BY 1, 3 DESC"
    )


_LLM_UNAVAILABLE_MSG = (
    "Cortex LLM (COMPLETE) is not available on this Snowflake account "
    "(trial accounts don't include AI functions). The sentiment and positioning "
    "analytics below are fully functional — upgrade the account to enable the AI "
    "executive briefing and natural-language Q&A."
)


async def executive_summary() -> tuple[str, bool]:
    """LLM summary of recent signals via Cortex. Returns (text, llm_available)."""
    rows = await client.execute(
        "SELECT r.BRAND_FOCUS AS BRAND, r.LLM_NAME AS MODEL, sc.SENTIMENT_SCORE AS SENTIMENT, "
        "sc.COMPETITIVE_POSITION AS POSITION, LEFT(sc.SCORING_RATIONALE, 400) AS RATIONALE "
        "FROM SCORING_RECORDS sc JOIN RESPONSES r ON r.RESPONSE_ID = sc.RESPONSE_ID "
        "WHERE sc.SENTIMENT_SCORE IS NOT NULL "
        "ORDER BY sc.CREATED_AT DESC LIMIT 60"
    )
    if not rows:
        return "Not enough scored data in Snowflake yet to summarize.", True

    lines = [
        f"- {r['BRAND']} | {r['MODEL']} | sentiment={r['SENTIMENT']} | "
        f"position={r['POSITION']} | {r['RATIONALE']}"
        for r in rows
    ]
    prompt = (
        "You are a pharmaceutical Medical Affairs analyst. Below are recent AI-model "
        "evaluations of how LLMs describe our brands (sentiment is -1..+1). Write a concise "
        "executive briefing (5-7 bullet points) of the most important signals: which brands "
        "are at risk, notable competitive positioning, and any divergence between models. "
        "Be specific and factual.\n\nDATA:\n" + "\n".join(lines)
    )
    try:
        return await cortex_complete(prompt, max_tokens=900), True
    except CortexLLMUnavailable:
        return _LLM_UNAVAILABLE_MSG, False
    except Exception as e:  # noqa: BLE001
        logger.warning("Executive summary failed: %s", e)
        return f"Executive summary unavailable: {e}", True


async def insights(force: bool = False) -> dict:
    """Bundle the Cortex insight sections for the UI. Cached for _TTL seconds."""
    if not client.is_enabled():
        return {"enabled": False}
    if not force:
        hit = _cached("insights")
        if hit is not None:
            return hit  # type: ignore[return-value]

    summary_text, llm_available = await executive_summary()
    result = {
        "enabled": True,
        "model": get_settings().snowflake_cortex_model,
        "llm_available": llm_available,
        "sentiment_by_brand": await sentiment_by_brand(),
        "sentiment_trend": await sentiment_trend(),
        "positioning_by_brand": await positioning_by_brand(),
        "executive_summary": summary_text,
    }
    return _store("insights", result)  # type: ignore[return-value]
