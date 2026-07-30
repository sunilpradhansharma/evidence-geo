"""The evidence hierarchy resolver (Phase 6).

Walks the four levels **in order** and stops at the first that genuinely answers the
question:

    L1  direct head-to-head evidence
    L2  a published synthesis that passed the Level-2 gate
    L3  our own computation — Bucher or netmeta, per the protocol
    L4  a structured evidence gap

The order is not a preference, it is the argument. A head-to-head trial answers the
question by randomisation; an indirect estimate answers it by assumption. Computing an NMA
when a suitable head-to-head trial exists would replace evidence with modelling.

**Every path ends at a named status, including every failure.** There is no ``None`` return
and no exception for "no answer" — see ``evidence.statuses``. A caller that received
``None`` would have to invent an explanation, and an invented explanation in an evidence
system is worse than no answer.

**Falling through is recorded, not forgotten.** ``considered`` accumulates every level that
was tried and rejected, with its reason. That is what lets the answer say *"a Cochrane
review covers both drugs but reports ACR20, and the head-to-head trial used week 12, so
this is an indirect estimate"* — three facts a reviewer needs and none of which survive a
resolver that only reports where it stopped.

**Pure by design.** No database import, no I/O, no clock. The service layer gathers the
evidence and hands it over, exactly as ``approvals.py`` keeps governance logic testable
without a session. The one impure step — the netmeta sidecar — is invoked by the service
and its response passed in.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from app.evidence import statuses, topology as topology_module
from app.evidence.engines import bucher, netmeta
from app.evidence.engines.pairwise import (
    PooledEffect,
    StudyContrast,
    from_analysis_scale,
    pool,
    to_analysis_scale,
)
from app.evidence.sources.published_nma import ParsedSynthesis
from app.evidence.suitability import ComparisonRequest, SuitabilityDecision, best_of

# Evidence levels, as the plan numbers them.
LEVEL_DIRECT = 1
LEVEL_PUBLISHED = 2
LEVEL_COMPUTED = 3
LEVEL_GAP = 4

# Set when Level 1 rested on more than one trial. One head-to-head trial's risk ratio is
# that trial's own result; pooling three of them is a meta-analysis WE performed, and
# labelling the two identically would present our synthesis as raw trial evidence.
FLAG_POOLED_DIRECT = "POOLED_ACROSS_MULTIPLE_STUDIES"

# --- sensitivity analysis (placebo_response_policy: SENSITIVITY_REQUIRED) --------------
# Four outcomes, kept apart because they call for different actions. "The protocol did not
# ask for one" and "it asked and we could not compute it" are not the same fact, and a
# single boolean would let the second hide inside the first — which is how a result ends up
# carrying a policy nobody applied.
SENSITIVITY_NOT_REQUIRED = "NOT_REQUIRED"
SENSITIVITY_NOT_APPLICABLE = "NOT_APPLICABLE"
SENSITIVITY_NOT_ESTIMABLE = "NOT_ESTIMABLE"
SENSITIVITY_COMPLETED = "COMPLETED"

# Flagged on the main answer when the protocol demanded a sensitivity analysis that could
# not be produced. The number stands — the policy governs disclosure, not estimability —
# but the gap in the disclosure travels with it.
FLAG_SENSITIVITY_NOT_ESTIMABLE = "SENSITIVITY_ANALYSIS_NOT_ESTIMABLE"
FLAG_SENSITIVITY_DIVERGES = "SENSITIVITY_ANALYSIS_DIVERGES"


@dataclass(frozen=True)
class EvidenceSet:
    """Everything the resolver may consider for one question.

    ``contrasts`` are **already scoped** by the service to the requested outcome, timepoint
    window, population stratum and treatment phase. Scoping happens in the query rather
    than here so the resolver never has to decide whether a week-12 result may stand in for
    week 16 — that is a protocol decision, and ``unsuitable_direct`` carries what the
    scoping rejected so the refusal stays visible.
    """

    contrasts: tuple[StudyContrast, ...] = field(default_factory=tuple)
    study_arms: Mapping[str, frozenset[str]] = field(default_factory=dict)
    syntheses: tuple[ParsedSynthesis, ...] = field(default_factory=tuple)
    administration_routes: Mapping[str, str] = field(default_factory=dict)
    # (study_id, reason) for direct evidence excluded on scope — endpoint, timepoint,
    # population or phase. Reported rather than dropped.
    unsuitable_direct: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    # (study_id, reason) for arms that could not yield a contrast at all.
    insufficient_data: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    # (study_id, reason) for arms the service WITHHELD because two in-scope rows describe
    # them and disagree. Separate from ``insufficient_data`` because the data is not
    # missing — it is contradictory, and only one of those is a curation task.
    ambiguous_arms: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    # ``{treatment: ((study_id, reason), ...)}`` for a requested treatment that a trial DOES
    # randomise but whose every row the scoping rejected.
    #
    # Keyed by treatment, not by pair, because that is the shape of the claim: it is what
    # makes "does not appear in any scoped trial" false. ``unsuitable_direct`` cannot serve
    # here — the service only fills it for a study holding BOTH requested treatments, so a
    # pair whose drugs were never randomised together loses the reason entirely.
    excluded_nodes: Mapping[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)

    def topology(self) -> topology_module.Topology:
        return topology_module.build({s: sorted(a) for s, a in self.study_arms.items()})


@dataclass(frozen=True)
class ResolutionAttempt:
    """One level that was tried, and what came of it."""

    level: int
    status: str
    succeeded: bool
    reason: str

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "status": self.status,
            "succeeded": self.succeeded,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SensitivityAnalysis:
    """The protocol's required second analysis, or a named account of why there is none.

    ``placebo_response_policy: SENSITIVITY_REQUIRED`` says *"compute the main analysis and
    a sensitivity analysis excluding one route, and report the divergence"*. Until this
    existed the policy was written onto every result and executed nowhere, so a result
    asserted a disclosure that had not happened — the failure mode the whole cross-class
    section of the plan exists to prevent.

    **Divergence is reported, never adjudicated.** There is no tolerance to breach and no
    downgrade: route mixing is a transitivity threat to disclose, not a quantity to correct,
    so the two numbers are put side by side and whether they agree is stated in the same
    vocabulary Bucher already uses for disagreeing anchors — do the intervals overlap?
    """

    policy: str | None
    status: str
    reason: str
    restricted_to_route: str | None = None
    routes_present: tuple[str, ...] = field(default_factory=tuple)
    routes_tried: tuple[str, ...] = field(default_factory=tuple)
    treatments_dropped: tuple[str, ...] = field(default_factory=tuple)
    studies_dropped: tuple[str, ...] = field(default_factory=tuple)

    effect_measure: str | None = None
    estimate: float | None = None
    ci_lower: float | None = None
    ci_upper: float | None = None
    engine: str | None = None
    anchor: str | None = None

    # Main minus restricted on the ANALYSIS scale (log for ratios), and the same difference
    # expressed the way the measure is read. Both, because the first is what the arithmetic
    # is done on and the second is what a reviewer quotes.
    divergence: float | None = None
    divergence_reported: float | None = None
    intervals_overlap: bool | None = None

    @property
    def ran(self) -> bool:
        return self.status == SENSITIVITY_COMPLETED

    @property
    def diverges(self) -> bool:
        """True only when the two analyses' intervals fail to overlap.

        Non-overlap is the same signal ``bucher`` uses for anchor disagreement, chosen so
        this module does not invent a second, differently-shaped notion of "materially
        different" for the reader to reconcile.
        """
        return self.ran and self.intervals_overlap is False

    def as_dict(self) -> dict:
        return {
            "policy": self.policy,
            "status": self.status,
            "ran": self.ran,
            "reason": self.reason,
            "restricted_to_route": self.restricted_to_route,
            "routes_present": list(self.routes_present),
            "routes_tried": list(self.routes_tried),
            "treatments_dropped": list(self.treatments_dropped),
            "studies_dropped": list(self.studies_dropped),
            "effect_measure": self.effect_measure,
            "estimate": self.estimate,
            "ci_lower": self.ci_lower,
            "ci_upper": self.ci_upper,
            "engine": self.engine,
            "anchor": self.anchor,
            "divergence": self.divergence,
            "divergence_reported": self.divergence_reported,
            "intervals_overlap": self.intervals_overlap,
            "diverges": self.diverges,
        }


@dataclass(frozen=True)
class ComparisonAnswer:
    """The resolver's answer, or its named refusal. Never empty, never an exception."""

    status: str
    evidence_level: int
    treatment: str
    comparator: str
    reason: str
    considered: tuple[ResolutionAttempt, ...] = field(default_factory=tuple)

    # Present on success. Which one is populated depends on the level.
    effect_measure: str | None = None
    estimate: float | None = None
    ci_lower: float | None = None
    ci_upper: float | None = None
    interval_type: str | None = None
    model: str | None = None

    engine: str | None = None
    engine_status: str | None = None
    anchor: str | None = None
    contributing_studies: tuple[str, ...] = field(default_factory=tuple)
    citation: str | None = None

    heterogeneity: dict | None = None
    flags: tuple[str, ...] = field(default_factory=tuple)

    # Populated for Level 3 only. Direct evidence needs no route sensitivity analysis:
    # a head-to-head trial randomised both arms against the same placebo in the same
    # protocol, so there is no transitivity chain for a route differential to travel down.
    sensitivity: SensitivityAnalysis | None = None

    @property
    def is_success(self) -> bool:
        return statuses.is_success(self.status)

    @property
    def is_releasable(self) -> bool:
        """Only releasable statuses may flow to downstream consumers."""
        return statuses.is_releasable(self.status)

    @property
    def is_internal_output(self) -> bool:
        """True when this must carry the internal-analytical-output label.

        Level 3 always qualifies. Level 1 qualifies **only when several trials were pooled**:
        a single head-to-head result belongs to the trial that produced it, but a pooled
        estimate across trials is our own meta-analysis and has to say so.
        """
        if self.evidence_level == LEVEL_COMPUTED:
            return True
        return self.evidence_level == LEVEL_DIRECT and FLAG_POOLED_DIRECT in self.flags

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "describes": statuses.describe(self.status),
            "evidence_level": self.evidence_level,
            "treatment": self.treatment,
            "comparator": self.comparator,
            "reason": self.reason,
            "is_success": self.is_success,
            "is_releasable": self.is_releasable,
            "is_internal_output": self.is_internal_output,
            "effect_measure": self.effect_measure,
            "estimate": self.estimate,
            "ci_lower": self.ci_lower,
            "ci_upper": self.ci_upper,
            "interval_type": self.interval_type,
            "model": self.model,
            "engine": self.engine,
            "engine_status": self.engine_status,
            "anchor": self.anchor,
            "contributing_studies": list(self.contributing_studies),
            "citation": self.citation,
            "heterogeneity": self.heterogeneity,
            "flags": list(self.flags),
            "sensitivity": self.sensitivity.as_dict() if self.sensitivity else None,
            "considered": [a.as_dict() for a in self.considered],
        }


