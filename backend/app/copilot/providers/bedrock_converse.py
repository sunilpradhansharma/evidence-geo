"""Bedrock Converse provider with tool-use for the copilot agent.

Reuses the same AWS region/credentials/model settings the rest of the app
already trusts (``app.config.settings``), and the same Converse path the
target/scoring clients use. The registry hands us Anthropic-shaped tool
schemas (``{name, description, input_schema}``); we translate them into
Converse ``toolConfig`` and translate ``toolUse`` blocks back into
:class:`ToolCall`. Synchronous boto3 calls run in a thread so the node layer
stays async.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import boto3
from botocore.config import Config

from app.config.settings import get_settings
from app.copilot.providers.base import (
    LLMMessage,
    LLMResponse,
    LLMUsage,
    ToolCall,
)

LOG = logging.getLogger("copilot.bedrock")


class BedrockConverseProvider:
    """Production provider. Talks to Bedrock via the Converse API."""

    def __init__(self, *, model_id: str, region: str) -> None:
        settings = get_settings()
        self._model_id = model_id
        self._region = region
        self._client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            aws_access_key_id=settings.aws_access_key_id or None,
            aws_secret_access_key=settings.aws_secret_access_key or None,
            config=Config(
                retries={"max_attempts": 2, "mode": "standard"},
                read_timeout=120,
                connect_timeout=15,
            ),
        )

    @property
    def model_id(self) -> str:
        return self._model_id

    async def invoke(
        self,
        messages: list[LLMMessage],
        *,
        system: str = "",
        temperature: float = 0.0,
        max_tokens: int = 1024,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
    ) -> LLMResponse:
        kwargs = self._build_kwargs(
            messages=messages,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
        )
        t0 = time.perf_counter()
        resp = await asyncio.to_thread(self._client.converse, **kwargs)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return _parse_response(resp, self._model_id, latency_ms)

    # ------------------------------------------------------------------
    def _build_kwargs(
        self,
        *,
        messages: list[LLMMessage],
        system: str,
        temperature: float,
        max_tokens: int,
        tools: list[dict[str, Any]] | None,
        tool_choice: dict[str, Any] | None,
    ) -> dict[str, Any]:
        converse_messages = _to_converse_messages(messages, system)
        # Any inline system messages were folded into ``system`` already.
        sys_text = system
        inference_config: dict[str, Any] = {"maxTokens": max_tokens}
        if temperature is not None:
            inference_config["temperature"] = temperature

        kwargs: dict[str, Any] = {
            "modelId": self._model_id,
            "messages": converse_messages,
            "inferenceConfig": inference_config,
        }
        if sys_text:
            kwargs["system"] = [{"text": sys_text}]
        if tools:
            tool_config: dict[str, Any] = {"tools": _to_converse_tools(tools)}
            converse_choice = _to_converse_tool_choice(tool_choice)
            if converse_choice is not None:
                tool_config["toolChoice"] = converse_choice
            kwargs["toolConfig"] = tool_config
        return kwargs


# ---------------------------------------------------------------------------
# Translation helpers
# ---------------------------------------------------------------------------
def _to_converse_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic ``{name, description, input_schema}`` -> Converse toolSpec."""
    out: list[dict[str, Any]] = []
    for t in tools:
        out.append(
            {
                "toolSpec": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "inputSchema": {"json": t.get("input_schema", {"type": "object"})},
                }
            }
        )
    return out


def _to_converse_tool_choice(
    tool_choice: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not tool_choice:
        return None
    ttype = tool_choice.get("type")
    if ttype == "any":
        return {"any": {}}
    if ttype == "auto":
        return {"auto": {}}
    if ttype == "tool" and tool_choice.get("name"):
        return {"tool": {"name": tool_choice["name"]}}
    return None


def _to_converse_messages(
    messages: list[LLMMessage], system: str
) -> list[dict[str, Any]]:
    """Build the Converse ``messages`` list.

    ``tool`` results must ride inside a ``user`` message immediately after
    the assistant ``toolUse`` turn, so we merge consecutive tool-role
    messages into a single user message with multiple ``toolResult`` blocks.
    """
    out: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    def flush_tool_results() -> None:
        nonlocal pending_tool_results
        if pending_tool_results:
            out.append({"role": "user", "content": pending_tool_results})
            pending_tool_results = []

    for m in messages:
        if m.role == "system":
            # Folded into the top-level system prompt by the caller; ignore.
            continue
        if m.role == "tool":
            pending_tool_results.append(_tool_result_block(m))
            continue
        # Non-tool message: flush any buffered tool results first.
        flush_tool_results()
        if m.role == "user":
            if m.content:
                out.append({"role": "user", "content": [{"text": m.content}]})
        elif m.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"text": m.content})
            for tc in m.tool_calls or []:
                blocks.append(
                    {
                        "toolUse": {
                            "toolUseId": tc.get("id", ""),
                            "name": tc.get("name", ""),
                            "input": tc.get("input", {}) or {},
                        }
                    }
                )
            if blocks:
                out.append({"role": "assistant", "content": blocks})
    flush_tool_results()
    return out


def _tool_result_block(m: LLMMessage) -> dict[str, Any]:
    """Build a Converse ``toolResult`` content block from a tool message."""
    content_block: dict[str, Any]
    try:
        parsed = json.loads(m.content)
        content_block = {"json": parsed} if isinstance(parsed, dict) else {"text": m.content}
    except (json.JSONDecodeError, TypeError):
        content_block = {"text": m.content}
    return {
        "toolResult": {
            "toolUseId": m.tool_call_id or "",
            "content": [content_block],
        }
    }


def _parse_response(resp: dict[str, Any], model_id: str, latency_ms: int) -> LLMResponse:
    stop_reason = resp.get("stopReason", "end_turn")
    message = resp.get("output", {}).get("message", {})
    blocks = message.get("content", []) or []
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for blk in blocks:
        if not isinstance(blk, dict):
            continue
        if "text" in blk:
            text_parts.append(str(blk.get("text", "")))
        elif "toolUse" in blk:
            tu = blk["toolUse"]
            tool_calls.append(
                ToolCall(
                    id=str(tu.get("toolUseId", "")),
                    name=str(tu.get("name", "")),
                    input=dict(tu.get("input", {}) or {}),
                )
            )
    usage = resp.get("usage", {})
    return LLMResponse(
        text="".join(text_parts),
        tool_calls=tool_calls,
        stop_reason=str(stop_reason),
        usage=LLMUsage(
            prompt_tokens=int(usage.get("inputTokens", 0)),
            completion_tokens=int(usage.get("outputTokens", 0)),
        ),
        latency_ms=latency_ms,
        model_id=model_id,
    )
