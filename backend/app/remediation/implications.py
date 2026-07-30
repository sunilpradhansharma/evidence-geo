"""Strategic implications of an evidence finding (Phase 9). Pure.

The existing GEO engine answers one question: *the AI positioned us weakly, what should we
publish?* Every gap it finds has the same shape and therefore the same remedy. Phase 8
produces findings that do **not** all have that shape, and treating them as if they did is
the mistake this module exists to prevent.

Four findings, four different actions, only two of which are content:

| Finding | What is actually wrong | Who fixes it |
|---|---|---|
| A model contradicts our **verified label** | the model is wrong | medical/regulatory escalation |
| A model hedges where our evidence is **clean** | our evidence is not reaching it | content |
| A model asserts a winner our evidence **cannot produce** | the evidence does not exist | evidence generation |
| The same, but the gap is our **verification backlog** | *we* have not done the work | a curator, internally |

**The last row is the one that matters.** A GEO recommendation saying *"publish a comparison
table"* when the real problem is that nobody has verified our own studies would send a brand
team to spend money on content while the actual fix is an afternoon of curation. Phase 7's
``attribute_gap`` already separates those two, so this reuses it rather than re-deciding, and
``is_externally_actionable`` returns ``False`` for the internal ones — they are reported, and
they do not become content recommendations.

Deterministic, so a recommendation's *reason for existing* is reproducible even though the
prose the LLM later drafts around it is not.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.evidence import claims as cl
from app.evidence import question_generation as qg

# --- strategic implications (plan: Phase 9 outputs) --------------------------------------
AI_MISINFORMATION_RISK = "AI_MISINFORMATION_RISK"
COMMUNICATION_GAP = "COMMUNICATION_GAP"
MISSING_COMPARATIVE_DATA = "MISSING_COMPARATIVE_DATA"
EVIDENCE_GENERATION_NEEDED = "EVIDENCE_GENERATION_NEEDED"
INTERNAL_CURATION_REQUIRED = "INTERNAL_CURATION_REQUIRED"
COMPETITOR_THREAT = "COMPETITOR_THREAT"
POSITIONING_OPPORTUNITY = "POSITIONING_OPPORTUNITY"

IMPLICATIONS = (
    AI_MISINFORMATION_RISK,
    COMMUNICATION_GAP,
    MISSING_COMPARATIVE_DATA,
    EVIDENCE_GENERATION_NEEDED,
    INTERNAL_CURATION_REQUIRED,
    COMPETITOR_THREAT,
    POSITIONING_OPPORTUNITY,
)

# Which implications a *content* recommendation can legitimately answer.
#
# `INTERNAL_CURATION_REQUIRED` is absent by design: the finding is about our own backlog and
# no published asset changes it. `EVIDENCE_GENERATION_NEEDED` is absent for a sharper reason
# — the remedy is a trial or a published synthesis, and a content brief proposing to fill a
# genuine evidence gap with a web page is how unsupported claims get written.
EXTERNALLY_ACTIONABLE = (
    AI_MISINFORMATION_RISK,
    COMMUNICATION_GAP,
    MISSING_COMPARATIVE_DATA,
    COMPETITOR_THREAT,
    POSITIONING_OPPORTUNITY,
)

# Whose queue this lands in. Named so the dashboard can route rather than badge.
OWNER_OF = {
    AI_MISINFORMATION_RISK: "Medical Affairs / Regulatory",
    COMMUNICATION_GAP: "Brand / Content",
    MISSING_COMPARATIVE_DATA: "Brand / Content (with Medical Affairs)",
    EVIDENCE_GENERATION_NEEDED: "Clinical Development / HEOR",
    INTERNAL_CURATION_REQUIRED: "Evidence curation (internal)",
    COMPETITOR_THREAT: "Competitive Intelligence",
    POSITIONING_OPPORTUNITY: "Brand / Content",
}

# Severity weights feeding the existing impact score, on the same 1.0-2.0 scale
# `gaps.POSITION_SEVERITY` already uses so the two finders rank against each other honestly.
#
# A safety contradiction sits above everything: a model denying a boxed warning that exists
# is the single most consequential thing this system can detect, and it must never be ranked
# below a second-line placement because a competitor happened to have more search volume.
SEVERITY_OF = {
    AI_MISINFORMATION_RISK: 2.5,
    COMPETITOR_THREAT: 1.6,
    MISSING_COMPARATIVE_DATA: 1.4,
    COMMUNICATION_GAP: 1.3,
    EVIDENCE_GENERATION_NEEDED: 1.2,
    INTERNAL_CURATION_REQUIRED: 1.0,
    POSITIONING_OPPORTUNITY: 1.0,
}

SAFETY_ESCALATION = 3.0  # a contradicted boxed warning outranks every other finding


@dataclass(frozen=True)
class Implication:
    """What a finding means strategically, and who can act on it."""

    implication: str
    reason: str
    severity: float
    confidence: float
    owner: str
    externally_actionable: bool
    evidence_action: str | None = None

    def as_dict(self) -> dict:
        return {
            "implication": self.implication,
            "reason": self.reason,
            "severity": self.severity,
            "confidence": self.confidence,
            "owner": self.owner,
            "externally_actionable": self.externally_actionable,
            "evidence_action": self.evidence_action,
        }


def confidence_for(
    *,
    classification: str,
    verification_states: list[str | None],
    is_releasable: bool | None = None,
) -> float:
    """How much weight this finding can carry, from the governance state of its evidence.

    Not a model score and not a guess: a recommendation resting on a verified label and a
    ratified network deserves more weight than one resting on a single unreviewed
    extraction, and the difference is knowable from stored columns.

    Deliberately caps below 1.0 when **anything** behind the finding is unverified. A
    recommendation is an instruction to spend money, and one built on an extraction nobody
    has checked should never present itself as certain.
    """
    if classification in (cl.EVIDENCE_UNAVAILABLE, cl.NOT_COMPARABLE):
        # We are reporting our own gap. Confident about the gap, not about any remedy.
        return 0.5

    checked = {
        state for state in verification_states
        if state in ("VERIFIED", "RATIFIED", "ACCEPTED")
    }
    unchecked = [state for state in verification_states if state not in checked]

    if not verification_states:
        return 0.5
    if not unchecked:
        base = 0.9
    elif checked:
        base = 0.7
    else:
        base = 0.4

    if is_releasable is False:
        # An exploratory number cannot support a recommendation any more than it can grade
        # a response — the same rule, one layer out.
        base = min(base, 0.4)
    return round(base, 2)


def classify(
    *,
    classification: str,
    certainty_verdict: str | None,
    claim_type: str,
    flags: tuple[str, ...] | list[str] = (),
    gap_attribution: str | None = None,
    required_evidence: str | None = None,
    verification_states: list[str | None] | None = None,
    is_releasable: bool | None = None,
) -> Implication | None:
    """The strategic implication of one Phase-8 finding, or ``None`` when there is none.

    ``None`` for an aligned finding with calibrated certainty: the model said what our
    evidence says, and manufacturing an action from that would fill the queue with work
    nobody needs done. A recommendation engine that always finds something is not measuring
    anything.
    """
    states = verification_states or []
    confidence = confidence_for(
        classification=classification,
        verification_states=states,
        is_releasable=is_releasable,
    )
    flags = tuple(flags)

    if classification == cl.CONTRADICTORY:
        safety = "SAFETY_CONTRADICTION" in flags or claim_type == cl.SAFETY_WARNING_CLAIM
        return Implication(
            implication=AI_MISINFORMATION_RISK,
            reason=(
                "the model stated something our verified evidence contradicts"
                + (" — and it concerns a boxed warning" if safety else "")
            ),
            severity=SAFETY_ESCALATION if safety else SEVERITY_OF[AI_MISINFORMATION_RISK],
            confidence=confidence,
            owner=OWNER_OF[AI_MISINFORMATION_RISK],
            externally_actionable=True,
        )

    if classification == cl.UNSUPPORTED:
        # The split the phase turns on. Phase 7 already decided whether a gap is about the
        # world or about us, so that decision is read here rather than made again.
        if gap_attribution == qg.ATTRIBUTION_CURATION:
            return Implication(
                implication=INTERNAL_CURATION_REQUIRED,
                reason=(
                    "the model asserted something we cannot currently check, but the reason "
                    "we cannot check it is our own verification backlog, not an absence of "
                    "evidence. No published content changes this"
                ),
                severity=SEVERITY_OF[INTERNAL_CURATION_REQUIRED],
                confidence=confidence,
                owner=OWNER_OF[INTERNAL_CURATION_REQUIRED],
                externally_actionable=False,
                evidence_action="Verify the studies blocking this comparison.",
            )
        if gap_attribution == qg.ATTRIBUTION_PROTOCOL:
            return Implication(
                implication=INTERNAL_CURATION_REQUIRED,
                reason=(
                    "the evidence exists but the approved analysis window excludes it, so "
                    "the comparison is unavailable by our own methodology rather than by "
                    "any fact about the trials"
                ),
                severity=SEVERITY_OF[INTERNAL_CURATION_REQUIRED],
                confidence=confidence,
                owner="Statistical review",
                externally_actionable=False,
                evidence_action=(
                    "Statistical review of the approved timepoint window for this endpoint."
                ),
            )
        if required_evidence:
            return Implication(
                implication=EVIDENCE_GENERATION_NEEDED,
                reason=(
                    "the model asserted a comparison no evidence supports, and closing it "
                    "requires new evidence rather than new content"
                ),
                severity=SEVERITY_OF[EVIDENCE_GENERATION_NEEDED],
                confidence=confidence,
                owner=OWNER_OF[EVIDENCE_GENERATION_NEEDED],
                externally_actionable=False,
                evidence_action=required_evidence,
            )
        return Implication(
            implication=MISSING_COMPARATIVE_DATA,
            reason=(
                "the model asserted a comparison our evidence cannot produce; the honest "
                "position is that the comparison is unavailable, and saying so publicly is "
                "itself the correction"
            ),
            severity=SEVERITY_OF[MISSING_COMPARATIVE_DATA],
            confidence=confidence,
            owner=OWNER_OF[MISSING_COMPARATIVE_DATA],
            externally_actionable=True,
        )

    if certainty_verdict == cl.UNDERCLAIMED:
        return Implication(
            implication=COMMUNICATION_GAP,
            reason=(
                "our evidence is more definite than the answer the model gave, which "
                "usually means the evidence is not reaching it"
            ),
            severity=SEVERITY_OF[COMMUNICATION_GAP],
            confidence=confidence,
            owner=OWNER_OF[COMMUNICATION_GAP],
            externally_actionable=True,
        )

    if certainty_verdict == cl.OVERCLAIMED:
        return Implication(
            implication=AI_MISINFORMATION_RISK,
            reason=(
                "the model stated more confidence than the interval supports; the estimate "
                "itself is not disputed, only the certainty attached to it"
            ),
            severity=SEVERITY_OF[AI_MISINFORMATION_RISK] - 0.5,
            confidence=confidence,
            owner=OWNER_OF[AI_MISINFORMATION_RISK],
            externally_actionable=True,
        )

    if classification in (cl.EVIDENCE_UNAVAILABLE, cl.NOT_COMPARABLE):
        # Reported for coverage, never actioned. This is our corpus talking about itself.
        return None
    return None


def competitor_threat(
    *, treatment: str, indication: str, reasons: list[str], has_posted_results: bool
) -> Implication:
    """A discovered competitor with evidence behind it (Phase 5 -> Phase 9).

    Only reached for candidates a reviewer has ACCEPTED, so the threat is a curated fact
    rather than a sweep's opinion. ``has_posted_results`` raises the severity because a
    competitor with posted results can enter a network and change a comparison, whereas one
    still recruiting cannot yet.
    """
    return Implication(
        implication=COMPETITOR_THREAT,
        reason=(
            f"{treatment} was accepted into the {indication} competitive set "
            f"({', '.join(reasons) or 'no reason recorded'})"
            + (" and has posted results" if has_posted_results else "")
        ),
        severity=SEVERITY_OF[COMPETITOR_THREAT] + (0.3 if has_posted_results else 0.0),
        confidence=0.9,
        owner=OWNER_OF[COMPETITOR_THREAT],
        externally_actionable=True,
    )
