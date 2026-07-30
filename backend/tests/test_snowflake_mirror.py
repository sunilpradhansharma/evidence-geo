from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.geo import loader as geo_loader
from app.models.theme import Theme
from app.snowflake import geo_sync, mirror, schema
from app.snowflake.tables import SPECS


def test_specs_cover_all_model_columns():
    assert len(SPECS) == len({spec.table for spec in SPECS})
    for spec in SPECS:
        model_columns = {column.name for column in spec.model.__table__.columns}
        assert set(spec.col_names) == model_columns, spec.table
        assert set(spec.pk) <= model_columns
        assert spec.watermark in model_columns


def test_expected_columns_includes_control_and_geo_tables():
    expected = schema.expected_columns()
    assert len(expected) == len(SPECS) + 3
    assert expected["GEO_SCHEMAS"] == geo_sync._COLUMNS
    assert {name for name, _ in expected["SYNC_STATE"]} == {
        "TABLE_NAME", "LAST_WATERMARK", "ROWS_SYNCED", "UPDATED_AT",
    }
    assert {name for name, _ in expected["APP_EVENTS"]} == {
        "EVENT_ID", "TS", "METHOD", "PATH", "STATUS_CODE", "DURATION_MS",
        "REQUEST_BODY", "RESPONSE_BODY", "CLIENT_HOST",
    }


@pytest.mark.asyncio
async def test_ensure_columns_adds_missing_fields_and_resets_watermarks(monkeypatch):
    expected = schema.expected_columns()
    missing = {("ALERTS", "ENTITY_TYPE"), ("QUESTIONS", "INDICATION")}
    live = [
        {"TABLE_NAME": table.upper(), "COLUMN_NAME": name.upper()}
        for table, columns in expected.items()
        for name, _ in columns
        if (table.upper(), name.upper()) not in missing
    ]
    calls = []

    async def execute(sql, params=None):
        calls.append((sql, params))
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return live
        return []

    monkeypatch.setattr(schema.client, "execute", execute)
    added, changed = await schema._ensure_columns()

    assert added == 2
    assert changed == {"ALERTS", "QUESTIONS"}
    alters = [sql for sql, _ in calls if sql.startswith("ALTER TABLE")]
    assert any('"ALERTS"' in sql and '"ENTITY_TYPE"' in sql for sql in alters)
    assert any('"QUESTIONS"' in sql and '"INDICATION"' in sql for sql in alters)
    resets = [params for sql, params in calls if sql.startswith("DELETE FROM SYNC_STATE")]
    assert resets == [("ALERTS",), ("QUESTIONS",)]


@pytest.mark.asyncio
async def test_incremental_sync_pages_through_equal_watermarks(monkeypatch):
    spec = next(item for item in SPECS if item.table == "THEMES")
    stamp = datetime(2026, 7, 13, tzinfo=timezone.utc)
    rows = [Theme(theme_id=f"theme-{index}", taxonomy_version=1, label=str(index), created_at=stamp) for index in range(5)]
    batches = [rows[:2], rows[2:4], rows[4:]]
    statements = []

    class Db:
        async def execute(self, statement):
            statements.append(statement)
            result = MagicMock()
            result.scalars.return_value.all.return_value = batches.pop(0)
            return result

    merge_batch = AsyncMock()
    set_watermark = AsyncMock()
    monkeypatch.setattr(mirror, "_BATCH", 2)
    monkeypatch.setattr(mirror, "_get_watermark", AsyncMock(return_value=None))
    monkeypatch.setattr(mirror, "_merge_batch", merge_batch)
    monkeypatch.setattr(mirror, "_set_watermark", set_watermark)

    total = await mirror._sync_table(Db(), spec)

    assert total == 5
    assert [len(call.args[1]) for call in merge_batch.await_args_list] == [2, 2, 1]
    set_watermark.assert_awaited_once()
    assert set_watermark.await_args.args[2] == 5
    second_sql = str(statements[1])
    assert "themes.created_at >" in second_sql
    assert "themes.created_at =" in second_sql
    assert "themes.theme_id >" in second_sql


@pytest.mark.asyncio
async def test_geo_incomplete_snapshot_preserves_existing_table(monkeypatch):
    brand = geo_loader.list_available_brands()[0]
    raw = geo_loader.get_brand_schema(brand)
    calls = []

    async def execute(sql, params=None):
        calls.append((sql, params))
        return []

    monkeypatch.setattr(geo_sync.client, "is_enabled", lambda: True)
    monkeypatch.setattr(geo_sync.client, "execute", execute)
    monkeypatch.setattr(geo_sync.geo_loader, "list_available_brands", lambda: [brand, "Missing"])
    monkeypatch.setattr(
        geo_sync.geo_loader,
        "get_brand_schema",
        lambda name: raw if name == brand else None,
    )

    written = await geo_sync.sync()

    assert written == 0
    assert not any(sql.startswith("INSERT OVERWRITE") for sql, _ in calls)
    assert not any(sql.startswith("MERGE INTO SYNC_STATE") for sql, _ in calls)


@pytest.mark.asyncio
async def test_geo_success_records_sync_state(monkeypatch):
    brand = geo_loader.list_available_brands()[0]
    raw = geo_loader.get_brand_schema(brand)
    calls = []

    async def execute(sql, params=None):
        calls.append((sql, params))
        return []

    monkeypatch.setattr(geo_sync.client, "is_enabled", lambda: True)
    monkeypatch.setattr(geo_sync.client, "execute", execute)
    monkeypatch.setattr(geo_sync.geo_loader, "list_available_brands", lambda: [brand])
    monkeypatch.setattr(geo_sync.geo_loader, "get_brand_schema", lambda name: raw)

    written = await geo_sync.sync()

    assert written == 1
    assert any(sql.startswith("INSERT OVERWRITE") for sql, _ in calls)
    sync_calls = [(sql, params) for sql, params in calls if sql.startswith("MERGE INTO SYNC_STATE")]
    assert len(sync_calls) == 1
    assert sync_calls[0][1][0] == "GEO_SCHEMAS"
    assert sync_calls[0][1][2] == 1


@pytest.mark.asyncio
async def test_startup_schema_forces_core_backfill(monkeypatch):
    reset = AsyncMock()
    monkeypatch.setattr(schema.client, "is_enabled", lambda: True)
    monkeypatch.setattr(schema.client, "execute_script", AsyncMock())
    monkeypatch.setattr(schema, "_ensure_columns", AsyncMock(return_value=(0, set())))
    monkeypatch.setattr(schema, "_reset_watermarks", reset)

    await schema.ensure_schema(startup_backfill=True)

    reset.assert_awaited_once_with(schema._STARTUP_BACKFILL_TABLES)
