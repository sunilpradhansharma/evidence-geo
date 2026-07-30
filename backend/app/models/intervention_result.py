"""InterventionResult model — the before/after comparison for an intervention.

Single-arm (v1): the estimated change is `post_kpi - official_baseline_kpi` per metric, with
detected confounders and a confidence tier. Deliberately non-causal — the interpretation says
"a change was observed after publication", never "the intervention caused it". A control arm
and difference-in-differences are deferred to a later phase.
"""
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base

CONFIDENCE_TIERS = ("HIGH", "MEDIUM", "LOW")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class InterventionResult(Base):
    """The computed before/after result for one intervention (append-only per measurement)."""

    __tablename__ = "intervention_results"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # UUID
    intervention_id: Mapped[str] = mapped_column(String(64), index=True)

    baseline_snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    post_snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # {kpi_key: {"baseline": float, "post": float, "change": float, "unit": "pp"|"score"}}
    metric_changes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # [{"code": str, "detail": str}] — model-version / release / scorer / prompt / sample flags
    confounders_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    outcome_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    interpretation: Mapped[str | None] = mapped_column(Text, nullable=True)

    measured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
