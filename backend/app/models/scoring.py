"""Scoring record model (FR-304, FR-401..408) — versioned, separate from responses."""
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ScoringRecord(Base):
    __tablename__ = "scoring_records"

    score_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # UUID
    response_id: Mapped[str] = mapped_column(String(64), index=True)

    score_version: Mapped[int] = mapped_column(Integer, default=1)
    prompt_version: Mapped[str] = mapped_column(String(32), default="v1")

    sentiment_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # -1.0 .. +1.0
    competitive_position: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # FIRST_LINE_RECOMMENDED | AMONG_OPTIONS | SECOND_LINE | NOT_RECOMMENDED | NOT_MENTIONED

    brand_mentions: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list
    key_claims: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list (<=5)
    scoring_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)

    scored_by: Mapped[str] = mapped_column(String(128), default="AI")  # model id or HUMAN
    override_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
