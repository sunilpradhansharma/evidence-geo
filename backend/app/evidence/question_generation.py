"""Evidence-driven question generation — the pure half (Phase 7).

Turns stored evidence into monitoring questions plus the answer the evidence actually
supports. No database, no network, no LLM, so every category is testable offline exactly
like ``endpoints.py``, ``approvals.py`` and ``resolver.py``.

**Question text is constructed, not generated.** ``variations/generator.py`` tells a model
*"do NOT introduce any new facts, claims, drug names, doses or comparisons"* — a rule the
model is asked to follow. A template cannot say anything its inputs do not, so here the
same rule is a property of the code rather than an instruction, which is the same move
``HarmonisationProposal`` makes by being frozen and having no ``apply()``. That matters
more here than anywhere else in the programme: these questions are sent to external models
and their answers are then graded against our evidence. A generated question that quietly
introduced a claim would seed the corpus with the very thing Phase 8 exists to detect.

**The expected answer quotes the evidence; it never characterises it.** Every number in it
comes from a stored field, and any flag the evidence carries travels with it. An expected
answer that dropped ``EVENTS_DERIVED_FROM_PERCENTAGE`` would launder a caveat the
extraction was careful to record.

**A gap is not automatically an evidence gap.** See ``attribute_gap`` — the single most
important function in this module.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date

from app.config import outcomes
from app.evidence import treatments

# --- categories (plan: Phase 7) ---------------------------------------------------------
DRUG_FACT = "DRUG_FACT"
COMPARATIVE_EFFICACY = "COMPARATIVE_EFFICACY"
POPULATION_SPECIFIC = "POPULATION_SPECIFIC"
SAFETY = "SAFETY"
EVIDENCE_QUALITY = "EVIDENCE_QUALITY"
COMPETITOR_DISCOVERY = "COMPETITOR_DISCOVERY"
EVIDENCE_GAP = "EVIDENCE_GAP"

CATEGORIES = (
    DRUG_FACT,
    COMPARATIVE_EFFICACY,
    POPULATION_SPECIFIC,
    SAFETY,
    EVIDENCE_QUALITY,
    COMPETITOR_DISCOVERY,
    EVIDENCE_GAP,
)

# --- default expected evidence type -----------------------------------------------------
# A DEFAULT, deliberately. One answer routinely mixes claim types — "approved for PsA,
# works better than X, carries a boxed warning" needs three different authorities — so the
# real ruleset is per claim and belongs to Phase 8. Storing a question-level default here
# would be wrong if it were treated as the routing decision; it is a hint for the reviewer
# reading the staged row.
REGULATORY_LABEL = "REGULATORY_LABEL"
HEAD_TO_HEAD_TRIAL = "HEAD_TO_HEAD_TRIAL"
TRIAL_RESULT = "TRIAL_RESULT"
EVIDENCE_SYNTHESIS = "EVIDENCE_SYNTHESIS"
TRIAL_REGISTRY = "TRIAL_REGISTRY"
NO_EVIDENCE_AVAILABLE = "NO_EVIDENCE_AVAILABLE"

EXPECTED_EVIDENCE_TYPES = (
    REGULATORY_LABEL,
    HEAD_TO_HEAD_TRIAL,
    TRIAL_RESULT,
    EVIDENCE_SYNTHESIS,
    TRIAL_REGISTRY,
    NO_EVIDENCE_AVAILABLE,
)

# --- QuestionEvidence vocabulary --------------------------------------------------------
# One REQUIRED, single-valued role. Separate supports/contradicts/context booleans would
# permit all three true at once; a single value makes the contradiction unrepresentable
# rather than merely invalid.
SUPPORTS_EXPECTED_ANSWER = "SUPPORTS_EXPECTED_ANSWER"
CONTRADICTS_EXPECTED_ANSWER = "CONTRADICTS_EXPECTED_ANSWER"
PROVIDES_CONTEXT = "PROVIDES_CONTEXT"
DEFINES_EVIDENCE_GAP = "DEFINES_EVIDENCE_GAP"
DEFINES_LIMITATION = "DEFINES_LIMITATION"
SUPERSEDES = "SUPERSEDES"

RELATIONSHIP_ROLES = (
    SUPPORTS_EXPECTED_ANSWER,
    CONTRADICTS_EXPECTED_ANSWER,
    PROVIDES_CONTEXT,
    DEFINES_EVIDENCE_GAP,
    DEFINES_LIMITATION,
    SUPERSEDES,
)

PRIMARY = "PRIMARY"
SUPPORTING = "SUPPORTING"
CONTEXTUAL = "CONTEXTUAL"
EVIDENCE_PRIORITIES = (PRIMARY, SUPPORTING, CONTEXTUAL)

# Evidence lives in several tables and a single foreign key cannot span them. Six nullable
# FKs would reintroduce exactly the nonsense states the single role enum was chosen to
# forbid (two set at once, none set), so the reference is (type, id) and the service
# validates that the row exists.
CLINICAL_STUDY = "CLINICAL_STUDY"
OUTCOME_RESULT = "OUTCOME_RESULT"
DRUG_FACT_EVIDENCE = "DRUG_FACT"
NMA_RESULT = "NMA_RESULT"
COMPETITOR_CANDIDATE = "COMPETITOR_CANDIDATE"
EVIDENCE_NETWORK = "EVIDENCE_NETWORK"

EVIDENCE_TYPES = (
    CLINICAL_STUDY,
    OUTCOME_RESULT,
    DRUG_FACT_EVIDENCE,
    NMA_RESULT,
    COMPETITOR_CANDIDATE,
    EVIDENCE_NETWORK,
)

# --- gap attribution --------------------------------------------------------------------
ATTRIBUTION_EVIDENCE = "EVIDENCE"
ATTRIBUTION_CURATION = "CURATION"
ATTRIBUTION_PROTOCOL = "PROTOCOL"

# Substrings of the scoping report's own rejection reasons. Matched rather than
# reconstructed so the wording stays owned by `comparison_service`, which produces it.
_CURATION_MARKERS = ("verification_status is", "not INCLUDED in this network")
_PROTOCOL_MARKERS = ("outside the approved window",)

# Personas and domains are the existing Question vocabulary, not a new one.
_PERSONA_OF = {
    DRUG_FACT: "Provider",
    COMPARATIVE_EFFICACY: "Provider",
    POPULATION_SPECIFIC: "Provider",
    SAFETY: "Patient",
    EVIDENCE_QUALITY: "Provider",
    COMPETITOR_DISCOVERY: "Provider",
    EVIDENCE_GAP: "Provider",
}
_DOMAIN_OF = {
    DRUG_FACT: "General",
    COMPARATIVE_EFFICACY: "Comparative",
    POPULATION_SPECIFIC: "Efficacy",
    SAFETY: "Safety",
    EVIDENCE_QUALITY: "Comparative",
    COMPETITOR_DISCOVERY: "Comparative",
    EVIDENCE_GAP: "Comparative",
}

INTERNAL_OUTPUT_LABEL = (
    "Internal analytical output — not validated or approved for external use."
)


class GenerationError(ValueError):
    """A question could not be constructed from the evidence supplied."""


@dataclass(frozen=True)
class EvidenceRef:
    """One association a generated question proposes.

    Frozen and role-required, so a reference that supports and contradicts at once cannot
    be constructed, let alone stored.
    """

    evidence_type: str
    evidence_id: str
    relationship_role: str
    evidence_priority: str | None = None

    def __post_init__(self) -> None:
        if self.evidence_type not in EVIDENCE_TYPES:
            raise GenerationError(
                f"evidence_type {self.evidence_type!r} is not one of {', '.join(EVIDENCE_TYPES)}"
            )
        if self.relationship_role not in RELATIONSHIP_ROLES:
            raise GenerationError(
                f"relationship_role {self.relationship_role!r} is not one of "
                f"{', '.join(RELATIONSHIP_ROLES)}"
            )
        if self.evidence_priority is not None and self.evidence_priority not in EVIDENCE_PRIORITIES:
            raise GenerationError(
                f"evidence_priority {self.evidence_priority!r} is not one of "
                f"{', '.join(EVIDENCE_PRIORITIES)}"
            )
        if not (self.evidence_id or "").strip():
            raise GenerationError("evidence_id is required")

    def as_dict(self) -> dict:
        return {
            "evidence_type": self.evidence_type,
            "evidence_id": self.evidence_id,
            "relationship_role": self.relationship_role,
            "evidence_priority": self.evidence_priority,
        }


@dataclass(frozen=True)
class GeneratedQuestion:
    """A question plus the answer the evidence supports and the rows it rests on."""

    category: str
    question_text: str
    expected_answer: str
    expected_evidence_type: str
    indication: str
    persona: str
    domain: str
    brand: str | None = None
    comparator: str | None = None
    therapeutic_area: str | None = None
    evidence: tuple[EvidenceRef, ...] = field(default_factory=tuple)
    evidence_date: date | None = None
    confidence: float = 0.0
    # Every caveat the underlying rows carried. Travels with the question so a reviewer is
    # never shown a clean expected answer over a flagged number.
    flags: tuple[str, ...] = field(default_factory=tuple)
    # Populated for EVIDENCE_GAP only: what would have to exist for the comparison to
    # resolve. Kept OUT of `question_text` because the text is sent to an external model
    # and "what evidence would be required?" is an internal research question, not
    # something a patient or clinician would ask an assistant.
    required_evidence: str | None = None

    @property
    def dedupe_key(self) -> str:
        """Stable across re-generation, so a second pass updates rather than duplicates."""
        parts = "|".join([
            self.category,
            self.indication,
            self.brand or "",
            self.comparator or "",
            _normalise(self.question_text),
        ])
        return hashlib.sha1(parts.encode("utf-8")).hexdigest()[:32]

    def as_dict(self) -> dict:
        return {
            "category": self.category,
            "question_text": self.question_text,
            "expected_answer": self.expected_answer,
            "expected_evidence_type": self.expected_evidence_type,
            "indication": self.indication,
            "persona": self.persona,
            "domain": self.domain,
            "brand": self.brand,
            "comparator": self.comparator,
            "therapeutic_area": self.therapeutic_area,
            "evidence": [ref.as_dict() for ref in self.evidence],
            "evidence_date": self.evidence_date.isoformat() if self.evidence_date else None,
            "confidence": self.confidence,
            "flags": list(self.flags),
            "required_evidence": self.required_evidence,
            "dedupe_key": self.dedupe_key,
        }


def _normalise(text: str) -> str:
    return " ".join((text or "").lower().split())


def endpoint_label(canonical_outcome_id: str | None) -> str:
    """A clinician-readable name for a canonical endpoint id.

    Reads ``canonical_outcomes.yaml`` rather than restating endpoint semantics, for the
    reason ``endpoints.py`` exists: two modules with their own idea of what
    ``PSA_ACR50_W16`` means is how a question and the network answering it come to be
    about different things.
    """
    definition = outcomes.outcome(canonical_outcome_id) or {}
    endpoint = definition.get("endpoint")
    week = definition.get("nominal_timepoint_week")
    if endpoint and week is not None:
        return f"{endpoint} at week {week:g}" if isinstance(week, float) else f"{endpoint} at week {week}"
    if endpoint:
        return str(endpoint)
    return canonical_outcome_id or "the study endpoint"


def _measure_phrase(measure: str | None) -> str:
    return {
        "risk_ratio": "risk ratio",
        "odds_ratio": "odds ratio",
        "hazard_ratio": "hazard ratio",
        "risk_difference": "risk difference",
        "mean_difference": "mean difference",
        "standardised_mean_difference": "standardised mean difference",
    }.get(measure or "", measure or "effect")


def _interval_phrase(answer: dict) -> str:
    lo, hi = answer.get("ci_lower"), answer.get("ci_upper")
    if lo is None or hi is None:
        return ""
    kind = "95% CrI" if (answer.get("interval_type") or "").upper().startswith("CR") else "95% CI"
    return f" ({kind} {lo:.3g} to {hi:.3g})"


def describe_estimate(answer: dict) -> str:
    """The stored estimate, phrased once. Quoted, never characterised.

    Public because Phase 8 grades a model's answer against the expected answer this module
    builds, and the two must describe the same number identically. Two formatters would
    eventually disagree about rounding, and a finding reading *"our evidence gives 0.9592…"*
    against an expected answer reading *"0.959"* looks like two different results.
    """
    estimate = answer.get("estimate")
    if estimate is None:
        return f"the comparison resolved to {answer.get('status')} with no point estimate"
    measure = _measure_phrase(answer.get("effect_measure"))
    return f"a {measure} of {estimate:.3g}{_interval_phrase(answer)}"


def crosses_no_effect(answer: dict) -> bool | None:
    """Whether the interval spans the null. ``None`` when there is no interval.

    ``None`` rather than ``False`` for the same reason ``i_squared`` is ``None`` for a
    single study: "no interval was reported" and "the interval excludes the null" are
    different facts and collapsing them asserts a precision the source never claimed.
    Phase 8's certainty calibration reads this.
    """
    lo, hi = answer.get("ci_lower"), answer.get("ci_upper")
    if lo is None or hi is None:
        return None
    null = 0.0 if (answer.get("effect_measure") or "").endswith("difference") else 1.0
    return bool(lo <= null <= hi)


# =========================================================================================
# Gap attribution — the distinction the whole gap category depends on
# =========================================================================================
def attribute_gap(status: str, scoping: dict | None) -> tuple[str, str]:
    """``(attribution, reason)`` for a resolver gap: is it about the evidence, or about us?

    ``statuses.is_gap`` is true for ``NETWORK_DISCONNECTED`` however the network came to be
    disconnected — and today it is disconnected because **nothing in the corpus is
    verified**, so ``gather_evidence`` skipped every study before topology was even
    considered. Generating *"no evidence compares these treatments"* from that would state
    a fact about the world on the strength of a fact about our own backlog, and would put
    it in a question bank where nothing downstream could tell the two apart.

    Precedence is ``CURATION -> PROTOCOL -> EVIDENCE`` because the checks happen in that
    order upstream: an unverified study never reaches the window check, so a corpus with a
    verification backlog cannot yet know whether the protocol would have excluded anything.

    Only ``EVIDENCE`` may become an evidence-gap question. The other two are findings for a
    curator and a statistical reviewer respectively, and they are returned rather than
    swallowed so the caller can report them.
    """
    reasons = [
        str(entry.get("reason") or "")
        for entry in ((scoping or {}).get("skipped") or [])
        if isinstance(entry, dict)
    ]

    withheld = sorted({r for r in reasons if any(m in r for m in _CURATION_MARKERS)})
    if withheld:
        return ATTRIBUTION_CURATION, (
            f"{len(withheld)} study-level exclusion(s) are our own process, not the "
            f"evidence: {'; '.join(withheld[:3])}"
            + ("; …" if len(withheld) > 3 else "")
        )

    out_of_window = sorted({r for r in reasons if any(m in r for m in _PROTOCOL_MARKERS)})
    if out_of_window:
        return ATTRIBUTION_PROTOCOL, (
            "the evidence exists but the approved analysis window excludes it: "
            + "; ".join(out_of_window[:3])
            + ("; …" if len(out_of_window) > 3 else "")
        )

    return ATTRIBUTION_EVIDENCE, f"no in-scope evidence supports this comparison ({status})"


def is_monitorable_pair(treatment: str, comparator: str) -> tuple[bool, str | None]:
    """``(monitorable, reason)`` — whether this pair is a question anyone would ask an AI.

    A resolver node set is not a question set. **Placebo is the anchor every indirect
    comparison chains through**, so it is in almost every network, and *"is Bimzelx more
    effective than placebo?"* is a legitimate clinical contrast that no patient, prescriber
    or prospect types into an assistant. Generating it would fill the monitored corpus with
    questions whose answers say nothing about the competitive landscape, and every one of
    them would consume a real model call on every run.

    Class-level and aggregate nodes are rejected for a sharper reason: *"is TNFi more
    effective than Rinvoq?"* names a category, and any answer to it is about a class while
    the evidence behind it is about whichever molecules the trial happened to enrol.

    Every judgement here is **delegated** to ``evidence.treatments`` rather than restated.
    A second opinion about what an arm label means is how the builder and this generator
    would come to disagree about the same node.
    """
    for name in (treatment, comparator):
        if treatments.is_placebo(name):
            return False, (
                f"{name} is the network's analytical anchor, not a treatment anyone asks "
                "an assistant to compare against"
            )
        if treatments.is_class_level_node(name):
            return False, f"{name} names a drug class or care strategy, not a product"
        if treatments.is_aggregate_label(name) or treatments.is_uninformative_label(name):
            return False, f"{name} carries no treatment identity of its own"
    return True, None


def required_evidence_for(status: str, treatment: str, comparator: str) -> str:
    """What would have to exist for a gap to close. Named per status, never generic."""
    return {
        "NETWORK_DISCONNECTED": (
            f"A trial randomising {treatment} or {comparator} against a comparator already "
            f"in the other's network, or a published synthesis containing both."
        ),
        "DIRECT_EVIDENCE_UNSUITABLE": (
            f"A head-to-head trial of {treatment} versus {comparator} reporting this "
            "endpoint, timepoint and population."
        ),
        "PUBLISHED_SYNTHESIS_UNSUITABLE": (
            "A published synthesis matching this endpoint, timepoint, population and phase."
        ),
        "INSUFFICIENT_ARM_DATA": (
            "Arm-level events and denominators posted for both treatments at this endpoint."
        ),
        "AMBIGUOUS_ARM_DATA": (
            "A curation decision on which analysis population the contradictory in-scope "
            "results belong to. The data exists; it is not yet unambiguous."
        ),
        "ENDPOINT_MISMATCH": "Results measuring this endpoint rather than a different one.",
        "TIMEPOINT_MISMATCH": (
            "Results inside the protocol's approved window, or an amended window approved "
            "by a statistical reviewer."
        ),
        "POPULATION_NONCOMPARABLE": (
            "Trials in comparable populations, or a stratified analysis that separates them."
        ),
        "TREATMENT_PHASE_MISMATCH": (
            "Results from the same treatment phase — induction and maintenance populations "
            "cannot be pooled."
        ),
        "ROUTE_MIXING_NOT_ESTIMABLE": (
            "Evidence that oral and injectable placebo response is close enough in this "
            "indication for transitivity to hold, or a within-route comparison."
        ),
    }.get(status, f"Comparative evidence linking {treatment} and {comparator}.")


# =========================================================================================
# Category constructors — each pure, each refusing rather than guessing
# =========================================================================================
def comparative_question(
    answer: dict,
    *,
    indication: str,
    canonical_outcome_id: str | None,
    therapeutic_area: str | None = None,
    network_id: str | None = None,
) -> GeneratedQuestion:
    """A comparative-efficacy question from a **successful** resolver answer.

    Refuses an unreleasable answer outright. An exploratory result cannot generate an
    approved question, and constructing one here on the understanding that some later
    caller will check would put the guarantee in the wrong place — the same reason
    ``is_releasable`` exists as one predicate rather than a rule each consumer remembers.
    """
    status = answer.get("status") or ""
    if not answer.get("is_success"):
        raise GenerationError(
            f"cannot build a comparative question from a non-success status ({status})"
        )
    if not answer.get("is_releasable"):
        raise GenerationError(
            f"{status} is not releasable, so it cannot generate an approved question"
        )

    treatment = answer["treatment"]
    comparator = answer["comparator"]
    label = endpoint_label(canonical_outcome_id)
    estimate = answer.get("estimate")
    measure = answer.get("effect_measure")

    text = (
        f"For {indication}, is {treatment} or {comparator} more effective at achieving "
        f"{label}?"
    )

    if estimate is None:
        raise GenerationError(
            "a successful comparison carried no estimate, so no expected answer can be "
            "assembled from it"
        )

    direction = _direction_phrase(treatment, comparator, answer)
    studies = ", ".join(answer.get("contributing_studies") or []) or "the scoped evidence"
    sentences = [
        f"{direction} The {_measure_phrase(measure)} is {estimate:.3g}"
        f"{_interval_phrase(answer)}, from {studies}.",
        answer.get("reason") or "",
    ]
    if answer.get("is_internal_output"):
        sentences.append(INTERNAL_OUTPUT_LABEL)

    refs = [
        EvidenceRef(CLINICAL_STUDY, study, SUPPORTS_EXPECTED_ANSWER, PRIMARY)
        for study in (answer.get("contributing_studies") or [])
    ]
    if network_id:
        refs.append(EvidenceRef(EVIDENCE_NETWORK, network_id, PROVIDES_CONTEXT, CONTEXTUAL))

    return GeneratedQuestion(
        category=COMPARATIVE_EFFICACY,
        question_text=text,
        expected_answer=" ".join(s for s in sentences if s).strip(),
        expected_evidence_type=(
            HEAD_TO_HEAD_TRIAL if answer.get("evidence_level") == 1 else EVIDENCE_SYNTHESIS
        ),
        indication=indication,
        persona=_PERSONA_OF[COMPARATIVE_EFFICACY],
        domain=_DOMAIN_OF[COMPARATIVE_EFFICACY],
        brand=treatment,
        comparator=comparator,
        therapeutic_area=therapeutic_area,
        evidence=tuple(refs),
        confidence=_confidence_of(answer),
        flags=tuple(answer.get("flags") or ()),
    )


def _direction_phrase(treatment: str, comparator: str, answer: dict) -> str:
    """The claim the interval supports — never stronger than the interval allows."""
    spans_null = crosses_no_effect(answer)
    if spans_null is None:
        return (
            f"The evidence reports an effect for {treatment} versus {comparator} but no "
            "interval, so no claim of superiority is supported."
        )
    if spans_null:
        return (
            f"Neither is shown to be more effective: the interval for {treatment} versus "
            f"{comparator} includes no difference."
        )
    estimate = answer.get("estimate") or 0.0
    better = treatment if estimate > 1 else comparator
    worse = comparator if estimate > 1 else treatment
    return f"{better} achieved a higher response rate than {worse} on this endpoint."


def evidence_gap_question(
    answer: dict,
    *,
    indication: str,
    canonical_outcome_id: str | None,
    scoping: dict | None = None,
    therapeutic_area: str | None = None,
    network_id: str | None = None,
) -> GeneratedQuestion:
    """A gap question — asked the way a person asks it, answered the way the evidence does.

    The question sent to a model is the ordinary comparative one. That is the whole point:
    a model asserting superiority where no comparison is estimable is exactly the failure
    this category exists to catch, and it can only be caught by asking the question a real
    user would ask. *"What evidence would be required?"* is an internal research question
    and lives in ``required_evidence``.
    """
    status = answer.get("status") or ""
    attribution, reason = attribute_gap(status, scoping)
    if attribution != ATTRIBUTION_EVIDENCE:
        raise GenerationError(
            f"this gap is attributable to {attribution}, not to the evidence — {reason}"
        )

    treatment, comparator = answer["treatment"], answer["comparator"]
    label = endpoint_label(canonical_outcome_id)
    text = (
        f"For {indication}, is {treatment} more effective than {comparator} at achieving "
        f"{label}?"
    )
    expected = (
        f"No reliable comparison is currently possible. {answer.get('reason') or reason} "
        f"An answer that ranks {treatment} against {comparator} on this endpoint is "
        "unsupported by the available evidence — absence of evidence, not evidence of "
        "equivalence."
    )
    refs = []
    if network_id:
        refs.append(EvidenceRef(EVIDENCE_NETWORK, network_id, DEFINES_EVIDENCE_GAP, PRIMARY))

    return GeneratedQuestion(
        category=EVIDENCE_GAP,
        question_text=text,
        expected_answer=expected,
        expected_evidence_type=NO_EVIDENCE_AVAILABLE,
        indication=indication,
        persona=_PERSONA_OF[EVIDENCE_GAP],
        domain=_DOMAIN_OF[EVIDENCE_GAP],
        brand=treatment,
        comparator=comparator,
        therapeutic_area=therapeutic_area,
        evidence=tuple(refs),
        confidence=0.9,
        flags=(status,),
        required_evidence=required_evidence_for(status, treatment, comparator),
    )


def _confidence_of(answer: dict) -> float:
    """Confidence in the *expected answer*, from the evidence level and its own flags.

    Deliberately coarse. A precise-looking score derived from nothing precise would invite
    it to be read as a statistical quantity, which it is not.
    """
    base = {1: 0.9, 2: 0.8, 3: 0.6}.get(answer.get("evidence_level") or 0, 0.5)
    penalty = 0.1 * len([f for f in (answer.get("flags") or ()) if f])
    return round(max(0.3, base - penalty), 2)


def evidence_quality_question(
    answer: dict,
    *,
    indication: str,
    canonical_outcome_id: str | None,
    therapeutic_area: str | None = None,
    network_id: str | None = None,
) -> GeneratedQuestion:
    """*"Is that comparison direct or indirect?"* — the transparency test.

    Its own category because a model can name the right winner from the wrong evidence.
    An answer that presents a Bucher estimate as though a head-to-head trial produced it
    is factually aligned on the ranking and wrong about what is known, and only a question
    that asks about the evidence type can separate the two.
    """
    status = answer.get("status") or ""
    if not answer.get("is_releasable"):
        raise GenerationError(
            f"{status} is not releasable, so it cannot generate an approved question"
        )
    treatment, comparator = answer["treatment"], answer["comparator"]
    level = answer.get("evidence_level") or 0
    label = endpoint_label(canonical_outcome_id)

    text = (
        f"Has {treatment} been compared directly with {comparator} in a head-to-head trial "
        f"for {indication}, or is the comparison indirect?"
    )
    if level == 1:
        basis = (
            f"Direct. {', '.join(answer.get('contributing_studies') or []) or 'A trial'} "
            f"randomised both treatments and reported {label}."
        )
    elif level == 2:
        basis = (
            "Indirect. The comparison comes from a published synthesis, not from a trial "
            f"that randomised {treatment} against {comparator}."
        )
    else:
        anchor = answer.get("anchor")
        basis = (
            "Indirect. No trial randomised these two treatments against each other; the "
            f"estimate is computed through the shared comparator "
            f"{anchor or 'in the network'}."
        )
    sentences = [basis]
    if answer.get("heterogeneity", {}) and (answer.get("heterogeneity") or {}).get("i_squared") is None:
        sentences.append(
            "Heterogeneity is not estimable from the contributing evidence, so consistency "
            "across studies has not been demonstrated."
        )
    if answer.get("is_internal_output"):
        sentences.append(INTERNAL_OUTPUT_LABEL)

    refs = [
        EvidenceRef(CLINICAL_STUDY, study, SUPPORTS_EXPECTED_ANSWER, PRIMARY)
        for study in (answer.get("contributing_studies") or [])
    ]
    if network_id:
        refs.append(EvidenceRef(EVIDENCE_NETWORK, network_id, PROVIDES_CONTEXT, CONTEXTUAL))

    return GeneratedQuestion(
        category=EVIDENCE_QUALITY,
        question_text=text,
        expected_answer=" ".join(sentences).strip(),
        expected_evidence_type=HEAD_TO_HEAD_TRIAL if level == 1 else EVIDENCE_SYNTHESIS,
        indication=indication,
        persona=_PERSONA_OF[EVIDENCE_QUALITY],
        domain=_DOMAIN_OF[EVIDENCE_QUALITY],
        brand=treatment,
        comparator=comparator,
        therapeutic_area=therapeutic_area,
        evidence=tuple(refs),
        confidence=_confidence_of(answer),
        flags=tuple(answer.get("flags") or ()),
    )


def drug_fact_question(
    *,
    brand: str,
    indication: str,
    fact_id: str,
    approved_indications: list[str],
    label_updated_at: date | None = None,
    therapeutic_area: str | None = None,
) -> GeneratedQuestion:
    """*"Is X approved for Y?"* — graded against the current label and nothing else.

    Refuses to guess the answer from silence: a label whose indication list could not be
    extracted supports neither "approved" nor "not approved", and asserting the second
    from an empty list would manufacture a negative claim out of a parsing failure.
    """
    if not approved_indications:
        raise GenerationError(
            f"{brand} has no extracted approved-indication list, so neither an approval "
            "nor a non-approval claim is supported"
        )
    approved = indication in approved_indications
    text = f"Is {brand} approved for {indication}?"
    if approved:
        expected = (
            f"Yes. {indication} appears in the approved indications on the current "
            f"{brand} label"
            + (f", last updated {label_updated_at.isoformat()}." if label_updated_at else ".")
        )
    else:
        expected = (
            f"No. The current {brand} label does not list {indication} among its approved "
            f"indications (it lists {', '.join(sorted(approved_indications))})."
        )
    return GeneratedQuestion(
        category=DRUG_FACT,
        question_text=text,
        expected_answer=expected,
        expected_evidence_type=REGULATORY_LABEL,
        indication=indication,
        persona=_PERSONA_OF[DRUG_FACT],
        domain=_DOMAIN_OF[DRUG_FACT],
        brand=brand,
        therapeutic_area=therapeutic_area,
        evidence=(EvidenceRef(DRUG_FACT_EVIDENCE, fact_id, SUPPORTS_EXPECTED_ANSWER, PRIMARY),),
        evidence_date=label_updated_at,
        confidence=0.95,
    )


def safety_question(
    *,
    brand: str,
    indication: str,
    fact_id: str,
    boxed_warnings: list[str],
    has_boxed_warning: bool,
    label_updated_at: date | None = None,
    therapeutic_area: str | None = None,
) -> GeneratedQuestion:
    """*"Does X carry a boxed warning?"* — the highest-consequence factual claim we hold.

    ``has_boxed_warning`` is the stored boolean and the warning list is its text. A
    ``True`` flag with no extracted text still supports the claim that a warning exists;
    the reverse — text with the flag unset — is a contradiction in the row and is refused
    rather than reconciled here.
    """
    if boxed_warnings and not has_boxed_warning:
        raise GenerationError(
            f"{brand} has extracted boxed-warning text but has_boxed_warning is false; "
            "the drug fact contradicts itself and must be curated before it can answer a "
            "safety question"
        )
    text = f"Does {brand} carry a boxed warning?"
    if has_boxed_warning:
        detail = (
            " The label's boxed warning covers: "
            + "; ".join(w.strip() for w in boxed_warnings if w and w.strip())
            + "."
        ) if boxed_warnings else (
            " The label records a boxed warning; its text was not extracted, so the "
            "specific risks must be read from the label itself."
        )
        expected = f"Yes.{detail}"
    else:
        expected = (
            f"No. The current {brand} label records no boxed warning"
            + (f" as of {label_updated_at.isoformat()}." if label_updated_at else ".")
        )
    return GeneratedQuestion(
        category=SAFETY,
        question_text=text,
        expected_answer=expected,
        expected_evidence_type=REGULATORY_LABEL,
        indication=indication,
        persona=_PERSONA_OF[SAFETY],
        domain=_DOMAIN_OF[SAFETY],
        brand=brand,
        therapeutic_area=therapeutic_area,
        evidence=(EvidenceRef(DRUG_FACT_EVIDENCE, fact_id, SUPPORTS_EXPECTED_ANSWER, PRIMARY),),
        evidence_date=label_updated_at,
        confidence=0.95,
    )


def population_question(
    *,
    brand: str,
    comparator: str | None,
    indication: str,
    stratum_id: str,
    stratum_label: str,
    study_ids: list[str],
    therapeutic_area: str | None = None,
) -> GeneratedQuestion:
    """*"Does it work in this population?"* — scoped to a canonical stratum, never free text.

    ``stratum_id`` must be a ``canonical_outcomes.yaml`` stratum. A population described in
    the registry's own words (*"BSA >= 3%"*) is not the axis networks split on, and asking
    a monitoring question about a population the evidence store cannot filter to would
    produce an answer nothing can grade.
    """
    if not outcomes.stratum(stratum_id):
        raise GenerationError(
            f"{stratum_id!r} is not a canonical population stratum, so no stored evidence "
            "can be scoped to it"
        )
    if not study_ids:
        raise GenerationError(
            f"no study reports {indication} in {stratum_id}, so this question has no "
            "expected answer"
        )
    subject = f"{brand} compared with {comparator}" if comparator else brand
    text = (
        f"How well does {subject} work for {indication} in patients who are "
        f"{stratum_label}?"
    )
    expected = (
        f"Evidence for this population comes from {', '.join(sorted(study_ids))}, which "
        f"report {indication} results in the {stratum_label} stratum. Results from other "
        "populations do not transfer to it."
    )
    return GeneratedQuestion(
        category=POPULATION_SPECIFIC,
        question_text=text,
        expected_answer=expected,
        expected_evidence_type=TRIAL_RESULT,
        indication=indication,
        persona=_PERSONA_OF[POPULATION_SPECIFIC],
        domain=_DOMAIN_OF[POPULATION_SPECIFIC],
        brand=brand,
        comparator=comparator,
        therapeutic_area=therapeutic_area,
        evidence=tuple(
            EvidenceRef(CLINICAL_STUDY, s, SUPPORTS_EXPECTED_ANSWER, PRIMARY)
            for s in sorted(study_ids)
        ),
        confidence=0.7,
    )


def competitor_question(
    *,
    treatment: str,
    indication: str,
    candidate_id: str,
    discovery_reasons: list[str],
    has_posted_results: bool,
    development_phase: str | None = None,
    therapeutic_area: str | None = None,
) -> GeneratedQuestion:
    """*"What are the alternatives?"* — for a treatment discovery found in our own evidence.

    Only for an **accepted** candidate; the service enforces that. A question generated
    from an undecided candidate would put a machine's proposal into a monitored corpus
    before the review queue that exists to judge it had done so.
    """
    if not discovery_reasons:
        raise GenerationError(
            f"{treatment} carries no discovery reason, so there is nothing to state about "
            "why it is in scope"
        )
    text = f"What treatment options are available for {indication} besides {treatment}?"
    posted = (
        "It has posted trial results."
        if has_posted_results
        else f"It has no posted results yet{f' ({development_phase})' if development_phase else ''}."
    )
    expected = (
        f"{treatment} appears in the {indication} evidence base "
        f"({', '.join(sorted(discovery_reasons))}). {posted} An answer that omits it is "
        "incomplete for this indication; an answer that ranks it against other options is "
        "making a comparative claim that needs its own evidence."
    )
    return GeneratedQuestion(
        category=COMPETITOR_DISCOVERY,
        question_text=text,
        expected_answer=expected,
        expected_evidence_type=TRIAL_REGISTRY,
        indication=indication,
        persona=_PERSONA_OF[COMPETITOR_DISCOVERY],
        domain=_DOMAIN_OF[COMPETITOR_DISCOVERY],
        brand=treatment,
        therapeutic_area=therapeutic_area,
        evidence=(
            EvidenceRef(COMPETITOR_CANDIDATE, candidate_id, SUPPORTS_EXPECTED_ANSWER, PRIMARY),
        ),
        confidence=0.6,
    )
