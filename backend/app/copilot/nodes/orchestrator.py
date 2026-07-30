"""Orchestrator node — async ReAct loop over the tool registry."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from app.copilot.prompts import ORCHESTRATOR_SYSTEM
from app.copilot.providers.base import LLMMessage, LLMProvider, LLMResponse
from app.copilot.state import AgentState, IntentEnum
from app.copilot.tools.registry import anthropic_tool_schemas

LOG = logging.getLogger("copilot.orchestrator")


def make_orchestrator_node(
    provider: LLMProvider,
) -> Callable[[AgentState], Awaitable[dict[str, Any]]]:
    async def orchestrator_node(state: AgentState) -> dict[str, Any]:
        msgs = list(state.get("messages") or [])
        convo = _to_llm_messages(msgs)
        if not convo:
            return {
                "messages": [
                    {
                        "role": "assistant",
                        "content": "What would you like to do? You can ask about your runs, questions, and results, or tell me to start a run or discover new questions.",
                        "tool_calls": [],
                    }
                ]
            }

        already_called = _has_prior_tool_calls(msgs)
        intent = state.get("intent")
        force_tool = not already_called and intent in (IntentEnum.ACTION, IntentEnum.DATA)
        tool_choice = {"type": "any"} if force_tool else None

        system = ORCHESTRATOR_SYSTEM
        preamble = _ui_context_preamble(state.get("ui_context"))
        if preamble:
            system = f"{system}\n\n{preamble}"

        resp = await _invoke_or_fallback(provider, convo, system=system, tool_choice=tool_choice)
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": resp.text or "",
            "tool_calls": [tc.model_dump() for tc in resp.tool_calls],
        }
        return {"messages": [assistant_msg]}

    return orchestrator_node


def route_after_orchestrator(state: AgentState, *, react_iter_cap: int) -> str:
    """tool_executor when the last assistant turn issued tool calls and we're
    under the iteration cap; otherwise finish at the validator."""
    messages = state.get("messages") or []
    last_tool_calls: list[Any] = []
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "assistant":
            last_tool_calls = m.get("tool_calls") or []
            break
    if last_tool_calls and int(state.get("react_iter") or 0) < react_iter_cap:
        return "tool_executor"
    return "validator"


# ---------------------------------------------------------------------------
def _ui_context_preamble(ui_context: dict[str, Any] | None) -> str:
    if not ui_context:
        return ""
    page = ui_context.get("path") or ui_context.get("page")
    if not page:
        return ""
    return f"CONTEXT: the user is currently on the '{page}' page of the app."


def _has_prior_tool_calls(messages: list[Any]) -> bool:
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls"):
            return True
    return False


def _to_llm_messages(messages: list[Any]) -> list[LLMMessage]:
    out: list[LLMMessage] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role", "")).lower()
        content = str(m.get("content", ""))
        if role == "user" and content:
            out.append(LLMMessage(role="user", content=content))
        elif role == "assistant":
            tool_calls = m.get("tool_calls")
            if not content and not tool_calls:
                continue
            out.append(
                LLMMessage(
                    role="assistant",
                    content=content,
                    tool_calls=list(tool_calls) if tool_calls else None,
                )
            )
        elif role == "tool" and content:
            out.append(
                LLMMessage(role="tool", content=content, tool_call_id=m.get("tool_call_id"))
            )
    return out


async def _invoke_or_fallback(
    provider: LLMProvider,
    convo: list[LLMMessage],
    *,
    system: str,
    tool_choice: dict[str, Any] | None,
) -> LLMResponse:
    try:
        return await provider.invoke(
            convo,
            system=system,
            temperature=0.2,
            max_tokens=2000,
            tools=anthropic_tool_schemas(),
            tool_choice=tool_choice,
        )
    except Exception as exc:  # noqa: BLE001 — provider/network errors
        LOG.warning("Orchestrator LLM call failed: %s; using fallback.", exc)
        return LLMResponse(
            text="I hit a transient issue. Please try your request again in a moment.",
            tool_calls=[],
        )
