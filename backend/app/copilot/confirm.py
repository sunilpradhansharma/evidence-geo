"""Confirmed-writes: HMAC-signed action tokens.

A mutating tool never auto-executes. The executor mints a token over the
canonical (tool_name, args, trace_id, issued_at) and returns a pending
action; the UI shows a Confirm card; ``POST /copilot/confirm`` re-verifies
the token so the executed action is byte-identical to the previewed one
(no tampering between preview and execute).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from functools import lru_cache

from app.config.settings import get_settings

# Tokens older than this (seconds) are rejected on confirm.
_MAX_AGE_S = 30 * 60


@lru_cache
def _secret() -> bytes:
    """Return the signing secret. Prefer the configured value; otherwise a
    per-process random secret (fine for the single-process POC — a restart
    just invalidates any in-flight confirm tokens)."""
    configured = (get_settings().copilot_action_secret or "").strip()
    if configured:
        return configured.encode("utf-8")
    return secrets.token_bytes(32)


def canonical_args(args: dict) -> str:
    return json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=str)


def mint_token(tool_name: str, args: dict, trace_id: str, issued_at: float) -> str:
    material = f"{tool_name}\n{canonical_args(args)}\n{trace_id}\n{issued_at}"
    return hmac.new(_secret(), material.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_token(
    token: str, tool_name: str, args: dict, trace_id: str, issued_at: float
) -> bool:
    if not token:
        return False
    if (time.time() - float(issued_at)) > _MAX_AGE_S:
        return False
    expected = mint_token(tool_name, args, trace_id, issued_at)
    return hmac.compare_digest(expected, token)
