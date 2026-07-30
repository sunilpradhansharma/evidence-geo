"""Social-listening post model (Social Listening surface — Apify).

Public social posts (Reddit/TikTok/Instagram/Facebook/X) scraped via Apify, PII/PHI
redacted, LLM-tagged, and stored for AGGREGATE analytics only. No author identity is
ever persisted (handles are dropped at scrub). Internal demo — Legal/Privacy/PV sign-off
required before any production use.

METHODOLOGY: ``engagement_score`` is a RAW per-channel metric — a Reddit upvote, a TikTok
view, and an Instagram like are NOT the same thing. It is weighted/compared per channel and
directionally only, never summed or ranked across channels (see services/social_service.py).
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SocialPost(Base):
    __tablename__ = "social_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Provenance (audit/reviewer context — NOT author identity; handles scrubbed)
    channel: Mapped[str] = mapped_column(String(32), index=True)            # reddit/tiktok/instagram/facebook/x
    source: Mapped[str | None] = mapped_column(String(48), nullable=True)   # e.g. "apify:reddit"
    post_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_domain: Mapped[str | None] = mapped_column(String(128), nullable=True)
    search_term: Mapped[str | None] = mapped_column(String(128), nullable=True)  # seed term/hashtag that surfaced it

    # Post text (lightly PII/PHI-redacted — phrasing preserved). ``text`` is the English
    # canonical (searchable) text; when the post was translated, ``text_original`` holds the
    # redacted source-language text and ``language`` the detected source language. Translation
    # always runs AFTER redaction, so no raw identifier can leak via the English copy.
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_original: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_translated: Mapped[bool] = mapped_column(Boolean, default=False)
    dedupe_hash: Mapped[str] = mapped_column(String(64), index=True)

    # LLM tags
    brand_focus: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    therapeutic_area: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    domain: Mapped[str | None] = mapped_column(String(32), nullable=True)   # Efficacy/Safety/Access/Comparative/General
    topic: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    sentiment: Mapped[float | None] = mapped_column(Float, nullable=True)   # -1..1
    sentiment_label: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Engagement (RAW per-channel metric — see module docstring; never cross-channel summed)
    engagement_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)  # platform total
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    # Comment-sentiment rollup (SEPARATE dimension from post sentiment): the average
    # sentiment of the comments we actually captured for this post, and how many we scraped +
    # scored (distinct from ``comment_count``, the platform's reported total). Populated after
    # the comments phase of the ingest. See app.models.social_comment.SocialComment.
    comment_sentiment: Mapped[float | None] = mapped_column(Float, nullable=True)
    comments_captured: Mapped[int] = mapped_column(Integer, default=0)

    # Community-crawl enrichment (myRAteam / Bezzy RA only — see app.social.community).
    # Patient communities carry far more signal than the single ``brand_focus``/``topic`` tags
    # capture, so a dedicated community pass stores richer, multi-valued reads as JSON:
    #   brand_mentions  — JSON list of {name, generic, company, owner, sentiment, context}
    #                     for EVERY monitored drug/brand named on the page (not just one).
    #   patient_signals — JSON object {concerns[], journey_stage, switching_drivers[],
    #                     qol_impacts[], access_barriers[], questions[]}.
    # Both are NULL for the platform channels (reddit/tiktok/…), which use the generic
    # classifier. Generated from the already-redacted English text, so no identifier can leak.
    brand_mentions: Mapped[str | None] = mapped_column(Text, nullable=True)
    patient_signals: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Compliance flags
    ae_flag: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    pii_flags: Mapped[str | None] = mapped_column(Text, nullable=True)      # JSON list of redacted PII/PHI types

    harvested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
