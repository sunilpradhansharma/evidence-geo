"""Phase 6 — pairwise pooling and the Bucher indirect comparison.

Expected values are hand-computed from the closed-form formulae and written out in the
test, so a failure says *"the arithmetic changed"* rather than *"the snapshot changed"*.

The tests that carry the module:

* ratios pool on the **log** scale — RR 0.5 and RR 2.0 average to 1.0, not 1.25
* a single study's I² is **None**, never 0
* a double-zero study is **excluded** from a ratio, not corrected into existence
* Sweeting's correction perturbs both arms **equally** on unbalanced designs
* Bucher **adds** variances and refuses to subtract mismatched measures
* two anchors that disagree are **reported**, never averaged
"""
from __future__ import annotations

import math

import pytest

from app.evidence.engines import bucher
from app.evidence.engines.pairwise import (
    FIXED,
    RANDOM,
    Z_95,
    BinaryArm,
    ContinuousArm as Cont,
    StudyContrast,
    binary_contrast,
    continuous_contrast,
    from_analysis_scale,
    heterogeneity_model,
    is_ratio_measure,
    pool,
    to_analysis_scale,
)
from app.evidence.engines import pairwise


def _arm(treatment: str, events: int, n: int) -> BinaryArm:
    return BinaryArm(treatment=treatment, events=events, sample_size=n)


# =====================================================================================
# Scale handling
# =====================================================================================
def test_ratio_measures_are_analysed_on_the_log_scale():
    assert is_ratio_measure("risk_ratio")
    assert is_ratio_measure("odds_ratio")
    assert is_ratio_measure("hazard_ratio")
    assert not is_ratio_measure("mean_difference")
    assert not is_ratio_measure("risk_difference")

    assert to_analysis_scale(2.0, "risk_ratio") == pytest.approx(math.log(2.0))
    assert to_analysis_scale(2.0, "mean_difference") == 2.0
    assert from_analysis_scale(math.log(2.0), "risk_ratio") == pytest.approx(2.0)


def test_a_non_positive_ratio_cannot_be_log_transformed():
    """Refused rather than clamped: a negative risk ratio is a data error, not a small one."""
    with pytest.raises(ValueError, match="cannot be log-transformed"):
        to_analysis_scale(0.0, "risk_ratio")


def test_averaging_ratios_on_the_natural_scale_would_be_wrong():
    """THE reason ratios are pooled as logs. RR 0.5 and RR 2.0 are equal and opposite.

    Their arithmetic mean is 1.25, which would report a 25% harm where the evidence is
    perfectly balanced.
    """
    contrasts = [
        StudyContrast("S1", "A", "B", math.log(0.5), 0.04, "risk_ratio"),
        StudyContrast("S2", "A", "B", math.log(2.0), 0.04, "risk_ratio"),
    ]
    pooled = pool(contrasts, treatment="A", comparator="B", model=FIXED)
    assert pooled.estimate == pytest.approx(0.0)
    assert pooled.estimate_reported == pytest.approx(1.0)
    assert (0.5 + 2.0) / 2 == 1.25  # what the naive approach would have said


# =====================================================================================
# Binary contrasts, hand-computed
# =====================================================================================
def test_risk_ratio_matches_the_closed_form():
    """40/100 vs 20/100: RR = 2.0, var = 1/40 - 1/100 + 1/20 - 1/100 = 0.055."""
    contrast, reason = binary_contrast(
        "S1", _arm("A", 40, 100), _arm("B", 20, 100), measure="risk_ratio"
    )
    assert reason is None
    assert contrast.effect == pytest.approx(math.log(2.0))
    assert contrast.variance == pytest.approx(0.055)
    assert contrast.standard_error == pytest.approx(math.sqrt(0.055))
    assert not contrast.flags


