"""LLMProvider protocol + canonical request/response models (async).

Ported from the reference agent, adapted to async because the Evidence
Monitoring Agent is a fully-async FastAPI app and the copilot tools call
async services. The provider exposes a single async ``invoke`` used by the
graph nodes; token/latency accounting rides on :class:`LLMResponse` so the
audit row can record cost without provider-specific shapes.
"""
from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class LLMMessage(BaseModel):
    """One turn in a chat-completion request.

    Tool messages carry the ``tool_call_id`` the model emitted so the
    assistant can resolve which call this is replying to. Assistant turns
    that issued tool calls carry the raw ``tool_calls`` list so the provider
    can rebuild Bedrock ``toolUse`` content blocks on subsequent passes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class LLMUsage(BaseModel):
    """Token usage from one provider call. Forwarded into the audit row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_tokens: int = 0
    completion_tokens: int = 0


class ToolCall(BaseModel):
    """One ``toolUse`` block extracted from a Bedrock Converse response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    """Single non-streaming completion result."""

    model_config = ConfigDict(frozen=True, extra="forbid", protected_namespaces=())

    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: str = Field(default="end_turn")
    usage: LLMUsage = Field(default_factory=LLMUsage)
    latency_ms: int = 0
    model_id: str = ""


@runtime_checkable
class LLMProvider(Protocol):
    """Minimal async LLM provider surface. Bedrock + Fake implement this."""

    @property
    def model_id(self) -> str:
        ...

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
        """Run a single non-streaming completion.

        ``tools`` is the Anthropic-shaped payload (a list of
        ``{name, description, input_schema}`` dicts); the provider adapts it
        to the backend's native tool format. ``tool_choice`` overrides the
        default 'auto' policy: ``{"type": "any"}`` forces SOME tool,
        ``{"type": "tool", "name": "X"}`` forces a specific tool.
        """
        ...
