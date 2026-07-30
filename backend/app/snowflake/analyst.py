"""Natural-language "Ask your data" over the Snowflake mirror.

Lets a user type a plain-English question in the UI and get an answer grounded in the
mirrored tables. Implementation is Cortex-powered text-to-SQL: SNOWFLAKE.CORTEX.COMPLETE
generates a read-only SELECT from a compact schema description, we hard-guard it to be
read-only, execute it, then have Cortex narrate the result rows.

This works with the existing key-pair connection and needs no extra setup. (A full Cortex
Analyst semantic-model deployment is a documented future upgrade — see docs/snowflake.md.)
"""
from __future__ import annotations

import re

from app.snowflake import client, cortex
from app.snowflake.tables import SPECS
from app.utils.logging import get_logger

logger = get_logger("snowflake.analyst")

_MAX_ROWS = 200
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|"
    r"CALL|COPY|PUT|REMOVE|USE|SET)\b",
    re.IGNORECASE,
)
_SQL_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _schema_description() -> str:
    parts: list[str] = []
    for spec in SPECS:
        cols = ", ".join(c for c, _ in spec.columns)
        parts.append(f"{spec.table}({cols})")
    return "\n".join(parts)


def _extract_sql(text: str) -> str:
    fence = _SQL_FENCE.search(text)
    candidate = fence.group(1) if fence else text
    candidate = candidate.strip().rstrip(";").strip()
    # Keep only from the first SELECT/WITH onward.
    m = re.search(r"\b(SELECT|WITH)\b", candidate, re.IGNORECASE)
    if m:
        candidate = candidate[m.start():]
    return candidate.strip()


def _is_read_only(sql: str) -> bool:
    head = sql.lstrip().upper()
    if not (head.startswith("SELECT") or head.startswith("WITH")):
        return False
    if ";" in sql.rstrip(";"):  # block stacked statements
        return False
    return _FORBIDDEN.search(sql) is None


def _enforce_limit(sql: str) -> str:
    if re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
        return sql
    return f"{sql}\nLIMIT {_MAX_ROWS}"


async def ask(question: str) -> dict:
    """Answer a natural-language question over the Snowflake tables."""
    if not client.is_enabled():
        return {"enabled": False, "answer": "Snowflake is not enabled."}

    gen_prompt = (
        "You are a Snowflake SQL expert. Generate ONE read-only Snowflake SQL query that "
        "answers the user's question using ONLY the tables/columns below. Rules: SELECT only "
        "(no DML/DDL), no semicolons, use fully-qualified column names where helpful, and add "
        f"a LIMIT {_MAX_ROWS} if the result could be large. Return ONLY the SQL in a ```sql "
        "code block.\n\nTABLES:\n" + _schema_description() +
        f"\n\nQUESTION: {question}"
    )
    try:
        raw_sql = await cortex.cortex_complete(gen_prompt, max_tokens=600)
    except cortex.CortexLLMUnavailable:
        return {
            "enabled": True, "question": question, "generated_sql": "",
            "columns": [], "rows": [], "error": "cortex_llm_unavailable",
            "answer": cortex._LLM_UNAVAILABLE_MSG,
        }
    sql = _extract_sql(raw_sql)

    if not sql or not _is_read_only(sql):
        logger.warning("Rejected non-read-only or empty SQL: %r", sql)
        return {
            "enabled": True,
            "question": question,
            "generated_sql": sql,
            "error": "Could not generate a safe read-only query for that question.",
            "columns": [],
            "rows": [],
            "answer": "I couldn't translate that into a safe query. Try rephrasing.",
        }

    sql = _enforce_limit(sql)
    try:
        rows = await client.execute(sql)
    except Exception as e:  # noqa: BLE001
        logger.warning("Generated SQL failed: %s", e)
        return {
            "enabled": True, "question": question, "generated_sql": sql,
            "error": str(e), "columns": [], "rows": [],
            "answer": "The generated query failed to run. Try rephrasing your question.",
        }

    columns = list(rows[0].keys()) if rows else []
    preview = rows[:50]
    answer_prompt = (
        "Answer the user's question in 2-4 sentences using ONLY the query result rows below. "
        "Be concise, factual, and cite concrete numbers. If the rows are empty, say no data "
        f"was found.\n\nQUESTION: {question}\n\nRESULT ROWS (JSON):\n{preview}"
    )
    try:
        answer = await cortex.cortex_complete(answer_prompt, max_tokens=600)
    except Exception as e:  # noqa: BLE001
        logger.warning("Answer narration failed: %s", e)
        answer = "Query ran successfully. See the result table below."

    return {
        "enabled": True,
        "question": question,
        "generated_sql": sql,
        "columns": columns,
        "rows": rows,
        "answer": answer.strip(),
    }
