"""Audit log model (SE-003, FR-208, IN-302) — append-only record of every external call."""
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    role: Mapped[str] = mapped_column(String(16), index=True)  # ORCHESTRATOR | TARGET | SYSTEM
    event: Mapped[str] = mapped_column(String(64), index=True)

    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    question_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    llm_target: Mapped[str | None] = mapped_column(String(64), nullable=True)

    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    context: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON, credential-redacted
