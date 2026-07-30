"""Question Repository model (FR-101..107, SE-002, DM-003)."""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question_id: Mapped[str] = mapped_column(String(64), index=True)  # logical id, stable across versions

    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    persona: Mapped[str] = mapped_column(String(32), index=True)  # Prospect | Provider | Patient
    therapeutic_area: Mapped[str] = mapped_column(String(64), index=True)
    indication: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    disease: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    # FR-108a: brand_focus is OPTIONAL — disease-state / pre-launch questions have no
    # primary AbbVie brand asset and instead carry competitor tags.
    brand_focus: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    domain: Mapped[str] = mapped_column(String(32), index=True)  # Efficacy|Safety|Access|Comparative|General

    # Monitoring mode (FR-108a). BRAND = standard focus-brand run; DISEASE_STATE =
    # brand-less landscape/pre-launch run scored as a multi-competitor matrix.
    monitoring_mode: Mapped[str] = mapped_column(String(16), default="BRAND", index=True)
    # Competitor tags for DISEASE_STATE questions — JSON list of competitor brand names.
    competitor_focus: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Intent classification (Triage Gate)
    intent_type: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    # CLINICAL | EXPERIENTIAL | SHORTHAND | SCREENING — populated by classifier

    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    # FR-116.4 — internal priority weight for demand ranking (priority_weight × search volume)
    priority_weight: Mapped[float] = mapped_column(Float, default=1.0)

    # FR-116 — provenance for questions created from a Prompt Volume coverage gap:
    #   PROMPT      = captured verbatim from a real question/prompt export
    #   SYNTHESIZED = auto-generated from a bare keyword
    #   KEYWORD     = raw keyword used as-is (analyst declined synthesis)
    # NULL for ordinary manually-authored questions.
    demand_origin: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)

    # Approval workflow (SE-002)
    approval_status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)  # PENDING|APPROVED|REJECTED
    approver_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Versioning (FR-103)
    version: Mapped[int] = mapped_column(Integer, default=1)
    superseded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Question Variations (phrasing-robustness grouping). A "variation group" is a base
    # question plus Claude-generated paraphrases that preserve the same intent; running the
    # group reveals how models answer different phrasings of the same question.
    #   variation_group_id : shared key for a base question and all its variations
    #                        (= the base question's logical question_id)
    #   variation_of       : base question_id for a generated variation; NULL for the base
    #   is_variation       : True for a generated/approved variation row
    #   generation_method  : provenance of a variation ("CLAUDE" | "MANUAL")
    variation_group_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    variation_of: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    is_variation: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    generation_method: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Soft delete (DM-003)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delete_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