def test_odds_ratio_matches_the_closed_form():
    """OR = (40/60)/(20/80) = 2.6667, var = 1/40+1/60+1/20+1/80 = 0.1041667."""
    contrast, _ = binary_contrast(
        "S1", _arm("A", 40, 100), _arm("B", 20, 100), measure="odds_ratio"
    )
    assert contrast.effect == pytest.approx(math.log(8.0 / 3.0))
    assert contrast.variance == pytest.approx(1 / 40 + 1 / 60 + 1 / 20 + 1 / 80)


def test_risk_difference_matches_the_closed_form():
    """RD = 0.4 - 0.2 = 0.2, var = .4*.6/100 + .2*.8/100 = 0.004."""
    contrast, _ = binary_contrast(
        "S1", _arm("A", 40, 100), _arm("B", 20, 100), measure="risk_difference"
    )
    assert contrast.effect == pytest.approx(0.2)
    assert contrast.variance == pytest.approx(0.004)


def test_a_continuous_mean_difference_matches_the_closed_form():
    """MD = 3, var = 25/50 + 16/50 = 0.82."""
    contrast, _ = continuous_contrast(
        "S1",
        Cont("A", mean=10.0, standard_deviation=5.0, sample_size=50),
        Cont("B", mean=7.0, standard_deviation=4.0, sample_size=50),
        measure="mean_difference",
    )
    assert contrast.effect == pytest.approx(3.0)
    assert contrast.variance == pytest.approx(0.82)


def test_the_standardised_mean_difference_applies_the_small_sample_correction():
    """Hedges' g, not Cohen's d: pooled SD 4.52769, d 0.66259, J = 1 - 3/391."""
    contrast, _ = continuous_contrast(
        "S1",
        Cont("A", mean=10.0, standard_deviation=5.0, sample_size=50),
        Cont("B", mean=7.0, standard_deviation=4.0, sample_size=50),
        measure="standardised_mean_difference",
    )
    pooled_sd = math.sqrt((49 * 25 + 49 * 16) / 98)
    d = 3.0 / pooled_sd
    g = d * (1 - 3 / (4 * 98 - 1))
    assert contrast.effect == pytest.approx(g)
    assert contrast.effect < d  # the correction shrinks it


def test_using_a_binary_measure_on_continuous_data_is_refused():
    with pytest.raises(ValueError, match="not a continuous effect measure"):
        continuous_contrast(
            "S1",
            Cont("A", 10.0, 5.0, 50), Cont("B", 7.0, 4.0, 50), measure="risk_ratio",
        )


def test_using_a_continuous_measure_on_binary_data_is_refused():
    with pytest.raises(ValueError, match="not a binary effect measure"):
        binary_contrast(
            "S1", _arm("A", 40, 100), _arm("B", 20, 100), measure="mean_difference",
        )


# =====================================================================================
# Zero events
# =====================================================================================
def test_a_double_zero_study_is_excluded_from_a_ratio():
    """No events in either arm means there is no ratio to estimate.

    Correcting it would manufacture an effect of exactly 1.0 with a finite interval out of
    a study that observed nothing.
    """
    contrast, reason = binary_contrast(
        "S1", _arm("A", 0, 100), _arm("B", 0, 100), measure="risk_ratio"
    )
    assert contrast is None
    assert reason == pairwise.FLAG_DOUBLE_ZERO_EXCLUDED


def test_a_single_zero_arm_is_corrected_and_flagged():
    """A continuity correction changes the data, so it is never silent."""
    contrast, reason = binary_contrast(
        "S1", _arm("A", 0, 100), _arm("B", 20, 100), measure="risk_ratio"
    )
    assert reason is None
    assert pairwise.FLAG_CONTINUITY_CORRECTED in contrast.flags
    assert contrast.effect < 0  # fewer events on A


