"""Preferred source model (FR-706a.7) — Medical-Affairs-designated authority domains per TA.

Soft-deletable with an audit trail (``active`` + ``effective_from``/``effective_to`` +
created/updated_by + change_reason) rather than hard delete, because source-policy changes
need to be reconstructable. Presence/absence of these domains in AI-cited sources is
recorded per run into ``preferred_source_observations`` (never computed on a GET).
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PreferredSource(Base):
    """A domain Medical Affairs wants AI models to cite for a given therapeutic area."""

    __tablename__ = "preferred_sources"
    __table_args__ = (
        UniqueConstraint("therapeutic_area", "authority_domain", name="uq_preferred_ta_domain"),
    )

    pref_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # UUID
    therapeutic_area: Mapped[str] = mapped_column(String(64), index=True)
    authority_domain: Mapped[str] = mapped_column(String(255), index=True)
    registrable_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by: Mapped[str] = mapped_column(String(64), default="Medical Affairs")
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    change_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
