"""Question-variation staging model (phrasing-robustness feature).

Claude-generated paraphrases of an approved base question land here FIRST as DRAFTs and
are NEVER sent to a monitored model until a human approves them (SE-001/SE-002 double
gate, mirroring ``harvested_question``). On approval a staging row is promoted to a real,
APPROVED ``Question`` row tagged into the base question's variation group, so the existing
run -> score -> consensus pipeline handles it unchanged.
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Lifecycle:
#   DRAFT     — generated (or manually added), awaiting human review
#   APPROVED  — reviewer approved; promoted to an APPROVED Question in the group
#   REJECTED  — reviewer discarded it (kept for audit, never runs)
STATUSES = ("DRAFT", "APPROVED", "REJECTED")


class QuestionVariation(Base):
    __tablename__ = "question_variations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Grouping. group_id == the base question's logical question_id; every variation of a
    # base question shares it, which is also stamped onto the promoted Question rows.
    variation_group_id: Mapped[str] = mapped_column(String(64), index=True)
    base_question_id: Mapped[str] = mapped_column(String(64), index=True)

    # The candidate phrasing (editable by the reviewer before approval).
    variation_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Stable normalized key used to de-duplicate drafts within a group (no double-gen spam).
    dedupe_hash: Mapped[str] = mapped_column(String(64), index=True)

    generation_method: Mapped[str] = mapped_column(String(32), default="CLAUDE")  # CLAUDE | MANUAL
    generation_model: Mapped[str | None] = mapped_column(String(128), nullable=True)  # model_id used

    # Compliance — PII/PHI categories found (JSON list). A non-empty flag blocks approval.
    pii_flags: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="DRAFT", index=True)
    promoted_question_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewer_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited: Mapped[bool] = mapped_column(Boolean, default=False)  # reviewer changed the text

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
