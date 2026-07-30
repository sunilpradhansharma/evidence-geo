"""Analyst node — answers HELP (how-to) and OFF_TOPIC turns.

HELP turns are grounded in the curated help corpus (no tools). OFF_TOPIC turns
get a short templated redirect (no LLM call).
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from app.copilot.help import search_help
from app.copilot.prompts import ANALYST_SYSTEM, OFF_TOPIC_REPLY
from app.copilot.providers.base import LLMMessage, LLMProvider
from app.copilot.state import AgentState, IntentEnum

LOG = logging.getLogger("copilot.analyst")


def make_analyst_node(
    provider: LLMProvider,
) -> Callable[[AgentState], Awaitable[dict[str, Any]]]:
    async def analyst_node(state: AgentState) -> dict[str, Any]:
        if state.get("intent") == IntentEnum.OFF_TOPIC:
            return {"messages": [{"role": "assistant", "content": OFF_TOPIC_REPLY, "tool_calls": []}]}

        text = _last_user_text(state.get("messages") or [])
        sections = search_help(text)
        context = "\n\n".join(f"## {s['title']}\n{s['body']}" for s in sections)
        system = f"{ANALYST_SYSTEM}\n\nHELP CONTEXT:\n{context}"
        try:
            resp = await provider.invoke(
                [LLMMessage(role="user", content=text or "How do I use this app?")],
                system=system,
                temperature=0.2,
                max_tokens=600,
            )
            answer = resp.text.strip()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Analyst LLM call failed: %s; returning raw help.", exc)
            answer = ""
        if not answer:
            # Fall back to the curated section bodies verbatim.
            answer = "\n\n".join(f"**{s['title']}**\n{s['body']}" for s in sections)
        return {"messages": [{"role": "assistant", "content": answer, "tool_calls": []}]}

    return analyst_node


def _last_user_text(messages: list[Any]) -> str:
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            return str(m.get("content", ""))
    return ""
