"""Phase 6 — the evidence hierarchy resolver.

The tests that carry the phase:

* **L1 beats L3 even when the network is richer.** A head-to-head trial answers by
  randomisation; an NMA answers by assumption.
* **L2 beats L3.** A published synthesis outranks anything we would compute.
* **Every path ends at a named status**, including every failure.
* **Falling through is recorded.** ``considered`` explains why the answer sits where it does.
* **A sidecar outage is a service status, never an evidence gap.**
* **Exploratory results are not releasable.**
"""
from __future__ import annotations

import math
from datetime import date

import pytest

from app.evidence import resolver, statuses
from app.evidence.engines import netmeta
from app.evidence.engines.pairwise import StudyContrast
from app.evidence.resolver import ComparisonRequest, EvidenceSet
from app.evidence.sources import published_nma as pn

PROTOCOL = "PSA_ACR50_W16_PRIMARY"
RULE = "NETMETA_IF_LOOPS_OR_MULTI_ARM_ELSE_BUCHER"


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


def _rr(study, a, b, rr, variance=0.04) -> StudyContrast:
    return StudyContrast(study, a, b, math.log(rr), variance, "risk_ratio")


def _synthesis(**overrides) -> pn.ParsedSynthesis:
    record = {
        "source_type": "COCHRANE",
        "citation": "Cochrane Database Syst Rev. 2024;3:CD013967.",
        "publication_date": "2024-03-01",
        "indication": "Psoriatic Arthritis",
        "endpoint": "ACR50",
        "timepoint_week": 16,
        "effect_measure": "RR",
        "model_type": "random-effects",
        "interval_type": "95% CI",
        "league_table": {"Upadacitinib 15 mg": {"Adalimumab 40 mg": "1.40 (1.10 to 1.80)"}},
        "included_studies": ["NCT03104400"],
    }
    record.update(overrides)
    return pn.parse(record)


# A star network: both drugs against placebo, no head-to-head, no loops.
STAR = EvidenceSet(
    contrasts=(
        _rr("S1", "Rinvoq", "Placebo", 2.0),
        _rr("S2", "Humira", "Placebo", 1.5),
    ),
    study_arms={"S1": frozenset({"Rinvoq", "Placebo"}), "S2": frozenset({"Humira", "Placebo"})},
    administration_routes={"Rinvoq": "ORAL", "Humira": "SUBCUTANEOUS", "Placebo": "ORAL"},
)


# =====================================================================================
# Level 1 — direct evidence wins
# =====================================================================================
def test_direct_evidence_answers_at_level_one():
    evidence = EvidenceSet(
        contrasts=(_rr("HEAD2HEAD", "Rinvoq", "Humira", 1.3),),
        study_arms={"HEAD2HEAD": frozenset({"Rinvoq", "Humira"})},
    )
    answer = resolver.resolve(evidence, _request(), model_selection_rule=RULE)

    assert answer.status == statuses.DIRECT_EVIDENCE_AVAILABLE
    assert answer.evidence_level == resolver.LEVEL_DIRECT
    assert answer.estimate == pytest.approx(1.3)
    assert answer.is_releasable
    assert not answer.is_internal_output


def test_direct_evidence_wins_even_when_the_network_could_answer_too():
    """A head-to-head trial answers by randomisation; an NMA answers by assumption."""
    evidence = EvidenceSet(
        contrasts=(
            _rr("HEAD2HEAD", "Rinvoq", "Humira", 1.3),
            _rr("S1", "Rinvoq", "Placebo", 2.0),
            _rr("S2", "Humira", "Placebo", 1.5),
        ),
        study_arms={
            "HEAD2HEAD": frozenset({"Rinvoq", "Humira"}),
            "S1": frozenset({"Rinvoq", "Placebo"}),
            "S2": frozenset({"Humira", "Placebo"}),
        },
        syntheses=(_synthesis(),),
    )
    answer = resolver.resolve(evidence, _request(), model_selection_rule=RULE)
    assert answer.evidence_level == resolver.LEVEL_DIRECT
    assert answer.anchor is None
    assert answer.engine is None


