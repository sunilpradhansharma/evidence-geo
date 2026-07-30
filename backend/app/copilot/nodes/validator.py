"""Validator node — final guardrail before END.

Light-touch for now: guarantee the turn ends with a non-empty assistant
message the UI can render. If the Orchestrator finished with an empty turn but
tools ran, synthesize a short answer from the last tool summary. This is the
seam where deterministic compliance checks can be added later.
"""
from __future__ import annotations

from typing import Any

from app.copilot.state import AgentState


def validator_node(state: AgentState) -> dict[str, Any]:
    messages = state.get("messages") or []
    if _has_final_assistant_text(messages):
        return {}

    # No usable assistant prose this turn — synthesize a fallback.
    if state.get("pending_action"):
        summary = (state.get("pending_action") or {}).get("summary", "this action")
        text = f"I'm ready to do this: {summary} Please confirm."
    else:
        summaries = state.get("tool_calls") or []
        ok_summaries = [s.get("summary") for s in summaries if s.get("ok") and s.get("summary")]
        if ok_summaries:
            text = ok_summaries[-1]
        else:
            text = "I couldn't complete that. Please rephrase or try a different request."
    return {"messages": [{"role": "assistant", "content": text, "tool_calls": []}]}


def _has_final_assistant_text(messages: list[Any]) -> bool:
    """True if there's an assistant message with real prose after the last user turn."""
    seen_user = False
    found = False
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "user":
            seen_user = True
            found = False
        elif role == "assistant" and str(m.get("content", "")).strip():
            found = True
    return seen_user and found
