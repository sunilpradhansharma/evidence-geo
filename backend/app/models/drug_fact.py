"""Regulatory drug facts (Phase 2) — versioned by label-update date.

Drug facts and clinical evidence are two entity families because they **version
differently**. A drug fact is superseded when the label changes; a study is superseded
when the extraction is corrected. Sharing one versioning rule would force one of them
to lie.

This entity is deliberately independent of the NMA stack: drug facts ship and stay
valuable even for an indication whose network turns out to be disconnected.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.evidence.lifecycles import EXTRACTED
from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DrugFact(Base):
    """Label-derived facts for one drug, as of one label version."""

    __tablename__ = "drug_facts"

    fact_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # Identity ------------------------------------------------------------------------
    brand: Mapped[str] = mapped_column(String(128), index=True)
    generic: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    molecule: Mapped[str | None] = mapped_column(String(128), nullable=True)
    manufacturer: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Pharmacology. `drug_class` / `administration_route` mirror the curated values in
    # brands.yaml; a label-sourced value that disagrees with the curated table is a
    # review signal, not an automatic overwrite.
    mechanism_of_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    drug_class: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    administration_route: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    dosage_form: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Approvals. JSON list[str] of indication names matching the brands.yaml overlay.
    approved_indications: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    label_updated_at: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)

    # Safety. JSON list[str] each.
    contraindications: Mapped[str | None] = mapped_column(Text, nullable=True)
    boxed_warnings: Mapped[str | None] = mapped_column(Text, nullable=True)
    common_adverse_events: Mapped[str | None] = mapped_column(Text, nullable=True)
    serious_adverse_events: Mapped[str | None] = mapped_column(Text, nullable=True)
    has_boxed_warning: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # Provenance ----------------------------------------------------------------------
    regulatory_source: Mapped[str | None] = mapped_column(String(64), nullable=True)  # FDA | EMA | ...
    source_payload_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    prescribing_information: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # Extraction quality. Confidence is an input to review, never a substitute for it —
    # a high-confidence extraction is still EXTRACTED until a human verifies it.
    extraction_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    extraction_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    mismatch_flags: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list[str]

    # Lifecycle 1 (row-level verification). See app.evidence.lifecycles.
    verification_status: Mapped[str] = mapped_column(String(24), default=EXTRACTED, index=True)
    verified_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Citability and approval are INDEPENDENT properties. A published label is citable
    # the moment it exists; the extracted interpretation is not approved for external
    # use until MLR says so. Conflating them is how unreviewed wording reaches an
    # external audience.
    source_is_citable: Mapped[bool] = mapped_column(Boolean, default=True)
    claim_is_approved_for_external_use: Mapped[bool] = mapped_column(Boolean, default=False)

    # Versioning — mirrors Question.version / superseded_by. Nothing is overwritten.
    version: Mapped[int] = mapped_column(Integer, default=1)
    superseded_by: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