def _heterogeneity_of(pooled: PooledEffect) -> dict:
    return {
        "model": pooled.model,
        "tau_squared": pooled.tau_squared,
        "q_statistic": pooled.q_statistic,
        "degrees_freedom": pooled.degrees_freedom,
        "i_squared": pooled.i_squared,
        "study_count": pooled.study_count,
    }


# =====================================================================================
# Level 1 — direct evidence
# =====================================================================================
def resolve_direct(
    evidence: EvidenceSet,
    request: ComparisonRequest,
    *,
    heterogeneity_rule: str | None = None,
) -> tuple[PooledEffect | None, str]:
    """``(pooled direct effect, reason)``. ``None`` when direct evidence cannot answer.

    The reason distinguishes *never randomised together* from *randomised together but not
    at this endpoint or timepoint*. Those are different findings: the second means a trial
    exists that a reviewer should know about.
    """
    a, b = request.nodes
    pooled = pool(
        evidence.contrasts, treatment=a, comparator=b,
        heterogeneity_rule=heterogeneity_rule,
    )
    if pooled is not None:
        return pooled, (
            f"{pooled.study_count} head-to-head "
            f"{'trial' if pooled.study_count == 1 else 'trials'} randomised {a} against {b}."
        )

    # Reported ahead of a scope mismatch: a withheld arm is the one finding here that says a
    # number is in the store and we refused to use it, which is what a curator must clear
    # before the remaining reasons can be trusted.
    if evidence.ambiguous_arms:
        return None, (
            "Direct evidence exists but an arm of it holds contradictory in-scope results, "
            "so no value was taken from it: "
            + "; ".join(sorted({r for _s, r in evidence.ambiguous_arms}))
            + "."
        )

    rejected = [r for _s, r in evidence.unsuitable_direct]
    if rejected:
        return None, (
            "Direct evidence exists but none of it matches this question: "
            + "; ".join(sorted(set(rejected)))
            + "."
        )
    if evidence.insufficient_data:
        return None, (
            "A head-to-head trial exists but its arm-level data cannot support an "
            "estimate: "
            + "; ".join(sorted({r for _s, r in evidence.insufficient_data}))
            + "."
        )
    return None, f"No trial randomised {a} against {b}."


