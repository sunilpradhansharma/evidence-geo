"""Minimal per-turn audit logging for the copilot.

Never raises. Writes a structured log line capturing the turn outcome so an
operator can trace what the copilot did (and who approved any write).
"""
from __future__ import annotations

from typing import Any

from app.utils.logging import get_logger

logger = get_logger("copilot.audit")


def write_audit(
    state: dict[str, Any],
    *,
    user_message: str,
    elapsed_ms: int,
    model_id: str | None = None,
    actor: str | None = None,
) -> None:
    try:
        intent = state.get("intent")
        intent_str = intent.value if hasattr(intent, "value") else str(intent)
        pending = state.get("pending_action") or None
        logger.info(
            "copilot turn trace_id=%s intent=%s tools=%s react_iter=%s pending=%s actor=%s model=%s elapsed_ms=%d msg=%r",
            state.get("trace_id"),
            intent_str,
            state.get("tools_used") or [],
            state.get("react_iter") or 0,
            (pending or {}).get("tool_name"),
            actor,
            model_id,
            elapsed_ms,
            (user_message or "")[:200],
        )
    except Exception:  # noqa: BLE001 — audit must never break a turn
        pass


def write_confirm_audit(
    *, trace_id: str, tool_name: str, ok: bool, actor: str | None, summary: str
) -> None:
    try:
        logger.info(
            "copilot confirm trace_id=%s tool=%s ok=%s actor=%s summary=%r",
            trace_id, tool_name, ok, actor, (summary or "")[:200],
        )
    except Exception:  # noqa: BLE001
        pass
