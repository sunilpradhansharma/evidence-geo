"""Phase 4 — published synthesis adapter and the Level-2 suitability gate.

Every test maps to a named acceptance criterion. The ones that carry the phase:

* the same league table is read identically from a matrix, a contrast list and a
  common-reference table
* a CrI is never presented as a CI
* the effect measure is never inferred from an estimate's magnitude
* a missing interval stays missing
* **an NMA containing both treatments can still be unsuitable** — the plan's own words
* an unrecoverable included-study list is disqualifying, not cosmetic
"""
from __future__ import annotations

from datetime import date

import pytest

from app.evidence import statuses
from app.evidence.sources import published_nma as pn
from app.evidence.suitability import ComparisonRequest, assess, best_of
from app.evidence.treatments import canonical_treatment

PROTOCOL = "PSA_ACR50_W16_PRIMARY"


def _record(**overrides) -> dict:
    """A well-formed PsA ACR50 synthesis; individual tests degrade one field at a time."""
    base = {
        "source_type": "COCHRANE",
        "source_identifier": "10.1002/14651858.CD013967",
        "citation": "Cochrane Database Syst Rev. 2024;3:CD013967.",
        "publication_date": "2024-03-01",
        "funding_source": "None declared",
        "indication": "Psoriatic Arthritis",
        "endpoint": "ACR50",
        "timepoint_week": 16,
        "treatment_phase": "PRIMARY",
        "effect_measure": "RR",
        "model_type": "random-effects",
        "interval_type": "95% CI",
        "grade_certainty": "MODERATE",
        "league_table": {
            "Upadacitinib 15 mg QD": {"Adalimumab 40 mg EOW": "1.40 (1.10 to 1.80)"},
        },
        "included_studies": ["NCT03104400", "NCT03104374"],
        "tau_squared": 0.04,
        "inconsistency": {"design_by_treatment_p": 0.42},
        "sucra": {"Upadacitinib": 85.2, "Adalimumab": 60.1},
    }
    base.update(overrides)
    return base


def _request(**overrides) -> ComparisonRequest:
    defaults = {
        "indication": "Psoriatic Arthritis",
        "treatment_a": "Rinvoq",
        "treatment_b": "Humira",
        "canonical_outcome_id": "PSA_ACR50_W16",
        "treatment_phase": "PRIMARY",
        "protocol_id": PROTOCOL,
        "as_of": date(2026, 1, 1),
    }
    defaults.update(overrides)
    return ComparisonRequest(**defaults)


# =====================================================================================
# One shared normaliser for treatment labels
# =====================================================================================
def test_published_labels_resolve_to_the_same_nodes_as_registry_arms():
    """Otherwise a published network and an internal one appear disjoint when they are not."""
    for label in ("Upadacitinib 15 mg QD", "RINVOQ", "upadacitinib", "ABT-494"):
        node, _ = canonical_treatment(label)
        assert node in ("Rinvoq", "ABT-494")
    assert canonical_treatment("Adalimumab 40 mg EOW")[0] == "Humira"
    assert canonical_treatment("Placebo (oral)")[0] == "Placebo"


# =====================================================================================
# Heterogeneous league-table shapes
# =====================================================================================
def test_a_matrix_league_table_is_parsed():
    parsed = pn.parse(_record())
    assert parsed.usable
    assert set(parsed.treatments) == {"Rinvoq", "Humira"}
    contrast = parsed.contrasts[0]
    assert (contrast.treatment, contrast.comparator) == ("Rinvoq", "Humira")
    assert contrast.estimate == 1.40
    assert (contrast.interval_lower, contrast.interval_upper) == (1.10, 1.80)


def test_a_contrast_list_yields_the_same_reading_as_a_matrix():
    """Journal format must not change the numbers."""
    from_matrix = pn.parse(_record()).contrasts[0]
    from_rows = pn.parse(_record(league_table=[{
        "treatment": "Upadacitinib 15 mg QD", "comparator": "Adalimumab 40 mg EOW",
        "estimate": 1.40, "ci_lower": 1.10, "ci_upper": 1.80,
    }])).contrasts[0]

    assert (from_rows.treatment, from_rows.comparator) == (from_matrix.treatment, from_matrix.comparator)
    assert from_rows.estimate == from_matrix.estimate
    assert from_rows.interval_lower == from_matrix.interval_lower
    assert from_rows.interval_upper == from_matrix.interval_upper


