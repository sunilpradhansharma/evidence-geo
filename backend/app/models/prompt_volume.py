"""AI Prompt Volume Intelligence models (FR-116).

Third-party SEARCH-DEMAND data (Semrush/Ahrefs) manually uploaded as a CSV and used as a
PROXY for AI-inquiry demand — NOT literal AI-prompt telemetry. Rows are append-only and
grouped into upload batches via ``batch_id``. PII is rejected pre-flight over the whole
file, so persisted rows are clean by construction.
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base

# The metric these rows represent — search-demand used as a proxy, never a literal AI count.
METRIC_SEARCH_VOLUME_PROXY = "search_volume_proxy"
# Demand derived from how often each distinct prompt RECURS in an upload that carries no
# volume column (a Profound / AlsoAsked prompt log). Still a proxy, not a literal AI count,
# but frequency-based rather than search-volume-based — labelled distinctly so the UI is honest.
METRIC_PROMPT_FREQUENCY = "prompt_frequency"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PromptVolumeBatch(Base):
    """One CSV upload. Carries the FR-116.5 forced source/date metadata + roll-up counts."""

    __tablename__ = "prompt_volume_batches"

    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # PV-<uuid>
    source_tool: Mapped[str] = mapped_column(String(64))       # Semrush | Ahrefs | Other (required)
    source_label: Mapped[str] = mapped_column(String(255))     # analyst-provided dataset label (required)
    dataset_date: Mapped[str] = mapped_column(String(32))      # analyst-provided ISO date (required)
    metric_type: Mapped[str] = mapped_column(String(32), default=METRIC_SEARCH_VOLUME_PROXY)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Whether keyword-only rows had a natural question auto-generated at ingest. Persisted so
    # on-demand gap re-computation (GET /gaps, alert sync) honours the analyst's upload choice.
    synthesize_questions: Mapped[bool] = mapped_column(Boolean, default=True)

    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_ingested: Mapped[int] = mapped_column(Integer, default=0)
    rows_rejected: Mapped[int] = mapped_column(Integer, default=0)
    gap_topics_flagged: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class PromptVolumeStaging(Base):
    """A single search query + volume, mapped to the taxonomy and matched to the bank."""

    __tablename__ = "prompt_volume_staging"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(64), index=True)

    query_text: Mapped[str] = mapped_column(Text)
    # The full natural-language question/prompt when the upload provided one (Profound /
    # AlsoAsked / AnswerThePublic / Semrush "Questions"); NULL for bare keyword exports.
    prompt_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_query: Mapped[str] = mapped_column(Text, index=True)  # dedupe / match key
    search_volume: Mapped[int] = mapped_column(Integer, default=0, index=True)
    keyword_difficulty: Mapped[float | None] = mapped_column(Float, nullable=True)
    cpc: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Taxonomy mapping (FR-116.1) — "Unmapped" + confidence 0.0 rather than a forced category.
    matched_therapeutic_area: Mapped[str] = mapped_column(String(64), default="Unmapped", index=True)
    matched_competitor: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    matched_brand: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    mapping_confidence: Mapped[float] = mapped_column(Float, default=0.0)

    # Bank coverage (FR-116.3/.4) — best-matching approved question, if any above threshold.
    matched_question_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    match_score: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
