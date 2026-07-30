"""GEO Intervention Recommendation model (BR-012).

A `Recommendation` is a persisted, LLM-generated content suggestion tied to a specific
competitive-position gap — a response where the focus brand was scored SECOND_LINE or
NOT_RECOMMENDED. It is a *strategic suggestion only*, never MLR-approved content
(`mlr_status` is fixed), and is enriched with SEMrush SEO metrics so the list can be
ranked by estimated impact. Rows are append-only and grouped into generation batches via
`batch_id` (a regenerate produces a fresh batch rather than mutating prior rows).
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base

# BR-012.4 — every recommendation is an unapproved strategic suggestion.
MLR_UNAPPROVED = "UNAPPROVED_SUGGESTION"

# Which finder produced this row (Phase 9). The two answer different questions about the
# same response — POSITIONING_GAP asks how the answer *reads*, EVIDENCE_GAP asks whether it
# is *right* — and a reader who cannot tell them apart will misread the remedy.
SOURCE_POSITIONING_GAP = "POSITIONING_GAP"
SOURCE_EVIDENCE_GAP = "EVIDENCE_GAP"
RECOMMENDATION_SOURCES = (SOURCE_POSITIONING_GAP, SOURCE_EVIDENCE_GAP)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Recommendation(Base):
    """A single ranked, evidence-backed content recommendation (append-only)."""

    __tablename__ = "recommendations"

    rec_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # UUID
    batch_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    # Provenance — the flagged gap this recommendation addresses
    source_response_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    question_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # Denormalised filter dimensions (BR-012.6)
    persona: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    therapeutic_area: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    indication: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    brand_focus: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    llm_name: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # The gap
    competitive_position: Mapped[str] = mapped_column(String(32))  # SECOND_LINE | NOT_RECOMMENDED
    gap_severity: Mapped[float] = mapped_column(Float, default=1.0)

    # Supporting evidence (BR-012.5)
    outperforming_competitor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    competitor_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    missing_citations: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list[str]

    # SEMrush enrichment (BR-012.2 external metrics)
    search_volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    domain_authority: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metrics_source: Mapped[str] = mapped_column(String(16), default="stub")  # live | stub
    volume_multiplier: Mapped[float] = mapped_column(Float, default=1.0)

    # Citation-gap signal (BR-005 evidence gaps): how many trusted sources the AI relied on
    # omit the focus brand + how often the outperforming competitor is cited. Feeds a
    # transparent citation multiplier in the impact score (a plain-math take on AEO
    # "citeability" — no external ML model).
    citation_gap_score: Mapped[float] = mapped_column(Float, default=0.0)
    citation_multiplier: Mapped[float] = mapped_column(Float, default=1.0)

    # LLM output (BR-012.1/.2)
    content_type: Mapped[str] = mapped_column(String(64))
    recommended_action: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Short structured outline for the suggested asset (JSON list[str]) — a content brief the
    # brand/content team can hand to MLR. Still an unapproved suggestion.
    content_brief: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Generic, non-promotional follow-up questions worth adding to monitoring (JSON list[str]).
    # Surfaced as suggestions only; adding to the bank still needs Medical-Affairs approval.
    suggested_questions: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Ranking (BR-012.3): impact_score = gap_severity × volume_multiplier × citation_multiplier
    impact_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)

    # MLR labelling (BR-012.4) — always an unapproved suggestion
    mlr_status: Mapped[str] = mapped_column(String(32), default=MLR_UNAPPROVED)

    # --- Phase 9: evidence-driven recommendations -------------------------------------
    # Defaulted to POSITIONING_GAP so every pre-existing row keeps its true provenance
    # without a backfill — they were all produced by the positioning finder.
    source_type: Mapped[str] = mapped_column(
        String(32), default=SOURCE_POSITIONING_GAP, index=True
    )

    # How much weight this recommendation can carry, derived from the GOVERNANCE STATE of
    # the evidence behind it — not from a model's self-report. A recommendation is an
    # instruction to spend money, and one resting on an extraction nobody has verified must
    # not present itself as certain. NULL on positioning rows, which rest on a score rather
    # than on evidence and have no governance state to read.
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # What the finding MEANS, and who owns it (app.remediation.implications).
    strategic_implication: Mapped[str | None] = mapped_column(
        String(48), nullable=True, index=True
    )
    implication_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # False when no publishable asset can close the finding — an internal curation backlog
    # or a genuine evidence gap. Stored rather than inferred at read time so a list filtered
    # to "content work" cannot accidentally include work that is not content.
    externally_actionable: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    # The non-content remedy, when there is one: "verify the studies blocking this
    # comparison", "a head-to-head trial reporting this endpoint". Named so the finding is
    # actionable even where the GEO engine has nothing to offer.
    evidence_action: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Provenance back into Phase 8, so a reviewer can read the claim that caused this.
    claim_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    claim_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    classification: Mapped[str | None] = mapped_column(String(32), nullable=True)
    certainty_verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)
    finding_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # CURATION / PROTOCOL / EVIDENCE — why the comparison was unavailable (Phase 7).
    gap_attribution: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