def test_the_correction_perturbs_both_arms_equally_when_arms_are_unbalanced():
    """Sweeting's whole purpose. A flat 0.5 shifts the smaller arm's risk more.

    With n=50 and n=150 the corrections are 0.25 and 0.75, so each arm's risk moves by
    1/200 — which is what leaves the ratio undistorted.
    """
    small = _arm("A", 0, 50)
    large = _arm("B", 30, 150)
    k_small, k_large = pairwise._corrections(small, large, "TREATMENT_ARM_CONTINUITY_CORRECTION")

    assert k_small == pytest.approx(0.25)
    assert k_large == pytest.approx(0.75)
    # Equal perturbation of each arm's observed risk is the property that matters.
    assert k_small / small.sample_size == pytest.approx(k_large / large.sample_size)


def test_the_fixed_correction_stays_at_a_half_for_both_arms():
    k1, k2 = pairwise._corrections(_arm("A", 0, 50), _arm("B", 30, 150), "FIXED_0_5_CORRECTION")
    assert (k1, k2) == (0.5, 0.5)


def test_balanced_arms_make_both_policies_agree():
    balanced_a, balanced_b = _arm("A", 0, 100), _arm("B", 20, 100)
    assert pairwise._corrections(balanced_a, balanced_b, "TREATMENT_ARM_CONTINUITY_CORRECTION") == (
        pytest.approx(0.5), pytest.approx(0.5)
    )


def test_the_exclusion_policy_drops_the_study_instead_of_correcting_it():
    contrast, reason = binary_contrast(
        "S1", _arm("A", 0, 100), _arm("B", 20, 100),
        measure="risk_ratio", zero_event_policy="EXCLUDE_ZERO_EVENT_STUDIES",
    )
    assert contrast is None
    assert reason == pairwise.FLAG_ZERO_EVENT_EXCLUDED


def test_a_zero_events_arm_needs_no_correction_for_a_risk_difference():
    """A risk difference is estimable at zero; only ratios divide by the cell."""
    contrast, reason = binary_contrast(
        "S1", _arm("A", 0, 100), _arm("B", 20, 100), measure="risk_difference"
    )
    assert reason is None
    assert pairwise.FLAG_CONTINUITY_CORRECTED not in contrast.flags
    assert contrast.effect == pytest.approx(-0.2)


def test_an_odds_ratio_also_corrects_a_zero_non_event_cell():
    """100/100 has zero non-events, which breaks an odds ratio but not a risk ratio."""
    contrast, _ = binary_contrast(
        "S1", _arm("A", 100, 100), _arm("B", 20, 100), measure="odds_ratio"
    )
    assert pairwise.FLAG_CONTINUITY_CORRECTED in contrast.flags


def test_an_empty_arm_cannot_contribute():
    contrast, reason = binary_contrast("S1", _arm("A", 0, 0), _arm("B", 20, 100))
    assert contrast is None
    assert reason == pairwise.FLAG_ZERO_VARIANCE


# =====================================================================================
# Direction
# =====================================================================================
def test_reversing_a_contrast_negates_it():
    contrast = StudyContrast("S1", "A", "B", math.log(2.0), 0.05, "risk_ratio")
    flipped = contrast.reversed()
    assert flipped.treatment == "B"
    assert flipped.comparator == "A"
    assert flipped.effect == pytest.approx(-math.log(2.0))
    assert flipped.variance == contrast.variance


def test_orienting_finds_a_contrast_in_either_direction():
    contrast = StudyContrast("S1", "A", "B", 0.5, 0.05, "risk_ratio")
    assert contrast.oriented("A", "B") is contrast
    assert contrast.oriented("B", "A").effect == pytest.approx(-0.5)
    assert contrast.oriented("A", "C") is None


def test_pooling_picks_up_contrasts_recorded_the_other_way_round():
    contrasts = [
        StudyContrast("S1", "A", "B", math.log(2.0), 0.04, "risk_ratio"),
        StudyContrast("S2", "B", "A", -math.log(2.0), 0.04, "risk_ratio"),
    ]
    pooled = pool(contrasts, treatment="A", comparator="B", model=FIXED)
    assert pooled.study_count == 2
    assert pooled.estimate == pytest.approx(math.log(2.0))


