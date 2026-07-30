"""Clinical evidence entities (Phase 2): ClinicalStudy, StudyArm, OutcomeResult.

The schema is shaped by the **clinical domain, not by any source payload**. Adapters
translate ClinicalTrials.gov, openFDA, PubMed and manual uploads into these tables;
anything else couples the store to one registry and makes published NMAs, FDA labels and
HTA documents awkward to represent.

Three design points that carry real weight downstream:

* **Results are arm-level, not pre-flattened pairwise.** Flattening a three-arm trial
  into independent pairwise rows double-counts patients and understates standard errors
  because it discards within-study correlation. The multi-arm structure has to survive
  all the way from extraction to the NMA wire contract, so it is preserved here at the
  bottom of the stack.
* **``treatment_phase`` is a first-class field.** Induction and maintenance populations
  differ — maintenance cohorts are re-randomised induction responders — so pooling them
  is a hard gate, not a warning. It is stored on the study and re-asserted per result.
* **Mismatch flags are recorded, never resolved here.** ``TIMEPOINT_MISMATCH`` and
  friends are surfaced to the resolver and the curation UI. Aligning Week 12 to Week 16
  is a protocol decision under statistician approval, not an extraction-time fixup.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.evidence.lifecycles import EXTRACTED
from app.models.database import Base

# Outcome payload shapes. `contrast` exists because published syntheses frequently
# report only relative effects — refusing them would discard most of the Level-2
# literature.
BINARY = "binary"
CONTINUOUS = "continuous"
CONTRAST = "contrast"
OUTCOME_TYPES = (BINARY, CONTINUOUS, CONTRAST)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ClinicalStudy(Base):
    """One trial, as extracted from a registry or publication."""

    __tablename__ = "clinical_studies"

    study_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # internal id
    registry_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # NCT…
    acronym: Mapped[str | None] = mapped_column(String(64), nullable=True)  # SELECT-PsA, VOYAGE-1…
    title: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Clinical scope ------------------------------------------------------------------
    indication: Mapped[str] = mapped_column(String(128), index=True)  # brands.yaml overlay key
    phase: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    study_design: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_randomised: Mapped[bool] = mapped_column(Boolean, default=True)

    # Population. `population_stratum` references canonical_outcomes.yaml
    # population_strata (BIO_NAIVE, TNF_IR, …) — not free text, so networks can be
    # split on it reliably.
    population_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    population_stratum: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    prior_treatment_status: Mapped[str | None] = mapped_column(String(128), nullable=True)
    enrollment: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # PRIMARY | INDUCTION | MAINTENANCE. Mixing phases in one network is blocked with
    # TREATMENT_PHASE_MISMATCH rather than warned about.
    treatment_phase: Mapped[str] = mapped_column(String(16), default="PRIMARY", index=True)

    sponsor: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    completion_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    results_first_posted: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)

    # Quality -------------------------------------------------------------------------
    risk_of_bias: Mapped[str | None] = mapped_column(String(32), nullable=True)  # LOW|SOME_CONCERNS|HIGH
    risk_of_bias_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Provenance + extraction ----------------------------------------------------------
    source_payload_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    extraction_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    extraction_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    mismatch_flags: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list[str]

    # Lifecycle 1 only. Whether this study belongs in a given network is Lifecycle 2 and
    # lives on NetworkMembership — a study can be VERIFIED here and excluded there.
    verification_status: Mapped[str] = mapped_column(String(24), default=EXTRACTED, index=True)
    verified_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    source_is_citable: Mapped[bool] = mapped_column(Boolean, default=True)
    claim_is_approved_for_external_use: Mapped[bool] = mapped_column(Boolean, default=False)

    version: Mapped[int] = mapped_column(Integer, default=1)
    superseded_by: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    arms: Mapped[list["StudyArm"]] = relationship(
        back_populates="study", cascade="all, delete-orphan", lazy="selectin"
    )
    outcomes: Mapped[list["OutcomeResult"]] = relationship(
        back_populates="study", cascade="all, delete-orphan", lazy="selectin"
    )


class StudyArm(Base):
    """One randomised arm. Arms are the unit the NMA wire contract transmits."""

    __tablename__ = "study_arms"

    arm_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    study_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("clinical_studies.study_id", ondelete="CASCADE"), index=True
    )

    label: Mapped[str | None] = mapped_column(String(255), nullable=True)  # as printed in the source
    treatment: Mapped[str] = mapped_column(String(128), index=True)  # normalised node name
    is_placebo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # Snapshotted from the curated table at extraction time rather than resolved live:
    # a stored result must remain explicable years later even if the curated config has
    # since been edited.
    drug_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    administration_route: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    # Dose is kept structured because `dose_policy` decides whether doses may be pooled,
    # and silently pooling doses is among the most common and most fatal NMA criticisms.
    dose_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    dose_unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dose_frequency: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dose_description: Mapped[str | None] = mapped_column(String(255), nullable=True)

    sample_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    study: Mapped[ClinicalStudy] = relationship(back_populates="arms")


class OutcomeResult(Base):
    """One endpoint result. Arm-level where available, contrast-level where not.

    ``arm_id`` is set for arm-level rows and NULL for contrast-level rows, where the
    result describes a comparison rather than a single arm. Both shapes round-trip to
    the sidecar.
    """

    __tablename__ = "outcome_results"

    result_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    study_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("clinical_studies.study_id", ondelete="CASCADE"), index=True
    )
    arm_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("study_arms.arm_id", ondelete="CASCADE"), nullable=True, index=True
    )

    # Endpoint identity. `canonical_outcome_id` references canonical_outcomes.yaml; a
    # result that cannot be mapped to one keeps the raw endpoint text and is flagged for
    # review rather than dropped.
    canonical_outcome_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    endpoint: Mapped[str] = mapped_column(String(128))
    endpoint_definition: Mapped[str | None] = mapped_column(Text, nullable=True)
    timepoint_week: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    population_stratum: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    treatment_phase: Mapped[str] = mapped_column(String(16), default="PRIMARY", index=True)

    outcome_type: Mapped[str] = mapped_column(String(16), default=BINARY)  # binary|continuous|contrast

    # binary
    events: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sample_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # continuous
    mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    standard_deviation: Mapped[float | None] = mapped_column(Float, nullable=True)
    # contrast
    comparator_treatment: Mapped[str | None] = mapped_column(String(128), nullable=True)
    effect_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    standard_error: Mapped[float | None] = mapped_column(Float, nullable=True)
    effect_measure: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ci_lower: Mapped[float | None] = mapped_column(Float, nullable=True)
    ci_upper: Mapped[float | None] = mapped_column(Float, nullable=True)
    p_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_significant: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    is_safety_outcome: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # Surfaced, never hidden. Populated at extraction; cleared only by a protocol
    # decision, never by the extraction pipeline.
    mismatch_flags: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list[str]

    source_payload_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # the fragment this came from
    extraction_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    verification_status: Mapped[str] = mapped_column(String(24), default=EXTRACTED, index=True)
    verified_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    version: Mapped[int] = mapped_column(Integer, default=1)
    superseded_by: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    study: Mapped[ClinicalStudy] = relationship(back_populates="outcomes")
