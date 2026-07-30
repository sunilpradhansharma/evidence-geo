"""Async SQLAlchemy engine, session factory, and declarative base."""
from collections.abc import AsyncGenerator

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config.settings import get_settings

settings = get_settings()

_is_sqlite = settings.database_url.startswith("sqlite")

engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
    # SQLite serializes writers against a single file. Without a busy timeout a
    # second concurrent writer (the post-run scoring pass, an in-flight run, the
    # scheduler, or the OpenEvidence worker) fails INSTANTLY with
    # "database is locked". A generous timeout makes it WAIT for the lock instead.
    # No-op for Postgres / other backends.
    connect_args={"timeout": 30} if _is_sqlite else {},
)


if _is_sqlite:

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):  # noqa: ANN001, ARG001
        """Enable WAL + a long busy timeout so concurrent writers don't collide.

        WAL lets readers run while a single writer commits (far less contention than
        the default rollback journal) and the mode persists in the DB file. The busy
        timeout (belt-and-suspenders with the connect_args timeout) makes any writer
        wait up to 30s for the lock rather than immediately raising OperationalError —
        which is what was leaving scoring records uninserted ("unscored").
        """
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db() -> None:
    """Create all tables. Used for local dev / first run."""
    # Import models so they register on Base.metadata
    from app.models import (  # noqa: F401
        alert,
        analysis_protocol,
        audit_log,
        brand_taxonomy,
        clinical_study,
        competitor_candidate,
        consensus,
        digest,
        drug_fact,
        evaluation_claim,
        evidence_network,
        harvested_question,
        intervention,
        intervention_event,
        intervention_result,
        measurement_snapshot,
        model_release,
        nma_result,
        preferred_source,
        preferred_source_observation,
        prompt_volume,
        prompt_volume_alert,
        question,
        question_evidence,
        question_variation,
        recommendation,
        recommendation_review,
        response,
        response_citation,
        response_diff,
        run,
        schedule,
        scoring,
        social_brief,
        social_comment,
        social_post,
        source_domain,
        source_payload,
        theme,
        workshop_summary,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_sqlite_schema(conn)


async def _make_questions_brand_focus_nullable(conn) -> None:
    columns = await conn.run_sync(lambda c: inspect(c).get_columns("questions"))
    brand_focus = next((column for column in columns if column["name"] == "brand_focus"), None)
    if brand_focus is None or brand_focus["nullable"]:
        return

    from app.models.question import Question

    await conn.execute(text(
        "UPDATE questions SET monitoring_mode = 'BRAND' WHERE monitoring_mode IS NULL"
    ))
    await conn.execute(text(
        "UPDATE questions SET priority_weight = 1.0 WHERE priority_weight IS NULL"
    ))
    await conn.execute(text(
        "UPDATE questions SET is_variation = 0 WHERE is_variation IS NULL"
    ))
    await conn.execute(text("ALTER TABLE questions RENAME TO questions_legacy"))
    legacy_indexes = await conn.run_sync(
        lambda c: [
            index["name"]
            for index in inspect(c).get_indexes("questions_legacy")
            if index.get("name")
        ]
    )
    for index_name in legacy_indexes:
        await conn.execute(text(f'DROP INDEX IF EXISTS "{index_name}"'))
    await conn.run_sync(lambda c: Question.__table__.create(c))
    column_names = ", ".join(f'"{column.name}"' for column in Question.__table__.columns)
    await conn.execute(text(
        f"INSERT INTO questions ({column_names}) "
        f"SELECT {column_names} FROM questions_legacy"
    ))
    await conn.execute(text("DROP TABLE questions_legacy"))


async def _make_responses_brand_focus_nullable(conn) -> None:
    """Relax the legacy NOT NULL on responses.brand_focus so brand-less DISEASE_STATE
    (All Brands / pre-launch) responses can persist (FR-108a).

    SQLite cannot drop a column constraint in place, so — only when brand_focus is still
    NOT NULL — rebuild the table from the current (nullable) model and copy every existing
    row. Mirrors _make_questions_brand_focus_nullable; the extra guard skips the implicit
    auto-index backing the uq_run_question_llm UNIQUE constraint (which cannot be dropped
    and disappears with the legacy table anyway). Idempotent: a no-op once brand_focus is
    nullable."""
    columns = await conn.run_sync(lambda c: inspect(c).get_columns("responses"))
    brand_focus = next((column for column in columns if column["name"] == "brand_focus"), None)
    if brand_focus is None or brand_focus["nullable"]:
        return

    from app.models.response import Response

    # Back-fill the one added-later NOT NULL column so legacy rows survive the copy.
    await conn.execute(text(
        "UPDATE responses SET monitoring_mode = 'BRAND' WHERE monitoring_mode IS NULL"
    ))
    await conn.execute(text("ALTER TABLE responses RENAME TO responses_legacy"))
    # SQLite keeps a table's named indexes on RENAME, so ix_responses_* still exist and would
    # collide when we recreate the table from the model. Drop them first, but skip the
    # sqlite_autoindex_* entries backing PK/UNIQUE constraints (they cannot be dropped).
    legacy_indexes = await conn.run_sync(
        lambda c: [
            index["name"]
            for index in inspect(c).get_indexes("responses_legacy")
            if index.get("name") and not index["name"].startswith("sqlite_autoindex")
        ]
    )
    for index_name in legacy_indexes:
        await conn.execute(text(f'DROP INDEX IF EXISTS "{index_name}"'))
    await conn.run_sync(lambda c: Response.__table__.create(c))
    # Copy via the columns common to both the model and the legacy table so a column the
    # legacy DB never had (or vice versa) can't break the INSERT.
    legacy_columns = await conn.run_sync(
        lambda c: {col["name"] for col in inspect(c).get_columns("responses_legacy")}
    )
    shared = [c.name for c in Response.__table__.columns if c.name in legacy_columns]
    column_names = ", ".join(f'"{name}"' for name in shared)
    await conn.execute(text(
        f"INSERT INTO responses ({column_names}) "
        f"SELECT {column_names} FROM responses_legacy"
    ))
    await conn.execute(text("DROP TABLE responses_legacy"))


async def _migrate_sqlite_schema(conn) -> None:
    """Apply lightweight SQLite schema upgrades for local/dev databases."""
    if conn.dialect.name != "sqlite":
        return

    # Several upgrades below rebuild a table via `ALTER TABLE x RENAME TO x_legacy`. Modern
    # SQLite (legacy_alter_table=OFF) "helpfully" rewrites references to the renamed table inside
    # OTHER objects' FOREIGN KEY clauses — so renaming `responses`->`responses_legacy` silently
    # repointed response_citations' FK at responses_legacy, which the rebuild then dropped,
    # leaving a dangling FK that broke every citation INSERT (FR-706a prod incident). Turn that
    # rewrite OFF for the whole migration so a RENAME only ever renames the one named table.
    await conn.execute(text("PRAGMA legacy_alter_table=ON"))

    def existing_columns(sync_conn, table_name: str) -> set[str]:
        inspector = inspect(sync_conn)
        if table_name not in inspector.get_table_names():
            return set()
        return {column["name"] for column in inspector.get_columns(table_name)}

    # AI-answer insights: the per-platform summary cache gained a `scope` column (workshop vs
    # all-questions) as part of a composite (scope, llm_name) key. It is a regenerable cache,
    # so an older single-key table is simply dropped and recreated from the current model.
    wps_columns = await conn.run_sync(existing_columns, "workshop_platform_summaries")
    if wps_columns and "scope" not in wps_columns:
        from app.models.workshop_summary import WorkshopPlatformSummary
        await conn.execute(text("DROP TABLE workshop_platform_summaries"))
        await conn.run_sync(lambda c: WorkshopPlatformSummary.__table__.create(c))

    questions_columns = await conn.run_sync(existing_columns, "questions")
    if "intent_type" not in questions_columns:
        await conn.execute(text("ALTER TABLE questions ADD COLUMN intent_type VARCHAR(16)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_questions_intent_type ON questions (intent_type)"))
    if "indication" not in questions_columns:
        await conn.execute(text("ALTER TABLE questions ADD COLUMN indication VARCHAR(128)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_questions_indication ON questions (indication)"))
    if "disease" not in questions_columns:
        await conn.execute(text("ALTER TABLE questions ADD COLUMN disease VARCHAR(128)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_questions_disease ON questions (disease)"))
    if "priority_weight" not in questions_columns:
        await conn.execute(text("ALTER TABLE questions ADD COLUMN priority_weight FLOAT DEFAULT 1.0"))

    responses_columns = await conn.run_sync(existing_columns, "responses")
    if "indication" not in responses_columns:
        await conn.execute(text("ALTER TABLE responses ADD COLUMN indication VARCHAR(128)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_responses_indication ON responses (indication)"))
    if "disease" not in responses_columns:
        await conn.execute(text("ALTER TABLE responses ADD COLUMN disease VARCHAR(128)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_responses_disease ON responses (disease)"))
    if "intent_type" not in responses_columns:
        await conn.execute(text("ALTER TABLE responses ADD COLUMN intent_type VARCHAR(16)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_responses_intent_type ON responses (intent_type)"))
    if "consensus_level" not in responses_columns:
        await conn.execute(text("ALTER TABLE responses ADD COLUMN consensus_level VARCHAR(16)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_responses_consensus_level ON responses (consensus_level)"))
    if "sources" not in responses_columns:
        await conn.execute(text("ALTER TABLE responses ADD COLUMN sources TEXT"))
    if "grounding_supports" not in responses_columns:
        await conn.execute(text("ALTER TABLE responses ADD COLUMN grounding_supports TEXT"))
    if "search_queries" not in responses_columns:
        await conn.execute(text("ALTER TABLE responses ADD COLUMN search_queries TEXT"))

    runs_columns = await conn.run_sync(existing_columns, "runs")
    if "consensus_full" not in runs_columns:
        await conn.execute(text("ALTER TABLE runs ADD COLUMN consensus_full INTEGER DEFAULT 0"))
    if "consensus_partial" not in runs_columns:
        await conn.execute(text("ALTER TABLE runs ADD COLUMN consensus_partial INTEGER DEFAULT 0"))
    if "consensus_missing" not in runs_columns:
        await conn.execute(text("ALTER TABLE runs ADD COLUMN consensus_missing INTEGER DEFAULT 0"))

    # FR-108a Disease-State / Pre-Launch mode: monitoring_mode + competitor_focus on
    # questions/responses, monitoring_mode on runs. SQLite cannot relax the historical
    # NOT NULL on brand_focus in place, so legacy question tables are rebuilt after all
    # additive question migrations finish. The ADD COLUMNs below back-fill mode fields
    # before that rebuild copies every row into the current nullable model schema.
    if "monitoring_mode" not in questions_columns:
        await conn.execute(text("ALTER TABLE questions ADD COLUMN monitoring_mode VARCHAR(16) DEFAULT 'BRAND'"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_questions_monitoring_mode ON questions (monitoring_mode)"))
    if "competitor_focus" not in questions_columns:
        await conn.execute(text("ALTER TABLE questions ADD COLUMN competitor_focus TEXT"))
    if "monitoring_mode" not in responses_columns:
        await conn.execute(text("ALTER TABLE responses ADD COLUMN monitoring_mode VARCHAR(16) DEFAULT 'BRAND'"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_responses_monitoring_mode ON responses (monitoring_mode)"))
    if "competitor_focus" not in responses_columns:
        await conn.execute(text("ALTER TABLE responses ADD COLUMN competitor_focus TEXT"))
    if "monitoring_mode" not in runs_columns:
        await conn.execute(text("ALTER TABLE runs ADD COLUMN monitoring_mode VARCHAR(16) DEFAULT 'BRAND'"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_runs_monitoring_mode ON runs (monitoring_mode)"))

    # FR-707a Model Release Event Correlation: annotate diffs with a correlated release.
    diffs_columns = await conn.run_sync(existing_columns, "response_diffs")
    if diffs_columns and "correlated_release_id" not in diffs_columns:
        await conn.execute(text("ALTER TABLE response_diffs ADD COLUMN correlated_release_id INTEGER"))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_response_diffs_correlated_release_id "
            "ON response_diffs (correlated_release_id)"
        ))

    # FR-707a: model updates are now auto-detected from drift spikes; tag each event's origin.
    releases_columns = await conn.run_sync(existing_columns, "model_release_log")
    if releases_columns and "source" not in releases_columns:
        await conn.execute(text("ALTER TABLE model_release_log ADD COLUMN source VARCHAR(16) DEFAULT 'manual'"))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_model_release_log_source "
            "ON model_release_log (source)"
        ))

    # FR-707a vendor version + changelog capture: enrich model_release_log with real
    # vendor signals (event kind, "what changed" summary, vendor effective date, our
    # first-seen timestamp, and an attribution confidence for the UI badge).
    if releases_columns:
        if "event_type" not in releases_columns:
            await conn.execute(text(
                "ALTER TABLE model_release_log ADD COLUMN event_type VARCHAR(16) DEFAULT 'release'"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_model_release_log_event_type "
                "ON model_release_log (event_type)"
            ))
        if "summary" not in releases_columns:
            await conn.execute(text("ALTER TABLE model_release_log ADD COLUMN summary TEXT"))
        if "effective_date" not in releases_columns:
            await conn.execute(text("ALTER TABLE model_release_log ADD COLUMN effective_date DATE"))
        if "first_seen_at" not in releases_columns:
            await conn.execute(text("ALTER TABLE model_release_log ADD COLUMN first_seen_at DATETIME"))
        if "confidence" not in releases_columns:
            await conn.execute(text("ALTER TABLE model_release_log ADD COLUMN confidence FLOAT"))
        if "alerted_at" not in releases_columns:
            await conn.execute(text("ALTER TABLE model_release_log ADD COLUMN alerted_at DATETIME"))

    consensus_columns = await conn.run_sync(existing_columns, "consensus_records")
    if consensus_columns:
        if "final_answer" not in consensus_columns:
            await conn.execute(text("ALTER TABLE consensus_records ADD COLUMN final_answer TEXT"))
        if "overall_sentiment" not in consensus_columns:
            await conn.execute(text("ALTER TABLE consensus_records ADD COLUMN overall_sentiment FLOAT"))
        if "sentiment_min" not in consensus_columns:
            await conn.execute(text("ALTER TABLE consensus_records ADD COLUMN sentiment_min FLOAT"))
        if "sentiment_max" not in consensus_columns:
            await conn.execute(text("ALTER TABLE consensus_records ADD COLUMN sentiment_max FLOAT"))
        if "overall_position" not in consensus_columns:
            await conn.execute(text("ALTER TABLE consensus_records ADD COLUMN overall_position VARCHAR(32)"))
        if "position_distribution" not in consensus_columns:
            await conn.execute(text("ALTER TABLE consensus_records ADD COLUMN position_distribution TEXT"))
        if "models_scored" not in consensus_columns:
            await conn.execute(text("ALTER TABLE consensus_records ADD COLUMN models_scored INTEGER DEFAULT 0"))

    harvested_columns = await conn.run_sync(existing_columns, "harvested_questions")
    if harvested_columns and "search_persona" not in harvested_columns:
        await conn.execute(text("ALTER TABLE harvested_questions ADD COLUMN search_persona VARCHAR(32)"))
    # Phase 7: evidence-generated questions stage in the SAME queue and carry the evidence
    # they rest on as a JSON proposal until promotion materialises the associations.
    if harvested_columns and "evidence_payload" not in harvested_columns:
        await conn.execute(text("ALTER TABLE harvested_questions ADD COLUMN evidence_payload TEXT"))

    # Social Listening: comment-sentiment + translation columns (social_comments is a
    # brand-new table created by create_all, so only social_posts needs back-fill ALTERs).
    social_columns = await conn.run_sync(existing_columns, "social_posts")
    if social_columns:
        if "text_original" not in social_columns:
            await conn.execute(text("ALTER TABLE social_posts ADD COLUMN text_original TEXT"))
        if "language" not in social_columns:
            await conn.execute(text("ALTER TABLE social_posts ADD COLUMN language VARCHAR(32)"))
        if "is_translated" not in social_columns:
            await conn.execute(text("ALTER TABLE social_posts ADD COLUMN is_translated BOOLEAN DEFAULT 0"))
        if "comment_sentiment" not in social_columns:
            await conn.execute(text("ALTER TABLE social_posts ADD COLUMN comment_sentiment FLOAT"))
        if "comments_captured" not in social_columns:
            await conn.execute(text("ALTER TABLE social_posts ADD COLUMN comments_captured INTEGER DEFAULT 0"))
        # Community-crawl enrichment (myRAteam/Bezzy): multi-drug mentions + patient signals.
        if "brand_mentions" not in social_columns:
            await conn.execute(text("ALTER TABLE social_posts ADD COLUMN brand_mentions TEXT"))
        if "patient_signals" not in social_columns:
            await conn.execute(text("ALTER TABLE social_posts ADD COLUMN patient_signals TEXT"))

    # Social Listening per-platform "AbbVie vs other brands" gists (social_briefs is created by
    # create_all, so existing dev DBs only need this back-fill ALTER for the new column).
    social_briefs_columns = await conn.run_sync(existing_columns, "social_briefs")
    if social_briefs_columns and "platform_summaries" not in social_briefs_columns:
        await conn.execute(text("ALTER TABLE social_briefs ADD COLUMN platform_summaries TEXT"))
    # Community-crawl unmet-need questions (voice-of-patient candidates).
    if social_briefs_columns and "unmet_questions" not in social_briefs_columns:
        await conn.execute(text("ALTER TABLE social_briefs ADD COLUMN unmet_questions TEXT"))

    # Source Authority Mapping (FR-706a.4): the alerts table gains entity_type/entity_id and
    # a NULLABLE score_id so non-scoring source-authority alerts can share it. Adding columns
    # is a simple ALTER, but relaxing score_id's NOT NULL needs a table rebuild on legacy DBs
    # (SQLite can't drop a column constraint in place). Rebuild once — when entity_type is
    # still missing — copying existing rows and stamping them entity_type='SCORE'.
    alerts_columns = await conn.run_sync(existing_columns, "alerts")
    if alerts_columns and "entity_type" not in alerts_columns:
        from app.models.alert import Alert  # local import: model already registered on Base

        await conn.execute(text("ALTER TABLE alerts RENAME TO alerts_legacy"))
        # SQLite keeps a table's named indexes on RENAME, so ix_alerts_* still exist and
        # would collide when we recreate the table from the model. Drop them first.
        legacy_indexes = await conn.run_sync(
            lambda c: [ix["name"] for ix in inspect(c).get_indexes("alerts_legacy") if ix.get("name")]
        )
        for ix_name in legacy_indexes:
            await conn.execute(text(f'DROP INDEX IF EXISTS "{ix_name}"'))
        await conn.run_sync(lambda c: Alert.__table__.create(c))
        await conn.execute(text(
            "INSERT INTO alerts "
            "(alert_id, score_id, response_id, entity_type, entity_id, rule_triggered, "
            " detail, acknowledged, created_at) "
            "SELECT alert_id, score_id, response_id, 'SCORE', NULL, rule_triggered, "
            " detail, acknowledged, created_at FROM alerts_legacy"
        ))
        await conn.execute(text("DROP TABLE alerts_legacy"))

    # GEO Intervention citation-gap fields (BR-005): a transparent citeability signal plus a
    # content brief + suggested monitoring questions on the recommendations table.
    recs_columns = await conn.run_sync(existing_columns, "recommendations")
    if recs_columns:
        if "citation_gap_score" not in recs_columns:
            await conn.execute(text("ALTER TABLE recommendations ADD COLUMN citation_gap_score FLOAT DEFAULT 0.0"))
        if "citation_multiplier" not in recs_columns:
            await conn.execute(text("ALTER TABLE recommendations ADD COLUMN citation_multiplier FLOAT DEFAULT 1.0"))
        if "content_brief" not in recs_columns:
            await conn.execute(text("ALTER TABLE recommendations ADD COLUMN content_brief TEXT"))
        if "suggested_questions" not in recs_columns:
            await conn.execute(text("ALTER TABLE recommendations ADD COLUMN suggested_questions TEXT"))
        # Phase 9: evidence-driven recommendations. `source_type` defaults to
        # POSITIONING_GAP so existing rows keep their true provenance with no backfill —
        # every one of them came from the positioning finder.
        for column, ddl in (
            ("source_type", "VARCHAR(32) DEFAULT 'POSITIONING_GAP'"),
            ("confidence", "FLOAT"),
            ("strategic_implication", "VARCHAR(48)"),
            ("implication_owner", "VARCHAR(64)"),
            ("externally_actionable", "BOOLEAN DEFAULT 1"),
            ("evidence_action", "TEXT"),
            ("claim_id", "VARCHAR(64)"),
            ("claim_text", "TEXT"),
            ("classification", "VARCHAR(32)"),
            ("certainty_verdict", "VARCHAR(16)"),
            ("finding_reason", "TEXT"),
            ("gap_attribution", "VARCHAR(16)"),
        ):
            if column not in recs_columns:
                await conn.execute(
                    text(f"ALTER TABLE recommendations ADD COLUMN {column} {ddl}")
                )

    # FR-116 real-prompt support: the staged row keeps the full prompt/question when the
    # upload provided one, and each coverage-gap alert carries the monitorable question so
    # "Create question" pre-fills a usable draft instead of a bare keyword.
    pv_staging_columns = await conn.run_sync(existing_columns, "prompt_volume_staging")
    if pv_staging_columns and "prompt_text" not in pv_staging_columns:
        await conn.execute(text("ALTER TABLE prompt_volume_staging ADD COLUMN prompt_text TEXT"))
    pv_alert_columns = await conn.run_sync(existing_columns, "prompt_volume_gap_alerts")
    if pv_alert_columns and "question" not in pv_alert_columns:
        await conn.execute(text("ALTER TABLE prompt_volume_gap_alerts ADD COLUMN question TEXT"))

    # Question Variations: grouping columns on questions so a base question and its
    # approved paraphrases share a variation group (question_variations is a brand-new
    # table created by create_all, so only questions needs back-fill ALTERs).
    if "variation_group_id" not in questions_columns:
        await conn.execute(text("ALTER TABLE questions ADD COLUMN variation_group_id VARCHAR(64)"))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_questions_variation_group_id "
            "ON questions (variation_group_id)"
        ))
    if "variation_of" not in questions_columns:
        await conn.execute(text("ALTER TABLE questions ADD COLUMN variation_of VARCHAR(64)"))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_questions_variation_of ON questions (variation_of)"
        ))
    if "is_variation" not in questions_columns:
        await conn.execute(text("ALTER TABLE questions ADD COLUMN is_variation BOOLEAN DEFAULT 0"))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_questions_is_variation ON questions (is_variation)"
        ))
    if "generation_method" not in questions_columns:
        await conn.execute(text("ALTER TABLE questions ADD COLUMN generation_method VARCHAR(32)"))
    if "demand_origin" not in questions_columns:
        await conn.execute(text("ALTER TABLE questions ADD COLUMN demand_origin VARCHAR(16)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_questions_demand_origin ON questions (demand_origin)"))

    await _make_questions_brand_focus_nullable(conn)
    await _make_responses_brand_focus_nullable(conn)

    # Source Authority RDAP + LLM enrichment (FR-706a): evidence + review-routing columns on
    # the domain cache. source_domains is created by create_all, so older dev DBs only need
    # these back-fill ALTERs for the new LLM-fallback classification to persist.
    source_domain_columns = await conn.run_sync(existing_columns, "source_domains")
    if source_domain_columns:
        if "classification_evidence" not in source_domain_columns:
            await conn.execute(text("ALTER TABLE source_domains ADD COLUMN classification_evidence TEXT"))
        if "requires_review" not in source_domain_columns:
            await conn.execute(text("ALTER TABLE source_domains ADD COLUMN requires_review BOOLEAN DEFAULT 0"))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_source_domains_requires_review "
                "ON source_domains (requires_review)"
            ))

    # FR-116 — analyst's per-upload choice of whether to auto-generate questions from bare
    # keywords. Older Prompt Volume batches predate the toggle and default to synthesis ON.
    pv_batch_columns = await conn.run_sync(existing_columns, "prompt_volume_batches")
    if pv_batch_columns and "synthesize_questions" not in pv_batch_columns:
        await conn.execute(text(
            "ALTER TABLE prompt_volume_batches ADD COLUMN synthesize_questions BOOLEAN DEFAULT 1"
        ))

    # Brand taxonomy: records which drug fields the source document declared as an explicit
    # null rather than omitting. The taxonomy tables are created by create_all, so only a
    # database that already holds them needs this back-fill — without it an insert fails with
    # "no column named null_fields_json" and the taxonomy cannot be seeded at all.
    taxonomy_drug_columns = await conn.run_sync(existing_columns, "taxonomy_drugs")
    if taxonomy_drug_columns and "null_fields_json" not in taxonomy_drug_columns:
        await conn.execute(text("ALTER TABLE taxonomy_drugs ADD COLUMN null_fields_json TEXT"))

    # Restore the default so the rename-rewrite behavior isn't left changed on the pooled
    # connection for ordinary app queries after startup.
    await conn.execute(text("PRAGMA legacy_alter_table=OFF"))
