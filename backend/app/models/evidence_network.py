"""Evidence networks and their membership decisions (Phase 2).

An ``EvidenceNetwork`` is one analysable question: *this indication, this canonical
outcome, this population stratum, this treatment phase, under this analysis protocol*.
Change any of those and it is a different network, not a variant of the same one.

``NetworkMembership`` is scoped to **network AND protocol** because inclusion is a
per-analysis judgement. The same VERIFIED study can legitimately be included in an RA
ACR50 network and excluded from RA ACR20 at the same instant. That is the whole reason
membership is not a column on ``ClinicalStudy``.

Ratification lives on the **network**, not the study — "is this network fit to compute
on?" is a question about the assembled evidence set, and no per-study flag can answer
it.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.evidence.lifecycles import DRAFT, PROPOSED
from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EvidenceNetwork(Base):
    """A treatment network scoped to one outcome, stratum, phase and protocol."""

    __tablename__ = "evidence_networks"

    network_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Scope. All four are part of the network's identity.
    indication: Mapped[str] = mapped_column(String(128), index=True)
    canonical_outcome_id: Mapped[str] = mapped_column(String(64), index=True)
    population_stratum: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    treatment_phase: Mapped[str] = mapped_column(String(16), default="PRIMARY", index=True)

    # The governing protocol. Membership decisions are only meaningful relative to one,
    # so a network without a protocol cannot be computed on in GOVERNED mode.
    protocol_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # Topology, recomputed as membership changes. `administration_routes` is stored per
    # node so a reviewer can see at a glance that a network mixes oral and injectable
    # agents — a transitivity threat to disclose, never to adjust away.
    treatment_nodes: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list[str]
    comparator_edges: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list[[a,b,n]]
    administration_routes: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON {node: route}
    is_connected: Mapped[bool | None] = mapped_column(nullable=True, index=True)
    has_closed_loops: Mapped[bool | None] = mapped_column(nullable=True)
    has_multi_arm_studies: Mapped[bool | None] = mapped_column(nullable=True)

    # Lifecycle 3. RATIFIED is reachable only through BOTH review stages in order —
    # see app.evidence.lifecycles.
    ratification_status: Mapped[str] = mapped_column(String(32), default=DRAFT, index=True)
    medical_reviewer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    medical_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    medical_review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    statistical_reviewer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    statistical_reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    statistical_review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    version: Mapped[int] = mapped_column(Integer, default=1)
    superseded_by: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    memberships: Mapped[list["NetworkMembership"]] = relationship(
        back_populates="network", cascade="all, delete-orphan", lazy="selectin"
    )


class NetworkMembership(Base):
    """One study's inclusion decision for one network under one protocol.

    ``exclusion_reason`` is required whenever the status is EXCLUDED — enforced in
    ``lifecycles.assert_transition``, because an unexplained exclusion removes evidence
    from an analysis and leaves a reviewer unable to tell a considered judgement from an
    accident.
    """

    __tablename__ = "network_memberships"

    membership_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    network_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("evidence_networks.network_id", ondelete="CASCADE"), index=True
    )
    study_id: Mapped[str] = mapped_column(String(64), index=True)
    # Denormalised so a membership row is interpretable without loading the network —
    # and so the same study can carry different decisions under different protocols.
    protocol_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    membership_status: Mapped[str] = mapped_column(String(24), default=PROPOSED, index=True)
    exclusion_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Why the pipeline proposed it — the pipeline proposes, a human ratifies.
    proposal_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    mismatch_flags: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list[str]

    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    network: Mapped[EvidenceNetwork] = relationship(back_populates="memberships")
