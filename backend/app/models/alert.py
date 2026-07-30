"""Alert model (FR-405, FR-706a.4) — a triggered alert about a response.

Scoring alerts (FR-405) link to a scoring record via ``score_id``; source-authority alerts
(FR-706a.4) are not tied to a score, so ``score_id`` is nullable and the generic
``entity_type``/``entity_id`` pair identifies what the alert is about (e.g.
``SOURCE_AUTHORITY``). All alerts still set ``response_id``, so they surface in the existing
per-response alert panel and the ``alert_only`` filter regardless of type.
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base

# entity_type values
ENTITY_SCORE = "SCORE"
ENTITY_SOURCE_AUTHORITY = "SOURCE_AUTHORITY"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Alert(Base):
    __tablename__ = "alerts"

    alert_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # UUID
    # Nullable: source-authority alerts are not tied to a scoring record.
    score_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    response_id: Mapped[str] = mapped_column(String(64), index=True)

    # What the alert is about — lets non-scoring alerts share this table cleanly.
    entity_type: Mapped[str] = mapped_column(String(24), default=ENTITY_SCORE, index=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    rule_triggered: Mapped[str] = mapped_column(String(48), index=True)
    # LOW_SENTIMENT | NOT_RECOMMENDED | COMPETITOR_ADVANTAGE
    # | ONLY_COMPETITOR_SOURCES | COMPETITOR_CONTROLLED_TOP_SOURCE | UNVERIFIED_TOP_SOURCE
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