# =====================================================================================
# Level 3 — computation
# =====================================================================================
def _governed_status(may_compute_governed: bool) -> str:
    """The status a completed computation carries.

    ``GOVERNED_SYNTHESIS_COMPLETED`` is the only status asserting *both* an approved
    protocol and a ratified network, so it is what a fully governed run reports. Anything
    short of that is ``EXPLORATORY_RESULT_COMPLETED``, which ``statuses.is_releasable``
    excludes — an exploratory result cannot become ratified evidence, generate approved
    questions, affect scoring or create a recommendation.

    The engine-specific statuses (``BUCHER_ITC_COMPLETED``, ``INTERNAL_NMA_COMPLETED``) are
    carried alongside on ``engine_status`` rather than in ``status``. They name the *method*
    and say nothing about governance, so using one as the headline status would let an
    ungoverned result look releasable.
    """
    return (
        statuses.GOVERNED_SYNTHESIS_COMPLETED if may_compute_governed
        else statuses.EXPLORATORY_RESULT_COMPLETED
    )


def resolve_computed(
    evidence: EvidenceSet,
    request: ComparisonRequest,
    *,
    model_selection_rule: str | None = None,
    heterogeneity_rule: str | None = None,
    may_compute_governed: bool = False,
    netmeta_response: netmeta.NetmetaResponse | None = None,
) -> ComparisonAnswer | None:
    """A Level-3 answer, or ``None`` when the network cannot support one.

    Engine choice is the protocol's ``model_selection_rule`` applied to the facts in
    ``evidence.topology`` — never whichever engine is available or gives a better number.
    """
    a, b = request.nodes
    graph = evidence.topology()

    if not graph.are_connected(a, b):
        return None

    engine = netmeta.select_engine(model_selection_rule, graph)
    status = _governed_status(may_compute_governed)

    if engine == netmeta.ENGINE:
        # The sidecar owns this computation. A missing response is a *service* condition,
        # never an evidence gap — the two must stay distinguishable so an outage cannot
        # masquerade as a finding about the evidence.
        if netmeta_response is None or not netmeta_response.ok:
            reason = (
                netmeta_response.reason if netmeta_response is not None
                else "the NMA sidecar was not consulted"
            )
            return ComparisonAnswer(
                status=statuses.NMA_SERVICE_UNAVAILABLE,
                evidence_level=LEVEL_COMPUTED,
                treatment=a, comparator=b,
                reason=f"Network meta-analysis is required here but {reason}.",
                engine=engine,
            )

        contrast = netmeta_response.contrast_for(a, b)
        if contrast is None:
            return None
        return ComparisonAnswer(
            status=status,
            evidence_level=LEVEL_COMPUTED,
            treatment=a, comparator=b,
            reason=(
                f"Estimated by network meta-analysis across {len(graph.nodes)} treatments; "
                f"the network has {graph.independent_loop_count} independent loop(s)."
            ),
            effect_measure=netmeta_response.effect_measure,
            estimate=contrast.estimate,
            ci_lower=contrast.ci_lower,
            ci_upper=contrast.ci_upper,
            interval_type="CI",
            model=netmeta_response.model,
            engine=engine,
            engine_status=statuses.INTERNAL_NMA_COMPLETED,
            contributing_studies=tuple(sorted(evidence.study_arms)),
            heterogeneity={
                "model": netmeta_response.model,
                "tau_squared": netmeta_response.tau_squared,
                "q_statistic": netmeta_response.q_statistic,
                "degrees_freedom": netmeta_response.degrees_freedom,
                "i_squared": netmeta_response.i_squared,
                "inconsistency": netmeta_response.inconsistency,
            },
            flags=netmeta_response.flags,
        )

    anchors = graph.shared_comparators(a, b)
    if not anchors:
        return None

    result = bucher.compare(
        list(evidence.contrasts), treatment=a, comparator=b, anchors=list(anchors),
        heterogeneity_rule=heterogeneity_rule,
        routes=dict(evidence.administration_routes),
    )
    if not result.estimable:
        return None

    # Several anchors mean several estimates. The FIRST is reported and every anchor is
    # named, because picking the anchor that gives the preferred answer is the specific
    # abuse that makes indirect comparisons distrusted.
    estimate = result.estimates[0]
    flags = list(estimate.flags)
    reason = (
        f"No trial randomised {a} against {b}, so this is an adjusted indirect comparison "
        f"anchored on {estimate.anchor}."
    )
    if len(result.estimates) > 1:
        reason += f" Other anchors available: {', '.join(result.anchors[1:])}."
    if result.anchors_disagree:
        reason += (
            " Those anchors' intervals do not overlap, which is evidence against "
            "transitivity in this network."
        )

    return ComparisonAnswer(
        status=status,
        evidence_level=LEVEL_COMPUTED,
        treatment=a, comparator=b,
        reason=reason,
        effect_measure=estimate.measure,
        estimate=estimate.estimate_reported,
        ci_lower=estimate.ci_lower_reported,
        ci_upper=estimate.ci_upper_reported,
        interval_type="CI",
        model=estimate.treatment_leg.model,
        engine=bucher.ENGINE,
        engine_status=statuses.BUCHER_ITC_COMPLETED,
        anchor=estimate.anchor,
        contributing_studies=tuple(sorted(
            set(estimate.treatment_leg.contributing_studies)
            | set(estimate.comparator_leg.contributing_studies)
        )),
        heterogeneity={
            "treatment_leg": _heterogeneity_of(estimate.treatment_leg),
            "comparator_leg": _heterogeneity_of(estimate.comparator_leg),
        },
        flags=tuple(dict.fromkeys(flags)),
    )