def test_several_head_to_head_trials_are_pooled():
    evidence = EvidenceSet(
        contrasts=(
            _rr("T1", "Rinvoq", "Humira", 1.3),
            _rr("T2", "Rinvoq", "Humira", 1.3),
        ),
        study_arms={
            "T1": frozenset({"Rinvoq", "Humira"}), "T2": frozenset({"Rinvoq", "Humira"})
        },
    )
    answer = resolver.resolve(evidence, _request(), model_selection_rule=RULE)
    assert answer.heterogeneity["study_count"] == 2
    assert "2 head-to-head trials" in answer.reason


def test_direct_evidence_the_wrong_endpoint_falls_through_but_is_reported():
    """The trial exists and a reviewer should know, so the refusal is not silent."""
    evidence = EvidenceSet(
        contrasts=STAR.contrasts,
        study_arms=STAR.study_arms,
        unsuitable_direct=(("HEAD2HEAD", "reports ACR20, not ACR50"),),
    )
    answer = resolver.resolve(evidence, _request(), model_selection_rule=RULE)

    assert answer.evidence_level == resolver.LEVEL_COMPUTED
    level_one = next(a for a in answer.considered if a.level == resolver.LEVEL_DIRECT)
    assert not level_one.succeeded
    assert level_one.status == statuses.DIRECT_EVIDENCE_UNSUITABLE
    assert "ACR20" in level_one.reason


# =====================================================================================
# Level 2 — a published synthesis outranks computation
# =====================================================================================
def test_a_suitable_published_synthesis_answers_at_level_two():
    evidence = EvidenceSet(
        contrasts=STAR.contrasts, study_arms=STAR.study_arms, syntheses=(_synthesis(),)
    )
    answer = resolver.resolve(evidence, _request(), model_selection_rule=RULE)

    assert answer.status == statuses.PUBLISHED_RESULT_AVAILABLE
    assert answer.evidence_level == resolver.LEVEL_PUBLISHED
    assert answer.estimate == pytest.approx(1.40)
    assert answer.citation.startswith("Cochrane")
    assert answer.engine is None
    assert not answer.is_internal_output


def test_an_unsuitable_published_synthesis_falls_through_with_its_reason():
    """The Cochrane review reports ACR20; the question asked ACR50."""
    evidence = EvidenceSet(
        contrasts=STAR.contrasts,
        study_arms=STAR.study_arms,
        syntheses=(_synthesis(endpoint="ACR20", canonical_outcome_id="PSA_ACR20_W16"),),
    )
    answer = resolver.resolve(evidence, _request(), model_selection_rule=RULE)

    assert answer.evidence_level == resolver.LEVEL_COMPUTED
    level_two = next(a for a in answer.considered if a.level == resolver.LEVEL_PUBLISHED)
    assert level_two.status == statuses.ENDPOINT_MISMATCH
    assert not level_two.succeeded


def test_no_published_synthesis_is_recorded_as_considered():
    answer = resolver.resolve(STAR, _request(), model_selection_rule=RULE)
    level_two = next(a for a in answer.considered if a.level == resolver.LEVEL_PUBLISHED)
    assert level_two.status == statuses.PUBLISHED_SYNTHESIS_UNSUITABLE
    assert "No published synthesis" in level_two.reason


# =====================================================================================
# Level 3 — Bucher
# =====================================================================================
def test_a_star_network_is_answered_by_bucher():
    answer = resolver.resolve(STAR, _request(), model_selection_rule=RULE)

    assert answer.evidence_level == resolver.LEVEL_COMPUTED
    assert answer.engine == "BUCHER"
    assert answer.engine_status == statuses.BUCHER_ITC_COMPLETED
    assert answer.anchor == "Placebo"
    assert answer.estimate == pytest.approx(2.0 / 1.5)
    assert answer.is_internal_output


def test_the_indirect_answer_names_its_anchor_in_the_reason():
    answer = resolver.resolve(STAR, _request(), model_selection_rule=RULE)
    assert "anchored on Placebo" in answer.reason
    assert "No trial randomised Rinvoq against Humira" in answer.reason


