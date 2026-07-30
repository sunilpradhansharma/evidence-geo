"""Run model (FR-503, NF-008) — a single scheduled or ad-hoc execution batch."""
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Run(Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # UUID

    trigger: Mapped[str] = mapped_column(String(16), default="ADHOC")  # SCHEDULED | ADHOC

    # Monitoring mode (FR-108a): BRAND | DISEASE_STATE — a disease-state run executes the
    # brand-less landscape question set and produces multi-competitor scoring.
    monitoring_mode: Mapped[str] = mapped_column(String(16), default="BRAND", index=True)

    status: Mapped[str] = mapped_column(String(32), default="RUNNING", index=True)
    # RUNNING | COMPLETED | FAILED | PAUSED_BUDGET | CANCELLED | AWAITING_OPENEVIDENCE
    # AWAITING_OPENEVIDENCE: an ad-hoc run with Provider questions has finished its
    # automated targets and is paused for manual OpenEvidence capture before Provider
    # consensus is computed (see openevidence_service). Auto-completes once every
    # Provider question has an OpenEvidence answer, or via "finalize without OE".

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    questions_attempted: Mapped[int] = mapped_column(Integer, default=0)
    responses_success: Mapped[int] = mapped_column(Integer, default=0)
    responses_failed: Mapped[int] = mapped_column(Integer, default=0)
    responses_truncated: Mapped[int] = mapped_column(Integer, default=0)
    responses_blocked: Mapped[int] = mapped_column(Integer, default=0)

    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    alerts_triggered: Mapped[int] = mapped_column(Integer, default=0)

    # Chairman consensus counts per run
    consensus_full: Mapped[int] = mapped_column(Integer, default=0)
    consensus_partial: Mapped[int] = mapped_column(Integer, default=0)
    consensus_missing: Mapped[int] = mapped_column(Integer, default=0)

    config_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
