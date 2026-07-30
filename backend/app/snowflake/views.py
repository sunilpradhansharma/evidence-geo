"""Idempotent Snowflake analytics-view bootstrap.

Parses the project's ``snowflake_views.sql`` and runs every
``CREATE OR REPLACE VIEW`` statement through the app's Snowflake connection so
the dashboard views exist without a manual Snowsight step.

The session-setup lines in the SQL file (``USE ROLE``/``USE WAREHOUSE``/
``USE SCHEMA`` and ``GRANT``) are intentionally skipped — the app's Snowflake
connection already selects its role, warehouse, and schema. Only the view DDL is
executed. All statements are ``CREATE OR REPLACE VIEW`` so this is safe to run on
every startup.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.snowflake import client
from app.utils.logging import get_logger

logger = get_logger("snowflake.views")


def _views_sql_path() -> Path | None:
    """Locate snowflake_views.sql.

    Primary location is the repo root (two levels above the ``backend`` dir).
    A couple of fallbacks are checked so packaging differences degrade
    gracefully rather than crash startup.
    """
    here = Path(__file__).resolve()
    candidates = [
        here.parents[3] / "snowflake_views.sql",  # repo root
        here.parents[2] / "snowflake_views.sql",  # backend/
        here.parents[1] / "snowflake_views.sql",  # backend/app/
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def _strip_comments(sql: str) -> str:
    no_block = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return "\n".join(
        line for line in no_block.splitlines() if not line.strip().startswith("--")
    )


def view_ddl_statements() -> list[str]:
    """Return only the ``CREATE OR REPLACE VIEW`` statements from the SQL file."""
    path = _views_sql_path()
    if path is None:
        logger.warning("snowflake_views.sql not found — skipping view bootstrap")
        return []
    cleaned = _strip_comments(path.read_text(encoding="utf-8"))
    stmts: list[str] = []
    for raw in cleaned.split(";"):
        stmt = raw.strip()
        if stmt.upper().startswith("CREATE OR REPLACE VIEW"):
            stmts.append(stmt)
    return stmts


async def ensure_views() -> None:
    """Create/refresh all analytics views. No-op when Snowflake is disabled.

    Each view is created independently so one malformed statement logs a warning
    instead of aborting the remaining views (and the downstream mirror bootstrap,
    which runs in the same startup try-block).
    """
    if not client.is_enabled():
        return
    stmts = view_ddl_statements()
    if not stmts:
        return
    ok = 0
    for stmt in stmts:
        try:
            await client.execute(stmt)
            ok += 1
        except Exception as e:  # noqa: BLE001 — one bad view must not skip the others
            logger.warning("View bootstrap failed for a statement: %s", e)
    logger.info("Snowflake analytics views ensured (%d/%d)", ok, len(stmts))