def test_a_route_mixed_anchor_is_disclosed_on_the_answer():
    """Rinvoq is oral and Humira is injectable; the placebo node is not one thing."""
    answer = resolver.resolve(STAR, _request(), model_selection_rule=RULE)
    assert "ANCHOR_MIXES_ADMINISTRATION_ROUTES" in answer.flags


def test_the_protocol_can_force_netmeta_on_a_star_network():
    """Engine choice is the protocol's, not the topology's alone."""
    response = netmeta.parse_response({
        "effect_measure": "risk_ratio", "model": "random", "package_version": "netmeta 2.8-2",
        "contrasts": [{"treatment": "Rinvoq", "comparator": "Humira",
                       "estimate": 1.33, "ci_lower": 1.05, "ci_upper": 1.69}],
    })
    answer = resolver.resolve(
        STAR, _request(), model_selection_rule="ALWAYS_NETMETA", netmeta_response=response
    )
    assert answer.engine == netmeta.ENGINE
    assert answer.engine_status == statuses.INTERNAL_NMA_COMPLETED
    assert answer.estimate == pytest.approx(1.33)


def test_a_closed_loop_routes_to_netmeta():
    """Bucher cannot reconcile a loop's competing paths."""
    looped = EvidenceSet(
        contrasts=(
            _rr("S1", "Rinvoq", "Placebo", 2.0),
            _rr("S2", "Humira", "Placebo", 1.5),
            _rr("S3", "Rinvoq", "Humira", 1.3),
        ),
        study_arms={
            "S1": frozenset({"Rinvoq", "Placebo"}),
            "S2": frozenset({"Humira", "Placebo"}),
            "S3": frozenset({"Rinvoq", "Humira"}),
        },
    )
    assert looped.topology().has_closed_loops
    # Direct evidence exists here, so scope it out to reach Level 3 and observe the engine.
    graph_only = EvidenceSet(
        contrasts=tuple(c for c in looped.contrasts if c.study_id != "S3"),
        study_arms=looped.study_arms,
        unsuitable_direct=(("S3", "week 12, outside the approved window"),),
    )
    assert netmeta.select_engine(RULE, graph_only.topology()) == netmeta.ENGINE


def test_a_multi_arm_study_routes_to_netmeta():
    """Bucher cannot represent within-study correlation."""
    three_arm = EvidenceSet(
        study_arms={"MULTI": frozenset({"Rinvoq", "Humira", "Placebo"})}
    )
    assert netmeta.select_engine(RULE, three_arm.topology()) == netmeta.ENGINE


# =====================================================================================
# The sidecar is a service, not an evidence gap
# =====================================================================================
def test_a_sidecar_outage_is_reported_as_a_service_status():
    """An infrastructure blip must never masquerade as a finding about the evidence."""
    answer = resolver.resolve(
        STAR, _request(),
        model_selection_rule="ALWAYS_NETMETA",
        netmeta_response=netmeta.NetmetaResponse.unavailable("connection refused"),
    )
    assert answer.status == statuses.NMA_SERVICE_UNAVAILABLE
    assert answer.status not in statuses.GAP_STATUSES
    assert not statuses.is_gap(answer.status)
    assert "connection refused" in answer.reason


def test_netmeta_required_but_never_consulted_is_also_a_service_status():
    answer = resolver.resolve(
        STAR, _request(), model_selection_rule="ALWAYS_NETMETA", netmeta_response=None
    )
    assert answer.status == statuses.NMA_SERVICE_UNAVAILABLE
    assert "not consulted" in answer.reason


# =====================================================================================
# Governance
# =====================================================================================
def test_an_ungoverned_computation_is_exploratory_and_not_releasable():
    answer = resolver.resolve(
        STAR, _request(), model_selection_rule=RULE, may_compute_governed=False
    )
    assert answer.status == statuses.EXPLORATORY_RESULT_COMPLETED
    assert answer.is_success
    assert not answer.is_releasable


def test_a_governed_computation_is_releasable():
    answer = resolver.resolve(
        STAR, _request(), model_selection_rule=RULE, may_compute_governed=True
    )
    assert answer.status == statuses.GOVERNED_SYNTHESIS_COMPLETED
    assert answer.is_releasable


