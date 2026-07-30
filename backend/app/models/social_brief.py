"""Social-listening AI narrative brief model (Social Listening surface).

Stores the LLM-synthesized QUALITATIVE read of the captured social sample for one
therapeutic area: a short narrative ("what people are actually saying") plus a handful of
representative verbatim quotes pulled from real posts/comments. This complements the
quantitative aggregates in services/social_service.py (sentiment, share of voice, themes)
which only ever tell you the numbers, not the story behind them.

One row per therapeutic area (upserted at the end of each ingest). The verbatims are stored
as a JSON list. Generated from already-redacted post/comment text, so no raw identifier can
leak. Internal demo — Legal/Privacy/PV sign-off required before any production use.
"""
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SocialBrief(Base):
    __tablename__ = "social_briefs"

    # One brief per therapeutic area (Obesity for the GLP-1 demo).
    therapeutic_area: Mapped[str] = mapped_column(String(64), primary_key=True)

    # LLM-synthesized narrative summary of the captured sample.
    narrative: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Representative verbatim quotes, JSON list of {quote, channel, brand, sentiment, topic}.
    verbatims: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-platform "AbbVie vs other brands" gists, JSON object of {channel: gist text}.
    platform_summaries: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Unmet-need questions patients are asking in the community crawls (myRAteam/Bezzy),
    # deduped/clustered across the captured sample. JSON list of {question, theme, brand}.
    # Voice-of-patient candidates that can be promoted into the Question Bank / Discover.
    unmet_questions: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Provenance: how many posts/comments were summarized and the model used.
    posts_analyzed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