# =====================================================================================
# Pooling and heterogeneity
# =====================================================================================
def test_inverse_variance_weighting_favours_the_more_precise_study():
    """Weights are 1/v, so the study with a quarter the variance gets four times the say."""
    contrasts = [
        StudyContrast("Precise", "A", "B", 1.0, 0.01, "risk_ratio"),
        StudyContrast("Vague", "A", "B", 2.0, 0.04, "risk_ratio"),
    ]
    pooled = pool(contrasts, treatment="A", comparator="B", model=FIXED)
    expected = (1 / 0.01 * 1.0 + 1 / 0.04 * 2.0) / (1 / 0.01 + 1 / 0.04)
    assert pooled.estimate == pytest.approx(expected)
    assert pooled.standard_error == pytest.approx(math.sqrt(1 / (1 / 0.01 + 1 / 0.04)))


def test_a_single_study_has_no_estimable_heterogeneity():
    """I² is None, not 0. Zero would assert homogeneity was assessed and confirmed."""
    pooled = pool(
        [StudyContrast("S1", "A", "B", 0.5, 0.04, "risk_ratio")],
        treatment="A", comparator="B", model=FIXED,
    )
    assert pooled.i_squared is None
    assert pooled.q_statistic is None
    assert pooled.degrees_freedom is None
    assert pooled.tau_squared == 0.0
    assert pairwise.FLAG_SINGLE_STUDY in pooled.flags


def test_identical_studies_show_no_heterogeneity():
    contrasts = [
        StudyContrast("S1", "A", "B", 0.5, 0.04, "risk_ratio"),
        StudyContrast("S2", "A", "B", 0.5, 0.04, "risk_ratio"),
    ]
    pooled = pool(contrasts, treatment="A", comparator="B", model=FIXED)
    assert pooled.q_statistic == pytest.approx(0.0)
    assert pooled.i_squared == pytest.approx(0.0)
    assert pooled.tau_squared == pytest.approx(0.0)


def test_divergent_studies_produce_heterogeneity_and_a_wider_random_effects_interval():
    """Random effects must not look more certain than fixed when studies disagree."""
    contrasts = [
        StudyContrast("S1", "A", "B", 0.1, 0.01, "risk_ratio"),
        StudyContrast("S2", "A", "B", 1.5, 0.01, "risk_ratio"),
    ]
    fixed = pool(contrasts, treatment="A", comparator="B", model=FIXED)
    random = pool(contrasts, treatment="A", comparator="B", model=RANDOM)

    assert fixed.i_squared > 50.0
    assert random.tau_squared > 0.0
    assert random.standard_error > fixed.standard_error
    assert pairwise.FLAG_HETEROGENEITY_HIGH in fixed.flags


def test_random_effects_equals_fixed_effects_when_tau_squared_is_zero():
    """Not a coincidence to paper over — the formulae coincide exactly."""
    contrasts = [
        StudyContrast("S1", "A", "B", 0.5, 0.04, "risk_ratio"),
        StudyContrast("S2", "A", "B", 0.5, 0.04, "risk_ratio"),
    ]
    fixed = pool(contrasts, treatment="A", comparator="B", model=FIXED)
    random = pool(contrasts, treatment="A", comparator="B", model=RANDOM)
    assert random.estimate == pytest.approx(fixed.estimate)
    assert random.standard_error == pytest.approx(fixed.standard_error)


def test_i_squared_is_truncated_at_zero():
    """Q below its degrees of freedom implies negative I², which is not a proportion."""
    contrasts = [
        StudyContrast("S1", "A", "B", 0.50, 1.0, "risk_ratio"),
        StudyContrast("S2", "A", "B", 0.51, 1.0, "risk_ratio"),
    ]
    pooled = pool(contrasts, treatment="A", comparator="B", model=FIXED)
    assert pooled.q_statistic < pooled.degrees_freedom
    assert pooled.i_squared == 0.0


