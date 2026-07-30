"""Adapt the final LangGraph state into the public AgentResponse."""
from __future__ import annotations

from typing import Any

from app.copilot.state import (
    AgentMessageOut,
    AgentResponse,
    IntentEnum,
    PendingAction,
    PromptOptions,
    ToolCallSummary,
)


def state_to_response(state: dict[str, Any]) -> AgentResponse:
    messages = state.get("messages") or []
    assistant_text = _assistant_text_after_last_user(messages)

    out_messages: list[AgentMessageOut] = []
    if assistant_text:
        out_messages.append(AgentMessageOut(role="assistant", content=assistant_text))

    tool_calls: list[ToolCallSummary] = []
    for s in state.get("tool_calls") or []:
        try:
            tool_calls.append(
                ToolCallSummary(
                    tool_name=str(s.get("tool_name", "")),
                    elapsed_ms=int(s.get("elapsed_ms", 0)),
                    ok=bool(s.get("ok", False)),
                    summary=str(s.get("summary", "")),
                )
            )
        except Exception:  # noqa: BLE001 — defensive; never break the response
            continue

    pending = None
    pa = state.get("pending_action")
    if pa:
        try:
            pending = PendingAction(**pa)
        except Exception:  # noqa: BLE001
            pending = None

    prompt_options = None
    po = state.get("prompt_options")
    if po and isinstance(po, dict):
        try:
            prompt_options = PromptOptions(**po)
        except Exception:  # noqa: BLE001 — never break the response over a picker
            prompt_options = None

    intent = state.get("intent") or IntentEnum.OFF_TOPIC

    return AgentResponse(
        trace_id=str(state.get("trace_id") or ""),
        intent=intent,
        messages=out_messages,
        tool_calls=tool_calls,
        ui_action=state.get("ui_action") or None,
        pending_action=pending,
        prompt_options=prompt_options,
        guardrail_flags=list(state.get("guardrail_flags") or []),
        refusal_card=state.get("refusal_card") or None,
    )


def _assistant_text_after_last_user(messages: list[Any]) -> str:
    last_user_idx = -1
    for i, m in enumerate(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            last_user_idx = i
    parts: list[str] = []
    for m in messages[last_user_idx + 1 :]:
        if isinstance(m, dict) and m.get("role") == "assistant":
            c = str(m.get("content", "")).strip()
            if c:
                parts.append(c)
    return "\n\n".join(parts)
