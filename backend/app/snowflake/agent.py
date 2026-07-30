"""Cortex Agent — conversational, plain-English Q&A for the global chat widget.

This powers the "Cortex Agent" chat bubble in the UI. It is built for NON-TECHNICAL
users: questions go in as plain English and answers come back as plain English — no SQL
is ever shown.

Pipeline (multi-turn aware):
  1. Send the conversation to the **Cortex Analyst REST API** over the native Semantic
     View (``EVIDENCE_DB.PUBLIC.EVIDENCE_SEMANTIC_VIEW``). Cortex Analyst returns a
     trustworthy SQL statement grounded in the semantic model.
  2. Execute that read-only SQL through the existing key-pair connection (``client``).
  3. Narrate the result rows into a short, friendly answer with ``CORTEX.COMPLETE``.

Auth uses a short-lived key-pair JWT (see ``jwt_auth``) — the same RSA key the connector
already uses. Everything degrades gracefully: when Snowflake/agent is disabled the call
is a no-op with a helpful message.
"""
from __future__ import annotations

import httpx

from app.config.settings import get_settings
from app.snowflake import client, cortex, jwt_auth
from app.utils.logging import get_logger

logger = get_logger("snowflake.agent")

_ANALYST_PATH = "/api/v2/cortex/analyst/message"
_TIMEOUT = 60.0
_MAX_NARRATE_ROWS = 50

_DISABLED_MSG = (
    "The Cortex Agent isn't connected right now. Ask an administrator to enable the "
    "Snowflake Cortex integration to chat with your evidence-monitoring data."
)


def is_enabled() -> bool:
    s = get_settings()
    return bool(s.snowflake_cortex_agent_enabled and client.is_enabled())


def _to_analyst_messages(message: str, history: list[dict]) -> list[dict]:
    """Convert UI chat turns into the Cortex Analyst messages payload.

    ``history`` is a list of ``{"role": "user"|"assistant", "content": str}``. Cortex
    Analyst expects role ``analyst`` for prior answers and a typed ``content`` array.
    """
    messages: list[dict] = []
    for turn in history:
        role = "analyst" if turn.get("role") == "assistant" else "user"
        text = (turn.get("content") or "").strip()
        if text:
            messages.append({"role": role, "content": [{"type": "text", "text": text}]})
    messages.append({"role": "user", "content": [{"type": "text", "text": message}]})
    return messages


async def _call_analyst(message: str, history: list[dict]) -> dict:
    """POST the conversation to the Cortex Analyst REST API. Returns parsed JSON."""
    s = get_settings()
    token = jwt_auth.generate_jwt()
    url = jwt_auth.rest_base_url(s.snowflake_account) + _ANALYST_PATH
    payload: dict = {"messages": _to_analyst_messages(message, history)}
    # Prefer the complete staged YAML model when configured; otherwise the native view.
    if s.snowflake_semantic_model_file:
        payload["semantic_model_file"] = s.snowflake_semantic_model_file
    else:
        payload["semantic_view"] = s.snowflake_semantic_view
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Snowflake-Authorization-Token-Type": "KEYPAIR_JWT",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
        resp = await http.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"Cortex Analyst HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.json()


def _extract(content: list[dict]) -> tuple[str, str, list[str]]:
    """Pull (analyst_text, sql, suggestions) out of a Cortex Analyst content array."""
    text, sql, suggestions = "", "", []
    for part in content or []:
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text", "") or text
        elif ptype == "sql":
            sql = part.get("statement", "") or sql
        elif ptype == "suggestions":
            suggestions = part.get("suggestions", []) or suggestions
    return text, sql, suggestions


