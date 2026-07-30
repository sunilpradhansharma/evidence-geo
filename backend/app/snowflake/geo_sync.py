"""Mirror the GEO verified-schema corpus into Snowflake.

Unlike every other mirrored dataset, GEO ground-truth is NOT in SQLite — it is a generated,
file-backed JSON-LD corpus (``config/geo/schema/*.json``) served by ``app.geo.loader``. To
satisfy "store all the data in Snowflake" we snapshot that corpus into a dedicated
``GEO_SCHEMAS`` table: a few distilled scalar columns for easy querying plus the full JSON-LD
doc and the Chairman ``context_view`` as ``VARIANT`` so nothing is lost.

Snapshot-replace semantics (stage every brand, then atomic ``INSERT OVERWRITE``) mirror the
``full_refresh`` table path in ``mirror.py`` — the corpus is tiny (a handful of brands) and
mutable (regenerated in place), so a full snapshot is both cheap and exactly correct.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.geo import loader as geo_loader
from app.geo.schema_model import DrugSchema
from app.snowflake import client
from app.utils.logging import get_logger

logger = get_logger("snowflake.geo")

GEO_TABLE = "GEO_SCHEMAS"

# Ordered (column, SF type). JSON-LD doc + distilled context view are VARIANT so the full
# structure is queryable in Snowflake/Cortex; the rest are handy scalar projections.
_COLUMNS: list[tuple[str, str]] = [
    ("BRAND", "VARCHAR"),
    ("GENERIC_NAME", "VARCHAR"),
    ("DRUG_CLASS", "VARCHAR"),
    ("DATA_SOURCE", "VARCHAR"),
    ("CLINICAL_VALUES_VERIFIED", "BOOLEAN"),
    ("LABEL_SOURCE", "VARCHAR"),
    ("LABEL_EFFECTIVE_TIME", "VARCHAR"),
    ("PRESCRIBING_INFORMATION", "VARCHAR"),
    ("CONTEXT_VIEW", "VARIANT"),
    ("JSONLD", "VARIANT"),
    ("SYNCED_AT", "TIMESTAMP_NTZ"),
]
# The two VARIANT columns are bound as JSON text and parsed in-SQL with TRY_PARSE_JSON.
_VARIANT_COLS = {"CONTEXT_VIEW", "JSONLD"}


def ddl() -> str:
    cols = ",\n  ".join(f'"{name}" {sftype}' for name, sftype in _COLUMNS)
    return f'CREATE TABLE IF NOT EXISTS {GEO_TABLE} (\n  {cols},\n  PRIMARY KEY ("BRAND")\n)'


def _select_placeholder() -> str:
    """Positional SELECT list: plain ``%s`` per column, TRY_PARSE_JSON(%s) for VARIANT ones."""
    parts = []
    for name, _ in _COLUMNS:
        parts.append(f"TRY_PARSE_JSON(%s)" if name in _VARIANT_COLS else "%s")
    return ", ".join(parts)


def _row_params(raw: dict, schema: DrugSchema, synced_at: datetime) -> tuple:
    prov = raw.get("provenance") or {}
    verified = prov.get("clinicalValuesVerified")
    return (
        schema.name,
        schema.non_proprietary_name,
        schema.drug_class,
        schema.data_source,
        bool(verified) if verified is not None else None,
        prov.get("labelSource"),
        prov.get("labelEffectiveTime"),
        schema.prescribing_information or raw.get("prescribingInformation"),
        json.dumps(schema.context_view()),
        json.dumps(raw),
        synced_at,
    )


async def _set_sync_state(written: int, synced_at: datetime) -> None:
    await client.execute(
        "MERGE INTO SYNC_STATE t USING (SELECT %s AS TABLE_NAME, %s AS LAST_WATERMARK, "
        "%s AS ROWS_SYNCED, %s AS UPDATED_AT) s ON t.TABLE_NAME = s.TABLE_NAME "
        "WHEN MATCHED THEN UPDATE SET LAST_WATERMARK = s.LAST_WATERMARK, "
        "ROWS_SYNCED = s.ROWS_SYNCED, UPDATED_AT = s.UPDATED_AT "
        "WHEN NOT MATCHED THEN INSERT (TABLE_NAME, LAST_WATERMARK, ROWS_SYNCED, UPDATED_AT) "
        "VALUES (s.TABLE_NAME, s.LAST_WATERMARK, s.ROWS_SYNCED, s.UPDATED_AT)",
        (GEO_TABLE, "full:" + synced_at.isoformat(), written, synced_at),
    )


async def sync() -> int:
    """Snapshot the on-disk GEO corpus into ``GEO_SCHEMAS``. No-op when disabled.

    Returns the number of brands written. Skips the overwrite when the corpus is empty so a
    missing/undeployed corpus never wipes previously-synced ground truth.
    """
    if not client.is_enabled():
        return 0

    brands = geo_loader.list_available_brands()
    if not brands:
        logger.info("GEO corpus empty — skipping GEO_SCHEMAS snapshot")
        return 0

    await client.execute(ddl())

    qcols = ", ".join(f'"{name}"' for name, _ in _COLUMNS)
    stg = f"{GEO_TABLE}__FULL_STG"
    await client.execute(f"CREATE OR REPLACE TEMPORARY TABLE {stg} LIKE {GEO_TABLE}")

    synced_at = datetime.now(timezone.utc).replace(tzinfo=None)
    snapshot_rows = []
    for brand in brands:
        raw = geo_loader.get_brand_schema(brand)
        if not raw:
            continue
        try:
            schema = DrugSchema.model_validate(raw)
        except Exception as e:  # noqa: BLE001 — a malformed record must not abort the snapshot
            logger.warning("GEO snapshot skipped %s: %s", brand, e)
            continue
        snapshot_rows.append(_row_params(raw, schema, synced_at))

    if len(snapshot_rows) != len(brands):
        logger.warning(
            "GEO snapshot incomplete (%d/%d valid); preserving the previous Snowflake snapshot",
            len(snapshot_rows), len(brands),
        )
        return 0

    insert_sql = f"INSERT INTO {stg} ({qcols}) SELECT {_select_placeholder()}"
    for params in snapshot_rows:
        await client.execute(insert_sql, params)

    written = len(snapshot_rows)
    await client.execute(
        f"INSERT OVERWRITE INTO {GEO_TABLE} ({qcols}) SELECT {qcols} FROM {stg}"
    )
    await _set_sync_state(written, synced_at)
    logger.info("Snapshotted %d GEO brand schema(s) -> %s", written, GEO_TABLE)
    return written
