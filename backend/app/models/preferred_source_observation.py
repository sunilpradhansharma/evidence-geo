"""Preferred-source observation (FR-706a.7) — recorded DURING run/classification.

For every preferred source of a response's therapeutic area, one row records whether that
domain was present in the response's cited sources. Written by the post-run classification
pass (idempotent on ``(preferred_source_id, response_id)``), never by a GET. This turns the
"absence metric" into durable, queryable history (e.g. "how often does GPT omit FDA for
this TA?") instead of a value recomputed — and duplicated — on every dashboard refresh.
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PreferredSourceObservation(Base):
    """Was a TA's preferred domain present in a given response's citations?"""

    __tablename__ = "preferred_source_observations"
    __table_args__ = (
        UniqueConstraint(
            "preferred_source_id", "response_id", name="uq_pref_obs_source_response"
        ),
    )

    observation_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # UUID
    preferred_source_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    response_id: Mapped[str] = mapped_column(String(64), index=True)

    llm_name: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    therapeutic_area: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    authority_domain: Mapped[str] = mapped_column(String(255), index=True)

    was_present: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
