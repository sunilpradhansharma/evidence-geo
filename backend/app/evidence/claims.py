"""Claim-level AI-vs-evidence evaluation — the vocabulary, the routing and the verdict (Phase 8).

Pure. No database, no LLM, no network. Every judgement in this module is a function of its
arguments, which is what makes an alignment finding reproducible: the same response and the
same evidence must always produce the same verdict, or the finding is an opinion with a
timestamp.

**The model observes; this module judges.** An LLM extracts *what the response said* —
subject, comparator, direction, magnitude, the hedging words it used — and never whether
that was right. Splitting it here is the whole design: a model asked to grade itself against
evidence produces a verdict nobody can reproduce or appeal, and this system has a medical
review gate at the end of it. What arrives from the extractor is therefore a set of
observations, validated on construction, and the grade is computed from them.

Four rules the phase exists to enforce:

**Routing is per claim, not per question.** *"Rinvoq is approved for PsA, works better than
Drug X, and carries a boxed warning"* is three claims with three different authorities. A
question-level ``expected_evidence_type`` can only route one of them, which is why Phase 7
stored it as a hint and left the ruleset here.

**Grading a claim against the wrong authority raises.** Checking a boxed-warning claim
against a league table is a category error, not a low score — it produces a confidently
wrong finding, which is worse than no finding. ``assert_routable`` raises ``CategoryError``
rather than returning a verdict, so the failure is loud and testable.

**Absence of evidence is never contradiction.** *"We have nothing that shows this"* and
*"our evidence shows the opposite"* are different findings with different actions: one is a
research gap, the other is a correction. They map to ``UNSUPPORTED`` and ``CONTRADICTORY``
and are never merged.

**Certainty calibration runs both ways.** A model asserting superiority where the interval
crosses no-effect is over-claiming. A model hedging where the evidence is clean is
*under*-claiming, and that is also a finding — it usually means our evidence is not reaching
the model, which is a communication gap Phase 9 can act on.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.config import outcomes
from app.evidence import question_generation as qg, statuses

# --- claim types (plan: Phase 8 routing table) ------------------------------------------
APPROVAL_CLAIM = "APPROVAL_CLAIM"
SAFETY_WARNING_CLAIM = "SAFETY_WARNING_CLAIM"
TRIAL_RESULT_CLAIM = "TRIAL_RESULT_CLAIM"
DIRECT_COMPARISON_CLAIM = "DIRECT_COMPARISON_CLAIM"
RANKING_CLAIM = "RANKING_CLAIM"
PIPELINE_CLAIM = "PIPELINE_CLAIM"
MECHANISM_CLAIM = "MECHANISM_CLAIM"
CERTAINTY_CLAIM = "CERTAINTY_CLAIM"

CLAIM_TYPES = (
    APPROVAL_CLAIM,
    SAFETY_WARNING_CLAIM,
    TRIAL_RESULT_CLAIM,
    DIRECT_COMPARISON_CLAIM,
    RANKING_CLAIM,
    PIPELINE_CLAIM,
    MECHANISM_CLAIM,
    CERTAINTY_CLAIM,
)

# --- classifications --------------------------------------------------------------------
ALIGNED = "ALIGNED"
PARTIALLY_ALIGNED = "PARTIALLY_ALIGNED"
CONTRADICTORY = "CONTRADICTORY"
UNSUPPORTED = "UNSUPPORTED"
OUTDATED = "OUTDATED"
IMPORTANT_OMISSION = "IMPORTANT_OMISSION"
EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
NOT_COMPARABLE = "NOT_COMPARABLE"

CLASSIFICATIONS = (
    ALIGNED,
    PARTIALLY_ALIGNED,
    CONTRADICTORY,
    UNSUPPORTED,
    OUTDATED,
    IMPORTANT_OMISSION,
    EVIDENCE_UNAVAILABLE,
    NOT_COMPARABLE,
)

# Which classifications count against the brand's alignment score. `EVIDENCE_UNAVAILABLE`
# and `NOT_COMPARABLE` are deliberately absent: they say something about OUR corpus, not
# about the model's answer, and scoring a model down for our own gaps would make the
# alignment metric fall as the evidence base thins — exactly backwards.
ADVERSE_CLASSIFICATIONS = (CONTRADICTORY, UNSUPPORTED, OUTDATED, IMPORTANT_OMISSION)

# --- the 13 evaluation dimensions -------------------------------------------------------
FACTUAL_ACCURACY = "FACTUAL_ACCURACY"
COMPARATIVE_RANKING_ACCURACY = "COMPARATIVE_RANKING_ACCURACY"
EVIDENCE_COVERAGE = "EVIDENCE_COVERAGE"
OMISSION = "IMPORTANT_OMISSION"
UNSUPPORTED_CLAIMS = "UNSUPPORTED_CLAIMS"
HALLUCINATED_STUDIES = "HALLUCINATED_STUDIES"
SAFETY_ACCURACY = "SAFETY_ACCURACY"
CITATION_QUALITY = "CITATION_QUALITY"
EVIDENCE_RECENCY = "EVIDENCE_RECENCY"
POPULATION_ACCURACY = "POPULATION_ACCURACY"
ENDPOINT_ACCURACY = "ENDPOINT_ACCURACY"
CERTAINTY_CALIBRATION = "CERTAINTY_CALIBRATION"
DIRECT_VS_INDIRECT_TRANSPARENCY = "DIRECT_VS_INDIRECT_TRANSPARENCY"

DIMENSIONS = (
    FACTUAL_ACCURACY,
    COMPARATIVE_RANKING_ACCURACY,
    EVIDENCE_COVERAGE,
    OMISSION,
    UNSUPPORTED_CLAIMS,
    HALLUCINATED_STUDIES,
    SAFETY_ACCURACY,
    CITATION_QUALITY,
    EVIDENCE_RECENCY,
    POPULATION_ACCURACY,
    ENDPOINT_ACCURACY,
    CERTAINTY_CALIBRATION,
    DIRECT_VS_INDIRECT_TRANSPARENCY,
)

# --- what the extractor observes --------------------------------------------------------
# Hedging strength, as the response worded it. An observation about language, not a
# judgement about whether that language was warranted — which is the entire point of the
# split, and why `calibrate_certainty` needs both this and the interval to say anything.
DEFINITIVE = "DEFINITIVE"      # "is more effective", "the best option"
PROBABLE = "PROBABLE"          # "generally more effective", "tends to"
HEDGED = "HEDGED"              # "may be", "some evidence suggests"
UNCERTAIN = "UNCERTAIN"        # "it is unclear", "evidence is limited"
CERTAINTY_LEVELS = (DEFINITIVE, PROBABLE, HEDGED, UNCERTAIN)

# Ordered strongest-first so calibration can compare two levels without a lookup table.
_CERTAINTY_RANK = {DEFINITIVE: 3, PROBABLE: 2, HEDGED: 1, UNCERTAIN: 0}

SUPERIOR = "SUPERIOR"
INFERIOR = "INFERIOR"
SIMILAR = "SIMILAR"
NO_DIRECTION = "NO_DIRECTION"
DIRECTIONS = (SUPERIOR, INFERIOR, SIMILAR, NO_DIRECTION)

ASSERTED = "ASSERTED"
NEGATED = "NEGATED"
POLARITIES = (ASSERTED, NEGATED)


class ClaimError(ValueError):
    """An extracted claim that is not well-formed enough to grade."""


class CategoryError(ClaimError):
    """A claim routed to evidence that cannot answer it.

    Raised rather than graded. Checking *"does Rinvoq carry a boxed warning?"* against a
    league table cannot produce a right answer, only a confident one, and a system with a
    medical review gate must fail loudly there instead of filing a finding.
    """


# =========================================================================================
# EvidencePolicy — which authority answers which claim
# =========================================================================================
# Per claim, from the plan's routing table. The value is the set of evidence families that
# can legitimately answer that claim type; anything else is a CategoryError.
#
# Note RANKING_CLAIM and DIRECT_COMPARISON_CLAIM are deliberately NOT the same. A ranking
# ("Rinvoq is the most effective") may rest on a synthesis; a direct comparison ("Rinvoq
# beats Humira head-to-head") is a claim about a trial that randomised both, and answering
# it from an indirect estimate would concede the very point the claim asserts.
_POLICY: dict[str, tuple[str, ...]] = {
    APPROVAL_CLAIM: (qg.DRUG_FACT_EVIDENCE,),
    SAFETY_WARNING_CLAIM: (qg.DRUG_FACT_EVIDENCE,),
    TRIAL_RESULT_CLAIM: (qg.CLINICAL_STUDY, qg.OUTCOME_RESULT),
    DIRECT_COMPARISON_CLAIM: (qg.CLINICAL_STUDY, qg.OUTCOME_RESULT),
    RANKING_CLAIM: (qg.NMA_RESULT, qg.EVIDENCE_NETWORK),
    PIPELINE_CLAIM: (qg.CLINICAL_STUDY, qg.COMPETITOR_CANDIDATE),
    MECHANISM_CLAIM: (qg.DRUG_FACT_EVIDENCE,),
    CERTAINTY_CLAIM: (qg.NMA_RESULT, qg.EVIDENCE_NETWORK),
}

# Human-readable, for the reviewer reading a refusal. Kept beside the policy so the two
# cannot drift.
POLICY_DESCRIPTION: dict[str, str] = {
    APPROVAL_CLAIM: "the current regulatory label",
    SAFETY_WARNING_CLAIM: "the current regulatory label",
    TRIAL_RESULT_CLAIM: "the original study",
    DIRECT_COMPARISON_CLAIM: "suitable head-to-head evidence",
    RANKING_CLAIM: "a suitable published or internal synthesis",
    PIPELINE_CLAIM: "the clinical trial registry",
    MECHANISM_CLAIM: "the regulatory label or a validated drug ontology",
    CERTAINTY_CLAIM: "a published review or internal statistical assessment",
}

# Which of the 13 dimensions a claim of each type can bear on. A finding is only ever
# recorded against a dimension its claim type can actually speak to — a mechanism claim
# says nothing about comparative ranking, and letting it score there would dilute the
# dimension that matters.
_DIMENSIONS_OF: dict[str, tuple[str, ...]] = {
    APPROVAL_CLAIM: (FACTUAL_ACCURACY, EVIDENCE_RECENCY),
    SAFETY_WARNING_CLAIM: (SAFETY_ACCURACY, FACTUAL_ACCURACY, EVIDENCE_RECENCY),
    TRIAL_RESULT_CLAIM: (
        FACTUAL_ACCURACY, ENDPOINT_ACCURACY, POPULATION_ACCURACY, CITATION_QUALITY,
    ),
    DIRECT_COMPARISON_CLAIM: (
        COMPARATIVE_RANKING_ACCURACY, CERTAINTY_CALIBRATION,
        DIRECT_VS_INDIRECT_TRANSPARENCY, ENDPOINT_ACCURACY, UNSUPPORTED_CLAIMS,
    ),
    RANKING_CLAIM: (
        COMPARATIVE_RANKING_ACCURACY, CERTAINTY_CALIBRATION,
        DIRECT_VS_INDIRECT_TRANSPARENCY, UNSUPPORTED_CLAIMS,
    ),
    PIPELINE_CLAIM: (FACTUAL_ACCURACY, EVIDENCE_RECENCY, CITATION_QUALITY),
    MECHANISM_CLAIM: (FACTUAL_ACCURACY,),
    CERTAINTY_CLAIM: (CERTAINTY_CALIBRATION, DIRECT_VS_INDIRECT_TRANSPARENCY),
}


def authoritative_evidence_for(claim_type: str) -> tuple[str, ...]:
    """The evidence families that can legitimately answer this claim type."""
    if claim_type not in _POLICY:
        raise ClaimError(f"{claim_type!r} is not a claim type: expected one of {CLAIM_TYPES}")
    return _POLICY[claim_type]


def dimensions_for(claim_type: str) -> tuple[str, ...]:
    """The evaluation dimensions a claim of this type can bear on."""
    if claim_type not in _DIMENSIONS_OF:
        raise ClaimError(f"{claim_type!r} is not a claim type: expected one of {CLAIM_TYPES}")
    return _DIMENSIONS_OF[claim_type]


def assert_routable(claim_type: str, evidence_type: str) -> None:
    """Raise ``CategoryError`` unless *evidence_type* can answer *claim_type*.

    The plan calls grading a boxed-warning claim against a league table "an explicit test
    failure", and this is that failure. It raises instead of returning a verdict because a
    category error has no correct verdict — a low score would still be a finding filed
    against a comparison that never should have been made.
    """
    allowed = authoritative_evidence_for(claim_type)
    if evidence_type not in allowed:
        raise CategoryError(
            f"{claim_type} cannot be graded against {evidence_type}: it is answered by "
            f"{POLICY_DESCRIPTION[claim_type]} ({', '.join(allowed)})"
        )


# =========================================================================================
# What the extractor returns
# =========================================================================================
@dataclass(frozen=True)
class ExtractedClaim:
    """One atomic claim a response made, as observed — never as judged.

    Frozen for the same reason ``HarmonisationProposal`` is: everything here came from a
    model reading text, and a grader that could edit its inputs before scoring them would
    make the finding untraceable to what was actually said.

    Validated on construction, so an extractor returning a claim type or certainty level
    outside the vocabulary fails at the boundary rather than producing a finding filed
    under a category nothing else recognises.
    """

    claim_text: str
    claim_type: str
    subject: str
    certainty: str = HEDGED
    comparator: str | None = None
    indication: str | None = None
    outcome: str | None = None
    direction: str = NO_DIRECTION
    polarity: str = ASSERTED
    # A number the response stated, e.g. "45% of patients" -> 45.0. None when it made a
    # qualitative claim, which is the common case and not a defect.
    magnitude: float | None = None
    magnitude_unit: str | None = None
    # Study identifiers the response named. Checked for existence, never assumed false —
    # see `grade_citations`.
    cited_identifiers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.claim_type not in CLAIM_TYPES:
            raise ClaimError(
                f"claim_type must be one of {CLAIM_TYPES}, got {self.claim_type!r}"
            )
        if self.certainty not in CERTAINTY_LEVELS:
            raise ClaimError(
                f"certainty must be one of {CERTAINTY_LEVELS}, got {self.certainty!r}"
            )
        if self.direction not in DIRECTIONS:
            raise ClaimError(
                f"direction must be one of {DIRECTIONS}, got {self.direction!r}"
            )
        if self.polarity not in POLARITIES:
            raise ClaimError(
                f"polarity must be one of {POLARITIES}, got {self.polarity!r}"
            )
        if not (self.claim_text or "").strip():
            raise ClaimError("claim_text is required: a claim must quote what was said")
        if not (self.subject or "").strip():
            raise ClaimError("subject is required: a claim must be about something")
        # A comparative claim without a comparator cannot be routed to head-to-head
        # evidence, which is the only thing that can answer it.
        if self.claim_type in (DIRECT_COMPARISON_CLAIM, RANKING_CLAIM):
            if self.direction == NO_DIRECTION:
                raise ClaimError(
                    f"{self.claim_type} needs a direction: a comparison that asserts "
                    "nothing about which is better is not a comparative claim"
                )

    def as_dict(self) -> dict:
        return {
            "claim_text": self.claim_text,
            "claim_type": self.claim_type,
            "subject": self.subject,
            "comparator": self.comparator,
            "indication": self.indication,
            "outcome": self.outcome,
            "direction": self.direction,
            "polarity": self.polarity,
            "certainty": self.certainty,
            "magnitude": self.magnitude,
            "magnitude_unit": self.magnitude_unit,
            "cited_identifiers": list(self.cited_identifiers),
        }


@dataclass(frozen=True)
class Finding:
    """The verdict on one claim, with the reason and the evidence it was judged against."""

    classification: str
    reason: str
    dimensions: tuple[str, ...] = ()
    evidence_refs: tuple[qg.EvidenceRef, ...] = ()
    # Populated only when certainty calibration actually ran, so a null here means "not
    # assessed" rather than "calibrated" — the two must not look alike in a rollup.
    certainty_verdict: str | None = None
    flags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.classification not in CLASSIFICATIONS:
            raise ClaimError(
                f"classification must be one of {CLASSIFICATIONS}, "
                f"got {self.classification!r}"
            )

    @property
    def is_adverse(self) -> bool:
        return self.classification in ADVERSE_CLASSIFICATIONS

    def as_dict(self) -> dict:
        return {
            "classification": self.classification,
            "reason": self.reason,
            "dimensions": list(self.dimensions),
            "evidence": [
                {
                    "evidence_type": ref.evidence_type,
                    "evidence_id": ref.evidence_id,
                    "relationship_role": ref.relationship_role,
                }
                for ref in self.evidence_refs
            ],
            "certainty_verdict": self.certainty_verdict,
            "flags": list(self.flags),
            "is_adverse": self.is_adverse,
        }


# =========================================================================================
# Certainty calibration
# =========================================================================================
CALIBRATED = "CALIBRATED"
OVERCLAIMED = "OVERCLAIMED"
UNDERCLAIMED = "UNDERCLAIMED"
CERTAINTY_VERDICTS = (CALIBRATED, OVERCLAIMED, UNDERCLAIMED)


def calibrate_certainty(
    claim_certainty: str,
    *,
    interval_crosses_null: bool | None,
    is_direct: bool | None = None,
    claims_a_winner: bool = True,
) -> tuple[str, str]:
    """``(verdict, reason)`` — the model's hedging against the statistical uncertainty.

    The differentiated capability in this phase, and the one worth getting exactly right.

    Over-claiming is the headline case: the interval includes no difference and the response
    asserts superiority anyway. **Under-claiming is also reported**, because a model hedging
    where our evidence is clean is not a harmless conservatism — it usually means the
    evidence is not reaching the model at all, which is a communication gap Phase 9 acts on.
    Reporting only one direction would make the metric look like a safety check instead of
    an alignment measure.

    ``claims_a_winner=False`` marks a claim of *equivalence*, which needs the opposite
    reading of the same interval. A wide interval that happens to include no difference is
    an absence of evidence, not evidence of absence — the same distinction the evidence-gap
    category rests on — so *"there is no difference between them"* stated definitively
    over-claims just as surely as an unsupported winner does, and grading it as calibrated
    would let a model establish equivalence from an underpowered trial.

    ``interval_crosses_null=None`` means the evidence had no interval to compare against.
    That is *not* calibration: it is the absence of the input, and it returns ``CALIBRATED``
    with a reason saying so rather than inventing a verdict from nothing.
    """
    rank = _CERTAINTY_RANK[claim_certainty]

    if interval_crosses_null is None:
        return CALIBRATED, (
            "no interval was available to calibrate against, so the response's certainty "
            "was not assessed"
        )

    if interval_crosses_null:
        if not claims_a_winner:
            if rank >= _CERTAINTY_RANK[DEFINITIVE]:
                return OVERCLAIMED, (
                    "the response asserts there is no difference, but an interval that "
                    "includes no difference is an absence of evidence, not evidence of "
                    "equivalence"
                )
            return CALIBRATED, (
                "the response reports no clear difference and the interval includes no "
                "difference — the wording matches the evidence"
            )
        if rank >= _CERTAINTY_RANK[PROBABLE]:
            return OVERCLAIMED, (
                f"the response is {claim_certainty.lower()} but the interval includes no "
                "difference, so the evidence does not distinguish the treatments"
            )
        return CALIBRATED, (
            "the response hedges and the interval includes no difference — the hedging "
            "matches the evidence"
        )

    # The interval excludes no-effect: the evidence does distinguish them.
    if rank <= _CERTAINTY_RANK[HEDGED]:
        return UNDERCLAIMED, (
            f"the response is {claim_certainty.lower()} but the interval excludes no "
            "difference — our evidence is more definite than the answer given, which "
            "usually means it is not reaching the model"
        )
    if is_direct is False and rank >= _CERTAINTY_RANK[DEFINITIVE]:
        # A definite assertion resting on an indirect estimate is not wrong, but the
        # response did not disclose the extra assumption it inherited.
        return OVERCLAIMED, (
            "the response is definitive but the estimate is indirect, and its transitivity "
            "assumption was not disclosed"
        )
    return CALIBRATED, "the response's certainty matches the interval"


# =========================================================================================
# Reading an estimate as a direction
# =========================================================================================
def favoured_treatment(answer: dict, canonical_outcome_id: str | None) -> str | None:
    """Which arm the estimate favours, or ``None`` when that cannot be established.

    Two things must both be known: which way the estimate points, and whether a higher
    event rate is *good*. The second is not inferable from the number — an ACR50 risk ratio
    of 1.4 favours the treatment, an adverse-event risk ratio of 1.4 favours the comparator,
    and the arithmetic is identical. ``benefit_direction`` is therefore required config with
    no default, and this returns ``None`` when it is missing rather than picking one.

    ``None`` is also returned when the interval includes no difference: an estimate that
    does not exclude the null does not favour anybody, and reading its point estimate as a
    winner is precisely the over-claim this phase exists to catch.
    """
    direction = outcomes.benefit_direction(canonical_outcome_id)
    if direction is None:
        return None
    if qg.crosses_no_effect(answer) is not False:
        return None

    estimate = answer.get("estimate")
    if estimate is None:
        return None
    measure = (answer.get("effect_measure") or "").lower()
    null_value = 0.0 if ("difference" in measure or "_diff" in measure) else 1.0

    treatment, comparator = answer.get("treatment"), answer.get("comparator")
    higher_favours = treatment if direction == outcomes.HIGHER_IS_BETTER else comparator
    lower_favours = comparator if direction == outcomes.HIGHER_IS_BETTER else treatment
    return higher_favours if float(estimate) > null_value else lower_favours


def claimed_winner(claim: ExtractedClaim) -> str | None:
    """The treatment the claim says is better, or ``None`` for a similarity claim.

    ``NEGATED`` flips it: *"Rinvoq is not more effective than Humira"* asserts the opposite
    of the same sentence without the "not", and grading it as though it claimed superiority
    would invert the verdict on every negated comparison.
    """
    if claim.direction == SIMILAR or claim.direction == NO_DIRECTION:
        return None
    superior = claim.direction == SUPERIOR
    if claim.polarity == NEGATED:
        superior = not superior
    return claim.subject if superior else claim.comparator


def _same_treatment(left: str | None, right: str | None) -> bool:
    """Whether two names denote the same node, resolved through the curated drug table.

    Delegated rather than string-compared: "upadacitinib", "RINVOQ" and "Upadacitinib 15 mg"
    are one treatment, and a case-sensitive comparison here would report a contradiction
    every time a model used the generic name.
    """
    if not left or not right:
        return False
    from app.evidence import treatments  # local import keeps the module import graph flat

    return treatments.canonical_treatment(left)[0].lower() == (
        treatments.canonical_treatment(right)[0].lower()
    )


# =========================================================================================
# Graders — one per claim type, each routed before it reads anything
# =========================================================================================
def evidence_unavailable(claim: ExtractedClaim, reason: str) -> Finding:
    """We hold nothing authoritative on this. **Not** an adverse finding.

    Says something about our corpus, not about the answer. Scoring a model down because we
    have not curated the evidence yet would make alignment fall as the evidence base thins,
    which is exactly backwards — and on today's prod corpus it would mark almost every
    response wrong.
    """
    return Finding(
        classification=EVIDENCE_UNAVAILABLE,
        reason=reason,
        dimensions=(EVIDENCE_COVERAGE,),
    )


def grade_approval(
    claim: ExtractedClaim,
    *,
    fact_id: str | None,
    approved_indications: list[str],
    label_updated_at=None,
) -> Finding:
    """An approval claim against the label. Answered by the label or not at all."""
    assert_routable(claim.claim_type, qg.DRUG_FACT_EVIDENCE)
    if fact_id is None:
        return evidence_unavailable(
            claim, f"no verified drug facts are held for {claim.subject}"
        )
    if not approved_indications:
        # An empty list is a failed extraction, not a regulatory finding of non-approval.
        # Reading it as "not approved" would turn our parsing gap into a claim about the FDA.
        return evidence_unavailable(
            claim,
            f"the verified label record for {claim.subject} has no extracted "
            "approved-indication list, so it cannot answer an approval claim either way",
        )

    ref = qg.EvidenceRef(qg.DRUG_FACT_EVIDENCE, fact_id, qg.SUPPORTS_EXPECTED_ANSWER)
    target = (claim.indication or "").strip().lower()
    if not target:
        return evidence_unavailable(
            claim, "the claim names no indication, so no label entry can be checked"
        )

    listed = any(target == item.strip().lower() for item in approved_indications)
    asserted = claim.polarity == ASSERTED
    dimensions = dimensions_for(claim.claim_type)
    as_of = f" (label as of {label_updated_at})" if label_updated_at else ""

    if listed == asserted:
        return Finding(
            classification=ALIGNED,
            reason=(
                f"the verified label {'lists' if listed else 'does not list'} "
                f"{claim.indication}{as_of}, which is what the response said"
            ),
            dimensions=dimensions,
            evidence_refs=(ref,),
        )
    return Finding(
        classification=CONTRADICTORY,
        reason=(
            f"the response says {claim.subject} is "
            f"{'approved' if asserted else 'not approved'} for {claim.indication}, but the "
            f"verified label{as_of} lists: {', '.join(approved_indications)}"
        ),
        dimensions=dimensions,
        evidence_refs=(qg.EvidenceRef(
            qg.DRUG_FACT_EVIDENCE, fact_id, qg.CONTRADICTS_EXPECTED_ANSWER
        ),),
    )


def grade_safety(
    claim: ExtractedClaim,
    *,
    fact_id: str | None,
    boxed_warnings: list[str],
    has_boxed_warning: bool | None,
    label_updated_at=None,
) -> Finding:
    """A boxed-warning claim against the label.

    Graded strictly: a model denying a boxed warning that exists is the single most
    consequential error in this system, and it is recorded as ``CONTRADICTORY`` against
    ``SAFETY_ACCURACY`` with no softening for hedged wording.
    """
    assert_routable(claim.claim_type, qg.DRUG_FACT_EVIDENCE)
    if fact_id is None:
        return evidence_unavailable(
            claim, f"no verified drug facts are held for {claim.subject}"
        )
    if has_boxed_warning is None:
        return evidence_unavailable(
            claim,
            f"the verified label record for {claim.subject} does not record whether a "
            "boxed warning is present",
        )
    if bool(boxed_warnings) and not has_boxed_warning:
        # The row contradicts itself; grading against it would pick one half arbitrarily.
        return evidence_unavailable(
            claim,
            f"the label record for {claim.subject} contradicts itself — it carries boxed "
            "warning text with the boxed-warning flag unset — so it cannot be graded against",
        )

    dimensions = dimensions_for(claim.claim_type)
    as_of = f" (label as of {label_updated_at})" if label_updated_at else ""
    asserted = claim.polarity == ASSERTED

    if bool(has_boxed_warning) == asserted:
        detail = f": {'; '.join(boxed_warnings)}" if boxed_warnings else ""
        return Finding(
            classification=ALIGNED,
            reason=(
                f"the verified label{as_of} "
                f"{'carries' if has_boxed_warning else 'carries no'} boxed warning"
                f"{detail}, which is what the response said"
            ),
            dimensions=dimensions,
            evidence_refs=(qg.EvidenceRef(
                qg.DRUG_FACT_EVIDENCE, fact_id, qg.SUPPORTS_EXPECTED_ANSWER
            ),),
        )
    return Finding(
        classification=CONTRADICTORY,
        reason=(
            f"the response says {claim.subject} "
            f"{'carries' if asserted else 'does not carry'} a boxed warning, but the "
            f"verified label{as_of} says otherwise"
            + (f": {'; '.join(boxed_warnings)}" if boxed_warnings else "")
        ),
        dimensions=dimensions,
        evidence_refs=(qg.EvidenceRef(
            qg.DRUG_FACT_EVIDENCE, fact_id, qg.CONTRADICTS_EXPECTED_ANSWER
        ),),
        flags=("SAFETY_CONTRADICTION",),
    )


def grade_comparison(
    claim: ExtractedClaim,
    *,
    answer: dict | None,
    canonical_outcome_id: str | None,
    network_id: str | None = None,
) -> Finding:
    """A comparative or ranking claim against the resolver's answer.

    The heart of the phase, and where the two rules that matter most both apply.

    **An exploratory result cannot grade a claim.** The execution-mode table is explicit
    that ``EXPLORATORY`` output may not affect AI scoring, so a non-releasable estimate is
    reported as evidence unavailable rather than quietly used. Without this the alignment
    dashboard would be built on numbers no statistician has approved.

    **A gap is unsupported, not contradictory.** When the evidence cannot produce the
    comparison, the model asserting a winner has said something we cannot back — but we have
    not shown it wrong, and telling a brand team "the model is contradicting our evidence"
    when the truth is "we have no evidence" would send them to argue a case they cannot make.
    """
    evidence_type = qg.NMA_RESULT if claim.claim_type == RANKING_CLAIM else qg.CLINICAL_STUDY
    assert_routable(claim.claim_type, evidence_type)

    dimensions = dimensions_for(claim.claim_type)
    if answer is None:
        return evidence_unavailable(
            claim,
            f"no network holds both {claim.subject} and {claim.comparator} on this endpoint",
        )

    status = answer.get("status") or ""
    network_ref = (
        (qg.EvidenceRef(qg.EVIDENCE_NETWORK, network_id, qg.PROVIDES_CONTEXT),)
        if network_id else ()
    )

    if not statuses.is_releasable(status):
        if statuses.is_gap(status):
            return _grade_against_a_gap(
                claim, status, dimensions, network_ref, detail=answer.get("reason")
            )
        # Governance or service states: EXPLORATORY, PROTOCOL_PENDING_APPROVAL,
        # NMA_SERVICE_UNAVAILABLE. None of them is a finding about the response.
        return evidence_unavailable(
            claim,
            f"the comparison resolves to {status}, which is not releasable and therefore "
            "may not be used to grade a response",
        )

    favoured = favoured_treatment(answer, canonical_outcome_id)
    crosses = qg.crosses_no_effect(answer)
    is_direct = answer.get("evidence_level") == 1
    claimed = claimed_winner(claim)
    verdict, calibration_reason = calibrate_certainty(
        claim.certainty, interval_crosses_null=crosses, is_direct=is_direct,
        claims_a_winner=claimed is not None,
    )
    estimate_text = _estimate_text(answer)
    refs = network_ref + tuple(
        qg.EvidenceRef(qg.CLINICAL_STUDY, study_id, qg.SUPPORTS_EXPECTED_ANSWER)
        for study_id in (answer.get("contributing_studies") or [])
    )

    # A claim of similarity, against an interval that includes no difference.
    if claimed is None:
        if crosses is True:
            return Finding(
                classification=(
                    PARTIALLY_ALIGNED if verdict == OVERCLAIMED else ALIGNED
                ),
                reason=(
                    f"{estimate_text}. {calibration_reason}"
                    if verdict == OVERCLAIMED
                    else f"the response reports no difference and {estimate_text}"
                ),
                dimensions=dimensions, evidence_refs=refs, certainty_verdict=verdict,
            )
        return Finding(
            classification=CONTRADICTORY,
            reason=(
                f"the response reports no difference, but {estimate_text} and the interval "
                f"excludes no difference, favouring {favoured}"
            ),
            dimensions=dimensions, evidence_refs=refs, certainty_verdict=verdict,
        )

    if favoured is None:
        if outcomes.benefit_direction(canonical_outcome_id) is None:
            return Finding(
                classification=NOT_COMPARABLE,
                reason=(
                    f"{canonical_outcome_id or 'this endpoint'} does not declare a "
                    "benefit_direction, so no estimate on it can be read as favouring "
                    "either treatment"
                ),
                dimensions=dimensions, evidence_refs=refs,
            )
        # The interval includes no difference, so the evidence names no winner.
        return Finding(
            classification=PARTIALLY_ALIGNED if verdict == OVERCLAIMED else ALIGNED,
            reason=(
                f"the response says {claimed} is better, but {estimate_text} — "
                f"{calibration_reason}"
            ),
            dimensions=dimensions, evidence_refs=refs, certainty_verdict=verdict,
        )

    if _same_treatment(favoured, claimed):
        if verdict == CALIBRATED:
            return Finding(
                classification=ALIGNED,
                reason=f"{estimate_text}, favouring {favoured} as the response said",
                dimensions=dimensions, evidence_refs=refs, certainty_verdict=verdict,
            )
        return Finding(
            classification=PARTIALLY_ALIGNED,
            reason=(
                f"{estimate_text}, favouring {favoured} as the response said, but "
                f"{calibration_reason}"
            ),
            dimensions=dimensions, evidence_refs=refs, certainty_verdict=verdict,
        )

    return Finding(
        classification=CONTRADICTORY,
        reason=(
            f"the response says {claimed} is better, but {estimate_text}, favouring "
            f"{favoured}"
        ),
        dimensions=dimensions,
        evidence_refs=network_ref + tuple(
            qg.EvidenceRef(qg.CLINICAL_STUDY, study_id, qg.CONTRADICTS_EXPECTED_ANSWER)
            for study_id in (answer.get("contributing_studies") or [])
        ),
        certainty_verdict=verdict,
    )


def _grade_against_a_gap(
    claim: ExtractedClaim,
    status: str,
    dimensions: tuple[str, ...],
    network_ref: tuple[qg.EvidenceRef, ...],
    *,
    detail: str | None = None,
) -> Finding:
    """A comparison the evidence cannot make. Asserting one anyway is unsupported.

    *detail* is the resolver's own sentence about **this** pair and outranks the status
    constant's generic description. ``NETWORK_DISCONNECTED`` reads identically for a drug
    nobody has studied and for a drug whose only trial our own protocol window excluded,
    and only the second is a conversation about the window. Reporting the generic line
    sends a reader hunting for trials that already exist — the same disclosure failure
    ``excluded_nodes`` was added upstream to prevent, undone one layer later.

    The status is still named alongside it, so a finding stays greppable by its
    machine-readable code rather than only by prose that varies per pair.
    """
    described = (detail or "").strip().rstrip(".") or statuses.describe(status)
    if claimed_winner(claim) is None or claim.certainty == UNCERTAIN:
        # The response declined to name a winner, which is what the evidence supports.
        return Finding(
            classification=ALIGNED,
            reason=(
                f"the response does not assert a winner, and the evidence cannot produce "
                f"this comparison ({status}: {described})"
            ),
            dimensions=dimensions,
            evidence_refs=network_ref,
        )
    return Finding(
        classification=UNSUPPORTED,
        reason=(
            f"the response asserts a winner, but the evidence cannot produce this "
            f"comparison ({status}: {described}). This is an absence of evidence, not a "
            "contradiction — nothing here shows the response is wrong"
        ),
        dimensions=dimensions + (UNSUPPORTED_CLAIMS,),
        evidence_refs=(
            network_ref[:1] and (qg.EvidenceRef(
                network_ref[0].evidence_type, network_ref[0].evidence_id,
                qg.DEFINES_EVIDENCE_GAP,
            ),) or ()
        ),
    )


def _estimate_text(answer: dict) -> str:
    """The stored estimate, quoted. Never characterised.

    Delegates the phrasing to ``question_generation.describe_estimate`` so a finding and the
    expected answer it is graded against print the same number the same way.
    """
    return f"our evidence gives {qg.describe_estimate(answer)}"


def grade_mechanism(
    claim: ExtractedClaim, *, fact_id: str | None, drug_class: str | None
) -> Finding:
    """A mechanism-of-action claim against the curated class.

    Deliberately shallow. We hold a drug's *class*, not a validated pharmacological
    ontology, so this can confirm a stated class and can say when we hold none — it cannot
    adjudicate a detailed mechanistic assertion, and pretending otherwise would produce
    confident findings about pharmacology from a one-word config field.
    """
    assert_routable(claim.claim_type, qg.DRUG_FACT_EVIDENCE)
    if not drug_class:
        return evidence_unavailable(
            claim,
            f"no curated drug class is held for {claim.subject}, and we hold no "
            "pharmacological ontology that could adjudicate a mechanism claim",
        )

    stated = (claim.claim_text or "").lower()
    refs = (
        (qg.EvidenceRef(qg.DRUG_FACT_EVIDENCE, fact_id, qg.SUPPORTS_EXPECTED_ANSWER),)
        if fact_id else ()
    )
    if drug_class.lower() in stated:
        return Finding(
            classification=ALIGNED,
            reason=f"the response names {claim.subject}'s curated class, {drug_class}",
            dimensions=dimensions_for(claim.claim_type),
            evidence_refs=refs,
        )
    return Finding(
        classification=EVIDENCE_UNAVAILABLE,
        reason=(
            f"{claim.subject} is curated as {drug_class}, which the response does not "
            "name. We hold no ontology able to judge the mechanism actually described, so "
            "this is recorded as uncheckable rather than wrong"
        ),
        dimensions=(EVIDENCE_COVERAGE,),
        evidence_refs=refs,
    )


def grade_pipeline(
    claim: ExtractedClaim,
    *,
    study_id: str | None,
    development_phase: str | None,
    candidate_id: str | None = None,
) -> Finding:
    """A pipeline claim against the registry.

    A registry record is evidence that a trial exists; its absence from *our* corpus is not
    evidence that it does not, because we ingest only the drugs curation has marked for full
    depth. That asymmetry is why a miss here is ``EVIDENCE_UNAVAILABLE``.
    """
    assert_routable(claim.claim_type, qg.CLINICAL_STUDY)
    if study_id is None and candidate_id is None:
        return evidence_unavailable(
            claim,
            f"no registry record is held for {claim.subject}; our corpus covers only the "
            "drugs curation has marked for trial ingestion, so this is not evidence that "
            "no such trial exists",
        )
    refs = tuple(
        ref for ref in (
            qg.EvidenceRef(qg.CLINICAL_STUDY, study_id, qg.SUPPORTS_EXPECTED_ANSWER)
            if study_id else None,
            qg.EvidenceRef(qg.COMPETITOR_CANDIDATE, candidate_id, qg.PROVIDES_CONTEXT)
            if candidate_id else None,
        ) if ref is not None
    )
    stated = (claim.claim_text or "").lower()
    if development_phase and development_phase.lower() not in stated:
        return Finding(
            classification=PARTIALLY_ALIGNED,
            reason=(
                f"a registry record for {claim.subject} exists, but it is "
                f"{development_phase} and the response does not say so"
            ),
            dimensions=dimensions_for(claim.claim_type),
            evidence_refs=refs,
        )
    return Finding(
        classification=ALIGNED,
        reason=f"a registry record for {claim.subject} exists"
               + (f" at {development_phase}" if development_phase else ""),
        dimensions=dimensions_for(claim.claim_type),
        evidence_refs=refs,
    )


def grade_trial_result(
    claim: ExtractedClaim,
    *,
    study_id: str | None,
    stored_value: float | None,
    stored_unit: str | None = None,
    tolerance: float = 0.05,
) -> Finding:
    """A stated trial number against the stored result.

    ``tolerance`` is a *rounding* allowance, not a similarity threshold: a response saying
    "about 45%" against a stored 45.3% is quoting the same number. Anything outside it is a
    different number, and a different number is a contradiction rather than a near miss.
    """
    assert_routable(claim.claim_type, qg.OUTCOME_RESULT)
    if study_id is None or stored_value is None:
        return evidence_unavailable(
            claim,
            f"no verified result is held for {claim.outcome or 'this endpoint'} in "
            f"{claim.subject}",
        )
    if claim.magnitude is None:
        return Finding(
            classification=ALIGNED,
            reason=(
                "the response states no number, so there is nothing to contradict the "
                f"stored result of {stored_value}{stored_unit or ''}"
            ),
            dimensions=dimensions_for(claim.claim_type),
            evidence_refs=(qg.EvidenceRef(
                qg.OUTCOME_RESULT, study_id, qg.PROVIDES_CONTEXT
            ),),
        )

    delta = abs(float(claim.magnitude) - float(stored_value))
    scale = max(abs(float(stored_value)), 1.0)
    if delta / scale <= tolerance:
        return Finding(
            classification=ALIGNED,
            reason=(
                f"the response states {claim.magnitude}{claim.magnitude_unit or ''} and the "
                f"stored result is {stored_value}{stored_unit or ''}"
            ),
            dimensions=dimensions_for(claim.claim_type),
            evidence_refs=(qg.EvidenceRef(
                qg.OUTCOME_RESULT, study_id, qg.SUPPORTS_EXPECTED_ANSWER
            ),),
        )
    return Finding(
        classification=CONTRADICTORY,
        reason=(
            f"the response states {claim.magnitude}{claim.magnitude_unit or ''} but the "
            f"stored result is {stored_value}{stored_unit or ''}"
        ),
        dimensions=dimensions_for(claim.claim_type),
        evidence_refs=(qg.EvidenceRef(
            qg.OUTCOME_RESULT, study_id, qg.CONTRADICTS_EXPECTED_ANSWER
        ),),
    )


# =========================================================================================
# Citations
# =========================================================================================
def grade_citations(
    claim: ExtractedClaim, *, resolvable: tuple[str, ...], unresolvable: tuple[str, ...]
) -> Finding | None:
    """A separate finding for the identifiers a claim cited, or ``None`` when it cited none.

    **A citation we cannot resolve is not a hallucination.** Our corpus holds trials for the
    drugs curation marked for full-depth ingestion and nothing else, so "not in our store"
    is overwhelmingly more likely to mean "not ingested" than "does not exist". Calling it
    hallucinated would be a confident accusation built on our own coverage gap — and
    "hallucinated studies" is a finding a brand team would escalate.

    ``HALLUCINATED_STUDIES`` therefore stays available as a dimension but is only reachable
    from a positive check against the registry, which is a network call and does not belong
    in a pure grader. What is recorded here is citation *quality*.
    """
    if not claim.cited_identifiers:
        return None
    if not unresolvable:
        return Finding(
            classification=ALIGNED,
            reason=f"every cited identifier resolves in our corpus: {', '.join(resolvable)}",
            dimensions=(CITATION_QUALITY,),
            evidence_refs=tuple(
                qg.EvidenceRef(qg.CLINICAL_STUDY, sid, qg.SUPPORTS_EXPECTED_ANSWER)
                for sid in resolvable
            ),
        )
    return Finding(
        classification=EVIDENCE_UNAVAILABLE,
        reason=(
            f"{len(unresolvable)} cited identifier(s) are not in our corpus: "
            f"{', '.join(unresolvable)}. Our corpus covers only curated full-depth drugs, "
            "so this is unverifiable rather than fabricated"
        ),
        dimensions=(CITATION_QUALITY,),
        flags=("UNVERIFIABLE_CITATION",),
        evidence_refs=tuple(
            qg.EvidenceRef(qg.CLINICAL_STUDY, sid, qg.SUPPORTS_EXPECTED_ANSWER)
            for sid in resolvable
        ),
    )


# =========================================================================================
# Response-level rollup
# =========================================================================================
def roll_up(findings: list[Finding]) -> dict:
    """Per-dimension and overall alignment across one response's findings.

    ``alignment_score`` counts only claims that were actually *checkable*: a response is not
    less aligned because our corpus is thin. ``coverage`` reports that separately, so a low
    score and a low coverage are never confused — the first is a model problem and the second
    is ours.
    """
    by_classification: dict[str, int] = {}
    by_dimension: dict[str, dict[str, int]] = {}
    certainty: dict[str, int] = {}

    for finding in findings:
        by_classification[finding.classification] = (
            by_classification.get(finding.classification, 0) + 1
        )
        if finding.certainty_verdict:
            certainty[finding.certainty_verdict] = (
                certainty.get(finding.certainty_verdict, 0) + 1
            )
        for dimension in finding.dimensions:
            bucket = by_dimension.setdefault(dimension, {"aligned": 0, "adverse": 0, "total": 0})
            bucket["total"] += 1
            if finding.is_adverse:
                bucket["adverse"] += 1
            elif finding.classification in (ALIGNED, PARTIALLY_ALIGNED):
                bucket["aligned"] += 1

    checkable = [
        f for f in findings
        if f.classification not in (EVIDENCE_UNAVAILABLE, NOT_COMPARABLE)
    ]
    aligned = sum(1 for f in checkable if f.classification == ALIGNED)
    partial = sum(1 for f in checkable if f.classification == PARTIALLY_ALIGNED)
    score = round((aligned + 0.5 * partial) / len(checkable), 4) if checkable else None

    return {
        "claim_count": len(findings),
        "checkable_count": len(checkable),
        "coverage": round(len(checkable) / len(findings), 4) if findings else None,
        "alignment_score": score,
        "by_classification": by_classification,
        "by_dimension": by_dimension,
        "certainty_calibration": certainty,
        "adverse_count": sum(1 for f in findings if f.is_adverse),
        "safety_contradictions": sum(
            1 for f in findings if "SAFETY_CONTRADICTION" in f.flags
        ),
    }
