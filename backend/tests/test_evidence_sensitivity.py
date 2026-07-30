"""The protocol's required sensitivity analysis, actually executed.

``placebo_response_policy: SENSITIVITY_REQUIRED`` is selected by three of the four
protocols and says: compute the main analysis **and** a sensitivity analysis excluding one
route, and report the divergence. It was written onto every stored result and executed
nowhere, so a result asserted a disclosure that had not happened.

The case worth reading is the last one. On a Rinvoq-versus-Humira network no single-route
subnetwork contains both drugs, so the sensitivity analysis is **not estimable** — and that
is the finding, not a failure: it says the comparison rests entirely on the cross-route
link, which is exactly the transitivity threat the policy exists to surface.
"""
from __future__ import annotations

import math

import pytest

from app.evidence import resolver, statuses
from app.evidence.engines.pairwise import StudyContrast
from app.evidence.resolver import ComparisonRequest, EvidenceSet

RULE = "NETMETA_IF_LOOPS_OR_MULTI_ARM_ELSE_BUCHER"
REQUIRED = "SENSITIVITY_REQUIRED"


def _request(a: str = "Rinvoq", b: str = "Humira") -> ComparisonRequest:
    return ComparisonRequest(
        indication="Psoriatic Arthritis",
        treatment_a=a,
        treatment_b=b,
        canonical_outcome_id="PSA_ACR50_W16",
        treatment_phase="PRIMARY",
    )


def _contrast(study: str, treatment: str, comparator: str, effect: float, var: float = 0.04):
    return StudyContrast(study, treatment, comparator, effect, var, "risk_ratio")


# A cross-route star: Rinvoq is oral, Humira and Taltz are subcutaneous, all anchored on
# placebo. This is the real PsA shape.
CROSS_ROUTE = EvidenceSet(
    contrasts=(
        _contrast("ORAL_1", "Rinvoq", "Placebo", math.log(2.0)),
        _contrast("SC_1", "Humira", "Placebo", math.log(1.6)),
    ),
    study_arms={
        "ORAL_1": frozenset({"Rinvoq", "Placebo"}),
        "SC_1": frozenset({"Humira", "Placebo"}),
    },
    administration_routes={"Rinvoq": "ORAL", "Humira": "SC"},
)

# A network where one route retains both compared treatments: Humira vs Taltz are both SC,
# and an oral study adds a second path that the restriction removes.
SINGLE_ROUTE_PATH = EvidenceSet(
    contrasts=(
        _contrast("SC_1", "Humira", "Placebo", math.log(1.6)),
        _contrast("SC_2", "Taltz", "Placebo", math.log(1.8)),
        _contrast("ORAL_1", "Rinvoq", "Placebo", math.log(2.0)),
    ),
    study_arms={
        "SC_1": frozenset({"Humira", "Placebo"}),
        "SC_2": frozenset({"Taltz", "Placebo"}),
        "ORAL_1": frozenset({"Rinvoq", "Placebo"}),
    },
    administration_routes={"Humira": "SC", "Taltz": "SC", "Rinvoq": "ORAL"},
)

SINGLE_ROUTE_ONLY = EvidenceSet(
    contrasts=(
        _contrast("SC_1", "Humira", "Placebo", math.log(1.6)),
        _contrast("SC_2", "Taltz", "Placebo", math.log(1.8)),
    ),
    study_arms={
        "SC_1": frozenset({"Humira", "Placebo"}),
        "SC_2": frozenset({"Taltz", "Placebo"}),
    },
    administration_routes={"Humira": "SC", "Taltz": "SC"},
)


# =====================================================================================
# When it runs at all
# =====================================================================================
def test_a_policy_that_does_not_require_one_says_so_rather_than_staying_silent():
    """CONTRAST_ONLY is a real choice, not an absence, so it is reported as one."""
    answer = resolver.resolve(
        CROSS_ROUTE, _request(), model_selection_rule=RULE,
        placebo_response_policy="CONTRAST_ONLY",
    )
    assert answer.sensitivity is not None
    assert answer.sensitivity.status == resolver.SENSITIVITY_NOT_REQUIRED
    assert answer.sensitivity.ran is False
    assert "CONTRAST_ONLY" in answer.sensitivity.reason


def test_a_single_route_network_records_why_no_analysis_was_needed():
    """The differential the policy guards against cannot arise, and that is worth saying."""
    answer = resolver.resolve(
        SINGLE_ROUTE_ONLY, _request("Humira", "Taltz"),
        model_selection_rule=RULE, placebo_response_policy=REQUIRED,
    )
    assert answer.sensitivity.status == resolver.SENSITIVITY_NOT_APPLICABLE
    assert answer.sensitivity.routes_present == ("SC",)
    assert "cannot arise" in answer.sensitivity.reason


def test_direct_evidence_carries_no_sensitivity_analysis():
    """A head-to-head trial has no transitivity chain for a route differential to travel."""
    direct = EvidenceSet(
        contrasts=(_contrast("H2H", "Rinvoq", "Humira", math.log(1.3)),),
        study_arms={"H2H": frozenset({"Rinvoq", "Humira"})},
        administration_routes={"Rinvoq": "ORAL", "Humira": "SC"},
    )
    answer = resolver.resolve(
        direct, _request(), model_selection_rule=RULE, placebo_response_policy=REQUIRED,
    )
    assert answer.evidence_level == resolver.LEVEL_DIRECT
    assert answer.sensitivity is None


