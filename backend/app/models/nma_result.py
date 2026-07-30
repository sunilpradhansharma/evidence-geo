"""Network meta-analysis results (Phase 2) — one table, two provenances.

``NMAResult`` is shared by **published** syntheses (extracted from a journal article)
and **computed** ones (produced by our own engines), discriminated by ``source``. They
share a table because consumers ask the same question of both — "what does the evidence
say about A vs B?" — and the resolver has to rank them against each other. They are
never presented identically: a published result is externally citable, a computed one
is labelled *"internal analytical output — not validated or approved for external
use"*.

Every computed row carries ``protocol_id`` + ``protocol_hash`` + engine and package
versions. Without them a result cannot answer "under what rules was this produced?",
which makes it unreviewable and therefore unusable.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base

PUBLISHED = "PUBLISHED"
COMPUTED = "COMPUTED"
RESULT_SOURCES = (PUBLISHED, COMPUTED)

EXPLORATORY = "EXPLORATORY"
GOVERNED = "GOVERNED"
EXECUTION_MODES = (EXPLORATORY, GOVERNED)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NMAResult(Base):
    """A league table plus its statistics, provenance and governance state."""

    __tablename__ = "nma_results"

    result_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(16), index=True)  # PUBLISHED | COMPUTED

    # Scope — mirrors EvidenceNetwork so published and computed results are comparable.
    indication: Mapped[str] = mapped_column(String(128), index=True)
    canonical_outcome_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    endpoint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    timepoint_week: Mapped[float | None] = mapped_column(Float, nullable=True)
    population_stratum: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    treatment_phase: Mapped[str] = mapped_column(String(16), default="PRIMARY", index=True)

    # COMPUTED provenance
    network_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    network_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    engine: Mapped[str | None] = mapped_column(String(32), nullable=True)  # BUCHER | NETMETA
    engine_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    package_version: Mapped[str | None] = mapped_column(String(32), nullable=True)  # netmeta x.y.z

    # PUBLISHED provenance
    source_payload_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    citation: Mapped[str | None] = mapped_column(Text, nullable=True)
    publication_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    funding_source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    grade_certainty: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # A hard requirement, not a nice-to-have: without the included-study list a
    # published NMA cannot be validated, reused, or overlap-checked against an internal
    # network. Phase 0 audits its recoverability.
    included_studies: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list[str]
    included_studies_recoverable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Governance ----------------------------------------------------------------------
    protocol_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Derived from canonical protocol content, never authored. Editing a protocol
    # changes the hash and therefore invalidates its prior approval.
    protocol_hash: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    execution_mode: Mapped[str] = mapped_column(String(16), default=EXPLORATORY, index=True)
    status: Mapped[str] = mapped_column(String(48), index=True)  # see evidence.statuses

    # Statistics ------------------------------------------------------------------------
    model_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # fixed | random
    effect_measure: Mapped[str | None] = mapped_column(String(32), nullable=True)
    league_table: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    estimates: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list of contrasts
    rankings: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    sucra: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON {treatment: score}
    tau_squared: Mapped[float | None] = mapped_column(Float, nullable=True)
    q_statistic: Mapped[float | None] = mapped_column(Float, nullable=True)
    degrees_freedom: Mapped[int | None] = mapped_column(Integer, nullable=True)
    heterogeneity_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    inconsistency: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON

    # Cross-class disclosure. Recorded on the RESULT, not just the network, so the
    # threat travels with the number wherever it is quoted.
    administration_routes: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON {node: route}
    is_route_mixed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    placebo_response_policy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sensitivity_analysis: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    sensitivity_divergence_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Citability and external approval are independent — a published NMA is citable
    # while our extracted interpretation of it is still unreviewed.
    source_is_citable: Mapped[bool] = mapped_column(Boolean, default=False)
    claim_is_approved_for_external_use: Mapped[bool] = mapped_column(Boolean, default=False)

    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def is_internal_output(self) -> bool:
        """True when this must carry the internal-analytical-output label."""
        return self.source == COMPUTED
