"""Network topology — shared by the Phase 0 audit and Phase 6 method selection.

The distinction under most scrutiny here is `loop_count` vs `independent_loop_count`.
A three-arm trial forms a graph triangle, but its three comparisons share a control group
and are correlated, so that triangle is NOT independent evidence and cannot be used to
assess inconsistency. Conflating the two would let the platform claim it had tested
consistency when it had only counted a shape.
"""
from __future__ import annotations

from app.evidence import topology


# =====================================================================================
# Connectivity
# =====================================================================================
def test_a_star_network_is_connected_through_placebo():
    t = topology.build({
        "S1": ["Rinvoq", "Placebo"],
        "S2": ["Tremfya", "Placebo"],
        "S3": ["Skyrizi", "Placebo"],
    })
    assert t.is_connected
    assert t.nodes == ("Placebo", "Rinvoq", "Skyrizi", "Tremfya")
    assert t.are_connected("Rinvoq", "Tremfya")


def test_a_disconnected_network_is_reported_not_bridged():
    """The precondition for any indirect estimate. False means a structured gap."""
    t = topology.build({
        "S1": ["Rinvoq", "Placebo"],
        "S2": ["DrugX", "DrugY"],
    })
    assert not t.is_connected
    assert len(t.components) == 2
    assert not t.are_connected("Rinvoq", "DrugX")


def test_shortest_path_is_returned_because_each_hop_compounds_transitivity():
    t = topology.build({
        "S1": ["Rinvoq", "Placebo"],
        "S2": ["Tremfya", "Placebo"],
        "S3": ["Tremfya", "Cosentyx"],
    })
    assert t.path("Rinvoq", "Cosentyx") == ("Rinvoq", "Placebo", "Tremfya", "Cosentyx")
    assert t.path("Rinvoq", "Placebo") == ("Rinvoq", "Placebo")


def test_path_is_empty_when_disconnected():
    t = topology.build({"S1": ["A", "B"], "S2": ["C", "D"]})
    assert t.path("A", "C") == ()


def test_an_unknown_treatment_is_not_connected_to_anything():
    t = topology.build({"S1": ["A", "B"]})
    assert not t.are_connected("A", "Nonexistent")
    assert t.path("A", "Nonexistent") == ()


# =====================================================================================
# Shared comparators — what an indirect comparison runs through
# =====================================================================================
def test_shared_comparators_are_the_anchors_for_an_indirect_comparison():
    t = topology.build({
        "S1": ["Rinvoq", "Placebo"],
        "S2": ["Tremfya", "Placebo"],
        "S3": ["Rinvoq", "Humira"],
        "S4": ["Tremfya", "Humira"],
    })
    assert t.shared_comparators("Rinvoq", "Tremfya") == ("Humira", "Placebo")


def test_no_shared_comparator_means_not_estimable_even_with_good_trials():
    """Phase 0 audits this per indication for exactly this reason."""
    t = topology.build({
        "S1": ["Rinvoq", "Placebo"],
        "S2": ["Tremfya", "Humira"],
        "S3": ["Humira", "Placebo"],
    })
    assert t.shared_comparators("Rinvoq", "Tremfya") == ()
    assert t.are_connected("Rinvoq", "Tremfya")  # connected, but only at two hops


def test_direct_evidence_is_detected():
    t = topology.build({"S1": ["Rinvoq", "Humira", "Placebo"]})
    assert t.has_direct_evidence("Rinvoq", "Humira")
    assert not t.has_direct_evidence("Rinvoq", "Cosentyx")


# =====================================================================================
# Loops — the distinction that matters
# =====================================================================================
def test_a_three_arm_trial_forms_a_triangle_that_is_not_independent_evidence():
    """SELECT-PsA 1 shape: one study, three arms, three correlated comparisons."""
    t = topology.build({"S1": ["Rinvoq", "Humira", "Placebo"]})
    assert t.loop_count == 1          # the graph has a cycle
    assert t.independent_loop_count == 0  # …but it cannot test inconsistency
    assert t.has_multi_arm_studies


def test_a_loop_built_from_separate_studies_is_independent():
    t = topology.build({
        "S1": ["Rinvoq", "Placebo"],
        "S2": ["Tremfya", "Placebo"],
        "S3": ["Rinvoq", "Tremfya"],
    })
    assert t.loop_count == 1
    assert t.independent_loop_count == 1
    assert not t.has_multi_arm_studies


def test_a_star_network_has_no_loops():
    t = topology.build({
        "S1": ["Rinvoq", "Placebo"],
        "S2": ["Tremfya", "Placebo"],
    })
    assert t.loop_count == 0
    assert not t.has_closed_loops


def test_a_comparison_replicated_across_studies_is_one_edge():
    t = topology.build({
        "S1": ["Rinvoq", "Placebo"],
        "S2": ["Rinvoq", "Placebo"],
    })
    assert t.edges == (("Placebo", "Rinvoq", 2),)
    assert t.loop_count == 0


# =====================================================================================
# Engine selection input
# =====================================================================================
def test_a_simple_star_is_the_only_bucher_eligible_shape():
    t = topology.build({
        "S1": ["Rinvoq", "Placebo"],
        "S2": ["Tremfya", "Placebo"],
        "S3": ["Skyrizi", "Placebo"],
    })
    assert t.is_simple_star


def test_a_multi_arm_trial_disqualifies_the_simple_star():
    t = topology.build({
        "S1": ["Rinvoq", "Humira", "Placebo"],
        "S2": ["Tremfya", "Placebo"],
    })
    assert not t.is_simple_star


def test_a_closed_loop_disqualifies_the_simple_star():
    t = topology.build({
        "S1": ["Rinvoq", "Placebo"],
        "S2": ["Tremfya", "Placebo"],
        "S3": ["Rinvoq", "Tremfya"],
    })
    assert not t.is_simple_star


def test_a_disconnected_network_is_not_a_star():
    t = topology.build({"S1": ["A", "B"], "S2": ["C", "D"]})
    assert not t.is_simple_star


def test_two_treatments_alone_are_not_a_star():
    """A single head-to-head is direct evidence, not a network to run Bucher on."""
    assert not topology.build({"S1": ["Rinvoq", "Humira"]}).is_simple_star


# =====================================================================================
# Construction edge cases
# =====================================================================================
def test_a_single_arm_study_contributes_no_edge():
    t = topology.build({"S1": ["Rinvoq"], "S2": ["Rinvoq", "Placebo"]})
    assert t.edges == (("Placebo", "Rinvoq", 1),)
    assert "S1" not in t.study_arms


def test_duplicate_treatments_in_one_study_collapse_to_one_node():
    """Two pooled doses are one node; a self-comparison is not an edge."""
    t = topology.build({"S1": ["Rinvoq", "Rinvoq", "Placebo"]})
    assert t.nodes == ("Placebo", "Rinvoq")
    assert not t.has_multi_arm_studies


def test_an_empty_network_is_not_connected():
    t = topology.build({})
    assert not t.is_connected
    assert t.loop_count == 0
    assert t.summary()["node_count"] == 0


def test_summary_is_flat_and_serialisable():
    t = topology.build({
        "S1": ["Rinvoq", "Humira", "Placebo"],
        "S2": ["Tremfya", "Placebo"],
    })
    summary = t.summary()
    assert summary["node_count"] == 4
    assert summary["is_connected"] is True
    assert summary["has_multi_arm_studies"] is True
    assert summary["multi_arm_studies"] == ["S1"]
    assert summary["independent_loop_count"] == 0
