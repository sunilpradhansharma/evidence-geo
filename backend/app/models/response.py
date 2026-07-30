"""Response Repository model (FR-301..304, DM-002) — append-only, immutable."""
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Response(Base):
    """Immutable, append-only response record. Never UPDATEd or DELETEd in app code.
    Derived fields (sentiment, positioning) live in scoring_records and are joined at read time."""

    __tablename__ = "responses"
    __table_args__ = (
        # Resume support (FR-504): one row per (run, question, target)
        UniqueConstraint("run_id", "question_id", "llm_name", name="uq_run_question_llm"),
    )

    response_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # UUID
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    llm_name: Mapped[str] = mapped_column(String(64), index=True)
    llm_model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)

    persona: Mapped[str] = mapped_column(String(32), index=True)
    question_id: Mapped[str] = mapped_column(String(64), index=True)
    question_text: Mapped[str] = mapped_column(Text)  # denormalised
    therapeutic_area: Mapped[str] = mapped_column(String(64), index=True)
    indication: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    disease: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    # FR-108a: nullable for disease-state / pre-launch (brand-less) responses.
    brand_focus: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    domain: Mapped[str] = mapped_column(String(32), index=True)

    # Monitoring mode (FR-108a): BRAND | DISEASE_STATE — denormalised from the question.
    monitoring_mode: Mapped[str] = mapped_column(String(16), default="BRAND", index=True)
    competitor_focus: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list

    # Intent classification (Triage Gate) + consensus (Chairman arbitration)
    intent_type: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    consensus_level: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    # FULL | PARTIAL | MISSING — set by Chairman after all targets respond for this question

    response_text: Mapped[str] = mapped_column(Text)  # full, unedited (DM-002)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    response_tokens: Mapped[int] = mapped_column(Integer, default=0)

    # Real retrieval provenance (Type B). Populated for grounded targets (e.g. Gemini Search
    # grounding); empty for parametric calls.
    sources: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list of {url,title,domain,redirect_url,...}
    grounding_supports: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON claim->source map
    search_queries: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list of engine queries

    finish_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)  # stop|length|blocked|error
    status: Mapped[str] = mapped_column(String(16), index=True)  # SUCCESS|FAILED|TRUNCATED|BLOCKED

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
