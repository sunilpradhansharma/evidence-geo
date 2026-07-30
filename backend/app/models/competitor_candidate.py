"""Discovered competitor candidates (Phase 5, Tier A).

A candidate is a treatment the **evidence** says competes in an indication while the
**curated config** does not list it there. Phase 0 measured curated coverage at 12-26% of
network nodes and ``scripts/ingest_evidence.py`` already counts uncurated arm labels as a
first-class figure; this table gives those labels provenance, a reason, and a review
decision.

Two boundaries this model exists to hold:

* **A candidate is a proposal, never a config write.** Accepting one records a decision and
  yields a YAML fragment for a human to commit. ``brands.yaml`` stays hand-authored, because
  the entire argument for the curated class/route table is that *a curated table is a
  reviewable artefact and an inferred label is an unreviewed assertion*. A queue that could
  write config would make it the second kind.
* **Class and route are copied from curation or left null.** Never inferred. Open-set class
  inference (RxNorm/ATC/ChEMBL) is deliberately out of scope, so an uncurated candidate
  carries ``drug_class = NULL`` rather than a guess.

``discovery_confidence`` is **not** an extraction confidence. It scores how strongly the
evidence suggests this molecule belongs on a competitor list, from the deterministic weights
in ``app.evidence.discovery``. It says nothing about whether any clinical value was read
correctly.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base

# Review lifecycle. Mirrors the harvested-question double gate: nothing a discovery pass
# produced reaches the curated config without a person, and a rejection is remembered so the
# same label is not re-proposed at every sweep.
NEW = "NEW"
ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
DEFERRED = "DEFERRED"
REVIEW_STATES = (NEW, ACCEPTED, REJECTED, DEFERRED)

# A decided candidate keeps its decision across re-runs; only an undecided one is refreshed.
# Same rule as ingestion's "a decided study is never overwritten".
DECIDED_STATES = (ACCEPTED, REJECTED)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CompetitorCandidate(Base):
    """One treatment proposed as a competitor in one indication."""

    __tablename__ = "competitor_candidates"

    # Deterministic, derived from (indication, treatment) — so a re-run updates one row
    # instead of accumulating near-duplicates, exactly like ``network_id_for``.
    candidate_id: Mapped[str] = mapped_column(String(128), primary_key=True)

    # Identity ------------------------------------------------------------------------
    treatment: Mapped[str] = mapped_column(String(128), index=True)  # node name in evidence
    generic: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sponsor: Mapped[str | None] = mapped_column(String(255), nullable=True)

    indication: Mapped[str] = mapped_column(String(128), index=True)
    therapeutic_area: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # Curated annotation, COPIED from brands.yaml or left null. Never inferred.
    drug_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    administration_route: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # True when the molecule is curated *somewhere* (a TA block or drug_catalog) even though
    # it is not on this indication's competitor list. The two are different gaps: one needs a
    # line added to an indication, the other needs a molecule characterised first.
    is_curated_drug: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # Tier A signals — all mechanically derived from rows already ingested ---------------
    discovery_reasons: Mapped[str] = mapped_column(Text)  # JSON list[str], see discovery.py
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    direct_comparison_count: Mapped[int] = mapped_column(Integer, default=0)
    compared_with: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list[str]
    shared_comparators: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list[str]
    published_nma_count: Mapped[int] = mapped_column(Integer, default=0)
    development_phase: Mapped[str | None] = mapped_column(String(32), nullable=True)
    has_posted_results: Mapped[bool] = mapped_column(Boolean, default=False)
    latest_evidence_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # JSON list[str] of the studies behind the signals, so a reviewer can read the source
    # rather than trusting a count.
    source_study_ids: Mapped[str | None] = mapped_column(Text, nullable=True)

    discovery_confidence: Mapped[float] = mapped_column(Float, default=0.0, index=True)

    # Review ----------------------------------------------------------------------------
    review_status: Mapped[str] = mapped_column(String(16), default=NEW, index=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when an accepted candidate's config fragment has been committed by a human. Until
    # then an ACCEPTED candidate is a decision, not a change to the taxonomy.
    config_applied: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
