"""Coverage-gap alert (FR-116.3 enhancement) — a trackable, de-duplicated gap signal.

Unlike scoring (FR-405) and source-authority (FR-706a.4) alerts — which are ABOUT a response
and therefore live in the shared, response-centric ``alerts`` table — a coverage gap is about a
PROMPT-VOLUME TOPIC that the Approved Question Bank does not cover. It has no response, and it
needs its own OPEN -> RESOLVED lifecycle (auto-resolved once the bank starts covering the
topic). Overloading the ``alerts`` table (non-nullable ``response_id``, digest inner-join to
Response, no ``resolved_at``) would corrupt those consumers, so gaps get a dedicated table.

De-dup is by ``topic_key`` (a normalized form of the topic label) so the same gap seen across
successive uploads maps to ONE row rather than a new alert each time.
"""
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base

STATUS_OPEN = "OPEN"
STATUS_RESOLVED = "RESOLVED"
STATUS_DISMISSED = "DISMISSED"

REASON_COVERED = "COVERED"  # auto-resolved: the Approved Question Bank now covers the topic


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PromptVolumeGapAlert(Base):
    __tablename__ = "prompt_volume_gap_alerts"

    alert_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # PVGAP-<uuid>
    # Stable cross-upload dedupe key = normalized topic label (one alert per distinct topic).
    topic_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    label: Mapped[str] = mapped_column(Text)
    # Monitorable question for this topic (real prompt from the upload, else synthesized from
    # the keyword) so "Create question" pre-fills a usable draft, not a bare keyword.
    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    therapeutic_area: Mapped[str | None] = mapped_column(String(64), nullable=True)
    competitor: Mapped[str | None] = mapped_column(String(128), nullable=True)

    status: Mapped[str] = mapped_column(String(16), default=STATUS_OPEN, index=True)

    # Latest demand signal for the topic (refreshed on every upload it appears in).
    combined_volume: Mapped[int] = mapped_column(Integer, default=0)
    opportunity_score: Mapped[float] = mapped_column(Float, default=0.0)
    query_count: Mapped[int] = mapped_column(Integer, default=0)

    first_seen_batch_id: Mapped[str] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_batch_id: Mapped[str] = mapped_column(String(64))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
