"""Recommendation triage/review state (GEO workflow + BR-010 auditability).

Recommendations are append-only, immutable batch rows, so the mutable analyst triage state
(status / owner / note) lives here, keyed by the recommendation's ``rec_id``. This replaces
the browser-local (localStorage) status so triage is shared across users and every change is
audited. One row per recommendation, upserted in place.
"""
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base

# Analyst triage lifecycle for a recommendation.
REVIEW_STATUSES = ("NEW", "REVIEWING", "ACTIONED", "DISMISSED")
DEFAULT_STATUS = "NEW"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RecommendationReview(Base):
    """Mutable triage state for a single recommendation (by rec_id)."""

    __tablename__ = "recommendation_reviews"

    rec_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), default=DEFAULT_STATUS, index=True)
    owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    updated_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
