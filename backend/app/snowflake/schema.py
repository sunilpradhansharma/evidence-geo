"""Idempotent Snowflake schema bootstrap.

Creates one mirror table per SQLite model (from ``tables.SPECS``) plus two control
tables: ``SYNC_STATE`` (per-table incremental watermark) and ``APP_EVENTS`` (raw API
input/output capture, so "any input and output" is stored in Snowflake). All DDL is
``CREATE TABLE IF NOT EXISTS`` so this is safe to run on every startup.
"""
from __future__ import annotations

from app.snowflake import client
from app.snowflake.tables import SPECS
from app.utils.logging import get_logger

logger = get_logger("snowflake.schema")

_STARTUP_BACKFILL_TABLES = {
    "ALERTS", "QUESTIONS", "RESPONSES", "RESPONSE_DIFFS", "RUNS",
}


def _q(name: str) -> str:
    """Quote an identifier as UPPERCASE so reserved words (e.g. TRIGGER) are legal.
    A quoted uppercase identifier matches unquoted uppercase usage in read queries."""
    return '"' + name.upper() + '"'


def _create_table_ddl(table: str, columns: list[tuple[str, str]], pk: tuple[str, ...]) -> str:
    cols = ",\n  ".join(f"{_q(name)} {sftype}" for name, sftype in columns)
    pk_clause = f",\n  PRIMARY KEY ({', '.join(_q(k) for k in pk)})" if pk else ""
    return f"CREATE TABLE IF NOT EXISTS {table} (\n  {cols}{pk_clause}\n)"


def expected_columns() -> dict[str, list[tuple[str, str]]]:
    from app.snowflake import geo_sync

    tables = {spec.table: list(spec.columns) for spec in SPECS}
    tables["SYNC_STATE"] = [
        ("TABLE_NAME", "VARCHAR"),
        ("LAST_WATERMARK", "VARCHAR"),
        ("ROWS_SYNCED", "NUMBER(38,0)"),
        ("UPDATED_AT", "TIMESTAMP_NTZ"),
    ]
    tables["APP_EVENTS"] = [
        ("EVENT_ID", "VARCHAR"),
        ("TS", "TIMESTAMP_NTZ"),
        ("METHOD", "VARCHAR"),
        ("PATH", "VARCHAR"),
        ("STATUS_CODE", "NUMBER(38,0)"),
        ("DURATION_MS", "NUMBER(38,0)"),
        ("REQUEST_BODY", "VARIANT"),
        ("RESPONSE_BODY", "VARIANT"),
        ("CLIENT_HOST", "VARCHAR"),
    ]
    tables[geo_sync.GEO_TABLE] = list(geo_sync._COLUMNS)
    return tables


async def _reset_watermarks(tables: set[str]) -> None:
    for table in sorted(tables):
        await client.execute("DELETE FROM SYNC_STATE WHERE TABLE_NAME = %s", (table,))


async def _ensure_columns() -> tuple[int, set[str]]:
    rows = await client.execute(
        "SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = CURRENT_SCHEMA()"
    )
    existing = {(row["TABLE_NAME"], row["COLUMN_NAME"]) for row in rows}
    added = 0
    changed_tables: set[str] = set()
    for table, columns in expected_columns().items():
        for name, sftype in columns:
            key = (table.upper(), name.upper())
            if key in existing:
                continue
            await client.execute(
                f"ALTER TABLE {_q(table)} ADD COLUMN IF NOT EXISTS {_q(name)} {sftype}"
            )
            existing.add(key)
            changed_tables.add(table)
            added += 1

    mirrored = {spec.table for spec in SPECS}
    await _reset_watermarks(changed_tables & mirrored)
    return added, changed_tables


def ddl_statements() -> list[str]:
    stmts: list[str] = []
    for spec in SPECS:
        stmts.append(_create_table_ddl(spec.table, spec.columns, spec.pk))

    stmts.append(
        "CREATE TABLE IF NOT EXISTS SYNC_STATE (\n"
        "  TABLE_NAME VARCHAR PRIMARY KEY,\n"
        "  LAST_WATERMARK VARCHAR,\n"
        "  ROWS_SYNCED NUMBER(38,0) DEFAULT 0,\n"
        "  UPDATED_AT TIMESTAMP_NTZ\n)"
    )
    stmts.append(
        "CREATE TABLE IF NOT EXISTS APP_EVENTS (\n"
        "  EVENT_ID VARCHAR PRIMARY KEY,\n"
        "  TS TIMESTAMP_NTZ,\n"
        "  METHOD VARCHAR,\n"
        "  PATH VARCHAR,\n"
        "  STATUS_CODE NUMBER(38,0),\n"
        "  DURATION_MS NUMBER(38,0),\n"
        "  REQUEST_BODY VARIANT,\n"
        "  RESPONSE_BODY VARIANT,\n"
        "  CLIENT_HOST VARCHAR\n)"
    )
    # GEO verified-schema corpus (file-backed, not a SQLite model — see geo_sync.py).
    from app.snowflake import geo_sync

    stmts.append(geo_sync.ddl())
    return stmts


async def ensure_schema(*, startup_backfill: bool = False) -> None:
    """Create all mirror + control tables if missing. No-op when Snowflake is disabled."""
    if not client.is_enabled():
        return
    await client.execute_script(ddl_statements())
    added, changed_tables = await _ensure_columns()
    if startup_backfill:
        await _reset_watermarks(_STARTUP_BACKFILL_TABLES)
    logger.info(
        "Snowflake schema ensured (%d mirror tables + SYNC_STATE + APP_EVENTS; "
        "%d column(s) added across %d table(s))",
        len(SPECS), added, len(changed_tables),
    )