def test_the_engine_status_is_carried_separately_from_the_governance_status():
    """Using the engine status as the headline would make an ungoverned result look releasable."""
    answer = resolver.resolve(
        STAR, _request(), model_selection_rule=RULE, may_compute_governed=False
    )
    assert answer.status == statuses.EXPLORATORY_RESULT_COMPLETED
    assert answer.engine_status == statuses.BUCHER_ITC_COMPLETED
    assert statuses.is_releasable(answer.engine_status)
    assert not answer.is_releasable


# =====================================================================================
# Level 4 — structured gaps
# =====================================================================================
def test_a_disconnected_network_is_a_named_gap():
    disconnected = EvidenceSet(
        contrasts=(
            _rr("S1", "Rinvoq", "Placebo", 2.0),
            _rr("S2", "Humira", "MTX", 1.5),
        ),
        study_arms={
            "S1": frozenset({"Rinvoq", "Placebo"}), "S2": frozenset({"Humira", "MTX"})
        },
    )
    answer = resolver.resolve(disconnected, _request(), model_selection_rule=RULE)
    assert answer.status == statuses.NETWORK_DISCONNECTED
    assert answer.evidence_level == resolver.LEVEL_GAP
    assert not answer.is_success
    assert answer.estimate is None


def test_a_treatment_absent_from_every_trial_is_reported_as_such():
    absent = EvidenceSet(
        contrasts=(_rr("S1", "Rinvoq", "Placebo", 2.0),),
        study_arms={"S1": frozenset({"Rinvoq", "Placebo"})},
    )
    answer = resolver.resolve(absent, _request(), model_selection_rule=RULE)
    assert answer.status == statuses.NETWORK_DISCONNECTED
    assert "Humira does not appear" in answer.reason


def test_an_excluded_treatment_is_not_reported_as_absent():
    """The Rinvoq-vs-Tremfya case: the trial exists and this analysis refused its rows.

    ``unsuitable_direct`` cannot carry this. The service only fills it for a study holding
    BOTH requested treatments, and nothing randomised Rinvoq against Tremfya — so the
    week-12 exclusion had nowhere to go and the gap claimed **nobody had studied Rinvoq**.
    That is a different finding from the true one, and it is the one that sends a reader
    hunting for a trial already in the corpus.
    """
    excluded = EvidenceSet(
        contrasts=(_rr("DISCOVER-1", "Tremfya", "Placebo", 1.5),),
        study_arms={"DISCOVER-1": frozenset({"Tremfya", "Placebo"})},
        excluded_nodes={
            "Rinvoq": (
                ("NCT03104400", "reports week 12, outside the approved window [14, 18]"),
            )
        },
    )
    answer = resolver.resolve(
        excluded, _request(treatment_b="Tremfya"), model_selection_rule=RULE
    )

    assert answer.status == statuses.NETWORK_DISCONNECTED
    assert answer.evidence_level == resolver.LEVEL_GAP
    assert "NCT03104400" in answer.reason
    assert "week 12" in answer.reason
    # The false claim must be gone, not merely accompanied by the true one.
    assert "does not appear in any scoped trial" not in answer.reason


def test_a_route_restricted_subnetwork_does_not_borrow_a_scoping_reason():
    """Restricting to one route drops a node for its ROUTE, not for a timepoint.

    Carrying ``excluded_nodes`` into the subnetwork would explain a route restriction with a
    week number — the same class of false disclosure the field exists to prevent.
    """
    evidence = EvidenceSet(
        contrasts=STAR.contrasts,
        study_arms=STAR.study_arms,
        administration_routes=STAR.administration_routes,
        excluded_nodes={"Rinvoq": (("NCT03104400", "reports week 12, outside [14, 18]"),)},
    )
    assert dict(resolver.restrict_to_route(evidence, "SUBCUTANEOUS").excluded_nodes) == {}


def test_an_empty_evidence_set_still_produces_a_named_status():
    """No answer is a legitimate product output; None is not."""
    answer = resolver.resolve(EvidenceSet(), _request(), model_selection_rule=RULE)
    assert answer.status in statuses.ALL_STATUSES
    assert answer.evidence_level == resolver.LEVEL_GAP
    assert statuses.describe(answer.status) != "Unknown comparison status."