async def _narrate(question: str, rows: list[dict]) -> str:
    """Turn result rows into a short, plain-English answer via CORTEX.COMPLETE."""
    preview = rows[:_MAX_NARRATE_ROWS]
    prompt = (
        "You are a friendly analyst assisting a non-technical pharmaceutical Medical "
        "Affairs user. Answer the user's question in 2-4 plain-English sentences using "
        "ONLY the data rows below. Cite concrete numbers and brand/model names. The rows "
        "are the result of the user's question and may already be sorted, filtered, or "
        "limited to the most relevant records, so give a direct, natural answer to the "
        "question. Do NOT assume they are the complete dataset, do NOT speculate about "
        "data that is not shown, and do NOT describe how many rows or results were "
        "returned. Do NOT "
        "mention SQL, databases, tables, or queries. If the rows are empty, say you "
        f"couldn't find any matching data.\n\nQUESTION: {question}\n\nDATA (JSON):\n{preview}"
    )
    try:
        answer = await cortex.cortex_complete(prompt, max_tokens=500)
        return answer.strip()
    except cortex.CortexLLMUnavailable:
        # No LLM narration available — fall back to a compact factual summary.
        if not rows:
            return "I couldn't find any matching data for that question."
        return f"Here's what I found ({len(rows)} result(s)): {preview}"
    except Exception as e:  # noqa: BLE001
        logger.warning("Narration failed: %s", e)
        if not rows:
            return "I couldn't find any matching data for that question."
        return f"Here's what I found ({len(rows)} result(s)): {preview}"


_CAPABILITIES = (
    "brand sentiment, how AI models position our brands competitively, model "
    "consensus, alerts, and monitoring run cost/volume"
)


async def _general_answer(question: str, analyst_text: str) -> str:
    """Fallback when Cortex Analyst can't ground a question to SQL.

    Answers general/conceptual questions and explains what the agent can help with via
    CORTEX.COMPLETE — without inventing specific data values.
    """
    prompt = (
        "You are the assistant for the Evidence Monitoring Agent, which tracks how AI "
        "models (Claude, Gemini, Llama, Nova, GPT-4o, etc.) describe AbbVie brands across "
        "personas and therapeutic areas. It can report on " + _CAPABILITIES + ". Answer "
        "the user's question helpfully in 2-4 plain-English sentences. If the question "
        "asks for specific data or numbers, do NOT invent them; instead briefly explain "
        "what you can answer and suggest how to rephrase. Do NOT mention SQL, tables, or "
        "databases.\n\nQUESTION: " + question
    )
    try:
        return (await cortex.cortex_complete(prompt, max_tokens=400)).strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("General fallback failed: %s", e)
        return analyst_text or (
            "I'm not sure how to answer that from the evidence-monitoring data. Try "
            "asking about " + _CAPABILITIES + "."
        )


async def chat(message: str, history: list[dict] | None = None) -> dict:
    """Answer a plain-English question over the evidence-monitoring data.

    Returns ``{"enabled": bool, "answer": str, "error"?: str}``. The answer is always
    plain English; SQL is never surfaced to the caller.
    """
    history = history or []
    if not is_enabled():
        return {"enabled": False, "answer": _DISABLED_MSG}

    # 1. Ask Cortex Analyst for grounded SQL (+ any analyst text / follow-up suggestions).
    try:
        data = await _call_analyst(message, history)
    except Exception as e:  # noqa: BLE001
        logger.warning("Cortex Analyst call failed: %s", e)
        return {
            "enabled": True,
            "answer": "Sorry, I couldn't reach the data service just now. Please try again in a moment.",
            "error": str(e),
        }

    content = (data.get("message") or {}).get("content", [])
    analyst_text, sql, suggestions = _extract(content)

    # 2. No SQL grounded — fall back to a general-LLM answer instead of refusing.
    if not sql:
        answer = await _general_answer(message, analyst_text)
        return {"enabled": True, "answer": answer, "suggestions": suggestions}

    # 3. Execute the grounded SQL (read-only by construction from the semantic view).
    try:
        rows = await client.execute(sql)
    except Exception as e:  # noqa: BLE001
        logger.warning("Cortex Analyst SQL failed: %s", e)
        return {
            "enabled": True,
            "answer": "I understood your question but ran into trouble pulling the data. "
            "Try rephrasing it a little.",
            "error": str(e),
        }

    # 4. Narrate rows into a friendly, plain-English answer.
    answer = await _narrate(message, rows)
    return {"enabled": True, "answer": answer, "suggestions": suggestions}
