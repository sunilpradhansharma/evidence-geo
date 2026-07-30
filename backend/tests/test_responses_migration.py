"""Regression: the responses.brand_focus NOT NULL migration (DISEASE_STATE support).

A legacy prod DB created the `responses` table with `brand_focus` NOT NULL (before FR-108a made
it nullable). DISEASE_STATE (All Brands) responses have brand_focus=None, so every such run
failed at db.flush() with `NOT NULL constraint failed: responses.brand_focus`, taking the whole
run down. This test builds that legacy table, runs `_make_responses_brand_focus_nullable`, and
confirms the column becomes nullable, the existing row survives the rebuild, and a brand-less
insert now succeeds. Pure SQLite on a temp file — no app engine, no network.
"""
import os
import tempfile

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.database import _make_responses_brand_focus_nullable

# Minimal legacy `responses` schema: every NOT NULL column the CURRENT model requires (so the
# rebuild's row-copy satisfies the new constraints), with brand_focus deliberately NOT NULL —
# the exact legacy constraint we are relaxing — plus the named + UNIQUE indexes SQLite keeps on
# RENAME (so the drop-then-recreate path is exercised).
_LEGACY_DDL = """
CREATE TABLE responses (
    response_id VARCHAR(64) NOT NULL PRIMARY KEY,
    run_id VARCHAR(64) NOT NULL,
    timestamp_utc DATETIME NOT NULL,
    llm_name VARCHAR(64) NOT NULL,
    llm_model_version VARCHAR(128),
    persona VARCHAR(32) NOT NULL,
    question_id VARCHAR(64) NOT NULL,
    question_text TEXT NOT NULL,
    therapeutic_area VARCHAR(64) NOT NULL,
    indication VARCHAR(128),
    disease VARCHAR(128),
    brand_focus VARCHAR(64) NOT NULL,
    domain VARCHAR(32) NOT NULL,
    monitoring_mode VARCHAR(16) NOT NULL DEFAULT 'BRAND',
    competitor_focus TEXT,
    intent_type VARCHAR(16),
    consensus_level VARCHAR(16),
    response_text TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    response_tokens INTEGER NOT NULL DEFAULT 0,
    sources TEXT,
    grounding_supports TEXT,
    search_queries TEXT,
    finish_reason VARCHAR(32),
    status VARCHAR(16) NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_run_question_llm UNIQUE (run_id, question_id, llm_name)
);
CREATE INDEX ix_responses_run_id ON responses (run_id);
CREATE INDEX ix_responses_brand_focus ON responses (brand_focus);
"""

_INSERT = (
    "INSERT INTO responses (response_id, run_id, timestamp_utc, llm_name, persona, question_id, "
    "question_text, therapeutic_area, brand_focus, domain, monitoring_mode, response_text, "
    "prompt_tokens, response_tokens, status, created_at) VALUES "
    "(:rid, :run, '2026-01-01 00:00:00', 'claude', 'Patient', :qid, 'Q?', 'Immunology', "
    ":brand, 'General', :mode, 'ans', 1, 2, 'SUCCESS', '2026-01-01 00:00:00')"
)


def _brand_focus_nullable(sync_conn) -> bool:
    col = next(c for c in inspect(sync_conn).get_columns("responses") if c["name"] == "brand_focus")
    return col["nullable"]


async def test_responses_brand_focus_migration_relaxes_not_null_and_keeps_rows():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        # Build the legacy table + a pre-existing BRAND row that must survive the rebuild.
        async with engine.begin() as conn:
            for stmt in (s.strip() for s in _LEGACY_DDL.split(";")):
                if stmt:
                    await conn.execute(text(stmt))
            await conn.execute(text(_INSERT), {
                "rid": "r1", "run": "run1", "qid": "q1", "brand": "Rinvoq", "mode": "BRAND",
            })

        # Precondition: legacy brand_focus is NOT NULL, and a brand-less insert is rejected.
        async with engine.connect() as conn:
            assert await conn.run_sync(_brand_focus_nullable) is False

        # Run the migration.
        async with engine.begin() as conn:
            await _make_responses_brand_focus_nullable(conn)

        async with engine.begin() as conn:
            # Column relaxed to nullable...
            assert await conn.run_sync(_brand_focus_nullable) is True
            # ...the legacy row survived the rebuild...
            kept = (await conn.execute(
                text("SELECT brand_focus FROM responses WHERE response_id = 'r1'")
            )).scalar()
            assert kept == "Rinvoq"
            # ...and a brand-less DISEASE_STATE insert now succeeds.
            await conn.execute(text(_INSERT), {
                "rid": "r2", "run": "run2", "qid": "q2", "brand": None, "mode": "DISEASE_STATE",
            })
            total = (await conn.execute(text("SELECT COUNT(*) FROM responses"))).scalar()
            assert total == 2

        # Idempotent: re-running is a no-op (must not raise or double-rebuild).
        async with engine.begin() as conn:
            await _make_responses_brand_focus_nullable(conn)
        async with engine.begin() as conn:
            assert await conn.run_sync(_brand_focus_nullable) is True
    finally:
        await engine.dispose()
        os.unlink(path)