def test_a_common_reference_table_fills_in_the_unnamed_comparator():
    parsed = pn.parse(_record(
        common_reference="Placebo",
        league_table=[{"treatment": "RINVOQ", "estimate": 2.1, "ci": [1.7, 2.6]}],
    ))
    contrast = parsed.contrasts[0]
    assert (contrast.treatment, contrast.comparator) == ("Rinvoq", "Placebo")
    assert (contrast.interval_lower, contrast.interval_upper) == (1.7, 2.6)


def test_an_interval_printed_inside_a_string_is_recovered():
    parsed = pn.parse(_record(league_table={"RINVOQ": {"Humira": "0.85 [0.70, 1.03]"}}))
    contrast = parsed.contrasts[0]
    assert contrast.estimate == 0.85
    assert (contrast.interval_lower, contrast.interval_upper) == (0.70, 1.03)


def test_a_negative_mean_difference_and_its_bounds_survive():
    parsed = pn.parse(_record(
        effect_measure="MD", league_table={"RINVOQ": {"Humira": "-0.50 (-1.20 to 0.20)"}},
    ))
    contrast = parsed.contrasts[0]
    assert contrast.estimate == -0.50
    assert (contrast.interval_lower, contrast.interval_upper) == (-1.20, 0.20)
    assert contrast.effect_measure == "mean_difference"


# =====================================================================================
# Things the adapter refuses to invent
# =====================================================================================
def test_a_credible_interval_is_never_presented_as_a_confidence_interval():
    """A CrI and a CI support different statements."""
    parsed = pn.parse(_record(
        interval_type=None, model_type="random",
        league_table=[{"treatment": "RINVOQ", "comparator": "Humira", "rr": 1.4,
                       "lower_cri": 1.1, "upper_cri": 1.8}],
    ))
    assert parsed.contrasts[0].interval_type == pn.CREDIBLE_INTERVAL


def test_an_unstated_interval_type_is_flagged_not_assumed():
    parsed = pn.parse(_record(
        interval_type=None, league_table={"RINVOQ": {"Humira": "1.40 (1.10 to 1.80)"}},
    ))
    assert pn.FLAG_INTERVAL_TYPE_UNSTATED in parsed.contrasts[0].flags


def test_the_effect_measure_is_never_inferred_from_magnitude():
    """An estimate near 1.0 could be a risk ratio or an odds ratio."""
    parsed = pn.parse(_record(
        effect_measure=None,
        league_table={"RINVOQ": {"Humira": "1.05 (0.95 to 1.16)"}},
    ))
    assert parsed.effect_measure is None
    assert pn.FLAG_NO_EFFECT_MEASURE in parsed.flags


def test_a_measure_named_key_does_state_the_measure():
    """Reading "or" off the key is reading the source, not guessing from magnitude."""
    parsed = pn.parse(_record(
        effect_measure=None,
        league_table=[{"treatment": "RINVOQ", "comparator": "Humira", "or": 1.05}],
    ))
    assert parsed.contrasts[0].effect_measure == "odds_ratio"


def test_a_missing_interval_stays_missing():
    parsed = pn.parse(_record(
        league_table=[{"treatment": "RINVOQ", "comparator": "Humira", "estimate": 1.4}],
    ))
    contrast = parsed.contrasts[0]
    assert not contrast.has_interval
    assert contrast.interval_lower is None
    assert pn.FLAG_NO_INTERVAL in contrast.flags


def test_contrast_direction_is_preserved_not_normalised():
    """Reversing a contrast means taking a reciprocal, so the pair is never reordered."""
    parsed = pn.parse(_record(league_table={"Adalimumab": {"Upadacitinib": 0.71}}))
    contrast = parsed.contrasts[0]
    assert (contrast.treatment, contrast.comparator) == ("Humira", "Rinvoq")


def test_ranking_scores_are_rescaled_from_percent_and_the_conversion_is_flagged():
    parsed = pn.parse(_record())
    assert parsed.ranking_metric == pn.SUCRA
    assert parsed.ranking_scores == {"Rinvoq": 0.852, "Humira": 0.601}
    assert pn.FLAG_RANKING_RESCALED in parsed.flags


def test_proportion_scale_ranking_scores_are_left_alone():
    parsed = pn.parse(_record(sucra={"Upadacitinib": 0.85}))
    assert parsed.ranking_scores == {"Rinvoq": 0.85}
    assert pn.FLAG_RANKING_RESCALED not in parsed.flags