def test_pooling_different_measures_is_refused():
    contrasts = [
        StudyContrast("S1", "A", "B", 0.5, 0.04, "risk_ratio"),
        StudyContrast("S2", "A", "B", 0.5, 0.04, "odds_ratio"),
    ]
    with pytest.raises(ValueError, match="different effect measures"):
        pool(contrasts, treatment="A", comparator="B", model=FIXED)


def test_pooling_nothing_returns_none_rather_than_a_zero_estimate():
    assert pool([], treatment="A", comparator="B") is None
    unrelated = [StudyContrast("S1", "C", "D", 0.5, 0.04, "risk_ratio")]
    assert pool(unrelated, treatment="A", comparator="B") is None


def test_the_confidence_interval_uses_the_normal_quantile():
    pooled = pool(
        [StudyContrast("S1", "A", "B", math.log(2.0), 0.04, "risk_ratio")],
        treatment="A", comparator="B", model=FIXED,
    )
    assert pooled.ci_lower == pytest.approx(math.log(2.0) - Z_95 * 0.2)
    assert pooled.ci_upper_reported == pytest.approx(math.exp(math.log(2.0) + Z_95 * 0.2))


# =====================================================================================
# Model selection from the protocol
# =====================================================================================
def test_the_protocol_rule_overrides_a_caller_supplied_model():
    """Otherwise a caller could quietly bypass the approved methodology."""
    contrasts = [
        StudyContrast("S1", "A", "B", 0.1, 0.01, "risk_ratio"),
        StudyContrast("S2", "A", "B", 1.5, 0.01, "risk_ratio"),
    ]
    pooled = pool(
        contrasts, treatment="A", comparator="B",
        model=FIXED, heterogeneity_rule="RANDOM_EFFECTS_ALWAYS",
    )
    assert pooled.model == RANDOM


@pytest.mark.parametrize(
    "rule,i2,expected",
    [
        ("FIXED_EFFECTS_ALWAYS", 90.0, FIXED),
        ("RANDOM_EFFECTS_ALWAYS", 0.0, RANDOM),
        ("RANDOM_EFFECTS_IF_I2_ABOVE_50", 80.0, RANDOM),
        ("RANDOM_EFFECTS_IF_I2_ABOVE_50", 10.0, FIXED),
    ],
)
def test_heterogeneity_rules_select_the_stated_model(rule, i2, expected):
    assert heterogeneity_model(rule, i2) == expected


def test_an_unknown_i_squared_selects_random_effects():
    """A single study cannot demonstrate homogeneity, so fixed effects would assert one."""
    assert heterogeneity_model("RANDOM_EFFECTS_IF_I2_ABOVE_50", None) == RANDOM


def test_the_zero_event_policy_comes_from_the_protocol():
    assert (
        pairwise.zero_event_policy_of("PSA_ACR50_W16_PRIMARY")
        == "TREATMENT_ARM_CONTINUITY_CORRECTION"
    )
    # An unknown protocol falls back to the less biased correction, not the flat 0.5.
    assert pairwise.zero_event_policy_of("NOPE") == "TREATMENT_ARM_CONTINUITY_CORRECTION"


# =====================================================================================
# Bucher
# =====================================================================================
def _leg(treatment, anchor, effect, variance, studies=("S1", "S2"), i2=10.0):
    return pairwise.PooledEffect(
        treatment=treatment, comparator=anchor, measure="risk_ratio", model=FIXED,
        estimate=effect, standard_error=math.sqrt(variance), study_count=len(studies),
        contributing_studies=tuple(studies), i_squared=i2, q_statistic=1.0,
        degrees_freedom=len(studies) - 1,
    )


