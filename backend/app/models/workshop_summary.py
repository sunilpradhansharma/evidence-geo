"""Cached per-AI-platform "general summary" for the AI-answer insights (BR-008a).

One row per (scope, AI platform). ``scope`` is "workshop" (the curated Rhem.csv set) or
"all" (every tracked question), so each insights tab caches its own narratives. Stores an
LLM-synthesized 2-3 sentence read of HOW that platform positions AbbVie's brands across that
scope, plus an ``input_signature`` of the answers it was built from so we only regenerate when
the underlying answers change. Mirrors the Social Listening brief pattern (best-effort,
regenerated in the background). The narrative is synthesized ONLY from already-scored answer
synopses, never from fabricated text.
"""
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WorkshopPlatformSummary(Base):
    __tablename__ = "workshop_platform_summaries"

    # Which insights tab this summary belongs to: "workshop" or "all".
    scope: Mapped[str] = mapped_column(String(16), primary_key=True, default="workshop")
    # Raw platform id (e.g. "gpt-4o"); the friendly label is derived at read time.
    llm_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Signature of the answers the summary was built from; a mismatch => regenerate.
    input_signature: Mapped[str | None] = mapped_column(String(64), nullable=True)
    responses_analyzed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)  # provenance
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
