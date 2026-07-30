"""``EvaluationClaim`` (Phase 8) — one atomic claim a response made, and how it graded.

Separate from ``scoring_records`` on purpose. A scoring record is one row per response
holding a sentiment and a position; a response contains *several* claims, each routed to a
different authority and each graded independently. Folding them into the scoring row would
force the same collapse the phase exists to avoid — *"Rinvoq is approved for PsA, works
better than Drug X, and carries a boxed warning"* would get one verdict when it needs three.

Append-only in practice, like ``responses``: a claim is re-extracted as a new row rather
than edited, so a finding can always be traced to the extraction that produced it.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EvaluationClaim(Base):
    """One claim, its routing, and its verdict."""

    __tablename__ = "evaluation_claims"

    claim_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    response_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    question_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    llm_name: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # --- what the extractor observed (never a judgement) ---------------------------------
    claim_text: Mapped[str] = mapped_column(Text)
    claim_type: Mapped[str] = mapped_column(String(32), index=True)
    subject: Mapped[str] = mapped_column(String(128), index=True)
    comparator: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    indication: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    outcome: Mapped[str | None] = mapped_column(String(128), nullable=True)
    direction: Mapped[str] = mapped_column(String(16), default="NO_DIRECTION")
    polarity: Mapped[str] = mapped_column(String(16), default="ASSERTED")
    certainty: Mapped[str] = mapped_column(String(16), default="HEDGED", index=True)
    magnitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    magnitude_unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cited_identifiers: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list

    # --- how it was routed ----------------------------------------------------------------
    # Stored, not recomputed at read time. The policy is a config decision that can change,
    # and a finding must stay interpretable against the rule that actually produced it.
    expected_evidence_policy: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- the verdict ----------------------------------------------------------------------
    classification: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    dimensions: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list
    evidence_links: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list
    certainty_verdict: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    flags: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list
    is_adverse: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # Provenance of the extraction itself, so a change in extractor behaviour is visible
    # rather than being mistaken for a change in the models being monitored.
    extracted_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    extraction_version: Mapped[str] = mapped_column(String(32), default="v1")
    claim_index: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
