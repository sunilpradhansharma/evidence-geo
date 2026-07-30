"""Sequential extraction verification pipeline (Phase 3A).

    source payload
      |
      v  ExtractionAgent      arm-level endpoints, placebo rates, baseline variance, N
      v  HarmonisationAgent   PROPOSES timepoint/strata alignment + rationale + citation
      v  ValidationAgent      re-derives each value against retained source
      |
      v  curation queue (a human ratifies)

A **pipeline**, not a council. "Council" implies debate or voting between peers; these
are three sequential stages with distinct responsibilities. Genuine multi-model
consensus is a different and more expensive design.

Two constraints in here are load-bearing, and both are enforced structurally rather
than by convention — because a convention is exactly what gets forgotten during the
next refactor:

**1. Harmonisation proposes; it never applies.** ``HarmonisationProposal`` is a frozen
dataclass with no path into ``OutcomeResult``. There is no ``apply()``. Aligning Week 12
to Week 16 is the statistical judgement that ``AnalysisProtocolDefinition.
approved_time_window`` exists to own under statistician approval, and the plan already
commits that mismatches are *surfaced, never hidden*. A proposal that contradicts the
governing window is **auto-rejected, not escalated** — the protocol wins without human
involvement, because escalating it would invite someone to overrule the protocol in a
review queue.

**2. A validation disagreement blocks promotion to VERIFIED.** Not a warning, not a
confidence penalty. The row stays MAPPED until a human resolves it.

Every stage output carries ``model_id``, ``prompt_version`` and ``pipeline_version``.
Without that a non-deterministic pipeline is unauditable, which is disqualifying in a
system with a medical review gate.

**Justify-or-drop.** ``run_baseline`` is the single ``chat_json`` call the agent pipeline
must beat on the same fixture corpus. If it does not, the documented descope is to ship
the baseline plus the validation stage alone — agent count is not a quality metric. Both
sit behind the same interface so removal is cheap.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.evidence import licensing
from app.evidence.lifecycles import MAPPED, VERIFIED

PIPELINE_VERSION = "1.0.0"

# Stage identifiers, recorded on every output so a value can be traced to its producer.
STAGE_BASELINE = "BASELINE"
STAGE_EXTRACTION = "EXTRACTION"
STAGE_HARMONISATION = "HARMONISATION"
STAGE_VALIDATION = "VALIDATION"

# Proposal dispositions. AUTO_REJECTED is terminal and never reaches a human.
PROPOSED = "PROPOSED"
AUTO_REJECTED = "AUTO_REJECTED"
ACCEPTED_BY_PROTOCOL = "ACCEPTED_BY_PROTOCOL"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class StageProvenance:
    """Who produced a value, with what, and under which pipeline version."""

    stage: str
    model_id: str | None
    prompt_version: str
    pipeline_version: str = PIPELINE_VERSION
    produced_at: datetime = field(default_factory=utcnow)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "pipeline_version": self.pipeline_version,
            "produced_at": self.produced_at.isoformat(),
        }


@dataclass(frozen=True)
class ExtractedValue:
    """One extracted datum plus everything needed to review or reproduce it."""

    field_name: str
    value: Any
    source_text: str | None
    confidence: float
    provenance: StageProvenance
    rationale: str | None = None


@dataclass(frozen=True)
class HarmonisationProposal:
    """A *suggestion* that two timepoints or strata are alignable. Never an instruction.

    Frozen and deliberately inert: it has no ``apply`` method and nothing in this module
    writes it onto an ``OutcomeResult``. The only supported consumers are the curation
    queue (for display) and ``screen_proposal`` (for auto-rejection).

    ``guideline_citation`` records the ISPOR/Cochrane basis the agent offered. It is
    context for a reviewer, **not authority** — a citation does not make a proposal
    correct, and the protocol still wins.
    """

    kind: str  # TIMEPOINT | STRATA
    from_value: Any
    to_value: Any
    rationale: str
    confidence: float
    provenance: StageProvenance
    guideline_citation: str | None = None
    disposition: str = PROPOSED
    # Deliberately NOT `rejection_reason`: OutcomeResult already owns that name for "why
    # this extraction failed verification". Two different concepts sharing one word on
    # the proposal/persisted boundary is exactly the ambiguity this module exists to
    # prevent, and a distinct vocabulary is what keeps the boundary checkable.
    auto_rejection_reason: str | None = None

    @property
    def is_actionable_by_human(self) -> bool:
        """False for auto-rejected proposals — they never enter the review queue."""
        return self.disposition == PROPOSED


@dataclass(frozen=True)
class ValidationOutcome:
    """The validation agent's verdict on one extracted value."""

    field_name: str
    agrees: bool
    expected: Any
    observed: Any
    reach: str  # FULL_SOURCE | FRAGMENT — bounded by licence class
    provenance: StageProvenance
    note: str | None = None


