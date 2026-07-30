"""Consensus record model — stores Chairman arbitration output per (run, question)."""
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ConsensusRecord(Base):
    """Stores the Chairman's consensus evaluation across all LLM responses for a question."""

    __tablename__ = "consensus_records"
    __table_args__ = (
        UniqueConstraint("run_id", "question_id", name="uq_consensus_run_question"),
    )

    consensus_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # UUID
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    question_id: Mapped[str] = mapped_column(String(64), index=True)

    consensus_level: Mapped[str] = mapped_column(String(16), index=True)
    # FULL | PARTIAL | MISSING

    agreed_recommendation: Mapped[str | None] = mapped_column(Text, nullable=True)
    divergence_points: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    # Synthesized "council" answer — single best answer merging all LLM responses (Chairman).
    final_answer: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Aggregate scoring across per-model responses (computed in the scoring pass, no LLM call).
    overall_sentiment: Mapped[float | None] = mapped_column(Float, nullable=True)  # mean of per-model sentiment
    sentiment_min: Mapped[float | None] = mapped_column(Float, nullable=True)  # dispersion low
    sentiment_max: Mapped[float | None] = mapped_column(Float, nullable=True)  # dispersion high
    overall_position: Mapped[str | None] = mapped_column(String(32), nullable=True)  # modal position
    position_distribution: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON {position: count}
    models_scored: Mapped[int] = mapped_column(Integer, default=0)  # number of scored responses aggregated

    geo_fallback_used: Mapped[bool] = mapped_column(default=False)
    geo_context: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON — brand schema data

    responses_evaluated: Mapped[int] = mapped_column(Integer, default=0)
    arbitration_model: Mapped[str] = mapped_column(String(128), default="")
    arbitration_tokens: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
