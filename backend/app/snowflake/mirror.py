"""Incremental, idempotent mirror of SQLite -> Snowflake.

Watermark-based: per table we remember the last synced value of a monotonic column
(``created_at`` / ``updated_at`` / ``id``) in ``SYNC_STATE``, read newer rows from SQLite
(async), and ``MERGE`` them into Snowflake so re-runs never duplicate. Kept entirely off
the app's hot write path — triggered after runs, after insights, on a schedule, or via
``POST /snowflake/sync``. Safe no-op when Snowflake is disabled.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import AsyncSessionLocal
from app.snowflake import client, schema
from app.snowflake.tables import SPECS, TableSpec
from app.utils.logging import get_logger

logger = get_logger("snowflake.mirror")

_BATCH = 1000
_mirror_lock = asyncio.Lock()


def _wm_type(spec: TableSpec) -> str:
    for name, sftype in spec.columns:
        if name == spec.watermark:
            return sftype
    return "VARCHAR"


def _is_ts_watermark(spec: TableSpec) -> bool:
    return _wm_type(spec) == "TIMESTAMP_NTZ"


def _normalize(value):
    """Make a value safe to bind to Snowflake (naive-UTC datetimes)."""
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _parse_watermark(spec: TableSpec, raw: str | None):
    if raw is None:
        return None
    if _is_ts_watermark(spec):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    try:
        return int(raw)
    except ValueError:
        return None


def _serialize_watermark(spec: TableSpec, value) -> str:
    if isinstance(value, datetime):
        return _normalize(value).isoformat()
    return str(value)


def _q(name: str) -> str:
    """Quote an identifier as UPPERCASE (matches the quoted-uppercase DDL columns)."""
    return '"' + name.upper() + '"'


def _pk_after(spec: TableSpec, values: tuple):
    terms = []
    for index, key in enumerate(spec.pk):
        prefix = [getattr(spec.model, prior) == values[pos] for pos, prior in enumerate(spec.pk[:index])]
        terms.append(and_(*prefix, getattr(spec.model, key) > values[index]))
    return or_(*terms)


async def _merge_batch(spec: TableSpec, rows: list) -> None:
    """Bulk-load a batch into a session temp table, then MERGE into the target in one
    statement. This avoids a per-row network round-trip: the multi-row INSERT is a single
    batched request and the MERGE is one more — ~3 round-trips per batch instead of N."""
    cols = spec.col_names
    qcols = ", ".join(_q(c) for c in cols)
    stg = f"{spec.table}__STG"

    await client.execute(f"CREATE OR REPLACE TEMPORARY TABLE {stg} LIKE {spec.table}")

    placeholders = ", ".join(["%s"] * len(cols))
    insert_sql = f"INSERT INTO {stg} ({qcols}) VALUES ({placeholders})"
    params = [tuple(_normalize(getattr(r, c)) for c in cols) for r in rows]
    await client.execute_many(insert_sql, params)

    on = " AND ".join(f"t.{_q(k)} = s.{_q(k)}" for k in spec.pk)
    non_pk = [c for c in cols if c not in spec.pk]
    matched = ""
    if non_pk:
        set_clause = ", ".join(f"t.{_q(c)} = s.{_q(c)}" for c in non_pk)
        matched = f" WHEN MATCHED THEN UPDATE SET {set_clause}"
    insert_vals = ", ".join(f"s.{_q(c)}" for c in cols)
    merge_sql = (
        f"MERGE INTO {spec.table} t USING {stg} s ON {on}"
        f"{matched} WHEN NOT MATCHED THEN INSERT ({qcols}) VALUES ({insert_vals})"
    )
    await client.execute(merge_sql)


async def _get_watermark(spec: TableSpec) -> str | None:
    rows = await client.execute(
        "SELECT LAST_WATERMARK FROM SYNC_STATE WHERE TABLE_NAME = %s", (spec.table,)
    )
    return rows[0]["LAST_WATERMARK"] if rows else None


async def _set_watermark(
    spec: TableSpec, watermark: str, rows_synced: int, *, accumulate: bool = True
) -> None:
    # Incremental syncs ADD the batch count to a running total; full-refresh snapshots
    # REPLACE it with the current table size (accumulate=False) so the stat stays truthful.
    rows_expr = "t.ROWS_SYNCED + s.ROWS_SYNCED" if accumulate else "s.ROWS_SYNCED"
    await client.execute(
        "MERGE INTO SYNC_STATE t USING (SELECT %s AS TABLE_NAME, %s AS LAST_WATERMARK, "
        "%s AS ROWS_SYNCED, %s AS UPDATED_AT) s ON t.TABLE_NAME = s.TABLE_NAME "
        "WHEN MATCHED THEN UPDATE SET LAST_WATERMARK = s.LAST_WATERMARK, "
        f"ROWS_SYNCED = {rows_expr}, UPDATED_AT = s.UPDATED_AT "
        "WHEN NOT MATCHED THEN INSERT (TABLE_NAME, LAST_WATERMARK, ROWS_SYNCED, UPDATED_AT) "
        "VALUES (s.TABLE_NAME, s.LAST_WATERMARK, s.ROWS_SYNCED, s.UPDATED_AT)",
        (spec.table, watermark, rows_synced, datetime.now(timezone.utc).replace(tzinfo=None)),
    )


async def _full_refresh_table(db: AsyncSession, spec: TableSpec) -> int:
    """Snapshot-replace a small/mutable table.

    Stages every current SQLite row into a session temp table, then atomically
    ``INSERT OVERWRITE`` the target so Snowflake exactly matches SQLite — including
    in-place edits and deletes an incremental watermark could never see. An empty
    source correctly clears the target.
    """
    cols = spec.col_names
    qcols = ", ".join(_q(c) for c in cols)
    stg = f"{spec.table}__FULL_STG"
    await client.execute(f"CREATE OR REPLACE TEMPORARY TABLE {stg} LIKE {spec.table}")

    placeholders = ", ".join(["%s"] * len(cols))
    insert_sql = f"INSERT INTO {stg} ({qcols}) VALUES ({placeholders})"
    pk_order = [getattr(spec.model, k).asc() for k in spec.pk]

    total = 0
    offset = 0
    while True:
        stmt = select(spec.model).order_by(*pk_order).offset(offset).limit(_BATCH)
        rows = list((await db.execute(stmt)).scalars().all())
        if not rows:
            break
        params = [tuple(_normalize(getattr(r, c)) for c in cols) for r in rows]
        await client.execute_many(insert_sql, params)
        total += len(rows)
        offset += len(rows)
        if len(rows) < _BATCH:
            break

    await client.execute(
        f"INSERT OVERWRITE INTO {spec.table} ({qcols}) SELECT {qcols} FROM {stg}"
    )
    await _set_watermark(
        spec,
        "full:" + datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        total,
        accumulate=False,
    )
    logger.info("Full-refreshed %d row(s) -> %s", total, spec.table)
    return total


async def _sync_table(db: AsyncSession, spec: TableSpec) -> int:
    if spec.full_refresh:
        return await _full_refresh_table(db, spec)
    wm_col = getattr(spec.model, spec.watermark)
    last_raw = await _get_watermark(spec)
    last_value = _parse_watermark(spec, last_raw)
    pk_order = [getattr(spec.model, key).asc() for key in spec.pk]

    total = 0
    cursor_watermark = None
    cursor_pk: tuple | None = None
    final_watermark = None
    while True:
        stmt = select(spec.model)
        if cursor_watermark is not None and cursor_pk is not None:
            stmt = stmt.where(
                or_(
                    wm_col > cursor_watermark,
                    and_(wm_col == cursor_watermark, _pk_after(spec, cursor_pk)),
                )
            )
        elif last_value is not None:
            stmt = stmt.where(wm_col > last_value)
        stmt = stmt.order_by(wm_col.asc(), *pk_order).limit(_BATCH)
        rows = list((await db.execute(stmt)).scalars().all())
        if not rows:
            break

        await _merge_batch(spec, rows)

        tail = rows[-1]
        cursor_watermark = getattr(tail, spec.watermark)
        cursor_pk = tuple(getattr(tail, key) for key in spec.pk)
        final_watermark = cursor_watermark
        total += len(rows)
        if len(rows) < _BATCH:
            break

    if total:
        await _set_watermark(spec, _serialize_watermark(spec, final_watermark), total)
        logger.info("Mirrored %d row(s) -> %s", total, spec.table)
    return total


async def run_mirror() -> dict:
    """Sync every table once. Concurrency-guarded; safe no-op when disabled."""
    if not client.is_enabled():
        return {"status": "disabled", "synced": {}}

    async with _mirror_lock:
        await schema.ensure_schema()
        synced: dict[str, int] = {}
        async with AsyncSessionLocal() as db:
            for spec in SPECS:
                try:
                    synced[spec.table] = await _sync_table(db, spec)
                except Exception as e:  # noqa: BLE001 — one table must not abort the rest
                    logger.warning("Mirror failed for %s: %s", spec.table, e)
                    synced[spec.table] = -1
        # GEO verified-schema corpus lives on disk (no SQLite model); snapshot it too so
        # "all data" reaches Snowflake. Best-effort — a GEO hiccup must not abort the pass.
        try:
            from app.snowflake import geo_sync  # late import: reads the file-backed loader

            synced["GEO_SCHEMAS"] = await geo_sync.sync()
        except Exception as e:  # noqa: BLE001
            logger.warning("Mirror failed for GEO_SCHEMAS: %s", e)
            synced["GEO_SCHEMAS"] = -1
        total = sum(v for v in synced.values() if v > 0)
        return {"status": "ok", "rows_synced": total, "synced": synced}


async def run_mirror_safe() -> None:
    """Fire-and-forget mirror used by hooks — never raises into the caller."""
    try:
        result = await run_mirror()
        logger.info("Mirror pass: %s", result.get("status"))
    except Exception as e:  # noqa: BLE001
        logger.warning("Mirror pass skipped: %s", e)


async def sync_state() -> list[dict]:
    """Per-table watermark + row counts for /snowflake/status."""
    if not client.is_enabled():
        return []
    try:
        return await client.execute(
            "SELECT TABLE_NAME, LAST_WATERMARK, ROWS_SYNCED, UPDATED_AT "
            "FROM SYNC_STATE ORDER BY TABLE_NAME"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("sync_state read failed: %s", e)
        return []