def test_missing_arm_data_is_distinguished_from_a_disconnected_network():
    evidence = EvidenceSet(
        contrasts=(),
        study_arms=STAR.study_arms,
        insufficient_data=(("S1", "no denominator posted for the Rinvoq arm"),),
    )
    answer = resolver.resolve(evidence, _request(), model_selection_rule=RULE)
    assert answer.status == statuses.INSUFFICIENT_ARM_DATA
    assert "no denominator" in answer.reason


def test_a_published_synthesis_reason_survives_into_the_gap():
    """When nothing can answer, the paper that nearly did is still named."""
    evidence = EvidenceSet(
        study_arms={"S1": frozenset({"Rinvoq", "Placebo"}), "S2": frozenset({"Humira", "MTX"})},
        contrasts=(_rr("S1", "Rinvoq", "Placebo", 2.0), _rr("S2", "Humira", "MTX", 1.5)),
        syntheses=(_synthesis(endpoint="ACR20", canonical_outcome_id="PSA_ACR20_W16"),),
    )
    answer = resolver.resolve(evidence, _request(), model_selection_rule=RULE)
    # The disconnection is the more fundamental obstruction and is reported first...
    assert answer.status == statuses.NETWORK_DISCONNECTED
    # ...but the published attempt is still on the record.
    level_two = next(a for a in answer.considered if a.level == resolver.LEVEL_PUBLISHED)
    assert level_two.status == statuses.ENDPOINT_MISMATCH


# =====================================================================================
# The audit trail of the walk
# =====================================================================================
def test_every_level_tried_is_recorded_in_order():
    answer = resolver.resolve(STAR, _request(), model_selection_rule=RULE)
    levels = [a.level for a in answer.considered]
    assert levels == [resolver.LEVEL_DIRECT, resolver.LEVEL_PUBLISHED, resolver.LEVEL_COMPUTED]
    assert [a.succeeded for a in answer.considered] == [False, False, True]


def test_the_answer_serialises_with_its_full_provenance():
    payload = resolver.resolve(STAR, _request(), model_selection_rule=RULE).as_dict()
    assert payload["evidence_level"] == resolver.LEVEL_COMPUTED
    assert payload["engine"] == "BUCHER"
    assert payload["anchor"] == "Placebo"
    assert payload["is_internal_output"] is True
    assert payload["is_releasable"] is False
    assert len(payload["considered"]) == 3
    assert payload["describes"]


def test_a_level_one_answer_records_only_the_level_it_stopped_at():
    evidence = EvidenceSet(
        contrasts=(_rr("HEAD2HEAD", "Rinvoq", "Humira", 1.3),),
        study_arms={"HEAD2HEAD": frozenset({"Rinvoq", "Humira"})},
    )
    answer = resolver.resolve(evidence, _request(), model_selection_rule=RULE)
    assert [a.level for a in answer.considered] == [resolver.LEVEL_DIRECT]


# =====================================================================================
# Sidecar contract, pure parts
# =====================================================================================
def test_the_request_keeps_multi_arm_studies_whole():
    """Flattening a three-arm trial double-counts its control group."""
    request = netmeta.build_request(
        {
            "MULTI": [
                netmeta.ArmPayload("Rinvoq", sample_size=100, events=45),
                netmeta.ArmPayload("Humira", sample_size=100, events=35),
                netmeta.ArmPayload("Placebo", sample_size=100, events=20),
            ]
        },
        outcome_type="binary", effect_measure="risk_ratio",
    )
    assert request.multi_arm_studies == ("MULTI",)
    payload = request.as_dict()
    assert len(payload["studies"][0]["arms"]) == 3


def test_a_single_arm_study_contributes_no_comparison():
    request = netmeta.build_request(
        {"SOLO": [netmeta.ArmPayload("Rinvoq", sample_size=100, events=45)]},
        outcome_type="binary", effect_measure="risk_ratio",
    )
    assert request.studies == ()


