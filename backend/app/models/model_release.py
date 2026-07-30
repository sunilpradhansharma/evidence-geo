"""Model release log (FR-707a) — tracks REAL provider version changes / updates.

Each row is an anchored vendor "update event": a real version transition observed in
our own traffic (Response.llm_model_version) and/or a vendor changelog entry describing
what changed. When material response drift is detected, the differ checks this log within
a lookback window and annotates the drift when a release plausibly explains it (organic
prompt change vs. structural model update)."""
from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base

# Provenance of a model-release event, from most to least authoritative.
SOURCE_API = "api"            # a real version transition seen in our own traffic
SOURCE_CHANGELOG = "changelog"  # vendor changelog / release-notes entry (what changed)
SOURCE_INFERRED = "inferred"  # reverse-guessed from a response-drift spike (OE/EvidenceMD)
SOURCE_MANUAL = "manual"      # admin-entered (legacy)
SOURCE_SEED = "seed"          # curated demo data
# Legacy alias kept for back-compat with rows created by the old spike-inference path.
SOURCE_AUTO = "auto"

# What kind of change the event represents.
EVENT_RELEASE = "release"
EVENT_RETRAIN = "retrain"
EVENT_CAPABILITY = "capability"
EVENT_DEPRECATION = "deprecation"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ModelReleaseLog(Base):
    __tablename__ = "model_release_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # The target/platform this release applies to. Matched against Response.llm_name
    # (e.g. "Claude", "Nova-Pro", "Gemini") case-insensitively during correlation.
    target_platform: Mapped[str] = mapped_column(String(64), index=True)
    # Effective date used for correlation/timeline: the vendor's stated effective date
    # when known (changelog), else the date we first saw the version in our traffic.
    release_date: Mapped[date] = mapped_column(Date, index=True)
    version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    release_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Provenance (see SOURCE_* above): api | changelog | inferred | manual | seed | auto.
    source: Mapped[str] = mapped_column(String(16), default="api", index=True)

    # What changed (see EVENT_* above): release | retrain | capability | deprecation.
    event_type: Mapped[str] = mapped_column(String(16), default=EVENT_RELEASE, index=True)
    # Short "what changed" summary extracted from the vendor changelog (when matched).
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Vendor's own effective date (from the changelog), independent of our first-seen date.
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # When THIS version first appeared in our own responses (ground-truth transition).
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Attribution confidence 0..1 (api-confirmed=1.0; changelog-matched=extractor confidence;
    # inferred=low). Drives the UI confidence badge.
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # When we emitted the high-impact alert signal for this event (audit + digest). NULL
    # until it crosses the impact threshold; set once to keep the signal idempotent.
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelVersionObservation(Base):
    """Lightweight rollup of which vendor version strings we've actually seen in our own
    responses, per target. Derived from Response.llm_model_version; powers "current live
    version" + real version-transition detection without rescanning every response."""

    __tablename__ = "model_version_observation"
    __table_args__ = (
        UniqueConstraint("target_platform", "version", name="uq_version_obs_platform_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_platform: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(128), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    response_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
