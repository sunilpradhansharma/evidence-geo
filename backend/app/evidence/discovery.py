"""Tier A competitor discovery (Phase 5) — pure, no DB import.

Tier A ships **only what is mechanically derivable** from evidence already ingested. Every
reason below is a fact about rows in the store, not an opinion about the market:

===============================  ==================================================
Reason                           Derived from
===============================  ==================================================
``DIRECTLY_COMPARED_TREATMENT``  randomised alongside a treatment we monitor
``PUBLISHED_NMA_TREATMENT``      a node of an ingested published synthesis
``APPROVED_INDICATION_...``      its own label names this indication
``SHARED_COMPARATOR_TREATMENT``  network topology — a comparator we also use
``PIPELINE_INDICATION_...``      registry development phase, no posted results
``NEWLY_ACTIVE_TRIAL_TREATMENT`` registry start/posting date inside the window
===============================  ==================================================

Tier B2 — *inferred* class relationships over an open drug set — stays out of scope, so this
module never assigns a ``drug_class`` it was not handed by curation. Labelling something
"same-class" by guesswork is worse than no label in a system with a medical review gate.

Kept pure for the same reason ``approvals.py`` and ``resolver.py`` are: the rule about what
counts as a competitor is worth testing without a session, and worth reading without one.

**Placebo is not a competitor.** Neither is ``Total``, ``Standard Care``, ``Arm B`` or
``bDMARD``. ``is_discoverable`` is the guard, and it delegates every one of those judgements
to ``evidence.treatments`` rather than restating them — a second opinion about what an arm
label means is how a fabricated node reaches a curated config file.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from app.evidence import treatments as treatment_labels

# --- reasons ----------------------------------------------------------------------------
DIRECTLY_COMPARED_TREATMENT = "DIRECTLY_COMPARED_TREATMENT"
PUBLISHED_NMA_TREATMENT = "PUBLISHED_NMA_TREATMENT"
APPROVED_INDICATION_COMPETITOR = "APPROVED_INDICATION_COMPETITOR"
SHARED_COMPARATOR_TREATMENT = "SHARED_COMPARATOR_TREATMENT"
PIPELINE_INDICATION_COMPETITOR = "PIPELINE_INDICATION_COMPETITOR"
NEWLY_ACTIVE_TRIAL_TREATMENT = "NEWLY_ACTIVE_TRIAL_TREATMENT"

DISCOVERY_REASONS = (
    DIRECTLY_COMPARED_TREATMENT,
    PUBLISHED_NMA_TREATMENT,
    APPROVED_INDICATION_COMPETITOR,
    SHARED_COMPARATOR_TREATMENT,
    PIPELINE_INDICATION_COMPETITOR,
    NEWLY_ACTIVE_TRIAL_TREATMENT,
)

# Deterministic weights, ordered by how directly the signal answers "does this molecule
# compete with ours here?". A head-to-head randomisation is the strongest answer available
# short of a label; a recently started trial is the weakest.
#
# Plain arithmetic on purpose. An LLM-assigned score could not be reproduced, and this
# number's only job is to sort a review queue — it must be explicable in one sentence to the
# person working through it.
REASON_WEIGHTS: dict[str, float] = {
    DIRECTLY_COMPARED_TREATMENT: 0.35,
    PUBLISHED_NMA_TREATMENT: 0.25,
    APPROVED_INDICATION_COMPETITOR: 0.20,
    SHARED_COMPARATOR_TREATMENT: 0.10,
    PIPELINE_INDICATION_COMPETITOR: 0.05,
    NEWLY_ACTIVE_TRIAL_TREATMENT: 0.05,
}

# Human-readable, so a queue does not make a reviewer decode an enum. One place, so the API
# and any UI agree.
REASON_LABELS: dict[str, str] = {
    DIRECTLY_COMPARED_TREATMENT: "Randomised head-to-head against a treatment we monitor",
    PUBLISHED_NMA_TREATMENT: "Appears in a published network meta-analysis we hold",
    APPROVED_INDICATION_COMPETITOR: "Its regulatory label names this indication",
    SHARED_COMPARATOR_TREATMENT: "Tested against a comparator our own trials also use",
    PIPELINE_INDICATION_COMPETITOR: "In development for this indication, results not posted",
    NEWLY_ACTIVE_TRIAL_TREATMENT: "Newly active trial evidence in this indication",
}

# A trial that started or posted results within this many days counts as newly active. Two
# years rather than one because registry posting lags the science by months, and a competitor
# whose pivotal trial started 18 months ago is emphatically current.
NEWLY_ACTIVE_DAYS = 730


@dataclass(frozen=True)
class TreatmentObservation:
    """Everything the evidence store has seen about one treatment in one indication.

    Assembled by the service from rows; the rules below never query anything. ``comparators``
    includes placebo deliberately — placebo is the anchor that makes a shared-comparator
    signal meaningful, even though it is never itself a candidate.
    """

    treatment: str
    indication: str
    study_ids: tuple[str, ...] = ()
    co_arm_treatments: tuple[str, ...] = ()
    comparators: tuple[str, ...] = ()
    published_nma_count: int = 0
    latest_evidence_date: date | None = None
    development_phase: str | None = None
    has_posted_results: bool = False
    started_recently: bool = False
    label_names_indication: bool = False
    # Curated annotation, handed in. This module does not look it up and never invents it.
    generic: str | None = None
    sponsor: str | None = None
    drug_class: str | None = None
    administration_route: str | None = None
    is_curated_drug: bool = False


@dataclass(frozen=True)
class Candidate:
    """A discovery proposal. Carries its reasons, not just its score."""

    treatment: str
    indication: str
    reasons: tuple[str, ...]
    discovery_confidence: float
    evidence_count: int
    direct_comparison_count: int
    compared_with: tuple[str, ...]
    shared_comparators: tuple[str, ...]
    published_nma_count: int
    development_phase: str | None
    has_posted_results: bool
    latest_evidence_date: date | None
    source_study_ids: tuple[str, ...]
    generic: str | None = None
    sponsor: str | None = None
    drug_class: str | None = None
    administration_route: str | None = None
    is_curated_drug: bool = False

    @property
    def reason_labels(self) -> tuple[str, ...]:
        return tuple(REASON_LABELS[r] for r in self.reasons if r in REASON_LABELS)


def is_discoverable(treatment: str | None) -> bool:
    """True when this node name could name a competing molecule.

    Every rejection delegates to ``evidence.treatments``. Placebo is the one that matters
    most: it is the most common node in every network built here, so a discovery pass without
    this guard would propose "Placebo" as the leading competitor in all eight indications.
    """
    text = (treatment or "").strip()
    if not text:
        return False
    if treatment_labels.is_placebo(text):
        return False
    # "Total" / "All Participants" — a sum across arms, not an arm.
    if treatment_labels.is_aggregate_label(text):
        return False
    # "Arm B", "2" — real arms whose labels name no molecule. Curation must read the source
    # before any of these can be characterised, so they are not proposals.
    if treatment_labels.is_uninformative_label(text):
        return False
    # "bDMARD", "anti-TNF", "Standard Care" — a class or a strategy. Accepting one as a
    # competitor would assert class equivalence nobody approved.
    if treatment_labels.is_class_level_node(text):
        return False
    return True


def is_strategy_trial(arm_treatments: Iterable[str | None]) -> bool:
    """True when any arm names a drug class or a care strategy, disqualifying the study.

    Study-level because ``is_discoverable`` structurally cannot see this. A withdrawal
    trial randomises *interrupt* against *continue* on whichever agent each patient was
    already taking, so its arms resolve to a list of clean molecule names. Read one at a
    time every one of them looks like a head-to-head; read together they are one strategy,
    and the comparison a candidate would claim never happened. The live case is
    NCT05080218, where eight of nine arms resolved to real drugs and the ninth
    ("Treatment Interruption - TNFi SQ" -> ``TNFi``) is the only one that gives it away.

    Same test the network builder applies before admitting a study to a molecule network,
    for the same reason: a study that cannot contribute a molecule *node* must not
    contribute a molecule *candidate* either.
    """
    return any(treatment_labels.is_class_level_node(t) for t in arm_treatments)


def normalise_names(names: object) -> frozenset[str]:
    """Lowercased comparison set for curated-name matching."""
    if not names:
        return frozenset()
    return frozenset(str(n).strip().lower() for n in names if str(n).strip())


def reasons_for(
    observation: TreatmentObservation,
    *,
    monitored: frozenset[str],
    our_comparators: frozenset[str],
) -> tuple[str, ...]:
    """Which Tier A reasons this observation satisfies, in weight order.

    ``monitored`` is the set of names we already track for the indication — our own brands
    plus the competitors already curated there. Being randomised against any of them is what
    makes a treatment a competitor rather than merely a drug that exists.
    """
    found: list[str] = []
    co_arms = normalise_names(observation.co_arm_treatments)

    if co_arms & monitored:
        found.append(DIRECTLY_COMPARED_TREATMENT)
    if observation.published_nma_count > 0:
        found.append(PUBLISHED_NMA_TREATMENT)
    if observation.label_names_indication:
        found.append(APPROVED_INDICATION_COMPETITOR)
    # Only informative when it is not already directly compared — a head-to-head trial
    # subsumes a shared anchor, and reporting both would double-count one fact.
    if (
        DIRECTLY_COMPARED_TREATMENT not in found
        and normalise_names(observation.comparators) & our_comparators
    ):
        found.append(SHARED_COMPARATOR_TREATMENT)
    if observation.study_ids and not observation.has_posted_results:
        found.append(PIPELINE_INDICATION_COMPETITOR)
    if observation.started_recently:
        found.append(NEWLY_ACTIVE_TRIAL_TREATMENT)

    return tuple(r for r in DISCOVERY_REASONS if r in found)


def confidence_for(reasons: tuple[str, ...]) -> float:
    """Summed reason weights, capped at 1.0.

    Deliberately a function of the *reasons alone* and not of study counts. Ten trials of a
    drug nobody randomised against ours is still weak evidence that it competes, and letting
    volume raise the score would rank a well-studied irrelevance above a single head-to-head.
    """
    return round(min(1.0, sum(REASON_WEIGHTS.get(r, 0.0) for r in reasons)), 3)


def assess(
    observation: TreatmentObservation,
    *,
    monitored: frozenset[str],
    our_comparators: frozenset[str],
) -> Candidate | None:
    """A candidate for this observation, or ``None`` when it is not one.

    ``None`` in three cases, each for a different reason:

    * the label names no molecule (placebo, a total, an enumerator, a class)
    * we already track it for this indication — known, so not a discovery
    * no Tier A reason holds, so there is nothing mechanically derivable to show a reviewer
    """
    if not is_discoverable(observation.treatment):
        return None
    if observation.treatment.strip().lower() in monitored:
        return None

    reasons = reasons_for(
        observation, monitored=monitored, our_comparators=our_comparators
    )
    if not reasons:
        return None

    compared_with = tuple(sorted(
        t for t in set(observation.co_arm_treatments)
        if t.strip().lower() in monitored
    ))
    shared = tuple(sorted(
        c for c in set(observation.comparators)
        if c.strip().lower() in our_comparators
    ))
    return Candidate(
        treatment=observation.treatment,
        indication=observation.indication,
        reasons=reasons,
        discovery_confidence=confidence_for(reasons),
        evidence_count=len(set(observation.study_ids)),
        direct_comparison_count=len(compared_with),
        compared_with=compared_with,
        shared_comparators=shared,
        published_nma_count=observation.published_nma_count,
        development_phase=observation.development_phase,
        has_posted_results=observation.has_posted_results,
        latest_evidence_date=observation.latest_evidence_date,
        source_study_ids=tuple(sorted(set(observation.study_ids))),
        generic=observation.generic,
        sponsor=observation.sponsor,
        drug_class=observation.drug_class,
        administration_route=observation.administration_route,
        is_curated_drug=observation.is_curated_drug,
    )


# --- cross-class presentation (B1, from the curated table only) --------------------------
@dataclass
class ClassGroup:
    """Treatments sharing a curated pharmacological class, for a cross-class view."""

    drug_class: str
    treatments: list[str] = field(default_factory=list)
    routes: dict[str, str] = field(default_factory=dict)


def group_by_class(
    entries: list[tuple[str, str | None, str | None]]
) -> tuple[list[ClassGroup], list[str]]:
    """``(groups, uncharacterised)`` from ``(treatment, drug_class, route)`` triples.

    The second return value is the honest half of the answer: a cross-class map that quietly
    omitted every uncurated molecule would look complete while hiding the actual gap, which
    Phase 0 measured at 74-88% of nodes. ``IL-23 vs JAK vs TNF`` is only a useful view when
    it is shown alongside how much of the network has no class at all.
    """
    groups: dict[str, ClassGroup] = {}
    uncharacterised: list[str] = []
    for treatment, drug_class, route in entries:
        if not drug_class:
            uncharacterised.append(treatment)
            continue
        group = groups.setdefault(drug_class, ClassGroup(drug_class=drug_class))
        if treatment not in group.treatments:
            group.treatments.append(treatment)
        if route:
            group.routes[treatment] = route
    for group in groups.values():
        group.treatments.sort()
    ordered = sorted(groups.values(), key=lambda g: (-len(g.treatments), g.drug_class))
    return ordered, sorted(set(uncharacterised))
