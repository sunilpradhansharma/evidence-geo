"""Raw API input/output capture into the Snowflake APP_EVENTS table.

Satisfies the requirement that "any input and output" of the application is stored in
Snowflake. A FastAPI middleware records each request/response (method, path, status,
duration, and credential-redacted bodies) as a fire-and-forget background task so it never
adds latency to the user response. Disabled when Snowflake is off or
SNOWFLAKE_CAPTURE_EVENTS=false.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from app.config.settings import get_settings
from app.snowflake import client
from app.utils.logging import get_logger, redact_phi

logger = get_logger("snowflake.events")

# Paths we never capture: docs/health noise, plus credential-bearing auth routes
# (login/register carry plaintext passwords + issue JWTs) which must never be persisted
# to Snowflake. Auth activity is still audited safely via the AUDIT_LOG table.
_SKIP_PREFIXES = ("/docs", "/openapi", "/redoc", "/snowflake/status", "/health", "/auth/")
_MAX_BODY = 100_000  # chars; bodies larger than this are truncated


def should_capture() -> bool:
    s = get_settings()
    return bool(s.snowflake_enabled and s.snowflake_capture_events and client.is_enabled())


def _clip(body: str | None) -> str | None:
    if body is None:
        return None
    body = redact_phi(body)  # G6: strip credentials AND PHI/PII before persisting bodies
    if len(body) > _MAX_BODY:
        return body[:_MAX_BODY] + "...[truncated]"
    return body


async def record_event(
    *, method: str, path: str, status_code: int, duration_ms: int,
    request_body: str | None, response_body: str | None, client_host: str | None,
) -> None:
    try:
        await client.execute(
            "INSERT INTO APP_EVENTS "
            "(EVENT_ID, TS, METHOD, PATH, STATUS_CODE, DURATION_MS, "
            "REQUEST_BODY, RESPONSE_BODY, CLIENT_HOST) "
            "SELECT %s, %s, %s, %s, %s, %s, TRY_PARSE_JSON(%s), TRY_PARSE_JSON(%s), %s",
            (
                str(uuid.uuid4()),
                datetime.now(timezone.utc).replace(tzinfo=None),
                method, path, status_code, duration_ms,
                _clip(request_body), _clip(response_body), client_host,
            ),
        )
    except Exception as e:  # noqa: BLE001 — capture must never break the app
        logger.warning("APP_EVENTS capture failed for %s %s: %s", method, path, e)


def fire_and_forget(coro) -> None:
    """Schedule capture without awaiting; hold a ref so it isn't GC'd."""
    task = asyncio.create_task(coro)
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


_PENDING: set[asyncio.Task] = set()


def is_skipped(path: str) -> bool:
    return any(path.startswith(p) for p in _SKIP_PREFIXES)
