"""Social-listening comment model (Social Listening surface — Apify).

Public comments/replies on the captured :class:`SocialPost` threads
(Reddit/TikTok/Instagram/Facebook/X), scraped via Apify, PII/PHI redacted, screened for
prompt-injection, AE-flagged (fail-closed for pharmacovigilance), and LLM-scored for
sentiment — stored for AGGREGATE analytics only. No author identity is ever persisted.

Comments are a SEPARATE sentiment dimension from posts: post sentiment stays the headline,
while comment sentiment captures the crowd's reaction. ``engagement_score`` (where a channel
exposes per-comment likes/upvotes) is a RAW per-channel metric, never summed across channels.
Internal demo — Legal/Privacy/PV sign-off required before any production use.
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SocialComment(Base):
    __tablename__ = "social_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Parent post this comment belongs to (the only linkage kept — no author identity).
    post_id: Mapped[int] = mapped_column(ForeignKey("social_posts.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)  # denormalized for filtering/aggregation

    # Comment text (lightly PII/PHI-redacted — phrasing preserved). ``text`` is the English
    # canonical text; when translated, ``text_original`` holds the redacted source-language
    # text and ``language`` the detected source language. Translation runs AFTER redaction.
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_original: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_translated: Mapped[bool] = mapped_column(Boolean, default=False)
    dedupe_hash: Mapped[str] = mapped_column(String(64), index=True)

    # LLM scoring (sentiment is the primary signal; topic optional/best-effort).
    sentiment: Mapped[float | None] = mapped_column(Float, nullable=True)   # -1..1
    sentiment_label: Mapped[str | None] = mapped_column(String(16), nullable=True)
    topic: Mapped[str | None] = mapped_column(String(160), nullable=True)

    # Engagement (RAW per-channel metric where available — e.g. comment upvotes/likes).
    engagement_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Compliance flags
    ae_flag: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    pii_flags: Mapped[str | None] = mapped_column(Text, nullable=True)      # JSON list of redacted PII/PHI types

    harvested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