# =====================================================================================
# The protocol's required sensitivity analysis
# =====================================================================================
def restrict_to_route(evidence: EvidenceSet, route: str) -> EvidenceSet:
    """The same evidence with every cross-route study removed. Pure.

    A study is kept when every treatment it contributes that has a **known** route has
    *this* route. Unrouted nodes do not disqualify a study, because "we do not know this
    arm's route" is not the same claim as "this arm's route differs" and excluding on the
    first would quietly shrink the network on missing catalog data rather than on evidence.

    The consequence is deliberate and is the whole point: restricting a Rinvoq-versus-Humira
    network to SC drops every oral study, so Rinvoq leaves the graph. The comparison then
    has no single-route path, which is precisely the transitivity threat the policy exists
    to disclose rather than a defect in this function.
    """
    routes = evidence.administration_routes
    kept_studies = {
        study_id: arms for study_id, arms in evidence.study_arms.items()
        if all(routes.get(t, route) == route for t in arms)
    }
    kept_names = set(kept_studies)
    return EvidenceSet(
        contrasts=tuple(c for c in evidence.contrasts if c.study_id in kept_names),
        study_arms=kept_studies,
        # A published synthesis is somebody else's analysis and cannot be re-restricted by
        # us, so it is not carried into a subnetwork we constructed.
        syntheses=(),
        administration_routes=routes,
        unsuitable_direct=evidence.unsuitable_direct,
        ambiguous_arms=evidence.ambiguous_arms,
        # ``excluded_nodes`` is deliberately NOT carried. In here a node goes missing because
        # this function removed its studies for having the wrong route, and attaching the
        # scoping reason to that absence would explain a route restriction with a timepoint
        # — the same class of false disclosure ``excluded_nodes`` exists to prevent. The
        # subnetwork falls back to the generic message, which is true of it.
    )