def test_a_reversed_league_table_cell_inverts_correctly_on_a_ratio_scale():
    """Getting the bound swap wrong yields an interval excluding its own estimate."""
    response = netmeta.parse_response({
        "effect_measure": "risk_ratio", "package_version": "netmeta 2.8-2",
        "contrasts": [{"treatment": "Rinvoq", "comparator": "Humira",
                       "estimate": 2.0, "ci_lower": 1.5, "ci_upper": 2.5}],
    })
    flipped = response.contrast_for("Humira", "Rinvoq")
    assert flipped.estimate == pytest.approx(0.5)
    assert flipped.ci_lower == pytest.approx(1 / 2.5)
    assert flipped.ci_upper == pytest.approx(1 / 1.5)
    assert flipped.ci_lower < flipped.estimate < flipped.ci_upper


def test_a_reversed_cell_negates_on_a_difference_scale():
    response = netmeta.parse_response({
        "effect_measure": "mean_difference", "package_version": "netmeta 2.8-2",
        "contrasts": [{"treatment": "A", "comparator": "B",
                       "estimate": 3.0, "ci_lower": 1.0, "ci_upper": 5.0}],
    })
    flipped = response.contrast_for("B", "A")
    assert flipped.estimate == pytest.approx(-3.0)
    assert flipped.ci_lower == pytest.approx(-5.0)
    assert flipped.ci_upper == pytest.approx(-1.0)


def test_an_empty_league_table_is_unavailable_not_a_null_result():
    """An NMA returning no contrasts did not succeed; reporting it as empty reads as 'no difference'."""
    response = netmeta.parse_response({"effect_measure": "risk_ratio", "contrasts": []})
    assert not response.ok
    assert response.status == statuses.NMA_SERVICE_UNAVAILABLE


def test_inconsistency_is_discarded_when_no_independent_loop_exists():
    """Within-study loops are correlated and prove nothing about inconsistency."""
    response = netmeta.parse_response(
        {
            "effect_measure": "risk_ratio", "package_version": "netmeta 2.8-2",
            "contrasts": [{"treatment": "A", "comparator": "B", "estimate": 1.2}],
            "inconsistency": {"design_by_treatment_p": 0.03},
        },
        independent_loop_count=0,
    )
    assert response.inconsistency is None
    assert netmeta.FLAG_INCONSISTENCY_NOT_ASSESSABLE in response.flags


def test_inconsistency_is_kept_when_the_network_can_support_it():
    response = netmeta.parse_response(
        {
            "effect_measure": "risk_ratio", "package_version": "netmeta 2.8-2",
            "contrasts": [{"treatment": "A", "comparator": "B", "estimate": 1.2}],
            "inconsistency": {"design_by_treatment_p": 0.03},
        },
        independent_loop_count=2,
    )
    assert response.inconsistency == {"design_by_treatment_p": 0.03}
    assert netmeta.FLAG_INCONSISTENCY_NOT_ASSESSABLE not in response.flags


def test_a_result_without_a_package_version_is_flagged_as_degraded():
    """Without it a statistical reviewer cannot reproduce the run."""
    response = netmeta.parse_response(
        {"effect_measure": "risk_ratio",
         "contrasts": [{"treatment": "A", "comparator": "B", "estimate": 1.2}]},
        independent_loop_count=1,
    )
    assert response.ok
    assert netmeta.FLAG_SIDECAR_DEGRADED in response.flags


def test_sucra_returned_as_a_percentage_is_rescaled():
    response = netmeta.parse_response(
        {"effect_measure": "risk_ratio", "package_version": "netmeta 2.8-2",
         "contrasts": [{"treatment": "A", "comparator": "B", "estimate": 1.2}],
         "sucra": {"A": 85.2, "B": 60.1}},
        independent_loop_count=1,
    )
    assert response.sucra == {"A": pytest.approx(0.852), "B": pytest.approx(0.601)}


def test_a_non_object_body_is_unavailable():
    assert not netmeta.parse_response("<html>502 Bad Gateway</html>").ok
    assert not netmeta.parse_response(None).ok


def test_an_invalid_model_is_refused_when_building_a_request():
    with pytest.raises(ValueError, match="model must be"):
        netmeta.build_request(
            {"S1": [netmeta.ArmPayload("A", 100, 40), netmeta.ArmPayload("B", 100, 20)]},
            outcome_type="binary", effect_measure="risk_ratio", model="bayesian",
        )
