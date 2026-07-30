"""InterventionEvent model — append-only workflow timeline (Activation & Impact).

This is the *authoritative* history for an intervention: every status transition,
assignment, publication and measurement step is recorded here and never mutated. A compact
mirror of each event is also written to the general `audit_log` (via utils.audit) so the
system-wide compliance trail stays complete, but the business-workflow semantics live here
(the call-centric audit_log schema is not shaped for state machines).
"""
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class InterventionEvent(Base):
    """One immutable event in an intervention's timeline."""

    __tablename__ = "intervention_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    intervention_id: Mapped[str] = mapped_column(String(64), index=True)

    event_type: Mapped[str] = mapped_column(String(48), index=True)
    # e.g. CREATED | STATUS_CHANGED | ASSIGNED | PUBLISHED | BASELINE_CAPTURED
    #      | MEASUREMENT_STARTED | MEASUREMENT_COMPLETED | OUTCOME_RECORDED
    previous_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    new_status: Mapped[str | None] = mapped_column(String(24), nullable=True)

    actor_name: Mapped[str | None] = mapped_column(String(120), nullable=True)  # free text (no auth)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