def _overlap(a: ComparisonAnswer, b_lower: float | None, b_upper: float | None) -> bool | None:
    """Whether two intervals overlap, or ``None`` when either is missing.

    ``None`` rather than ``True``: an absent interval is not agreement, and defaulting to
    "they overlap" would report a reassuring finding out of missing data.
    """
    if None in (a.ci_lower, a.ci_upper, b_lower, b_upper):
        return None
    return a.ci_lower <= b_upper and b_lower <= a.ci_upper


def _divergence(main: ComparisonAnswer, restricted: ComparisonAnswer) -> tuple[float | None, float | None]:
    """``(analysis-scale difference, reported-scale difference)`` between two estimates.

    Ratios are differenced on the **log** scale for the same reason they are pooled there:
    RR 0.5 and RR 2.0 are equal and opposite, and their arithmetic difference of 1.5 would
    describe a symmetric disagreement as a large one-sided one. The reported figure is the
    ratio of the two ratios, which is how a reviewer reads it.
    """
    measure = main.effect_measure or restricted.effect_measure
    if main.estimate is None or restricted.estimate is None or not measure:
        return None, None
    try:
        main_scale = to_analysis_scale(main.estimate, measure)
        restricted_scale = to_analysis_scale(restricted.estimate, measure)
    except ValueError:
        return None, None
    difference = restricted_scale - main_scale
    return difference, from_analysis_scale(difference, measure)


