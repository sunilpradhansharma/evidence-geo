"""Tool registry — single source of truth for the copilot's tools.

Aggregates the per-domain ``SPECS`` lists, exposes ``get_tool`` for the
executor and ``anthropic_tool_schemas`` (which the Bedrock provider adapts to
Converse ``toolConfig``). JSON schemas are generated from each tool's Pydantic
input class so the wire schema can never drift from the Python validation.
"""
from __future__ import annotations

from typing import Any

from app.copilot.tools import (
    activation_tools,
    backfill_tools,
    evidence_tools,
    help_tools,
    insight_tools,
    openevidence_tools,
    question_tools,
    read_tools,
    review_tools,
    run_tools,
    social_tools,
)
from app.copilot.tools.schemas import ToolSpec

_ALL_SPECS: list[ToolSpec] = [
    *read_tools.SPECS,
    *help_tools.SPECS,
    *run_tools.SPECS,
    *question_tools.SPECS,
    *review_tools.SPECS,
    *insight_tools.SPECS,
    *openevidence_tools.SPECS,
    *social_tools.SPECS,
    *backfill_tools.SPECS,
    *evidence_tools.SPECS,
    *activation_tools.SPECS,
]

TOOLS: dict[str, ToolSpec] = {spec.name: spec for spec in _ALL_SPECS}


def get_tool(name: str) -> ToolSpec:
    if name not in TOOLS:
        raise KeyError(f"Unknown tool {name!r}. Registered: {sorted(TOOLS)}.")
    return TOOLS[name]


def anthropic_tool_schemas() -> list[dict[str, Any]]:
    """Return the ``{name, description, input_schema}`` list for the provider."""
    out: list[dict[str, Any]] = []
    for spec in TOOLS.values():
        schema = spec.input_schema.model_json_schema()
        schema.pop("title", None)
        out.append(
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": schema,
            }
        )
    return out
