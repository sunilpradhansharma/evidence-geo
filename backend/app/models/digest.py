"""Stakeholder-differentiated digest models (BR-008a).

Role-specific, admin-configurable intelligence digests. A DigestProfile maps an
organizational role (PV, Brand, Medical Affairs, …) to a cadence, recipients, and
delivery methods; its DigestRules narrow which alerts that role receives. Each run
is recorded as a DigestRun (the generated artifact + delivery bookkeeping)."""
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DigestProfile(Base):
    __tablename__ = "digest_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Organizational role this digest serves, e.g. "PV", "Brand", "Medical Affairs".
    role: Mapped[str] = mapped_column(String(64), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Weekly cadence (5-field cron), evaluated in `timezone` (BR-008a.1/4).
    cron: Mapped[str] = mapped_column(String(64), default="0 8 * * 1")  # Mon 08:00
    timezone: Mapped[str] = mapped_column(String(64), default="America/Chicago")

    recipients: Mapped[str | None] = mapped_column(Text, nullable=True)        # JSON list of emails
    delivery_methods: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list: email|webhook|in_app
    webhook_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    rules: Mapped[list["DigestRule"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", lazy="selectin",
    )
    # Deleting a profile also removes its generated digest history. Cascade at both the ORM
    # and DB layers so a profile with past runs can actually be deleted (SQLite runs with
    # PRAGMA foreign_keys=ON, which otherwise blocks the delete).
    runs: Mapped[list["DigestRun"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan",
    )


class DigestRule(Base):
    __tablename__ = "digest_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("digest_profiles.id", ondelete="CASCADE"), index=True)

    # Each filter is a JSON list; empty/None = no restriction on that dimension.
    # Alert categories map to Alert.rule_triggered (LOW_SENTIMENT / NOT_RECOMMENDED /
    # COMPETITOR_ADVANTAGE). domains/therapeutic_areas/personas/llm_names filter via the
    # linked Response (e.g. PV -> domains=["Safety"], Brand -> COMPETITOR_ADVANTAGE).
    alert_categories: Mapped[str | None] = mapped_column(Text, nullable=True)
    domains: Mapped[str | None] = mapped_column(Text, nullable=True)
    therapeutic_areas: Mapped[str | None] = mapped_column(Text, nullable=True)
    personas: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_names: Mapped[str | None] = mapped_column(Text, nullable=True)

    profile: Mapped["DigestProfile"] = relationship(back_populates="rules")


class DigestRun(Base):
    __tablename__ = "digest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("digest_profiles.id", ondelete="CASCADE"), index=True,
    )
    role: Mapped[str] = mapped_column(String(64), index=True)

    profile: Mapped["DigestProfile"] = relationship(back_populates="runs")

    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    findings_count: Mapped[int] = mapped_column(Integer, default=0)
    findings: Mapped[str | None] = mapped_column(Text, nullable=True)   # JSON list of top findings
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)    # LLM exec summary
    html: Mapped[str | None] = mapped_column(Text, nullable=True)       # rendered HTML body
    pdf_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    delivered_email: Mapped[bool] = mapped_column(Boolean, default=False)
    delivered_webhook: Mapped[bool] = mapped_column(Boolean, default=False)
    delivery_detail: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