def resolve_sensitivity(
    evidence: EvidenceSet,
    request: ComparisonRequest,
    main: ComparisonAnswer,
    *,
    placebo_response_policy: str | None,
    model_selection_rule: str | None = None,
    heterogeneity_rule: str | None = None,
    netmeta_response: netmeta.NetmetaResponse | None = None,
) -> SensitivityAnalysis:
    """Run the protocol's route-restricted second analysis, or say precisely why not.

    **Every route in the network is tried**, in a deterministic order, and the first that
    still connects both treatments is the one reported. Choosing a route by name — "restrict
    to subcutaneous" — would be an arbitrary preference dressed as methodology, and on a
    cross-route pair it would always fail while looking like a considered choice.
    """
    a, b = request.nodes
    if placebo_response_policy != "SENSITIVITY_REQUIRED":
        return SensitivityAnalysis(
            policy=placebo_response_policy,
            status=SENSITIVITY_NOT_REQUIRED,
            reason=(
                f"The governing protocol's placebo_response_policy is "
                f"{placebo_response_policy or 'unset'}, which does not require a "
                "sensitivity analysis."
            ),
        )

    nodes = set(evidence.topology().nodes)
    routes_present = tuple(sorted({
        r for t, r in evidence.administration_routes.items() if t in nodes and r
    }))
    if len(routes_present) < 2:
        return SensitivityAnalysis(
            policy=placebo_response_policy,
            status=SENSITIVITY_NOT_APPLICABLE,
            reason=(
                "Every treatment in this network shares one administration route"
                + (f" ({routes_present[0]})" if routes_present else " or has none recorded")
                + ", so the placebo differential the policy guards against cannot arise "
                "here."
            ),
            routes_present=routes_present,
        )

    dropped_treatments: set[str] = set()
    dropped_studies: set[str] = set()
    for route in routes_present:
        subset = restrict_to_route(evidence, route)
        surviving = set(subset.topology().nodes)
        if a not in surviving or b not in surviving:
            dropped_treatments |= {t for t in (a, b) if t not in surviving}
            dropped_studies |= set(evidence.study_arms) - set(subset.study_arms)
            continue

        restricted = resolve_computed(
            subset, request,
            model_selection_rule=model_selection_rule,
            heterogeneity_rule=heterogeneity_rule,
            # The restricted run is an internal disclosure, never a releasable result of
            # its own, so it is not offered the governed status.
            may_compute_governed=False,
            netmeta_response=netmeta_response,
        )
        if restricted is None or restricted.estimate is None:
            dropped_studies |= set(evidence.study_arms) - set(subset.study_arms)
            continue

        difference, reported = _divergence(main, restricted)
        return SensitivityAnalysis(
            policy=placebo_response_policy,
            status=SENSITIVITY_COMPLETED,
            reason=(
                f"Repeated with the network restricted to {route} studies "
                f"({len(subset.study_arms)} of {len(evidence.study_arms)})."
            ),
            restricted_to_route=route,
            routes_present=routes_present,
            routes_tried=routes_present,
            studies_dropped=tuple(sorted(
                set(evidence.study_arms) - set(subset.study_arms)
            )),
            effect_measure=restricted.effect_measure,
            estimate=restricted.estimate,
            ci_lower=restricted.ci_lower,
            ci_upper=restricted.ci_upper,
            engine=restricted.engine,
            anchor=restricted.anchor,
            divergence=difference,
            divergence_reported=reported,
            intervals_overlap=_overlap(main, restricted.ci_lower, restricted.ci_upper),
        )

    lost = ", ".join(sorted(dropped_treatments)) or "one of the treatments"
    return SensitivityAnalysis(
        policy=placebo_response_policy,
        status=SENSITIVITY_NOT_ESTIMABLE,
        reason=(
            f"No single-route subnetwork connects {a} and {b}: restricting to any of "
            f"{', '.join(routes_present)} loses {lost}. The comparison therefore rests "
            "entirely on the cross-route link, which is the transitivity threat this "
            "policy exists to surface — it cannot be tested by excluding a route."
        ),
        routes_present=routes_present,
        routes_tried=routes_present,
        treatments_dropped=tuple(sorted(dropped_treatments)),
        studies_dropped=tuple(sorted(dropped_studies)),
    )


