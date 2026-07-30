"""Bucher adjusted indirect comparison (Phase 6, Level 3).

When A and B have never been randomised against each other but both have been compared
against a common comparator C, the difference of the two pooled contrasts estimates A vs B:

    d_AB = d_AC - d_BC
    var(d_AB) = var(d_AC) + var(d_BC)

Both lines are exact, and both are only valid **on the analysis scale** — which is why
``pairwise`` pools ratios as logs. Subtracting risk ratios directly instead of log risk
ratios produces a number that looks plausible and means nothing.

The variances add because the two legs come from *different* trials and are therefore
independent. That independence is also the method's fatal weakness: it is what makes the
arithmetic easy and what makes the **transitivity assumption** load-bearing. If the trials
of A-vs-C enrolled a different population from the trials of B-vs-C, the subtraction
silently attributes that difference to the drugs.

So this module refuses more often than it computes:

* **A shared comparator is required.** No anchor, no estimate — ``NETWORK_DISCONNECTED``.
* **Both legs must use the same effect measure.** Subtracting a log odds ratio from a log
  risk ratio is meaningless arithmetic that Python would perform happily.
* **Anchoring through a treatment that is not really one node** is the failure mode nobody
  catches: oral and injectable placebo are both called "Placebo" and are not
  interchangeable. Route mixing is surfaced here as a policy decision, not resolved.
* **Multiple anchors are not silently averaged.** Two anchors give two indirect estimates,
  and if they disagree that disagreement *is* the finding. Averaging them would hide it,
  and a network with two anchors has a closed loop, which is ``netmeta``'s job.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.evidence.engines.pairwise import (
    Z_95,
    PooledEffect,
    from_analysis_scale,
    is_ratio_measure,
)

ENGINE = "BUCHER"
ENGINE_VERSION = "1.0.0"

FLAG_ROUTE_MIXED_ANCHOR = "ANCHOR_MIXES_ADMINISTRATION_ROUTES"
FLAG_ANCHOR_DISAGREEMENT = "ANCHORS_DISAGREE"
FLAG_SINGLE_STUDY_LEG = "LEG_RESTS_ON_A_SINGLE_STUDY"
FLAG_HETEROGENEOUS_LEG = "LEG_HETEROGENEITY_ABOVE_THRESHOLD"


@dataclass(frozen=True)
class IndirectEstimate:
    """One indirect comparison of ``treatment`` vs ``comparator`` through ``anchor``."""

    treatment: str
    comparator: str
    anchor: str
    measure: str
    estimate: float
    standard_error: float
    treatment_leg: PooledEffect
    comparator_leg: PooledEffect
    flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ci_lower(self) -> float:
        return self.estimate - Z_95 * self.standard_error

    @property
    def ci_upper(self) -> float:
        return self.estimate + Z_95 * self.standard_error

    @property
    def estimate_reported(self) -> float:
        return from_analysis_scale(self.estimate, self.measure)

    @property
    def ci_lower_reported(self) -> float:
        return from_analysis_scale(self.ci_lower, self.measure)

    @property
    def ci_upper_reported(self) -> float:
        return from_analysis_scale(self.ci_upper, self.measure)

    @property
    def study_count(self) -> int:
        """Studies behind BOTH legs, deduplicated.

        A study contributing to both legs is a multi-arm trial and is counted once — the
        alternative double-counts patients, which is the specific error that makes a naive
        pairwise flattening of a three-arm trial understate its standard error.
        """
        return len(
            set(self.treatment_leg.contributing_studies)
            | set(self.comparator_leg.contributing_studies)
        )

    def as_dict(self) -> dict:
        return {
            "treatment": self.treatment,
            "comparator": self.comparator,
            "anchor": self.anchor,
            "effect_measure": self.measure,
            "estimate": self.estimate_reported,
            "ci_lower": self.ci_lower_reported,
            "ci_upper": self.ci_upper_reported,
            "interval_type": "CI",
            "estimate_log_scale": self.estimate if is_ratio_measure(self.measure) else None,
            "standard_error": self.standard_error,
            "study_count": self.study_count,
            "legs": {
                f"{self.treatment}_vs_{self.anchor}": self.treatment_leg.as_dict(),
                f"{self.comparator}_vs_{self.anchor}": self.comparator_leg.as_dict(),
            },
            "flags": list(self.flags),
        }


@dataclass(frozen=True)
class BucherResult:
    """Every indirect estimate available for one pair, plus why any were refused.

    ``estimates`` is a tuple because a pair can have several anchors. The **caller** — under
    the protocol — decides which to report; this engine will not pick one, because choosing
    the anchor that gives the preferred answer is exactly the abuse that makes indirect
    comparisons distrusted.
    """

    treatment: str
    comparator: str
    estimates: tuple[IndirectEstimate, ...] = field(default_factory=tuple)
    refusals: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def estimable(self) -> bool:
        return bool(self.estimates)

    @property
    def anchors(self) -> tuple[str, ...]:
        return tuple(e.anchor for e in self.estimates)

    @property
    def anchors_disagree(self) -> bool:
        """True when two anchors' confidence intervals do not overlap.

        Non-overlap is a strong signal that transitivity fails somewhere in the network.
        It is reported, never averaged away.
        """
        if len(self.estimates) < 2:
            return False
        lowers = [e.ci_lower for e in self.estimates]
        uppers = [e.ci_upper for e in self.estimates]
        return max(lowers) > min(uppers)

    def as_dict(self) -> dict:
        return {
            "treatment": self.treatment,
            "comparator": self.comparator,
            "estimable": self.estimable,
            "anchors": list(self.anchors),
            "anchors_disagree": self.anchors_disagree,
            "estimates": [e.as_dict() for e in self.estimates],
            "refusals": [{"anchor": a, "reason": r} for a, r in self.refusals],
        }


def combine(
    treatment_leg: PooledEffect,
    comparator_leg: PooledEffect,
    *,
    routes: dict[str, str] | None = None,
) -> IndirectEstimate:
    """Subtract two pooled legs sharing an anchor. Both must be *vs* the same anchor.

    Raises ``ValueError`` on a measure or anchor mismatch rather than returning a flagged
    result, because there is no meaningful number to attach a flag to.
    """
    if treatment_leg.comparator != comparator_leg.comparator:
        raise ValueError(
            f"legs do not share an anchor: {treatment_leg.comparator!r} vs "
            f"{comparator_leg.comparator!r}"
        )
    if treatment_leg.measure != comparator_leg.measure:
        raise ValueError(
            f"cannot subtract a {comparator_leg.measure} from a {treatment_leg.measure} — "
            "both legs must be on the same effect measure"
        )

    anchor = treatment_leg.comparator
    effect = treatment_leg.estimate - comparator_leg.estimate
    variance = treatment_leg.variance + comparator_leg.variance

    flags: list[str] = []
    # The anchor is one node in the graph but may be two different things clinically.
    # Oral and injectable placebo have measurably different response rates, so anchoring
    # across them breaks transitivity. Disclosed here; the protocol's route_mixing_policy
    # decides what to do about it.
    if routes:
        leg_routes = {
            routes.get(t)
            for t in (treatment_leg.treatment, comparator_leg.treatment)
            if routes.get(t)
        }
        if len(leg_routes) > 1:
            flags.append(FLAG_ROUTE_MIXED_ANCHOR)

    for leg in (treatment_leg, comparator_leg):
        if leg.study_count == 1:
            flags.append(FLAG_SINGLE_STUDY_LEG)
        if leg.i_squared is not None and leg.i_squared > 50.0:
            flags.append(FLAG_HETEROGENEOUS_LEG)

    return IndirectEstimate(
        treatment=treatment_leg.treatment,
        comparator=comparator_leg.treatment,
        anchor=anchor,
        measure=treatment_leg.measure,
        estimate=effect,
        standard_error=math.sqrt(variance),
        treatment_leg=treatment_leg,
        comparator_leg=comparator_leg,
        flags=tuple(dict.fromkeys(flags)),
    )


def compare(
    contrasts: list,
    *,
    treatment: str,
    comparator: str,
    anchors: list[str],
    model: str = "fixed",
    heterogeneity_rule: str | None = None,
    routes: dict[str, str] | None = None,
) -> BucherResult:
    """Indirect estimates of *treatment* vs *comparator* through each viable anchor.

    Every anchor is attempted and every failure recorded, so *"Placebo anchors it but
    Humira does not, because only one arm reports the endpoint"* is a reportable finding
    rather than a silently shorter list.
    """
    from app.evidence.engines.pairwise import pool

    estimates: list[IndirectEstimate] = []
    refusals: list[tuple[str, str]] = []

    for anchor in dict.fromkeys(anchors):
        if anchor in (treatment, comparator):
            refusals.append((anchor, "anchor is one of the two treatments being compared"))
            continue

        leg_a = pool(
            contrasts, treatment=treatment, comparator=anchor,
            model=model, heterogeneity_rule=heterogeneity_rule,
        )
        leg_b = pool(
            contrasts, treatment=comparator, comparator=anchor,
            model=model, heterogeneity_rule=heterogeneity_rule,
        )
        if leg_a is None:
            refusals.append((anchor, f"no usable evidence for {treatment} versus {anchor}"))
            continue
        if leg_b is None:
            refusals.append((anchor, f"no usable evidence for {comparator} versus {anchor}"))
            continue
        if leg_a.measure != leg_b.measure:
            refusals.append((
                anchor,
                f"legs report different effect measures ({leg_a.measure}, {leg_b.measure})",
            ))
            continue

        estimates.append(combine(leg_a, leg_b, routes=routes))

    result = BucherResult(
        treatment=treatment,
        comparator=comparator,
        estimates=tuple(estimates),
        refusals=tuple(refusals),
    )
    if result.anchors_disagree:
        # Recorded on every estimate, because any one of them may be quoted alone and the
        # disagreement is a property of the set, not of the estimate a reader happens to see.
        result = BucherResult(
            treatment=treatment,
            comparator=comparator,
            estimates=tuple(
                IndirectEstimate(
                    **{
                        **e.__dict__,
                        "flags": tuple(dict.fromkeys((*e.flags, FLAG_ANCHOR_DISAGREEMENT))),
                    }
                )
                for e in estimates
            ),
            refusals=tuple(refusals),
        )
    return result
