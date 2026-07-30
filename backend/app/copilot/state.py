"""Copilot agent state + public response schema.

``AgentState`` is the LangGraph state (TypedDict). Public-facing JSON
contracts returned by the API are Pydantic so the OpenAPI schema is
auto-generated.
"""
from __future__ import annotations

import enum
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field


class IntentEnum(str, enum.Enum):
    """Router output classes."""

    ACTION = "ACTION"
    """Do something via a (confirmed) write tool."""
    DATA = "DATA"
    """Answer a question about the app's data via read tools."""
    HELP = "HELP"
    """Explain how to use the app (curated guide)."""
    OFF_TOPIC = "OFF_TOPIC"
    """Greeting / out-of-scope / unsupported."""


def add_messages(left: list[Any] | None, right: list[Any] | None) -> list[Any]:
    """Reducer: append right-hand messages to the running history."""
    return [*(left or []), *(right or [])]


class AgentState(TypedDict, total=False):
    """LangGraph state. Every node reads + writes a subset of these."""

    trace_id: str
    messages: Annotated[list[Any], add_messages]
    ui_context: dict[str, Any]
    intent: IntentEnum
    fast_path_hit: bool

    # Tool execution
    tools_used: list[str]
    tool_calls: list[dict[str, Any]]
    react_iter: int
    tool_just_ran: bool
    last_tool_result: dict[str, Any] | None

    # UI integration
    ui_action: dict[str, Any] | None
    prompt_options: dict[str, Any] | None
    """Set when a tool result offers a fixed set of choices; the UI renders it as
    a dropdown so the user can pick a value instead of typing it."""

    # Confirmed-writes
    pending_action: dict[str, Any] | None
    """Set when a mutating tool needs user confirmation. Shape:
    ``{token, tool_name, args, summary, issued_at, governance, reviewer_required}``."""
    confirmed_tool: dict[str, Any] | None
    """Set on the /confirm path so the executor runs the approved write."""

    # Guardrails
    guardrail_flags: list[str]
    refusal_card: dict[str, Any] | None


# ---------------------------------------------------------------------------
# Public response schema
# ---------------------------------------------------------------------------
class AgentMessageOut(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["user", "assistant", "tool"]
    content: str
    tool_name: str | None = None


class ToolCallSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str
    elapsed_ms: int
    ok: bool
    summary: str


class PendingField(BaseModel):
    """One human-readable parameter shown on the confirmation card.

    ``value`` is already formatted for display (e.g. unset filters render as
    "All", unset limits as "Auto (default)") so the UI can show it verbatim.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    label: str
    value: str
    editable: bool = False
    type: Literal["text", "select", "number", "boolean"] = "text"
    options: list[str] = Field(default_factory=list)
    allow_empty: bool = False
    raw: Any = None


class PendingAction(BaseModel):
    """A write action awaiting user confirmation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    token: str
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    summary: str
    issued_at: float
    trace_id: str
    governance: bool = False
    nav_target: str | None = None
    fields: list[PendingField] = Field(default_factory=list)
    """The action's effective parameters/options, shown before the user confirms."""
    presets: list[dict[str, Any]] = Field(default_factory=list)
    """Optional one-click "quick fill" presets ({label, description, args}) the UI
    can apply via /copilot/preview to re-mint this action with a common config."""


class PromptOption(BaseModel):
    """One selectable choice in a :class:`PromptOptions` dropdown."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str
    label: str
    hint: str | None = None


class PromptOptions(BaseModel):
    """A structured single-choice prompt the UI renders as a dropdown so the user
    can pick a value (e.g. a therapeutic area) instead of typing it. Selecting an
    option sends ``send_template`` (with ``{value}`` substituted) as the next
    message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str = ""
    param: str | None = None
    options: list[PromptOption] = Field(default_factory=list)
    send_template: str | None = None


class AgentResponse(BaseModel):
    """JSON returned by ``POST /copilot/chat`` and the SSE ``done`` event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str
    intent: IntentEnum
    messages: list[AgentMessageOut]
    tool_calls: list[ToolCallSummary] = Field(default_factory=list)
    ui_action: dict[str, Any] | None = None
    pending_action: PendingAction | None = None
    prompt_options: PromptOptions | None = None
    guardrail_flags: list[str] = Field(default_factory=list)
    refusal_card: dict[str, Any] | None = None