# =====================================================================================
# Level 4 — the gap
# =====================================================================================
def _gap(
    evidence: EvidenceSet,
    request: ComparisonRequest,
    published: SuitabilityDecision | None,
) -> tuple[str, str]:
    """``(status, reason)`` for a question nothing could answer.

    Ordered by how fundamental the obstruction is. A disconnected network is reported ahead
    of a scope mismatch because no amount of endpoint harmonisation would fix it, so it is
    the more actionable finding.
    """
    a, b = request.nodes
    graph = evidence.topology()

    if a not in graph.nodes or b not in graph.nodes:
        missing = [t for t in (a, b) if t not in graph.nodes]
        # A node can be missing for two reasons that read identically off the graph and mean
        # opposite things: nobody studied the drug, or somebody did and this analysis refused
        # the rows. Reporting the first when the second is true sends a reader hunting for
        # trials that already exist, and hides that a protocol decision — not the evidence —
        # is what closed the question.
        excluded = [
            (t, evidence.excluded_nodes.get(t) or ()) for t in missing
        ]
        named = [(t, rows) for t, rows in excluded if rows]
        if named:
            return statuses.NETWORK_DISCONNECTED, (
                "; ".join(
                    f"{t} is randomised by "
                    + ", ".join(sorted({s for s, _r in rows}))
                    + " but contributed no usable row to this analysis ("
                    + "; ".join(sorted({r for _s, r in rows}))
                    + ")"
                    for t, rows in named
                )
                + ". No path of shared comparators can exist without it."
            )
        return statuses.NETWORK_DISCONNECTED, (
            f"{', '.join(missing)} does not appear in any scoped trial, so no path of "
            "shared comparators can exist."
        )
    if not graph.are_connected(a, b):
        return statuses.NETWORK_DISCONNECTED, (
            f"{a} and {b} sit in unconnected parts of the network — no shared comparator "
            "links them."
        )
    if not graph.shared_comparators(a, b):
        return statuses.NETWORK_DISCONNECTED, (
            f"{a} and {b} are connected only through a multi-step path with no common "
            "comparator, which no engine here will estimate across."
        )
    if evidence.ambiguous_arms:
        return statuses.AMBIGUOUS_ARM_DATA, (
            "The network connects these treatments but an arm holds contradictory in-scope "
            "results, so it was withheld rather than resolved by row order: "
            + "; ".join(sorted({r for _s, r in evidence.ambiguous_arms}))
            + "."
        )
    if evidence.insufficient_data:
        return statuses.INSUFFICIENT_ARM_DATA, (
            "The network connects these treatments but the arm-level data needed to "
            "compute an estimate is missing: "
            + "; ".join(sorted({r for _s, r in evidence.insufficient_data}))
            + "."
        )
    if evidence.unsuitable_direct:
        return statuses.DIRECT_EVIDENCE_UNSUITABLE, (
            "Direct evidence exists but does not match this question: "
            + "; ".join(sorted({r for _s, r in evidence.unsuitable_direct}))
            + "."
        )
    if published is not None and not published.suitable:
        return published.status, published.reason_text

    return statuses.INSUFFICIENT_ARM_DATA, (
        f"{a} and {b} are connected but no engine produced an estimate from the scoped "
        "evidence."
    )