# =====================================================================================
# The headline case: the restriction cannot be made
# =====================================================================================
def test_a_cross_route_pair_has_no_single_route_subnetwork_and_says_precisely_why():
    answer = resolver.resolve(
        CROSS_ROUTE, _request(), model_selection_rule=RULE,
        placebo_response_policy=REQUIRED,
    )

    assert answer.evidence_level == resolver.LEVEL_COMPUTED
    assert answer.estimate is not None  # the main analysis still stands
    sens = answer.sensitivity
    assert sens.status == resolver.SENSITIVITY_NOT_ESTIMABLE
    assert sens.routes_tried == ("ORAL", "SC")
    assert set(sens.treatments_dropped) == {"Rinvoq", "Humira"}
    assert "rests\nentirely" in sens.reason or "rests entirely" in sens.reason


def test_an_unrunnable_sensitivity_analysis_is_flagged_on_the_main_answer():
    """The number stands, but the missing disclosure travels with it."""
    answer = resolver.resolve(
        CROSS_ROUTE, _request(), model_selection_rule=RULE,
        placebo_response_policy=REQUIRED,
    )
    assert resolver.FLAG_SENSITIVITY_NOT_ESTIMABLE in answer.flags


def test_the_policy_is_never_reported_as_applied_when_it_was_not():
    """The defect this workstream exists to close, asserted directly."""
    answer = resolver.resolve(
        CROSS_ROUTE, _request(), model_selection_rule=RULE,
        placebo_response_policy=REQUIRED,
    )
    payload = answer.as_dict()
    assert payload["sensitivity"]["policy"] == REQUIRED
    assert payload["sensitivity"]["ran"] is False
    assert payload["sensitivity"]["estimate"] is None


# =====================================================================================
# When it does run
# =====================================================================================
def test_a_restrictable_network_produces_a_second_estimate_and_a_divergence():
    answer = resolver.resolve(
        SINGLE_ROUTE_PATH, _request("Humira", "Taltz"),
        model_selection_rule=RULE, placebo_response_policy=REQUIRED,
    )

    sens = answer.sensitivity
    assert sens.status == resolver.SENSITIVITY_COMPLETED
    assert sens.ran is True
    assert sens.restricted_to_route == "SC"
    assert sens.estimate is not None
    # The oral study contributes nothing to this pair, so the two analyses coincide —
    # a divergence of zero is a real result and is reported as one.
    assert sens.divergence == pytest.approx(0.0)
    assert sens.divergence_reported == pytest.approx(1.0)
    assert sens.intervals_overlap is True
    assert sens.diverges is False
    assert "ORAL_1" in sens.studies_dropped


def test_divergence_is_computed_on_the_log_scale_for_ratios():
    """RR 0.5 and RR 2.0 are equal and opposite; their arithmetic difference is not 1."""
    main = resolver.ComparisonAnswer(
        status=statuses.EXPLORATORY_RESULT_COMPLETED, evidence_level=3,
        treatment="A", comparator="B", reason="", effect_measure="risk_ratio",
        estimate=2.0, ci_lower=1.5, ci_upper=2.5,
    )
    restricted = resolver.ComparisonAnswer(
        status=statuses.EXPLORATORY_RESULT_COMPLETED, evidence_level=3,
        treatment="A", comparator="B", reason="", effect_measure="risk_ratio",
        estimate=0.5, ci_lower=0.4, ci_upper=0.6,
    )
    difference, reported = resolver._divergence(main, restricted)

    assert difference == pytest.approx(math.log(0.25))
    assert reported == pytest.approx(0.25)


def test_a_missing_interval_is_not_read_as_agreement():
    """Defaulting to 'they overlap' would manufacture reassurance out of absent data."""
    main = resolver.ComparisonAnswer(
        status=statuses.EXPLORATORY_RESULT_COMPLETED, evidence_level=3,
        treatment="A", comparator="B", reason="", estimate=1.2,
    )
    assert resolver._overlap(main, 0.9, 1.4) is None


# =====================================================================================
# The restriction itself
# =====================================================================================
def test_restricting_drops_cross_route_studies_and_their_contrasts_together():
    subset = resolver.restrict_to_route(SINGLE_ROUTE_PATH, "SC")

    assert set(subset.study_arms) == {"SC_1", "SC_2"}
    assert {c.study_id for c in subset.contrasts} == {"SC_1", "SC_2"}


def test_an_unrouted_node_does_not_disqualify_a_study():
    """"We do not know this arm's route" is not "this arm's route differs".

    Placebo usually has no curated route, and excluding every placebo-controlled study
    would empty the subnetwork on missing catalog data rather than on evidence.
    """
    subset = resolver.restrict_to_route(SINGLE_ROUTE_PATH, "SC")
    assert "Placebo" in set().union(*subset.study_arms.values())


def test_a_restricted_set_carries_no_published_syntheses():
    """Someone else's analysis cannot be re-restricted by us."""
    with_synthesis = EvidenceSet(
        contrasts=SINGLE_ROUTE_PATH.contrasts,
        study_arms=SINGLE_ROUTE_PATH.study_arms,
        administration_routes=SINGLE_ROUTE_PATH.administration_routes,
        syntheses=(),
    )
    assert resolver.restrict_to_route(with_synthesis, "SC").syntheses == ()
