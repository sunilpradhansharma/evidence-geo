"""Declarative spec of every table mirrored from SQLite into Snowflake.

A single source of truth consumed by both ``schema.py`` (DDL bootstrap) and ``mirror.py``
(incremental sync). Each spec lists the source SQLAlchemy model, the primary key (used as
the MERGE key for idempotent upserts), a monotonic ``watermark`` column for incremental
reads, and the ordered columns with their Snowflake types.

JSON-ish columns are kept as VARCHAR in Snowflake (the SQLite side already stores JSON
text); Cortex functions operate on text fine, and this keeps binding trivial.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.models.alert import Alert
from app.models.audit_log import AuditLog
from app.models.consensus import ConsensusRecord
from app.models.digest import DigestProfile, DigestRule, DigestRun
from app.models.evaluation_claim import EvaluationClaim
from app.models.harvested_question import HarvestedQuestion
from app.models.model_release import ModelReleaseLog, ModelVersionObservation
from app.models.preferred_source import PreferredSource
from app.models.preferred_source_observation import PreferredSourceObservation
from app.models.prompt_volume import PromptVolumeBatch, PromptVolumeStaging
from app.models.prompt_volume_alert import PromptVolumeGapAlert
from app.models.question import Question
from app.models.question_variation import QuestionVariation
from app.models.recommendation import Recommendation
from app.models.recommendation_review import RecommendationReview
from app.models.response import Response
from app.models.response_citation import ResponseCitation
from app.models.response_diff import ResponseDiff
from app.models.run import Run
from app.models.scoring import ScoringRecord
from app.models.social_brief import SocialBrief
from app.models.social_comment import SocialComment
from app.models.social_post import SocialPost
from app.models.source_domain import SourceDomain
from app.models.theme import ResponseTheme, Theme

# Snowflake type aliases used below.
_INT = "NUMBER(38,0)"
_FLT = "FLOAT"
_BOOL = "BOOLEAN"
_TS = "TIMESTAMP_NTZ"
_STR = "VARCHAR"
_DATE = "DATE"


@dataclass(frozen=True)
class TableSpec:
    table: str                       # Snowflake table name
    model: type                      # source SQLAlchemy model
    pk: tuple[str, ...]              # MERGE key (model attribute names)
    watermark: str                   # monotonic column for incremental reads
    columns: list[tuple[str, str]]  # (attribute name == SF column, SF type)
    # Small, mutable tables (config/caches/rollups) that lack a reliable monotonic
    # "updated" column, or whose rows can be deleted, are snapshot-replaced every pass
    # (staging + atomic INSERT OVERWRITE) so Snowflake exactly matches SQLite — including
    # in-place edits and deletes. Large append-only tables keep watermark-incremental sync.
    full_refresh: bool = False

    @property
    def col_names(self) -> list[str]:
        return [c for c, _ in self.columns]


SPECS: list[TableSpec] = [
    TableSpec(
        table="QUESTIONS", model=Question, pk=("id",), watermark="updated_at",
        columns=[
            ("id", _INT), ("question_id", _STR), ("question_text", _STR),
            ("persona", _STR), ("therapeutic_area", _STR), ("indication", _STR),
            ("disease", _STR), ("brand_focus", _STR), ("domain", _STR),
            ("monitoring_mode", _STR), ("competitor_focus", _STR),
            ("intent_type", _STR), ("active", _BOOL), ("priority_weight", _FLT),
            ("demand_origin", _STR), ("approval_status", _STR),
            ("approver_name", _STR), ("version", _INT), ("superseded_by", _INT),
            ("variation_group_id", _STR), ("variation_of", _STR),
            ("is_variation", _BOOL), ("generation_method", _STR),
            ("deleted_at", _TS), ("delete_reason", _STR),
            ("created_at", _TS), ("updated_at", _TS),
        ],
    ),
    TableSpec(
        table="RESPONSES", model=Response, pk=("response_id",), watermark="created_at",
        columns=[
            ("response_id", _STR), ("run_id", _STR), ("timestamp_utc", _TS),
            ("llm_name", _STR), ("llm_model_version", _STR), ("persona", _STR),
            ("question_id", _STR), ("question_text", _STR), ("therapeutic_area", _STR),
            ("indication", _STR), ("disease", _STR), ("brand_focus", _STR),
            ("domain", _STR), ("monitoring_mode", _STR), ("competitor_focus", _STR),
            ("intent_type", _STR), ("consensus_level", _STR), ("response_text", _STR),
            ("prompt_tokens", _INT), ("response_tokens", _INT), ("sources", _STR),
            ("grounding_supports", _STR), ("search_queries", _STR),
            ("finish_reason", _STR), ("status", _STR), ("created_at", _TS),
        ],
    ),
    TableSpec(
        table="SCORING_RECORDS", model=ScoringRecord, pk=("score_id",), watermark="created_at",
        columns=[
            ("score_id", _STR), ("response_id", _STR), ("score_version", _INT),
            ("prompt_version", _STR), ("sentiment_score", _FLT),
            ("competitive_position", _STR), ("brand_mentions", _STR), ("key_claims", _STR),
            ("scoring_rationale", _STR), ("scored_by", _STR), ("override_rationale", _STR),
            ("created_at", _TS),
        ],
    ),
    TableSpec(
        table="CONSENSUS_RECORDS", model=ConsensusRecord, pk=("consensus_id",), watermark="created_at",
        columns=[
            ("consensus_id", _STR), ("run_id", _STR), ("question_id", _STR),
            ("consensus_level", _STR), ("agreed_recommendation", _STR),
            ("divergence_points", _STR), ("confidence", _FLT), ("final_answer", _STR),
            ("overall_sentiment", _FLT), ("sentiment_min", _FLT), ("sentiment_max", _FLT),
            ("overall_position", _STR), ("position_distribution", _STR),
            ("models_scored", _INT), ("geo_fallback_used", _BOOL), ("geo_context", _STR),
            ("responses_evaluated", _INT), ("arbitration_model", _STR),
            ("arbitration_tokens", _INT), ("created_at", _TS),
        ],
    ),
    TableSpec(
        table="ALERTS", model=Alert, pk=("alert_id",), watermark="created_at",
        columns=[
            ("alert_id", _STR), ("score_id", _STR), ("response_id", _STR),
            ("entity_type", _STR), ("entity_id", _STR),
            ("rule_triggered", _STR), ("detail", _STR), ("acknowledged", _BOOL),
            ("created_at", _TS),
        ],
    ),
    TableSpec(
        table="AUDIT_LOG", model=AuditLog, pk=("id",), watermark="id",
        columns=[
            ("id", _INT), ("timestamp", _TS), ("role", _STR), ("event", _STR),
            ("run_id", _STR), ("question_id", _STR), ("llm_target", _STR),
            ("http_status", _INT), ("tokens", _INT), ("context", _STR),
        ],
    ),
    TableSpec(
        table="RUNS", model=Run, pk=("run_id",), watermark="started_at",
        columns=[
            ("run_id", _STR), ("trigger", _STR), ("monitoring_mode", _STR),
            ("status", _STR), ("started_at", _TS), ("ended_at", _TS),
            ("questions_attempted", _INT), ("responses_success", _INT),
            ("responses_failed", _INT), ("responses_truncated", _INT),
            ("responses_blocked", _INT), ("total_tokens", _INT),
            ("estimated_cost_usd", _FLT), ("alerts_triggered", _INT),
            ("consensus_full", _INT), ("consensus_partial", _INT),
            ("consensus_missing", _INT), ("config_snapshot", _STR), ("notes", _STR),
        ],
    ),
    TableSpec(
        table="THEMES", model=Theme, pk=("theme_id",), watermark="created_at",
        columns=[
            ("theme_id", _STR), ("taxonomy_version", _INT), ("label", _STR),
            ("description", _STR), ("keywords", _STR), ("category", _STR),
            ("created_at", _TS),
        ],
    ),
    TableSpec(
        table="RESPONSE_THEMES", model=ResponseTheme, pk=("id",), watermark="created_at",
        columns=[
            ("id", _STR), ("response_id", _STR), ("theme_id", _STR),
            ("taxonomy_version", _INT), ("relevance", _FLT), ("matched_keywords", _STR),
            ("created_at", _TS),
        ],
    ),
    TableSpec(
        table="RESPONSE_DIFFS", model=ResponseDiff, pk=("id",), watermark="created_at",
        columns=[
            ("id", _INT), ("question_id", _STR), ("llm_name", _STR),
            ("current_response_id", _STR), ("previous_response_id", _STR),
            ("similarity_ratio", _FLT), ("material_change", _BOOL), ("diff_text", _STR),
            ("correlated_release_id", _INT), ("created_at", _TS),
        ],
    ),
    TableSpec(
        table="HARVESTED_QUESTIONS", model=HarvestedQuestion, pk=("id",), watermark="updated_at",
        columns=[
            ("id", _INT), ("source", _STR), ("source_url", _STR), ("source_domain", _STR),
            ("source_title", _STR), ("search_query", _STR), ("raw_excerpt", _STR),
            ("question_text", _STR), ("dedupe_hash", _STR), ("persona", _STR),
            ("therapeutic_area", _STR), ("brand_focus", _STR), ("domain", _STR),
            ("intent_type", _STR), ("relevance_score", _FLT), ("search_persona", _STR),
            ("pii_flags", _STR), ("ae_flag", _BOOL), ("status", _STR),
            ("promoted_question_id", _STR), ("review_note", _STR),
            # Phase 7. The evidence proposal behind a generated question, mirrored as text
            # so Cortex can answer "which staged questions rest on which studies?" without
            # a second sync of the association table.
            ("evidence_payload", _STR),
            ("harvested_at", _TS), ("updated_at", _TS),
        ],
    ),
    # Phase 8 claim-level evaluation. Append-only: a re-extraction writes new claim rows
    # rather than editing old ones, so `created_at` is a sound watermark and Cortex can be
    # asked "which models contradicted the label this month" over the whole history.
    TableSpec(
        table="EVALUATION_CLAIMS", model=EvaluationClaim, pk=("claim_id",),
        watermark="created_at",
        columns=[
            ("claim_id", _STR), ("response_id", _STR), ("run_id", _STR),
            ("question_id", _STR), ("llm_name", _STR),
            ("claim_text", _STR), ("claim_type", _STR), ("subject", _STR),
            ("comparator", _STR), ("indication", _STR), ("outcome", _STR),
            ("direction", _STR), ("polarity", _STR), ("certainty", _STR),
            ("magnitude", _FLT), ("magnitude_unit", _STR), ("cited_identifiers", _STR),
            ("expected_evidence_policy", _STR),
            ("classification", _STR), ("reason", _STR), ("dimensions", _STR),
            ("evidence_links", _STR), ("certainty_verdict", _STR), ("flags", _STR),
            ("is_adverse", _BOOL),
            ("extracted_by", _STR), ("extraction_version", _STR), ("claim_index", _INT),
            ("created_at", _TS),
        ],
    ),
    # Social Listening. Watermark on the autoincrement id: post rows are immutable after the
    # ingest finishes (the comment-sentiment rollup is written during the same run, before the
    # post-ingest mirror fires), so id-based incremental sync captures their final state.
    TableSpec(
        table="SOCIAL_POSTS", model=SocialPost, pk=("id",), watermark="id",
        columns=[
            ("id", _INT), ("channel", _STR), ("source", _STR), ("post_url", _STR),
            ("source_domain", _STR), ("search_term", _STR), ("text", _STR),
            ("text_original", _STR), ("language", _STR), ("is_translated", _BOOL),
            ("dedupe_hash", _STR), ("brand_focus", _STR), ("therapeutic_area", _STR),
            ("domain", _STR), ("topic", _STR), ("sentiment", _FLT), ("sentiment_label", _STR),
            ("engagement_score", _INT), ("comment_count", _INT), ("comment_sentiment", _FLT),
            ("comments_captured", _INT), ("posted_at", _TS), ("ae_flag", _BOOL),
            ("pii_flags", _STR), ("brand_mentions", _STR), ("patient_signals", _STR),
            ("harvested_at", _TS),
        ],
    ),
    TableSpec(
        table="SOCIAL_COMMENTS", model=SocialComment, pk=("id",), watermark="id",
        columns=[
            ("id", _INT), ("post_id", _INT), ("channel", _STR), ("text", _STR),
            ("text_original", _STR), ("language", _STR), ("is_translated", _BOOL),
            ("dedupe_hash", _STR), ("sentiment", _FLT), ("sentiment_label", _STR),
            ("topic", _STR), ("engagement_score", _INT), ("posted_at", _TS),
            ("ae_flag", _BOOL), ("pii_flags", _STR), ("harvested_at", _TS),
        ],
    ),
    # ------------------------------------------------------------------ #
    # Source Authority (FR-706a)
    # ------------------------------------------------------------------ #
    TableSpec(
        table="SOURCE_DOMAINS", model=SourceDomain, pk=("domain_id",),
        watermark="created_at", full_refresh=True,
        columns=[
            ("domain_id", _STR), ("authority_domain", _STR), ("registrable_domain", _STR),
            ("publisher_name", _STR), ("registrant_organization", _STR), ("registrar_name", _STR),
            ("control_type", _STR), ("authority_type", _STR), ("display_category", _STR),
            ("verification", _STR), ("whois_visibility", _STR), ("classification_status", _STR),
            ("classification_source", _STR), ("classification_confidence", _FLT),
            ("classification_reason", _STR), ("classification_evidence", _STR),
            ("requires_review", _BOOL), ("rules_version", _INT), ("enriched_at", _TS),
            ("enrichment_expires_at", _TS), ("created_at", _TS),
        ],
    ),
    TableSpec(
        table="RESPONSE_CITATIONS", model=ResponseCitation, pk=("citation_id",),
        watermark="created_at",
        columns=[
            ("citation_id", _STR), ("response_id", _STR), ("run_id", _STR), ("domain_id", _STR),
            ("authority_domain", _STR), ("llm_name", _STR), ("persona", _STR),
            ("therapeutic_area", _STR), ("indication", _STR), ("brand_focus", _STR),
            ("citation_count", _INT), ("citation_urls", _STR), ("first_citation_position", _INT),
            ("created_at", _TS),
        ],
    ),
    TableSpec(
        table="PREFERRED_SOURCES", model=PreferredSource, pk=("pref_id",),
        watermark="updated_at", full_refresh=True,
        columns=[
            ("pref_id", _STR), ("therapeutic_area", _STR), ("authority_domain", _STR),
            ("registrable_domain", _STR), ("note", _STR), ("active", _BOOL),
            ("effective_from", _TS), ("effective_to", _TS), ("created_by", _STR),
            ("updated_by", _STR), ("change_reason", _STR), ("created_at", _TS), ("updated_at", _TS),
        ],
    ),
    TableSpec(
        table="PREFERRED_SOURCE_OBSERVATIONS", model=PreferredSourceObservation,
        pk=("observation_id",), watermark="observed_at",
        columns=[
            ("observation_id", _STR), ("preferred_source_id", _STR), ("run_id", _STR),
            ("response_id", _STR), ("llm_name", _STR), ("therapeutic_area", _STR),
            ("authority_domain", _STR), ("was_present", _BOOL), ("observed_at", _TS),
        ],
    ),
    # ------------------------------------------------------------------ #
    # Prompt Volume Intelligence (FR-116)
    # ------------------------------------------------------------------ #
    TableSpec(
        table="PROMPT_VOLUME_BATCHES", model=PromptVolumeBatch, pk=("batch_id",),
        watermark="created_at",
        columns=[
            ("batch_id", _STR), ("source_tool", _STR), ("source_label", _STR),
            ("dataset_date", _STR), ("metric_type", _STR), ("filename", _STR),
            ("synthesize_questions", _BOOL), ("rows_total", _INT), ("rows_ingested", _INT),
            ("rows_rejected", _INT), ("gap_topics_flagged", _INT), ("created_at", _TS),
        ],
    ),
    TableSpec(
        table="PROMPT_VOLUME_STAGING", model=PromptVolumeStaging, pk=("id",), watermark="id",
        columns=[
            ("id", _INT), ("batch_id", _STR), ("query_text", _STR), ("prompt_text", _STR),
            ("normalized_query", _STR), ("search_volume", _INT), ("keyword_difficulty", _FLT),
            ("cpc", _FLT), ("matched_therapeutic_area", _STR), ("matched_competitor", _STR),
            ("matched_brand", _STR), ("mapping_confidence", _FLT), ("matched_question_id", _STR),
            ("match_score", _FLT), ("created_at", _TS),
        ],
    ),
    TableSpec(
        table="PROMPT_VOLUME_GAP_ALERTS", model=PromptVolumeGapAlert, pk=("alert_id",),
        watermark="updated_at",
        columns=[
            ("alert_id", _STR), ("topic_key", _STR), ("label", _STR), ("question", _STR),
            ("therapeutic_area", _STR), ("competitor", _STR), ("status", _STR),
            ("combined_volume", _INT), ("opportunity_score", _FLT), ("query_count", _INT),
            ("first_seen_batch_id", _STR), ("first_seen_at", _TS), ("last_seen_batch_id", _STR),
            ("last_seen_at", _TS), ("resolved_at", _TS), ("resolved_reason", _STR),
            ("created_at", _TS), ("updated_at", _TS),
        ],
    ),
    # ------------------------------------------------------------------ #
    # GEO Intervention Recommendations (BR-012)
    # ------------------------------------------------------------------ #
    TableSpec(
        table="RECOMMENDATIONS", model=Recommendation, pk=("rec_id",), watermark="created_at",
        columns=[
            ("rec_id", _STR), ("batch_id", _STR), ("created_at", _TS),
            ("source_response_id", _STR), ("question_id", _STR), ("run_id", _STR),
            ("persona", _STR), ("therapeutic_area", _STR), ("indication", _STR),
            ("brand_focus", _STR), ("llm_name", _STR), ("competitive_position", _STR),
            ("gap_severity", _FLT), ("outperforming_competitor", _STR), ("competitor_domain", _STR),
            ("missing_citations", _STR), ("search_volume", _INT), ("domain_authority", _INT),
            ("metrics_source", _STR), ("volume_multiplier", _FLT), ("citation_gap_score", _FLT),
            ("citation_multiplier", _FLT), ("content_type", _STR), ("recommended_action", _STR),
            ("rationale", _STR), ("content_brief", _STR), ("suggested_questions", _STR),
            ("impact_score", _FLT), ("mlr_status", _STR),
            # Phase 9. `externally_actionable` in particular belongs in Cortex: "how much of
            # this quarter's backlog is content work and how much is our own curation" is a
            # question the recommendation list alone cannot answer.
            ("source_type", _STR), ("confidence", _FLT), ("strategic_implication", _STR),
            ("implication_owner", _STR), ("externally_actionable", _BOOL),
            ("evidence_action", _STR), ("claim_id", _STR), ("claim_text", _STR),
            ("classification", _STR), ("certainty_verdict", _STR), ("finding_reason", _STR),
            ("gap_attribution", _STR),
        ],
    ),
    TableSpec(
        table="RECOMMENDATION_REVIEWS", model=RecommendationReview, pk=("rec_id",),
        watermark="updated_at",
        columns=[
            ("rec_id", _STR), ("status", _STR), ("owner", _STR), ("note", _STR),
            ("updated_by", _STR), ("updated_at", _TS), ("created_at", _TS),
        ],
    ),
    # ------------------------------------------------------------------ #
    # Model Release / Version tracking (FR-707a)
    # ------------------------------------------------------------------ #
    TableSpec(
        table="MODEL_RELEASE_LOG", model=ModelReleaseLog, pk=("id",),
        watermark="id", full_refresh=True,
        columns=[
            ("id", _INT), ("target_platform", _STR), ("release_date", _DATE), ("version", _STR),
            ("release_notes", _STR), ("url", _STR), ("source", _STR), ("event_type", _STR),
            ("summary", _STR), ("effective_date", _DATE), ("first_seen_at", _TS),
            ("confidence", _FLT), ("alerted_at", _TS), ("created_at", _TS),
        ],
    ),
    TableSpec(
        table="MODEL_VERSION_OBSERVATION", model=ModelVersionObservation, pk=("id",),
        watermark="updated_at", full_refresh=True,
        columns=[
            ("id", _INT), ("target_platform", _STR), ("version", _STR), ("first_seen_at", _TS),
            ("last_seen_at", _TS), ("response_count", _INT), ("created_at", _TS), ("updated_at", _TS),
        ],
    ),
    # ------------------------------------------------------------------ #
    # Stakeholder Digests (BR-008a)
    # ------------------------------------------------------------------ #
    TableSpec(
        table="DIGEST_PROFILES", model=DigestProfile, pk=("id",),
        watermark="updated_at", full_refresh=True,
        columns=[
            ("id", _INT), ("role", _STR), ("description", _STR), ("enabled", _BOOL),
            ("cron", _STR), ("timezone", _STR), ("recipients", _STR), ("delivery_methods", _STR),
            ("webhook_url", _STR), ("created_at", _TS), ("updated_at", _TS),
        ],
    ),
    TableSpec(
        table="DIGEST_RULES", model=DigestRule, pk=("id",), watermark="id", full_refresh=True,
        columns=[
            ("id", _INT), ("profile_id", _INT), ("alert_categories", _STR), ("domains", _STR),
            ("therapeutic_areas", _STR), ("personas", _STR), ("llm_names", _STR),
        ],
    ),
    TableSpec(
        table="DIGEST_RUNS", model=DigestRun, pk=("id",), watermark="id",
        columns=[
            ("id", _INT), ("profile_id", _INT), ("role", _STR), ("generated_at", _TS),
            ("period_start", _TS), ("period_end", _TS), ("findings_count", _INT),
            ("findings", _STR), ("summary", _STR), ("html", _STR), ("pdf_path", _STR),
            ("delivered_email", _BOOL), ("delivered_webhook", _BOOL), ("delivery_detail", _STR),
        ],
    ),
    # ------------------------------------------------------------------ #
    # Question Variations (phrasing robustness)
    # ------------------------------------------------------------------ #
    TableSpec(
        table="QUESTION_VARIATIONS", model=QuestionVariation, pk=("id",), watermark="updated_at",
        columns=[
            ("id", _INT), ("variation_group_id", _STR), ("base_question_id", _STR),
            ("variation_text", _STR), ("dedupe_hash", _STR), ("generation_method", _STR),
            ("generation_model", _STR), ("pii_flags", _STR), ("status", _STR),
            ("promoted_question_id", _STR), ("reviewer_name", _STR), ("review_note", _STR),
            ("edited", _BOOL), ("created_at", _TS), ("updated_at", _TS),
        ],
    ),
    # ------------------------------------------------------------------ #
    # Social Listening AI narrative brief
    # ------------------------------------------------------------------ #
    TableSpec(
        table="SOCIAL_BRIEFS", model=SocialBrief, pk=("therapeutic_area",),
        watermark="updated_at", full_refresh=True,
        columns=[
            ("therapeutic_area", _STR), ("narrative", _STR), ("verbatims", _STR),
            ("platform_summaries", _STR), ("unmet_questions", _STR),
            ("posts_analyzed", _INT), ("model", _STR), ("updated_at", _TS),
        ],
    ),
]


def spec_by_table() -> dict[str, TableSpec]:
    return {s.table: s for s in SPECS}
