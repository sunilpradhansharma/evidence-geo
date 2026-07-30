"""Insight theme models (Advanced Analytics).

A `Theme` is a discovered topic in the Response Repository (an LLM-built taxonomy of
what the models are actually saying). `ResponseTheme` maps an immutable response to the
theme(s) it expresses, with the matched keywords kept as transparent evidence. Themes are
versioned: every full taxonomy rebuild bumps `taxonomy_version`, so historical assignments
are never mutated and the "current" view is always max(taxonomy_version).
"""
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Theme(Base):
    """A discovered theme/topic in the response corpus (one row per theme per version)."""

    __tablename__ = "themes"

    theme_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # UUID
    taxonomy_version: Mapped[int] = mapped_column(Integer, index=True, default=1)

    label: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    keywords: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list[str]
    category: Mapped[str | None] = mapped_column(String(48), nullable=True)  # optional grouping

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResponseTheme(Base):
    """Assignment of a response to a theme (keyword-evidence based, append-only)."""

    __tablename__ = "response_themes"
    __table_args__ = (
        UniqueConstraint("response_id", "theme_id", "taxonomy_version", name="uq_response_theme_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # UUID
    response_id: Mapped[str] = mapped_column(String(64), index=True)
    theme_id: Mapped[str] = mapped_column(String(64), index=True)
    taxonomy_version: Mapped[int] = mapped_column(Integer, index=True, default=1)

    relevance: Mapped[float] = mapped_column(Float, default=1.0)  # match strength (0..1)
    matched_keywords: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list[str]

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