def test_bucher_subtracts_the_legs_and_adds_their_variances():
    """d_AB = d_AC - d_BC, var = var_AC + var_BC. Both exact."""
    leg_a = _leg("A", "Placebo", math.log(2.0), 0.04)
    leg_b = _leg("B", "Placebo", math.log(1.5), 0.09)

    estimate = bucher.combine(leg_a, leg_b)
    assert estimate.estimate == pytest.approx(math.log(2.0) - math.log(1.5))
    assert estimate.estimate_reported == pytest.approx(2.0 / 1.5)
    assert estimate.standard_error == pytest.approx(math.sqrt(0.04 + 0.09))
    assert estimate.anchor == "Placebo"


def test_the_indirect_interval_is_wider_than_either_leg():
    """Variances add, so an indirect estimate is always less precise than its legs."""
    leg_a = _leg("A", "Placebo", math.log(2.0), 0.04)
    leg_b = _leg("B", "Placebo", math.log(1.5), 0.09)
    estimate = bucher.combine(leg_a, leg_b)
    assert estimate.standard_error > leg_a.standard_error
    assert estimate.standard_error > leg_b.standard_error


def test_legs_that_do_not_share_an_anchor_are_refused():
    with pytest.raises(ValueError, match="do not share an anchor"):
        bucher.combine(
            _leg("A", "Placebo", 0.5, 0.04), _leg("B", "Humira", 0.3, 0.04)
        )


def test_legs_on_different_measures_are_refused():
    """Subtracting a log odds ratio from a log risk ratio is meaningless arithmetic."""
    leg_a = _leg("A", "Placebo", 0.5, 0.04)
    leg_b = pairwise.PooledEffect(
        treatment="B", comparator="Placebo", measure="odds_ratio", model=FIXED,
        estimate=0.3, standard_error=0.2, study_count=2,
    )
    with pytest.raises(ValueError, match="both legs must be on the same effect measure"):
        bucher.combine(leg_a, leg_b)


def test_a_route_mixed_anchor_is_disclosed():
    """Oral and injectable placebo are one graph node and two different things."""
    estimate = bucher.combine(
        _leg("Rinvoq", "Placebo", math.log(2.0), 0.04),
        _leg("Humira", "Placebo", math.log(1.5), 0.04),
        routes={"Rinvoq": "ORAL", "Humira": "SUBCUTANEOUS"},
    )
    assert bucher.FLAG_ROUTE_MIXED_ANCHOR in estimate.flags


def test_a_single_route_anchor_raises_no_route_flag():
    estimate = bucher.combine(
        _leg("Skyrizi", "Placebo", math.log(2.0), 0.04),
        _leg("Humira", "Placebo", math.log(1.5), 0.04),
        routes={"Skyrizi": "SUBCUTANEOUS", "Humira": "SUBCUTANEOUS"},
    )
    assert bucher.FLAG_ROUTE_MIXED_ANCHOR not in estimate.flags


def test_a_leg_resting_on_one_study_is_flagged():
    estimate = bucher.combine(
        _leg("A", "Placebo", 0.5, 0.04, studies=("S1",), i2=None),
        _leg("B", "Placebo", 0.3, 0.04),
    )
    assert bucher.FLAG_SINGLE_STUDY_LEG in estimate.flags


def test_a_multi_arm_study_in_both_legs_is_counted_once():
    """Double-counting patients is what makes naive pairwise flattening understate error."""
    estimate = bucher.combine(
        _leg("A", "Placebo", 0.5, 0.04, studies=("SHARED", "S2")),
        _leg("B", "Placebo", 0.3, 0.04, studies=("SHARED", "S3")),
    )
    assert estimate.study_count == 3


