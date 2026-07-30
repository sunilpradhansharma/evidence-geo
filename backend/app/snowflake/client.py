"""Async-safe Snowflake connection + query helpers.

The official Snowflake connector is synchronous, and Snowflake is a warehouse (poor for
high-frequency single-row OLTP). So we keep it OFF the app's hot async write path: every
call here runs in a worker thread via ``asyncio.to_thread`` and the app's live writes
still go to SQLite. Data reaches Snowflake through the batched mirror (see ``mirror.py``).

Connection is a lazy, reused singleton built from settings. Key-pair auth is preferred
(no MFA prompts for the unattended prod backend); a password fallback is supported for a
quick POC. When Snowflake is disabled or misconfigured, ``is_enabled()`` is False and the
query helpers raise ``SnowflakeDisabled`` — callers treat that as a no-op.
"""
from __future__ import annotations

import asyncio
import base64
import threading
from typing import Any

from app.config.settings import get_settings
from app.utils.logging import get_logger

logger = get_logger("snowflake.client")

_conn: Any = None
_conn_lock = threading.Lock()
_private_key_der: bytes | None = None


class SnowflakeDisabled(RuntimeError):
    """Raised when a Snowflake call is attempted but the integration is disabled."""


def is_enabled() -> bool:
    """True only when enabled AND minimally configured (account + user + an auth method)."""
    s = get_settings()
    if not s.snowflake_enabled:
        return False
    if not (s.snowflake_account and s.snowflake_user):
        return False
    has_keypair = bool(s.snowflake_private_key_path or s.snowflake_private_key_b64)
    has_password = bool(s.snowflake_password)
    return has_keypair or has_password


def _load_private_key() -> bytes | None:
    """Load + serialize the RSA private key to DER (what the connector wants)."""
    global _private_key_der
    if _private_key_der is not None:
        return _private_key_der

    s = get_settings()
    raw: bytes | None = None
    if s.snowflake_private_key_b64:
        raw = base64.b64decode(s.snowflake_private_key_b64)
    elif s.snowflake_private_key_path:
        with open(s.snowflake_private_key_path, "rb") as f:
            raw = f.read()
    if raw is None:
        return None

    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization

    passphrase = s.snowflake_private_key_passphrase.encode() if s.snowflake_private_key_passphrase else None
    key = serialization.load_pem_private_key(raw, password=passphrase, backend=default_backend())
    _private_key_der = key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return _private_key_der


def _connect_sync() -> Any:
    """Build (or reuse) the singleton connection. Runs in a worker thread."""
    global _conn
    with _conn_lock:
        if _conn is not None:
            return _conn
        if not is_enabled():
            raise SnowflakeDisabled("Snowflake is disabled or not configured")

        import snowflake.connector as sf

        s = get_settings()
        kwargs: dict[str, Any] = {
            "account": s.snowflake_account,
            "user": s.snowflake_user,
            "warehouse": s.snowflake_warehouse or None,
            "database": s.snowflake_database or None,
            "schema": s.snowflake_schema or None,
            "role": s.snowflake_role or None,
            "client_session_keep_alive": True,
            "application": "EvidenceMonitoringAgent",
        }
        pk = _load_private_key()
        if pk is not None:
            kwargs["private_key"] = pk
        elif s.snowflake_password:
            kwargs["password"] = s.snowflake_password

        _conn = sf.connect(**{k: v for k, v in kwargs.items() if v is not None})
        logger.info("Snowflake connection established (account=%s db=%s)", s.snowflake_account, s.snowflake_database)
        return _conn


def _execute_sync(sql: str, params: Any = None, *, many: bool = False) -> list[dict]:
    conn = _connect_sync()
    cur = conn.cursor()
    try:
        if many:
            cur.executemany(sql, params or [])
            return []
        cur.execute(sql, params or None)
        if cur.description is None:
            return []
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        cur.close()


async def execute(sql: str, params: Any = None) -> list[dict]:
    """Run a single statement and return rows as list[dict] (empty for DDL/DML)."""
    return await asyncio.to_thread(_execute_sync, sql, params, many=False)


async def execute_many(sql: str, seq_of_params: list) -> None:
    """Batch DML (INSERT/MERGE) with ``executemany``."""
    if not seq_of_params:
        return
    await asyncio.to_thread(_execute_sync, sql, seq_of_params, many=True)


async def execute_script(statements: list[str]) -> None:
    """Run several DDL/DML statements in order (e.g. schema bootstrap)."""
    def _run() -> None:
        conn = _connect_sync()
        cur = conn.cursor()
        try:
            for stmt in statements:
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)
        finally:
            cur.close()

    await asyncio.to_thread(_run)


async def ping() -> dict:
    """Lightweight connectivity check for /snowflake/status."""
    if not is_enabled():
        return {"enabled": False, "connected": False, "detail": "disabled or not configured"}
    try:
        rows = await execute(
            "SELECT CURRENT_ACCOUNT() AS ACCOUNT, CURRENT_WAREHOUSE() AS WAREHOUSE, "
            "CURRENT_DATABASE() AS DATABASE, CURRENT_SCHEMA() AS SCHEMA, CURRENT_ROLE() AS ROLE"
        )
        info = rows[0] if rows else {}
        return {"enabled": True, "connected": True, **{k.lower(): v for k, v in info.items()}}
    except Exception as e:  # noqa: BLE001
        logger.warning("Snowflake ping failed: %s", e)
        return {"enabled": True, "connected": False, "detail": str(e)}


def close() -> None:
    global _conn
    with _conn_lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:  # noqa: BLE001
                pass
            _conn = None
