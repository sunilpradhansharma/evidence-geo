"""Tool input/output schemas.

Every tool's output is wrapped in :class:`ToolResultData` so the executor can
attach metadata uniformly. ``data`` is JSON-safe (dumped via
``model_dump(mode='json')`` upstream) so it round-trips through the LLM
tool_result block and the SSE wire.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field


class ToolInput(BaseModel):
    """Base for every tool's input schema."""

    model_config = ConfigDict(extra="ignore")


class ToolResultData(BaseModel):
    """Envelope returned by every tool dispatch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str
    ok: bool
    summary: str = Field(
        description="One-sentence human-readable summary for the UI tool chip."
    )
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    nav_target: str | None = Field(
        default=None,
        description="When set (after a confirmed write), the UI navigates here.",
    )
    job: dict[str, Any] | None = Field(
        default=None,
        description="When set, a long-running background job the UI should poll "
        "for completion, e.g. {'kind': 'harvest'} or {'kind': 'run', 'run_id': '...'}.",
    )
    prompt_options: dict[str, Any] | None = Field(
        default=None,
        description="When set, a structured single-choice prompt the UI renders as a "
        "dropdown so the user can pick a value instead of typing it. Shape: "
        "{prompt, param, options:[{value,label,hint}], send_template}. Never shown "
        "in the tool chip; surfaced alongside the assistant's question.",
    )


@dataclass(frozen=True)
class ToolSpec:
    """One tool's wiring metadata.

    ``callable`` is an async function accepting the validated input model and
    returning a :class:`ToolResultData`. ``mutating`` tools are intercepted by
    the executor for confirmation; ``governance`` tools additionally require a
    reviewer/approver name. ``nav_target`` is the page the UI navigates to
    after the action is confirmed and executed.
    """

    name: str
    description: str
    input_schema: type[BaseModel]
    callable: Callable[[Any], Awaitable[ToolResultData]]
    mutating: bool = False
    governance: bool = False
    nav_target: str | None = None