def test_compare_builds_the_indirect_estimate_from_raw_contrasts():
    contrasts = [
        StudyContrast("S1", "Rinvoq", "Placebo", math.log(2.0), 0.04, "risk_ratio"),
        StudyContrast("S2", "Humira", "Placebo", math.log(1.5), 0.04, "risk_ratio"),
    ]
    result = bucher.compare(
        contrasts, treatment="Rinvoq", comparator="Humira", anchors=["Placebo"]
    )
    assert result.estimable
    assert result.anchors == ("Placebo",)
    assert result.estimates[0].estimate == pytest.approx(math.log(2.0) - math.log(1.5))


def test_a_missing_leg_is_reported_as_a_refusal_not_an_empty_result():
    contrasts = [StudyContrast("S1", "Rinvoq", "Placebo", math.log(2.0), 0.04, "risk_ratio")]
    result = bucher.compare(
        contrasts, treatment="Rinvoq", comparator="Humira", anchors=["Placebo"]
    )
    assert not result.estimable
    assert result.refusals[0][0] == "Placebo"
    assert "Humira versus Placebo" in result.refusals[0][1]


def test_an_anchor_that_is_one_of_the_compared_treatments_is_refused():
    contrasts = [StudyContrast("S1", "A", "B", 0.5, 0.04, "risk_ratio")]
    result = bucher.compare(contrasts, treatment="A", comparator="B", anchors=["A"])
    assert not result.estimable
    assert "one of the two treatments" in result.refusals[0][1]


def test_two_anchors_give_two_estimates_and_are_not_averaged():
    """If anchors disagree, that disagreement IS the finding."""
    contrasts = [
        StudyContrast("S1", "A", "Placebo", math.log(2.0), 0.04, "risk_ratio"),
        StudyContrast("S2", "B", "Placebo", math.log(1.5), 0.04, "risk_ratio"),
        StudyContrast("S3", "A", "Humira", math.log(1.1), 0.04, "risk_ratio"),
        StudyContrast("S4", "B", "Humira", math.log(1.05), 0.04, "risk_ratio"),
    ]
    result = bucher.compare(
        contrasts, treatment="A", comparator="B", anchors=["Placebo", "Humira"]
    )
    assert len(result.estimates) == 2
    assert set(result.anchors) == {"Placebo", "Humira"}


def test_non_overlapping_anchors_are_flagged_as_disagreeing():
    """A strong signal that transitivity fails somewhere in the network."""
    contrasts = [
        StudyContrast("S1", "A", "Placebo", math.log(4.0), 0.001, "risk_ratio"),
        StudyContrast("S2", "B", "Placebo", math.log(1.0), 0.001, "risk_ratio"),
        StudyContrast("S3", "A", "Humira", math.log(1.0), 0.001, "risk_ratio"),
        StudyContrast("S4", "B", "Humira", math.log(1.0), 0.001, "risk_ratio"),
    ]
    result = bucher.compare(
        contrasts, treatment="A", comparator="B", anchors=["Placebo", "Humira"]
    )
    assert result.anchors_disagree
    assert all(bucher.FLAG_ANCHOR_DISAGREEMENT in e.flags for e in result.estimates)


def test_overlapping_anchors_are_not_flagged():
    contrasts = [
        StudyContrast("S1", "A", "Placebo", math.log(2.0), 0.04, "risk_ratio"),
        StudyContrast("S2", "B", "Placebo", math.log(1.5), 0.04, "risk_ratio"),
        StudyContrast("S3", "A", "Humira", math.log(2.1), 0.04, "risk_ratio"),
        StudyContrast("S4", "B", "Humira", math.log(1.4), 0.04, "risk_ratio"),
    ]
    result = bucher.compare(
        contrasts, treatment="A", comparator="B", anchors=["Placebo", "Humira"]
    )
    assert not result.anchors_disagree
    assert all(bucher.FLAG_ANCHOR_DISAGREEMENT not in e.flags for e in result.estimates)


def test_a_single_estimate_cannot_disagree_with_itself():
    contrasts = [
        StudyContrast("S1", "A", "Placebo", math.log(2.0), 0.04, "risk_ratio"),
        StudyContrast("S2", "B", "Placebo", math.log(1.5), 0.04, "risk_ratio"),
    ]
    result = bucher.compare(
        contrasts, treatment="A", comparator="B", anchors=["Placebo"]
    )
    assert not result.anchors_disagree


