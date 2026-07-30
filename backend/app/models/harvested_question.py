"""Harvested-question staging model (Discovery feature).

Real user-asked questions scraped from public health communities land here FIRST —
never directly in the approved Question Repository. A human reviewer promotes a staged
row, which creates a PENDING Question that still must clear the Medical-Affairs approval
gate (SE-001/SE-002) before it can ever be sent to an LLM. This double gate keeps
untrusted, possibly-PII, possibly-adverse-event content out of monitoring runs.
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Lifecycle:
#   CLASSIFIED      — staged + LLM-tagged, awaiting human review
#   QUARANTINED_AE  — possible adverse-event content; held for safety/PV review, NOT ingestible
#   PROMOTED        — reviewer pushed it to the Question Repository (as PENDING)
#   REJECTED        — reviewer discarded it
STATUSES = ("CLASSIFIED", "QUARANTINED_AE", "PROMOTED", "REJECTED")


class HarvestedQuestion(Base):
    __tablename__ = "harvested_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Provenance (for audit / reviewer context — NOT author identity)
    source: Mapped[str] = mapped_column(String(32), index=True)  # e.g. "tavily"
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_domain: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    source_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    search_query: Mapped[str | None] = mapped_column(Text, nullable=True)  # which seed query found it
    raw_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)   # context for reviewer only

    # The real, verbatim question (lightly PII-redacted — phrasing preserved)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    dedupe_hash: Mapped[str] = mapped_column(String(64), index=True)

    # Suggested tags (LLM) — reviewer can override at promotion time
    persona: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    therapeutic_area: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    brand_focus: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    domain: Mapped[str | None] = mapped_column(String(32), nullable=True)
    intent_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    relevance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    search_persona: Mapped[str | None] = mapped_column(String(32), nullable=True)  # persona lens that surfaced it

    # Compliance flags
    pii_flags: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list of redacted PII types
    ae_flag: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    status: Mapped[str] = mapped_column(String(16), default="CLASSIFIED", index=True)
    promoted_question_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Phase 7. A question generated from the evidence store stages here alongside the
    # web-harvested ones so reviewers keep ONE queue, and carries the evidence it rests on
    # as a JSON *proposal*: QuestionEvidence.question_id is NOT NULL and no question exists
    # until promotion, so the association cannot be written yet. Materialised in promote().
    # NULL for every web-harvested row, which is what `source == "evidence"` distinguishes.
    evidence_payload: Mapped[str | None] = mapped_column(Text, nullable=True)

    harvested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
