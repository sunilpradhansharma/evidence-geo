"""Intervention model (Activation & Impact, thin v1).

An `Intervention` is the owned, auditable *business action* created from a GEO
recommendation. Unlike the append-only `Recommendation` (an analytical suggestion) and its
lightweight `RecommendationReview` triage note, an intervention carries ownership, a
publication record, a frozen measurement cohort, and links to the before/after measurement
snapshots + result. It is mutable *current* state; every transition is preserved in the
append-only `intervention_events` table (and mirrored to the general `audit_log`).

Thin-v1 scope: originates only from a GEO recommendation, no RBAC (free-text owner/reviewer),
no control arm, Patient/Prospect personas only (Provider parks in AWAITING_OPENEVIDENCE).
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base

# Workflow lifecycle (mutable current status; full history in intervention_events).
STATUSES = (
    "PROPOSED", "IN_PROGRESS", "PUBLISHED", "MEASURING", "COMPLETED",
    "DEFERRED", "CANCELLED",
)
DEFAULT_STATUS = "PROPOSED"

# Measurement state machine, advanced by the daily sweep (see activation/sweep.py).
MEASUREMENT_STATUSES = (
    "PLANNED",           # created; discovery baseline captured; not yet published
    "BASELINE_RUNNING",  # publish launched the official pre-publication baseline runs
    "MEASURING",         # official baseline finalized; waiting for post_due_at
    "POST_RUNNING",      # post-publication measurement runs launched
    "DONE",              # post snapshot + result computed
    "ERROR",             # a launch/compute step failed (surfaced, non-fatal)
)
DEFAULT_MEASUREMENT_STATUS = "PLANNED"

# Analytical outcome (deliberately non-causal for a single-arm comparison).
OUTCOME_STATUSES = ("IMPROVED", "NO_CLEAR_CHANGE", "WORSENED", "INCONCLUSIVE")

SOURCE_GEO_RECOMMENDATION = "GEO_RECOMMENDATION"
DEFAULT_PRIMARY_METRIC = "consideration_rate"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Intervention(Base):
    """A single owned, measurable intervention created from a GEO recommendation."""

    __tablename__ = "interventions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # UUID
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    # Provenance — the analytical recommendation this action addresses.
    recommendation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_type: Mapped[str] = mapped_column(String(32), default=SOURCE_GEO_RECOMMENDATION)
    source_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Frozen copy of the recommendation's evidence at creation (JSON) — "why this exists".
    evidence_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Denormalised filter dimensions (copied from the source recommendation).
    therapeutic_area: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    indication: Mapped[str | None] = mapped_column(String(128), nullable=True)
    brand_focus: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # Human-facing detail.
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str | None] = mapped_column(String(16), nullable=True)  # LOW | MEDIUM | HIGH

    # Lightweight ownership (NO auth/RBAC in v1 — plain free-text names).
    owner_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reviewer_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    review_required: Mapped[bool] = mapped_column(Boolean, default=False)
    review_status: Mapped[str | None] = mapped_column(String(24), nullable=True)

    status: Mapped[str] = mapped_column(String(24), default=DEFAULT_STATUS, index=True)

    # Publication record.
    publication_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    publication_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Frozen measurement cohort (locked at publish).
    monitoring_mode: Mapped[str] = mapped_column(String(16), default="BRAND")
    target_question_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list
    target_personas_json: Mapped[str | None] = mapped_column(Text, nullable=True)      # JSON list
    target_models_json: Mapped[str | None] = mapped_column(Text, nullable=True)        # JSON list; null=all enabled
    target_metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)       # JSON list of KPI keys
    primary_metric: Mapped[str] = mapped_column(String(48), default=DEFAULT_PRIMARY_METRIC)
    measurement_wait_days: Mapped[int] = mapped_column(Integer, default=14)
    repetitions_per_question: Mapped[int] = mapped_column(Integer, default=3)

    # Measurement lifecycle + snapshot links.
    measurement_status: Mapped[str] = mapped_column(
        String(24), default=DEFAULT_MEASUREMENT_STATUS, index=True
    )
    post_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discovery_baseline_snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    official_baseline_snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    post_snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    outcome_status: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
