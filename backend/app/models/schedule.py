"""Schedule model (FR-501, FR-502) — singleton row holding the daily-run config.

Exactly one row (id=1) ever exists. It persists whether the unattended daily
run is enabled, the cron expression, and the timezone the cron is evaluated in.
Living in the host-mounted SQLite DB means the on/off state survives redeploys.
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Schedule(Base):
    __tablename__ = "schedule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    # OFF by default — the daily run only fires once a user enables it in the UI.
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # Standard 5-field cron, evaluated in `timezone`. Default = midnight.
    cron: Mapped[str] = mapped_column(String(64), default="0 0 * * *")
    timezone: Mapped[str] = mapped_column(String(64), default="America/Chicago")

    # Bookkeeping for the UI.
    last_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