# =====================================================================================
# The walk
# =====================================================================================
def resolve(
    evidence: EvidenceSet,
    request: ComparisonRequest,
    *,
    model_selection_rule: str | None = None,
    heterogeneity_rule: str | None = None,
    effect_measure: str | None = None,
    may_compute_governed: bool = False,
    netmeta_response: netmeta.NetmetaResponse | None = None,
    max_published_age_years: int | None = None,
    placebo_response_policy: str | None = None,
) -> ComparisonAnswer:
    """Walk the hierarchy and return the first level that answers the question.

    Always returns a ``ComparisonAnswer``. Levels tried and rejected are recorded in
    ``considered`` so the answer explains why it sits where it does.

    ``placebo_response_policy`` is the protocol's, and when it is ``SENSITIVITY_REQUIRED``
    a Level-3 answer carries the route-restricted second analysis it asks for. Passing it
    is what stops a result recording a policy that was never executed.
    """
    a, b = request.nodes
    considered: list[ResolutionAttempt] = []

    # --- Level 1 ----------------------------------------------------------------------
    direct, direct_reason = resolve_direct(
        evidence, request, heterogeneity_rule=heterogeneity_rule
    )
    if direct is not None:
        considered.append(ResolutionAttempt(
            LEVEL_DIRECT, statuses.DIRECT_EVIDENCE_AVAILABLE, True, direct_reason
        ))
        return ComparisonAnswer(
            status=statuses.DIRECT_EVIDENCE_AVAILABLE,
            evidence_level=LEVEL_DIRECT,
            treatment=a, comparator=b,
            reason=direct_reason,
            effect_measure=direct.measure,
            estimate=direct.estimate_reported,
            ci_lower=direct.ci_lower_reported,
            ci_upper=direct.ci_upper_reported,
            interval_type="CI",
            model=direct.model,
            contributing_studies=direct.contributing_studies,
            heterogeneity=_heterogeneity_of(direct),
            flags=(
                (*direct.flags, FLAG_POOLED_DIRECT) if direct.study_count > 1
                else direct.flags
            ),
            considered=tuple(considered),
        )
    considered.append(ResolutionAttempt(
        LEVEL_DIRECT,
        # Ordered to match `resolve_direct`'s reason. A withheld arm also lands in
        # `unsuitable_direct`, so checking that first would label a contradiction a scope
        # mismatch and the trail would disagree with the sentence beside it.
        statuses.AMBIGUOUS_ARM_DATA if evidence.ambiguous_arms
        else statuses.DIRECT_EVIDENCE_UNSUITABLE if evidence.unsuitable_direct
        else statuses.NETWORK_DISCONNECTED,
        False, direct_reason,
    ))

    # --- Level 2 ----------------------------------------------------------------------
    published_decision: SuitabilityDecision | None = None
    if evidence.syntheses:
        kwargs = (
            {"max_age_years": max_published_age_years}
            if max_published_age_years is not None else {}
        )
        chosen, published_decision = best_of(list(evidence.syntheses), request, **kwargs)
        considered.append(ResolutionAttempt(
            LEVEL_PUBLISHED, published_decision.status,
            published_decision.suitable, published_decision.reason_text,
        ))
        if published_decision.suitable and chosen is not None:
            contrast = published_decision.matched_contrast
            return ComparisonAnswer(
                status=statuses.PUBLISHED_RESULT_AVAILABLE,
                evidence_level=LEVEL_PUBLISHED,
                treatment=a, comparator=b,
                reason=(
                    "A published synthesis answers this question directly, which outranks "
                    "any estimate we would compute ourselves."
                ),
                effect_measure=getattr(contrast, "effect_measure", None),
                estimate=getattr(contrast, "estimate", None),
                ci_lower=getattr(contrast, "interval_lower", None),
                ci_upper=getattr(contrast, "interval_upper", None),
                interval_type=getattr(contrast, "interval_type", None),
                model=chosen.model_type,
                citation=chosen.citation,
                contributing_studies=chosen.included_studies,
                considered=tuple(considered),
            )
    else:
        considered.append(ResolutionAttempt(
            LEVEL_PUBLISHED, statuses.PUBLISHED_SYNTHESIS_UNSUITABLE, False,
            "No published synthesis was found for this question.",
        ))

    # --- Level 3 ----------------------------------------------------------------------
    computed = resolve_computed(
        evidence, request,
        model_selection_rule=model_selection_rule,
        heterogeneity_rule=heterogeneity_rule,
        may_compute_governed=may_compute_governed,
        netmeta_response=netmeta_response,
    )
    if computed is not None:
        considered.append(ResolutionAttempt(
            LEVEL_COMPUTED, computed.status,
            statuses.is_success(computed.status), computed.reason,
        ))
        # Only a completed computation gets one. A service outage has no estimate to
        # compare against, and running a second analysis to disclose a number that does
        # not exist would report a divergence of nothing from nothing.
        if computed.estimate is None:
            return ComparisonAnswer(
                **{**computed.__dict__, "considered": tuple(considered)}
            )
        sensitivity = resolve_sensitivity(
            evidence, request, computed,
            placebo_response_policy=placebo_response_policy,
            model_selection_rule=model_selection_rule,
            heterogeneity_rule=heterogeneity_rule,
            netmeta_response=netmeta_response,
        )
        flags = list(computed.flags)
        if sensitivity.status == SENSITIVITY_NOT_ESTIMABLE:
            flags.append(FLAG_SENSITIVITY_NOT_ESTIMABLE)
        if sensitivity.diverges:
            flags.append(FLAG_SENSITIVITY_DIVERGES)
        return ComparisonAnswer(**{
            **computed.__dict__,
            "flags": tuple(dict.fromkeys(flags)),
            "sensitivity": sensitivity,
            "considered": tuple(considered),
        })

    # --- Level 4 ----------------------------------------------------------------------
    status, reason = _gap(evidence, request, published_decision)
    considered.append(ResolutionAttempt(LEVEL_GAP, status, False, reason))
    return ComparisonAnswer(
        status=status,
        evidence_level=LEVEL_GAP,
        treatment=a, comparator=b,
        reason=reason,
        effect_measure=effect_measure,
        considered=tuple(considered),
    )