def test_p_score_is_recorded_as_p_score_not_relabelled_sucra():
    parsed = pn.parse(_record(sucra=None, p_score={"Upadacitinib": 0.91}))
    assert parsed.ranking_metric == pn.P_SCORE


# =====================================================================================
# Records that cannot be trusted at all
# =====================================================================================
def test_a_synthesis_with_no_estimates_is_not_usable():
    parsed = pn.parse(_record(league_table=None))
    assert not parsed.usable
    assert any("no league_table or estimates" in p for p in parsed.problems)


def test_a_synthesis_with_no_indication_is_not_usable():
    parsed = pn.parse(_record(indication=None))
    assert not parsed.usable


def test_an_unrecognised_treatment_phase_is_not_defaulted():
    """Induction and maintenance are never poolable, so a bad phase cannot be shrugged off."""
    parsed = pn.parse(_record(treatment_phase="RESCUE"))
    assert not parsed.usable
    assert any("treatment_phase" in p for p in parsed.problems)


def test_a_missing_included_study_list_is_flagged_but_still_stored():
    """The citation is worth keeping; the Level-2 gate is what refuses on it."""
    parsed = pn.parse(_record(included_studies=[]))
    assert parsed.usable
    assert not parsed.included_studies_recoverable
    assert pn.FLAG_STUDIES_NOT_RECOVERABLE in parsed.flags


def test_declared_unrecoverable_overrides_a_present_list():
    parsed = pn.parse(_record(included_studies_recoverable=False))
    assert not parsed.included_studies_recoverable


# =====================================================================================
# Mapping onto the shared result table
# =====================================================================================
def test_a_published_result_is_citable_but_its_extraction_is_not_approved():
    """The source's authority does not transfer to our unreviewed reading of it."""
    parsed = pn.parse(_record())
    row = pn.to_nma_result(parsed, result_id="R1", status=statuses.PUBLISHED_RESULT_AVAILABLE)
    assert row.source == "PUBLISHED"
    assert row.source_is_citable
    assert not row.claim_is_approved_for_external_use
    assert not row.is_internal_output
    assert row.grade_certainty == "MODERATE"
    assert row.canonical_outcome_id == "PSA_ACR50_W16"


# =====================================================================================
# The Level-2 gate
# =====================================================================================
def test_a_fully_matching_synthesis_is_suitable():
    decision = assess(pn.parse(_record()), _request())
    assert decision.suitable
    assert decision.status == statuses.PUBLISHED_RESULT_AVAILABLE
    assert decision.matched_contrast.estimate == 1.40


def test_containing_both_treatments_is_not_sufficient():
    """The plan's own example: an NMA with both drugs can still be unsuitable.

    Nothing is wrong with this paper. It reports ACR20, and the question asked ACR50.
    """
    parsed = pn.parse(_record(
        endpoint="ACR20", canonical_outcome_id="PSA_ACR20_W16",
    ))
    assert set(parsed.treatments) == {"Rinvoq", "Humira"}

    decision = assess(parsed, _request())
    assert not decision.suitable
    assert decision.status == statuses.ENDPOINT_MISMATCH
    assert "endpoint" in decision.failed_dimensions


def test_an_out_of_window_timepoint_is_refused_against_the_protocol():
    """The protocol's approved window is narrower than the outcome's own."""
    decision = assess(pn.parse(_record(timepoint_week=12)), _request())
    assert not decision.suitable
    assert decision.status == statuses.TIMEPOINT_MISMATCH


def test_a_phase_mismatch_is_refused_with_its_own_status():
    decision = assess(
        pn.parse(_record(treatment_phase="MAINTENANCE")),
        _request(treatment_phase="PRIMARY"),
    )
    assert not decision.suitable
    assert decision.status == statuses.TREATMENT_PHASE_MISMATCH


def test_a_population_mismatch_is_refused():
    decision = assess(pn.parse(_record()), _request(population_stratum="BIO_EXPERIENCED"))
    assert not decision.suitable
    assert decision.status == statuses.POPULATION_NONCOMPARABLE


def test_an_unrecoverable_study_list_disqualifies_the_synthesis():
    """A hard requirement, not a nice-to-have."""
    decision = assess(pn.parse(_record(included_studies=[])), _request())
    assert not decision.suitable
    assert "included_studies" in decision.failed_dimensions


