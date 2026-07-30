"""Phase 0 audit logic. Pure; no network.

The first test is the regression for the defect that made the first live run unusable:
the audit reported `Connected: no` for Psoriatic Arthritis while simultaneously reporting
all six drug pairs as DIRECT, because it was measuring the whole swept graph instead of
the drugs in scope.
"""
from __future__ import annotations

import pytest

from app.evidence import topology
from scripts.evidence_coverage_audit import IndicationAudit


def _audit(study_arms: dict[str, list[str]], **kwargs) -> IndicationAudit:
    audit = IndicationAudit(indication="Psoriatic Arthritis", **kwargs)
    audit.network = topology.build(study_arms)
    return audit


# =====================================================================================
# Focus-drug connectivity — the defect
# =====================================================================================
def test_unrelated_islands_do_not_make_the_focus_network_disconnected():
    """The regression. A search sweeps in trials of agents nobody asked about."""
    audit = _audit({
        "S1": ["Rinvoq", "Humira", "Placebo"],
        "S2": ["Skyrizi", "Placebo"],
        "S3": ["Tremfya", "Placebo"],
        # An unrelated island, exactly what an indication-wide search pulls in.
        "S4": ["SomeOtherDrug", "AnotherDrug"],
    }, arm_level_available=3)

    assert not audit.network.is_connected      # whole graph: two components
    assert audit.focus_network_connected       # …but the drugs in scope are connected
    assert audit.level3_feasible


def test_a_genuinely_split_focus_network_is_reported():
    audit = _audit({
        "S1": ["Rinvoq", "Placebo"],
        "S2": ["Tremfya", "SomeComparator"],
    }, arm_level_available=2)
    assert not audit.focus_network_connected
    assert not audit.level3_feasible


def test_one_focus_drug_alone_is_not_a_comparison():
    audit = _audit({"S1": ["Rinvoq", "Placebo"]}, arm_level_available=2)
    assert audit.focus_drugs_present == ("Rinvoq",)
    assert not audit.focus_network_connected


def test_connectivity_without_arm_level_data_is_still_not_feasible():
    """Connected is necessary but not sufficient — Level 3 needs numbers to compute on."""
    audit = _audit({
        "S1": ["Rinvoq", "Placebo"],
        "S2": ["Tremfya", "Placebo"],
    }, arm_level_available=1)
    assert audit.focus_network_connected
    assert not audit.level3_feasible


def test_an_empty_network_is_not_feasible():
    audit = _audit({})
    assert audit.focus_drugs_present == ()
    assert not audit.level3_feasible
    assert audit.catalog_coverage is None


# =====================================================================================
# Catalog coverage — makes node inflation visible
# =====================================================================================
def test_catalog_coverage_reports_the_share_of_curated_nodes():
    audit = _audit({"S1": ["Rinvoq", "Placebo", "Unknown Agent", "Another Unknown"]})
    audit.curated_nodes = 2  # Rinvoq + Placebo
    assert audit.catalog_coverage == 50.0


# =====================================================================================
# Treatment phase
# =====================================================================================
def test_phase_is_separable_only_when_both_phases_are_present():
    audit = _audit({}, phase_counts={"INDUCTION": 3, "MAINTENANCE": 2})
    assert audit.phase_separable


def test_primary_only_is_not_separable():
    """What the first run actually reported for UC and Crohn's — but from a column default."""
    audit = _audit({}, phase_counts={"PRIMARY": 12})
    assert not audit.phase_separable


def test_induction_without_maintenance_is_not_separable():
    audit = _audit({}, phase_counts={"INDUCTION": 5, "PRIMARY": 2})
    assert not audit.phase_separable


# =====================================================================================
# Placebo response by route
# =====================================================================================
def test_route_spread_is_measured_within_one_canonical_outcome():
    """A PASI75 rate and an ACR20 rate are not comparable; averaging them invents a gap."""
    audit = _audit({}, placebo_rates={
        ("PSA_ACR20_W16", "ORAL"): [36.2, 33.0],
        ("PSA_ACR20_W16", "SC"): [35.1],
        # A different endpoint with a wildly different placebo rate must not contaminate.
        ("PSA_PASI90_W16", "SC"): [5.0],
    })
    spread = audit.placebo_route_spread()
    assert spread is not None
    outcome_id, points = spread
    assert outcome_id == "PSA_ACR20_W16"
    assert points == pytest.approx(abs(statistics_mean([36.2, 33.0]) - 35.1), abs=0.01)
    assert points < 5  # route-mixing defensible on this endpoint


def test_a_single_route_is_not_a_spread():
    audit = _audit({}, placebo_rates={("PSA_ACR20_W16", "ORAL"): [36.2]})
    assert audit.placebo_route_spread() is None


def test_no_mapped_placebo_results_means_not_measurable():
    audit = _audit({})
    assert audit.placebo_route_spread() is None


def test_the_largest_spread_across_outcomes_is_reported():
    audit = _audit({}, placebo_rates={
        ("A", "ORAL"): [10.0], ("A", "SC"): [12.0],
        ("B", "ORAL"): [10.0], ("B", "SC"): [30.0],
    })
    outcome_id, points = audit.placebo_route_spread()
    assert outcome_id == "B"
    assert points == pytest.approx(20.0)


def statistics_mean(values):
    return sum(values) / len(values)