def test_the_bucher_result_serialises_with_both_legs_visible():
    """A reviewer must be able to audit the two legs the subtraction used."""
    contrasts = [
        StudyContrast("S1", "Rinvoq", "Placebo", math.log(2.0), 0.04, "risk_ratio"),
        StudyContrast("S2", "Humira", "Placebo", math.log(1.5), 0.04, "risk_ratio"),
    ]
    payload = bucher.compare(
        contrasts, treatment="Rinvoq", comparator="Humira", anchors=["Placebo"]
    ).as_dict()

    assert payload["estimable"]
    legs = payload["estimates"][0]["legs"]
    assert "Rinvoq_vs_Placebo" in legs
    assert "Humira_vs_Placebo" in legs
    assert legs["Rinvoq_vs_Placebo"]["estimate"] == pytest.approx(2.0)


# =====================================================================================
# End to end from arm counts
# =====================================================================================
def test_an_indirect_comparison_from_raw_event_counts():
    """The path a real analysis takes: arms to contrasts to legs to an indirect estimate."""
    rinvoq_vs_placebo, _ = binary_contrast(
        "SELECT-PsA-1", _arm("Rinvoq", 45, 100), _arm("Placebo", 20, 100)
    )
    humira_vs_placebo, _ = binary_contrast(
        "SELECT-PsA-2", _arm("Humira", 35, 100), _arm("Placebo", 20, 100)
    )

    result = bucher.compare(
        [rinvoq_vs_placebo, humira_vs_placebo],
        treatment="Rinvoq", comparator="Humira", anchors=["Placebo"],
    )
    assert result.estimable
    estimate = result.estimates[0]
    # (45/100 / 20/100) / (35/100 / 20/100) = 45/35
    assert estimate.estimate_reported == pytest.approx(45 / 35)
    assert estimate.ci_lower_reported < estimate.estimate_reported < estimate.ci_upper_reported
    assert estimate.study_count == 2


# =====================================================================================
# The protocol decides the arm-data shape
# =====================================================================================
def test_each_effect_measure_names_the_arm_data_it_needs():
    assert pairwise.outcome_type_for("risk_ratio") == "binary"
    assert pairwise.outcome_type_for("odds_ratio") == "binary"
    assert pairwise.outcome_type_for("risk_difference") == "binary"
    assert pairwise.outcome_type_for("mean_difference") == "continuous"
    assert pairwise.outcome_type_for("standardised_mean_difference") == "continuous"


def test_the_outcome_type_literals_match_the_stored_column_values():
    """They are duplicated to keep this module free of an ORM import, so drift is possible
    and would silently reject every row in a network."""
    from app.models.clinical_study import BINARY, CONTINUOUS

    assert pairwise.BINARY_OUTCOME == BINARY
    assert pairwise.CONTINUOUS_OUTCOME == CONTINUOUS


def test_an_unrecognised_measure_raises_rather_than_defaulting_to_binary():
    """A wrong default is silent: it would make a risk ratio out of means."""
    with pytest.raises(ValueError, match="not a recognised effect measure"):
        pairwise.outcome_type_for("bayesian_shrinkage")
    with pytest.raises(ValueError, match="not a recognised effect measure"):
        pairwise.outcome_type_for(None)


def test_a_hazard_ratio_pools_as_a_ratio_but_has_no_arm_level_shape():
    """It is a ratio measure, but survival data is not events-and-N. Treating it as binary
    would invite an arm-level analysis of data that cannot support one."""
    assert pairwise.is_ratio_measure("hazard_ratio")
    with pytest.raises(ValueError, match="not a recognised effect measure"):
        pairwise.outcome_type_for("hazard_ratio")