@dataclass
class PipelineResult:
    """Everything one pass produced, plus whether it may be promoted."""

    values: list[ExtractedValue] = field(default_factory=list)
    proposals: list[HarmonisationProposal] = field(default_factory=list)
    validations: list[ValidationOutcome] = field(default_factory=list)
    license_class: str = licensing.RESTRICTED
    errors: list[str] = field(default_factory=list)

    @property
    def disagreements(self) -> list[ValidationOutcome]:
        return [v for v in self.validations if not v.agrees]

    @property
    def validation_reach(self) -> str:
        """How far validation could actually reach, given the licence tier."""
        return licensing.validation_reach(self.license_class)

    @property
    def next_verification_status(self) -> str:
        """``VERIFIED`` only when every validated value agreed.

        A disagreement blocks promotion outright. Downgrading it to a confidence penalty
        would let a contradicted value flow onward carrying a slightly smaller number,
        which is precisely the silent failure the validation stage exists to prevent.
        """
        return MAPPED if self.disagreements else VERIFIED

    @property
    def actionable_proposals(self) -> list[HarmonisationProposal]:
        return [p for p in self.proposals if p.is_actionable_by_human]

    def coverage_report(self) -> dict[str, Any]:
        """Validation coverage, reported **per licence tier** rather than as one figure.

        A restricted source can only be re-checked against the retained fragment, so a
        single headline percentage would overstate what was actually verified.
        """
        return {
            "license_class": self.license_class,
            "validation_reach": self.validation_reach,
            "values_extracted": len(self.values),
            "values_validated": len(self.validations),
            "disagreements": len(self.disagreements),
            "proposals_total": len(self.proposals),
            "proposals_auto_rejected": sum(
                1 for p in self.proposals if p.disposition == AUTO_REJECTED
            ),
        }


def screen_proposal(
    proposal: HarmonisationProposal,
    *,
    approved_time_window: tuple[float, float] | None,
) -> HarmonisationProposal:
    """Auto-reject a proposal that contradicts the governing protocol window.

    Returns a new proposal (the type is frozen) with ``disposition=AUTO_REJECTED`` when
    the target timepoint falls outside ``approved_time_window``. **No escalation path.**
    Routing this to a human would invite someone to overrule an approved statistical
    protocol from a review queue, which is exactly the authority the protocol holds.

    A ``None`` window means no protocol governs yet, so the proposal stays PROPOSED and
    a human decides — the honest state during development, before any protocol exists.
    """
    if proposal.kind != "TIMEPOINT" or approved_time_window is None:
        return proposal

    low, high = approved_time_window
    try:
        target = float(proposal.to_value)
    except (TypeError, ValueError):
        return _reject(proposal, f"target timepoint {proposal.to_value!r} is not numeric")

    if not (low <= target <= high):
        return _reject(
            proposal,
            f"target Week {target:g} falls outside the protocol's approved window "
            f"[{low:g}, {high:g}]; the protocol governs",
        )
    return proposal


def _reject(proposal: HarmonisationProposal, reason: str) -> HarmonisationProposal:
    return HarmonisationProposal(
        kind=proposal.kind,
        from_value=proposal.from_value,
        to_value=proposal.to_value,
        rationale=proposal.rationale,
        confidence=proposal.confidence,
        provenance=proposal.provenance,
        guideline_citation=proposal.guideline_citation,
        disposition=AUTO_REJECTED,
        auto_rejection_reason=reason,
    )