def test_a_stale_synthesis_is_refused():
    decision = assess(pn.parse(_record(publication_date="2015-01-01")), _request())
    assert not decision.suitable
    assert "recency" in decision.failed_dimensions


def test_recency_can_be_disabled_explicitly():
    """Age is a proxy; a caller doing its own overlap check may switch it off."""
    decision = assess(
        pn.parse(_record(publication_date="2015-01-01")), _request(), max_age_years=None
    )
    assert decision.suitable


def test_a_missing_treatment_is_reported_as_such():
    parsed = pn.parse(_record(league_table={"Tremfya": {"Humira": 1.2}}))
    decision = assess(parsed, _request())
    assert not decision.suitable
    assert "treatments" in decision.failed_dimensions
    assert any("Rinvoq" in r for r in decision.reasons)


def test_both_treatments_present_but_the_pair_is_not_published():
    """Both nodes are in the network, yet the league table never reports them together."""
    parsed = pn.parse(_record(
        league_table=[
            {"treatment": "RINVOQ", "comparator": "Placebo", "estimate": 2.1},
            {"treatment": "Adalimumab", "comparator": "Placebo", "estimate": 1.5},
        ],
    ))
    assert set(parsed.treatments) == {"Rinvoq", "Humira", "Placebo"}
    decision = assess(parsed, _request())
    assert not decision.suitable
    assert "estimate" in decision.failed_dimensions


def test_every_failure_is_reported_not_just_the_first():
    """A reviewer chasing a paper needs the whole list."""
    decision = assess(
        pn.parse(_record(
            timepoint_week=52, treatment_phase="MAINTENANCE", included_studies=[],
            publication_date="2014-01-01",
        )),
        _request(),
    )
    assert not decision.suitable
    assert {"timepoint", "treatment_phase", "included_studies", "recency"} <= set(
        decision.failed_dimensions
    )


def test_a_reversed_direction_is_disclosed_rather_than_silently_flipped():
    parsed = pn.parse(_record(league_table={"Adalimumab 40 mg": {"Upadacitinib 15 mg": 0.71}}))
    decision = assess(parsed, _request())
    assert decision.suitable
    assert "direction reversed" in decision.reason_text


def test_a_dose_the_source_never_reported_is_refused():
    decision = assess(pn.parse(_record()), _request(requested_dose="30 mg"))
    assert not decision.suitable
    assert "dose" in decision.failed_dimensions


def test_the_dose_actually_reported_passes():
    decision = assess(pn.parse(_record()), _request(requested_dose="15 mg"))
    assert decision.suitable


def test_an_untrustworthy_extraction_is_refused_before_any_dimension_check():
    decision = assess(pn.parse(_record(league_table=None)), _request())
    assert not decision.suitable
    assert decision.failed_dimensions == ("extraction",)


# =====================================================================================
# Choosing among several syntheses
# =====================================================================================
def test_the_most_recent_suitable_synthesis_wins():
    old = pn.parse(_record(publication_date="2022-01-01", citation="older"))
    new = pn.parse(_record(publication_date="2024-03-01", citation="newer"))
    chosen, decision = best_of([old, new], _request())
    assert decision.suitable
    assert chosen.citation == "newer"


def test_the_closest_miss_is_returned_when_nothing_is_suitable():
    """The citation stays visible when the resolver falls through to Level 3."""
    wrong_endpoint = pn.parse(_record(canonical_outcome_id="PSA_ACR20_W16"))
    wrong_everything = pn.parse(_record(
        canonical_outcome_id="PSA_ACR20_W16", treatment_phase="MAINTENANCE",
        included_studies=[], publication_date="2013-01-01",
    ))
    chosen, decision = best_of([wrong_everything, wrong_endpoint], _request())
    assert not decision.suitable
    assert chosen is wrong_endpoint
    assert decision.failed_dimensions == ("endpoint",)


def test_no_syntheses_at_all_is_a_structured_answer():
    chosen, decision = best_of([], _request())
    assert chosen is None
    assert decision.status == statuses.PUBLISHED_SYNTHESIS_UNSUITABLE
    assert not decision.suitable


@pytest.mark.parametrize("status", [statuses.PUBLISHED_SYNTHESIS_UNSUITABLE])
def test_the_new_status_is_registered_as_a_gap(status):
    """A gap is a finding, not a failure — it must be in GAP_STATUSES to be treated as one."""
    assert statuses.is_gap(status)
    assert not statuses.is_releasable(status)
    assert statuses.describe(status) != "Unknown comparison status."
