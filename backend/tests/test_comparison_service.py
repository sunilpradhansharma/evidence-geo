"""Phase 6 — comparison resolution end to end against a database.

Covers the wiring the pure resolver tests cannot: scoping from real rows, the governance
gate, persistence provenance, and the reasons a curator's trial was not used.

The tests that carry this layer:

* an out-of-window trial becomes ``unsuitable_direct``, not silence
* an unverified study cannot reach a computation
* GOVERNED without an approved protocol **downgrades** rather than raising
* a persisted result records its derived ``protocol_hash`` and is never citable
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.evidence import lifecycles, protocols, statuses
from app.models.clinical_study import (
    BINARY,
    CONTINUOUS,
    ClinicalStudy,
    OutcomeResult,
    StudyArm,
)
from app.models.database import Base
from app.models.evidence_network import EvidenceNetwork, NetworkMembership
from app.models.nma_result import COMPUTED, EXPLORATORY, GOVERNED, NMAResult
from app.services import comparison_service as svc

PROTOCOL = "PSA_ACR50_W16_PRIMARY"
OUTCOME = "PSA_ACR50_W16"
NETWORK = "NET-PSA-ACR50"


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from app.models import (  # noqa: F401 — register tables on Base.metadata
        analysis_protocol,
        audit_log,
        clinical_study,
        evidence_network,
        nma_result,
        source_payload,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _network(**overrides) -> EvidenceNetwork:
    defaults = {
        "network_id": NETWORK,
        "indication": "Psoriatic Arthritis",
        "canonical_outcome_id": OUTCOME,
        "population_stratum": None,
        "treatment_phase": "PRIMARY",
        "protocol_id": PROTOCOL,
        "ratification_status": lifecycles.DRAFT,
        "version": 1,
    }
    defaults.update(overrides)
    return EvidenceNetwork(**defaults)


async def _add_study(
    db,
    study_id: str,
    arms: list[tuple[str, int, int, str]],
    *,
    week: float = 16,
    outcome_id: str | None = OUTCOME,
    phase: str = "PRIMARY",
    verification: str = lifecycles.VERIFIED,
    stratum: str | None = None,
    include: bool = True,
) -> None:
    """One study with binary arm-level results. ``arms`` is (treatment, events, n, route)."""
    db.add(ClinicalStudy(
        study_id=study_id, indication="Psoriatic Arthritis", treatment_phase=phase,
        verification_status=verification, is_randomised=True, population_stratum=stratum,
    ))
    for index, (treatment, events, n, route) in enumerate(arms):
        arm_id = f"{study_id}:A{index}"
        db.add(StudyArm(
            arm_id=arm_id, study_id=study_id, treatment=treatment,
            is_placebo=treatment == "Placebo", administration_route=route, sample_size=n,
        ))
        db.add(OutcomeResult(
            result_id=f"{study_id}:R{index}", study_id=study_id, arm_id=arm_id,
            canonical_outcome_id=outcome_id, endpoint="ACR50", timepoint_week=week,
            treatment_phase=phase, population_stratum=stratum,
            outcome_type=BINARY, events=events, sample_size=n,
        ))
    if include:
        db.add(NetworkMembership(
            membership_id=f"M-{study_id}", network_id=NETWORK, study_id=study_id,
            protocol_id=PROTOCOL, membership_status=lifecycles.INCLUDED,
        ))
    await db.commit()


async def _star_network(db) -> None:
    """Rinvoq and Humira against placebo, no head-to-head."""
    db.add(_network())
    await db.commit()
    await _add_study(db, "S1", [("Rinvoq", 45, 100, "ORAL"), ("Placebo", 20, 100, "ORAL")])
    await _add_study(
        db, "S2", [("Humira", 35, 100, "SUBCUTANEOUS"), ("Placebo", 18, 100, "SUBCUTANEOUS")]
    )


# =====================================================================================
# Scoping
# =====================================================================================
async def test_a_star_network_resolves_by_bucher(db_session):
    await _star_network(db_session)
    answer = await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Rinvoq", treatment_b="Humira"
    )
    assert answer["evidence_level"] == 3
    assert answer["engine"] == "BUCHER"
    assert answer["anchor"] == "Placebo"
    assert answer["estimate"] > 1.0
    assert answer["scoping"]["included_study_count"] == 2


async def test_a_head_to_head_trial_resolves_at_level_one(db_session):
    db_session.add(_network())
    await db_session.commit()
    await _add_study(
        db_session, "H2H",
        [("Rinvoq", 45, 100, "ORAL"), ("Humira", 35, 100, "SUBCUTANEOUS")],
    )
    answer = await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Rinvoq", treatment_b="Humira"
    )
    assert answer["status"] == statuses.DIRECT_EVIDENCE_AVAILABLE
    assert answer["evidence_level"] == 1
    assert answer["contributing_studies"] == ["H2H"]


async def test_an_out_of_window_head_to_head_trial_is_reported_not_silently_dropped(db_session):
    """The protocol approved weeks 14-18; this trial reports week 12."""
    db_session.add(_network())
    await db_session.commit()
    await _add_study(
        db_session, "EARLY",
        [("Rinvoq", 45, 100, "ORAL"), ("Humira", 35, 100, "SUBCUTANEOUS")], week=12,
    )
    await _add_study(
        db_session, "S1", [("Rinvoq", 45, 100, "ORAL"), ("Placebo", 20, 100, "ORAL")]
    )
    await _add_study(
        db_session, "S2",
        [("Humira", 35, 100, "SUBCUTANEOUS"), ("Placebo", 18, 100, "SUBCUTANEOUS")],
    )

    answer = await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Rinvoq", treatment_b="Humira"
    )
    assert answer["evidence_level"] == 3
    level_one = next(a for a in answer["considered"] if a["level"] == 1)
    assert level_one["status"] == statuses.DIRECT_EVIDENCE_UNSUITABLE
    assert "week 12" in level_one["reason"]


async def test_a_different_endpoint_is_scoped_out(db_session):
    db_session.add(_network())
    await db_session.commit()
    await _add_study(
        db_session, "ACR20",
        [("Rinvoq", 60, 100, "ORAL"), ("Humira", 50, 100, "SUBCUTANEOUS")],
        outcome_id="PSA_ACR20_W16",
    )
    answer = await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Rinvoq", treatment_b="Humira"
    )
    assert answer["evidence_level"] == 4
    assert answer["scoping"]["included_study_count"] == 0


async def test_a_maintenance_result_cannot_enter_a_primary_network(db_session):
    """Maintenance populations are re-randomised induction responders."""
    db_session.add(_network())
    await db_session.commit()
    await _add_study(
        db_session, "MAINT",
        [("Rinvoq", 45, 100, "ORAL"), ("Humira", 35, 100, "SUBCUTANEOUS")],
        phase="MAINTENANCE",
    )
    answer = await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Rinvoq", treatment_b="Humira"
    )
    assert answer["evidence_level"] == 4
    level_one = next(a for a in answer["considered"] if a["level"] == 1)
    assert "MAINTENANCE" in level_one["reason"]


async def test_an_unverified_study_cannot_reach_a_computation(db_session):
    """Computing on unchecked extractions is not exploratory, it is wrong."""
    db_session.add(_network())
    await db_session.commit()
    await _add_study(
        db_session, "RAW",
        [("Rinvoq", 45, 100, "ORAL"), ("Humira", 35, 100, "SUBCUTANEOUS")],
        verification=lifecycles.EXTRACTED,
    )
    answer = await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Rinvoq", treatment_b="Humira"
    )
    assert answer["evidence_level"] == 4
    level_one = next(a for a in answer["considered"] if a["level"] == 1)
    assert "not been verified" in level_one["reason"]


async def test_a_study_excluded_from_the_network_is_not_used(db_session):
    """Membership is a per-analysis judgement, so exclusion here is decisive."""
    db_session.add(_network())
    await db_session.commit()
    await _add_study(
        db_session, "S1", [("Rinvoq", 45, 100, "ORAL"), ("Placebo", 20, 100, "ORAL")]
    )
    await _add_study(
        db_session, "OUT",
        [("Rinvoq", 90, 100, "ORAL"), ("Humira", 10, 100, "SUBCUTANEOUS")], include=False,
    )
    answer = await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Rinvoq", treatment_b="Humira"
    )
    level_one = next(a for a in answer["considered"] if a["level"] == 1)
    assert "not an included member" in level_one["reason"]


async def test_missing_arm_data_is_named_as_such(db_session):
    db_session.add(_network())
    await db_session.commit()
    db_session.add(ClinicalStudy(
        study_id="NODENOM", indication="Psoriatic Arthritis",
        verification_status=lifecycles.VERIFIED,
    ))
    for index, treatment in enumerate(("Rinvoq", "Humira")):
        db_session.add(StudyArm(
            arm_id=f"NODENOM:A{index}", study_id="NODENOM", treatment=treatment,
        ))
        db_session.add(OutcomeResult(
            result_id=f"NODENOM:R{index}", study_id="NODENOM", arm_id=f"NODENOM:A{index}",
            canonical_outcome_id=OUTCOME, endpoint="ACR50", timepoint_week=16,
            outcome_type=BINARY, events=40, sample_size=None,
        ))
    db_session.add(NetworkMembership(
        membership_id="M-NODENOM", network_id=NETWORK, study_id="NODENOM",
        protocol_id=PROTOCOL, membership_status=lifecycles.INCLUDED,
    ))
    await db_session.commit()

    answer = await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Rinvoq", treatment_b="Humira"
    )
    assert answer["status"] in (
        statuses.INSUFFICIENT_ARM_DATA, statuses.NETWORK_DISCONNECTED
    )
    assert answer["evidence_level"] == 4


# =====================================================================================
# Gaps
# =====================================================================================
async def test_a_disconnected_pair_is_a_named_gap_not_an_error(db_session):
    db_session.add(_network())
    await db_session.commit()
    await _add_study(
        db_session, "S1", [("Rinvoq", 45, 100, "ORAL"), ("Placebo", 20, 100, "ORAL")]
    )
    await _add_study(
        db_session, "S2", [("Tremfya", 40, 100, "SUBCUTANEOUS"), ("MTX", 20, 100, "ORAL")]
    )
    answer = await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Rinvoq", treatment_b="Tremfya"
    )
    assert answer["status"] == statuses.NETWORK_DISCONNECTED
    assert answer["evidence_level"] == 4
    assert answer["estimate"] is None
    assert answer["describes"]


async def _never_randomised_together(db) -> None:
    """The real Rinvoq-vs-Tremfya shape, including the noise a live corpus carries.

    SELECT-PsA 1 reports ACR50 at week 12 and the protocol approves 14-18, so Rinvoq
    contributes nothing. DISCOVER-1 is in window. No study holds both treatments.

    The two ACR20 studies are not padding. They are why a naive implementation misreports:
    one buries the week-12 finding under endpoint noise, and the other stranded Tremfya in
    ``excluded_nodes`` while Tremfya sat in the network via DISCOVER-1.
    """
    db.add(_network())
    await db.commit()
    await _add_study(
        db, "SELECT-PSA-1",
        [("Rinvoq", 45, 100, "ORAL"), ("Placebo", 20, 100, "ORAL")], week=12,
    )
    await _add_study(
        db, "DISCOVER-1",
        [("Tremfya", 35, 100, "SUBCUTANEOUS"), ("Placebo", 18, 100, "SUBCUTANEOUS")],
    )
    # Right drugs, wrong endpoint entirely — true of most rows in any real corpus.
    await _add_study(
        db, "RINVOQ-ACR20",
        [("Rinvoq", 55, 100, "ORAL"), ("Placebo", 25, 100, "ORAL")],
        outcome_id="PSA_ACR20_W16",
    )
    await _add_study(
        db, "TREMFYA-ACR20",
        [("Tremfya", 50, 100, "SUBCUTANEOUS"), ("Placebo", 22, 100, "SUBCUTANEOUS")],
        outcome_id="PSA_ACR20_W16",
    )


async def test_a_stranded_node_is_attributed_even_with_no_shared_trial(db_session):
    """``unsuitable_direct`` cannot hold this, which is why ``excluded_nodes`` exists.

    It is only filled for a study carrying BOTH requested treatments. For a pair never
    randomised together the week-12 refusal has nowhere to go, and the reason is lost.
    """
    await _never_randomised_together(db_session)
    network = await svc._network(db_session, NETWORK)
    request = svc.ComparisonRequest(
        indication="Psoriatic Arthritis", treatment_a="Rinvoq", treatment_b="Tremfya",
        canonical_outcome_id=OUTCOME, protocol_id=PROTOCOL,
    )
    evidence, report = await svc.gather_evidence(db_session, network, request)

    # The property that made the misstatement possible, pinned so it cannot regress silently.
    assert evidence.unsuitable_direct == ()
    # ...and the reason recovered anyway, attributed to the node it is about. Exactly one:
    # RINVOQ-ACR20 measures something else, which is not why Rinvoq is missing.
    rows = report["excluded_nodes"]["Rinvoq"]
    assert len(rows) == 1
    assert rows[0]["study_id"] == "SELECT-PSA-1"
    assert "week 12" in rows[0]["reason"]


async def test_a_node_that_reached_the_network_elsewhere_is_not_called_excluded(db_session):
    """TREMFYA-ACR20 refuses Tremfya, but DISCOVER-1 puts it in the graph.

    Reporting it as excluded would assert an exclusion the network itself contradicts — the
    failure this field exists to prevent, reintroduced one layer down.
    """
    await _never_randomised_together(db_session)
    network = await svc._network(db_session, NETWORK)
    request = svc.ComparisonRequest(
        indication="Psoriatic Arthritis", treatment_a="Rinvoq", treatment_b="Tremfya",
        canonical_outcome_id=OUTCOME, protocol_id=PROTOCOL,
    )
    _evidence, report = await svc.gather_evidence(db_session, network, request)

    assert "Tremfya" in {t for arms in _evidence.study_arms.values() for t in arms}
    assert "Tremfya" not in report["excluded_nodes"]


async def test_a_gap_names_the_excluded_trial_rather_than_claiming_absence(db_session):
    """"No trial studied Rinvoq" and "the protocol excluded the trial" are different
    findings. Only the second is true here, and it is the one that is actionable."""
    await _never_randomised_together(db_session)
    answer = await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Rinvoq", treatment_b="Tremfya"
    )

    assert answer["status"] == statuses.NETWORK_DISCONNECTED
    assert "SELECT-PSA-1" in answer["reason"]
    assert "week 12" in answer["reason"]
    assert "does not appear in any scoped trial" not in answer["reason"]
    # A refusal nobody can read is not a disclosure. The endpoint noise that would bury the
    # one actionable line must not reach the sentence a reviewer sees.
    assert "RINVOQ-ACR20" not in answer["reason"]
    assert "PSA_ACR20_W16" not in answer["reason"]
    assert len(answer["reason"]) < 300


async def test_a_treatment_in_no_trial_at_all_still_reads_as_absent(db_session):
    """The counterpart. Naming a trial we do not have would be the opposite error."""
    await _never_randomised_together(db_session)
    answer = await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Cosentyx", treatment_b="Tremfya"
    )

    assert answer["status"] == statuses.NETWORK_DISCONNECTED
    assert "Cosentyx does not appear in any scoped trial" in answer["reason"]


async def test_an_unknown_network_is_a_client_error(db_session):
    """Distinct from a comparison that is simply not estimable."""
    with pytest.raises(svc.ComparisonError, match="does not exist"):
        await svc.resolve_comparison(
            db_session, network_id="NOPE", treatment_a="Rinvoq", treatment_b="Humira"
        )


# =====================================================================================
# Governance
# =====================================================================================
async def test_governed_without_an_approved_protocol_downgrades_rather_than_raising(db_session):
    """The honest report is "computed but not releasable", not an error hiding a number."""
    await _star_network(db_session)
    answer = await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Rinvoq", treatment_b="Humira",
        execution_mode=GOVERNED,
    )
    assert answer["requested_execution_mode"] == GOVERNED
    assert answer["execution_mode"] == EXPLORATORY
    assert answer["status"] == statuses.EXPLORATORY_RESULT_COMPLETED
    assert not answer["is_releasable"]
    assert answer["governance"]["blocking_status"] == statuses.PROTOCOL_PENDING_APPROVAL


async def test_exploratory_never_consults_the_governance_gate(db_session):
    """Which is what lets Phase 6 be used before any approver exists."""
    await _star_network(db_session)
    answer = await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Rinvoq", treatment_b="Humira",
        execution_mode=EXPLORATORY,
    )
    assert answer["governance"] is None
    assert answer["status"] == statuses.EXPLORATORY_RESULT_COMPLETED


async def test_an_exploratory_result_is_a_success_but_not_releasable(db_session):
    await _star_network(db_session)
    answer = await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Rinvoq", treatment_b="Humira"
    )
    assert answer["is_success"]
    assert not answer["is_releasable"]
    assert answer["is_internal_output"]


# =====================================================================================
# Route mixing
# =====================================================================================
async def test_route_mixing_is_recorded_on_the_answer(db_session):
    """Rinvoq is oral and Humira is injectable — the threat travels with the number."""
    await _star_network(db_session)
    answer = await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Rinvoq", treatment_b="Humira"
    )
    assert answer["is_route_mixed"]
    assert answer["administration_routes"]["Rinvoq"] == "ORAL"
    assert "ANCHOR_MIXES_ADMINISTRATION_ROUTES" in answer["flags"]


# =====================================================================================
# Persistence
# =====================================================================================
async def test_a_persisted_result_records_its_derived_protocol_hash(db_session):
    await _star_network(db_session)
    answer = await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Rinvoq", treatment_b="Humira",
        persist=True,
    )
    row = (await db_session.execute(select(NMAResult))).scalar_one()

    assert row.result_id == answer["result_id"]
    assert row.source == COMPUTED
    assert row.protocol_id == PROTOCOL
    assert row.protocol_hash == protocols.content_hash(PROTOCOL)
    assert row.network_id == NETWORK
    assert row.engine == "BUCHER"
    assert row.execution_mode == EXPLORATORY
    assert row.is_route_mixed


async def test_a_computed_result_is_never_citable_and_never_externally_approved(db_session):
    """Computing a number does not grant it authority."""
    await _star_network(db_session)
    await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Rinvoq", treatment_b="Humira",
        persist=True,
    )
    row = (await db_session.execute(select(NMAResult))).scalar_one()
    assert not row.source_is_citable
    assert not row.claim_is_approved_for_external_use
    assert row.is_internal_output


async def test_the_stored_estimate_keeps_its_anchor(db_session):
    await _star_network(db_session)
    await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Rinvoq", treatment_b="Humira",
        persist=True,
    )
    row = (await db_session.execute(select(NMAResult))).scalar_one()
    estimates = json.loads(row.estimates)
    assert estimates[0]["anchor"] == "Placebo"
    assert estimates[0]["interval_type"] == "CI"


async def test_a_gap_is_not_persisted(db_session):
    """There is no result to store, and storing a null one would look like an answer."""
    db_session.add(_network())
    await db_session.commit()
    await _add_study(
        db_session, "S1", [("Rinvoq", 45, 100, "ORAL"), ("Placebo", 20, 100, "ORAL")]
    )
    await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Rinvoq", treatment_b="Humira",
        persist=True,
    )
    assert (await db_session.execute(select(NMAResult))).scalars().first() is None


# =====================================================================================
# The evidence view
# =====================================================================================
async def test_gather_evidence_reports_what_scoping_excluded(db_session):
    """"Why was my trial not used?" must be answerable without running a computation."""
    db_session.add(_network())
    await db_session.commit()
    await _add_study(
        db_session, "EARLY",
        [("Rinvoq", 45, 100, "ORAL"), ("Humira", 35, 100, "SUBCUTANEOUS")], week=12,
    )
    network = await svc._network(db_session, NETWORK)
    request = svc.ComparisonRequest(
        indication="Psoriatic Arthritis", treatment_a="Rinvoq", treatment_b="Humira",
        canonical_outcome_id=OUTCOME, protocol_id=PROTOCOL,
    )
    evidence, report = await svc.gather_evidence(db_session, network, request)

    assert report["approved_time_window"] == [14.0, 18.0]
    assert report["effect_measure"] == "risk_ratio"
    assert report["zero_event_policy"] == "TREATMENT_ARM_CONTINUITY_CORRECTION"
    assert any("week 12" in reason for _s, reason in evidence.unsuitable_direct)


async def test_the_outcome_type_comes_from_the_protocol_not_the_rows(db_session):
    """The sidecar must be told the shape an approver signed off, not one sniffed from data."""
    await _star_network(db_session)
    network = await svc._network(db_session, NETWORK)
    request = svc.ComparisonRequest(
        indication="Psoriatic Arthritis", treatment_a="Rinvoq", treatment_b="Humira",
        canonical_outcome_id=OUTCOME, protocol_id=PROTOCOL,
    )
    _evidence, report = await svc.gather_evidence(db_session, network, request)

    assert report["effect_measure"] == "risk_ratio"
    assert report["outcome_type"] == BINARY


async def test_a_continuous_row_under_a_ratio_protocol_is_refused_not_crashed(db_session):
    """Passing it through would raise inside binary_contrast and surface as a 500.

    It is a scoping decision — this row cannot answer this protocol's question — so it has
    to come back as a named reason a curator can act on.
    """
    db_session.add(_network())
    await db_session.commit()
    db_session.add(ClinicalStudy(
        study_id="CONTIN", indication="Psoriatic Arthritis",
        verification_status=lifecycles.VERIFIED, is_randomised=True,
    ))
    for index, treatment in enumerate(("Rinvoq", "Humira")):
        db_session.add(StudyArm(
            arm_id=f"CONTIN:A{index}", study_id="CONTIN", treatment=treatment, sample_size=100,
        ))
        db_session.add(OutcomeResult(
            result_id=f"CONTIN:R{index}", study_id="CONTIN", arm_id=f"CONTIN:A{index}",
            canonical_outcome_id=OUTCOME, endpoint="ACR50", timepoint_week=16,
            outcome_type=CONTINUOUS, mean=12.5, standard_deviation=4.0, sample_size=100,
        ))
    db_session.add(NetworkMembership(
        membership_id="M-CONTIN", network_id=NETWORK, study_id="CONTIN",
        protocol_id=PROTOCOL, membership_status=lifecycles.INCLUDED,
    ))
    await db_session.commit()

    answer = await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Rinvoq", treatment_b="Humira"
    )
    assert answer["status"] in statuses.ALL_STATUSES
    assert answer["evidence_level"] == 4
    level_one = next(a for a in answer["considered"] if a["level"] == 1)
    assert "requires binary" in level_one["reason"]


async def test_an_undefined_protocol_falls_back_visibly_rather_than_silently(db_session):
    """The fallback is acceptable only because the scoping report states it.

    A default buried in the wire payload would be a methodology choice nobody approved and
    nobody could see; one printed in `scoping` is auditable.
    """
    db_session.add(_network(protocol_id="BOGUS_PROTOCOL"))
    await db_session.commit()
    network = await svc._network(db_session, NETWORK)
    request = svc.ComparisonRequest(
        indication="Psoriatic Arthritis", treatment_a="Rinvoq", treatment_b="Humira",
        canonical_outcome_id=OUTCOME, protocol_id="BOGUS_PROTOCOL",
    )
    # An undefined protocol yields no effect_measure, so the risk_ratio fallback applies and
    # the run proceeds — the fallback is the one place a default is acceptable, because it
    # is visible in the scoping report rather than buried in the wire payload.
    _evidence, report = await svc.gather_evidence(db_session, network, request)
    assert report["effect_measure"] == "risk_ratio"
    assert report["outcome_type"] == BINARY


async def test_the_matrix_resolves_every_pair(db_session):
    """Three nodes, three pairs: two answered directly, one only indirectly.

    The two placebo comparisons were randomised, so they are Level 1 and releasable without
    our governance — they are the trials' own results. Rinvoq versus Humira has to be
    modelled, so it is exploratory until the protocol is approved and the network ratified.
    """
    await _star_network(db_session)
    matrix = await svc.resolve_all_pairs(db_session, network_id=NETWORK)

    assert matrix["node_count"] == 3
    assert matrix["pair_count"] == 3
    assert all(c["status"] in statuses.ALL_STATUSES for c in matrix["comparisons"])

    by_pair = {(c["treatment"], c["comparator"]): c for c in matrix["comparisons"]}
    assert by_pair[("Humira", "Placebo")]["status"] == statuses.DIRECT_EVIDENCE_AVAILABLE
    assert by_pair[("Placebo", "Rinvoq")]["status"] == statuses.DIRECT_EVIDENCE_AVAILABLE
    assert by_pair[("Humira", "Rinvoq")]["status"] == statuses.EXPLORATORY_RESULT_COMPLETED
    assert matrix["releasable_count"] == 2


# =====================================================================================
# Two in-scope rows for one arm (issue 6)
# =====================================================================================
async def _add_row(
    db, study_id: str, arm_index: int, *, suffix: str, events: int, n: int = 100,
    week: float = 16,
) -> None:
    """A further in-scope result for an arm that already has one.

    Real corpus shape: the registry posts one result twice, once as its own measure and once
    inside a combined by-visit measure, so both rows are faithful readings of the same arm.
    """
    db.add(OutcomeResult(
        result_id=f"{study_id}:{suffix}", study_id=study_id, arm_id=f"{study_id}:A{arm_index}",
        canonical_outcome_id=OUTCOME, endpoint="ACR50", timepoint_week=week,
        treatment_phase="PRIMARY", outcome_type=BINARY, events=events, sample_size=n,
    ))
    await db.commit()


async def _gathered(db):
    network = await svc._network(db, NETWORK)
    request = svc.ComparisonRequest(
        indication="Psoriatic Arthritis", treatment_a="Rinvoq", treatment_b="Humira",
        canonical_outcome_id=OUTCOME, protocol_id=PROTOCOL,
    )
    return await svc.gather_evidence(db, network, request)


async def test_two_disagreeing_rows_for_one_arm_withhold_it(db_session):
    """The defect: ``usable[treatment] = payload`` let the last row read win, silently.

    26 of the 44 duplicate identities in the live corpus carry different numbers, so this
    was a number chosen by row order in a system whose whole point is that numbers are
    chosen by an approved protocol.
    """
    db_session.add(_network())
    await db_session.commit()
    await _add_study(
        db_session, "DUP",
        [("Rinvoq", 45, 100, "ORAL"), ("Humira", 35, 100, "SUBCUTANEOUS")],
    )
    await _add_row(db_session, "DUP", 0, suffix="R9", events=51)

    evidence, report = await _gathered(db_session)

    assert [s for s, _r in evidence.ambiguous_arms] == ["DUP"]
    reason = evidence.ambiguous_arms[0][1]
    assert "Rinvoq" in reason
    assert "45/100" in reason and "51/100" in reason
    # Withheld, not resolved: neither value may be used, so the study contrasts nothing.
    assert "DUP" not in evidence.study_arms
    assert evidence.contrasts == ()
    assert len(report["ambiguous_arms"]) == 1


async def test_the_refusal_does_not_depend_on_which_row_was_read_first(db_session):
    """Same conflict, opposite insertion order, identical finding.

    Order-independence is the actual fix; reporting a conflict would be worth little if the
    report itself still varied with row order.
    """
    db_session.add(_network())
    await db_session.commit()
    await _add_study(
        db_session, "ORD-A",
        [("Rinvoq", 45, 100, "ORAL"), ("Humira", 35, 100, "SUBCUTANEOUS")],
    )
    await _add_row(db_session, "ORD-A", 0, suffix="R9", events=51)
    await _add_study(
        db_session, "ORD-B",
        [("Rinvoq", 51, 100, "ORAL"), ("Humira", 35, 100, "SUBCUTANEOUS")],
    )
    await _add_row(db_session, "ORD-B", 0, suffix="R9", events=45)

    evidence, _report = await _gathered(db_session)

    by_study = dict(evidence.ambiguous_arms)
    assert set(by_study) == {"ORD-A", "ORD-B"}
    assert by_study["ORD-A"] == by_study["ORD-B"]


async def test_identical_duplicate_rows_are_one_fact_stated_twice(db_session):
    """18 of the 44 are the same number twice; refusing those would discard real evidence."""
    db_session.add(_network())
    await db_session.commit()
    await _add_study(
        db_session, "SAME",
        [("Rinvoq", 45, 100, "ORAL"), ("Humira", 35, 100, "SUBCUTANEOUS")],
    )
    await _add_row(db_session, "SAME", 0, suffix="R9", events=45)

    evidence, report = await _gathered(db_session)

    assert evidence.ambiguous_arms == ()
    assert report["ambiguous_arms"] == []
    assert evidence.study_arms["SAME"] == frozenset({"Rinvoq", "Humira"})

    answer = await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Rinvoq", treatment_b="Humira"
    )
    assert answer["status"] == statuses.DIRECT_EVIDENCE_AVAILABLE


async def test_a_third_agreeing_row_cannot_resurrect_a_withheld_arm(db_session):
    """Two of three rows agreeing is not a majority verdict — it is still a conflict."""
    db_session.add(_network())
    await db_session.commit()
    await _add_study(
        db_session, "TRIPLE",
        [("Rinvoq", 45, 100, "ORAL"), ("Humira", 35, 100, "SUBCUTANEOUS")],
    )
    await _add_row(db_session, "TRIPLE", 0, suffix="R8", events=51)
    await _add_row(db_session, "TRIPLE", 0, suffix="R9", events=45)

    evidence, _report = await _gathered(db_session)

    assert [s for s, _r in evidence.ambiguous_arms] == ["TRIPLE"]
    # The duplicate collapses, so the conflict is between two candidate values, not three.
    assert "2 contradictory" in evidence.ambiguous_arms[0][1]


async def test_two_in_window_timepoints_that_disagree_are_also_a_conflict(db_session):
    """The other route to this defect, and the one a widened window would open.

    The protocol approves weeks 14-18. A trial posting the arm at both week 14 and week 16
    offers two legal values, and no protocol field states which the analysis is of.
    """
    db_session.add(_network())
    await db_session.commit()
    await _add_study(
        db_session, "WEEKS",
        [("Rinvoq", 45, 100, "ORAL"), ("Humira", 35, 100, "SUBCUTANEOUS")],
    )
    await _add_row(db_session, "WEEKS", 0, suffix="R9", events=51, week=14)

    evidence, _report = await _gathered(db_session)

    assert [s for s, _r in evidence.ambiguous_arms] == ["WEEKS"]
    assert "week 14" in evidence.ambiguous_arms[0][1]
    assert "week 16" in evidence.ambiguous_arms[0][1]


async def test_two_dose_arms_of_one_study_collapse_but_no_longer_do_so_in_silence(db_session):
    """SELECT-PsA 1's shape: 15 mg and 30 mg upadacitinib both resolve to ``Rinvoq``.

    Every protocol declares ``dose_policy: SEPARATE_BY_APPROVED_DOSE`` and nothing reads it,
    so one arm's numbers stand in for the treatment. Separating doses would rename nodes in
    every stored network and pooling them would contradict the approved protocol, so this
    asserts the **disclosure**, not a resolution — see issue 8.
    """
    db_session.add(_network())
    await db_session.commit()
    await _add_study(
        db_session, "DOSES",
        [("Rinvoq", 45, 100, "ORAL"), ("Humira", 35, 100, "SUBCUTANEOUS")],
    )
    db_session.add(StudyArm(
        arm_id="DOSES:A9", study_id="DOSES", treatment="Rinvoq",
        label="Upadacitinib 30 mg QD", administration_route="ORAL", sample_size=100,
    ))
    db_session.add(OutcomeResult(
        result_id="DOSES:R9", study_id="DOSES", arm_id="DOSES:A9",
        canonical_outcome_id=OUTCOME, endpoint="ACR50", timepoint_week=16,
        treatment_phase="PRIMARY", outcome_type=BINARY, events=58, sample_size=100,
    ))
    await db_session.commit()

    _evidence, report = await _gathered(db_session)

    shared = report["arms_sharing_a_node"]
    assert [entry["study_id"] for entry in shared] == ["DOSES"]
    assert "Upadacitinib 30 mg QD" in shared[0]["reason"]
    assert "dose_policy" in shared[0]["reason"]
    # Two arms sharing a node is not a contradiction within one arm, so nothing is withheld.
    assert _evidence.ambiguous_arms == ()


async def test_a_withheld_arm_is_named_as_ambiguous_not_as_a_scope_mismatch(db_session):
    """"Missing" and "contradictory" send a curator to different places.

    Only the second one names a study whose analysis populations somebody must choose
    between, so it must not be filed under the reason for an out-of-window trial.
    """
    await _star_network(db_session)
    await _add_study(
        db_session, "DUP",
        [("Rinvoq", 45, 100, "ORAL"), ("Humira", 35, 100, "SUBCUTANEOUS")],
    )
    await _add_row(db_session, "DUP", 0, suffix="R9", events=51)

    answer = await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Rinvoq", treatment_b="Humira"
    )
    level_one = next(a for a in answer["considered"] if a["level"] == 1)
    assert level_one["status"] == statuses.AMBIGUOUS_ARM_DATA
    assert "contradictory" in level_one["reason"]
    # The star network still answers indirectly — one withheld arm does not silence the rest.
    assert answer["evidence_level"] == 3


async def test_a_single_head_to_head_trial_is_not_internal_output(db_session):
    """That number belongs to the trial that produced it, not to us."""
    db_session.add(_network())
    await db_session.commit()
    await _add_study(
        db_session, "H2H",
        [("Rinvoq", 45, 100, "ORAL"), ("Humira", 35, 100, "SUBCUTANEOUS")],
    )
    answer = await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Rinvoq", treatment_b="Humira"
    )
    assert not answer["is_internal_output"]
    assert "POOLED_ACROSS_MULTIPLE_STUDIES" not in answer["flags"]


async def test_pooling_several_head_to_head_trials_is_internal_output(db_session):
    """Pooling three trials is a meta-analysis WE performed and must say so."""
    db_session.add(_network())
    await db_session.commit()
    for name in ("H2H-1", "H2H-2", "H2H-3"):
        await _add_study(
            db_session, name,
            [("Rinvoq", 45, 100, "ORAL"), ("Humira", 35, 100, "SUBCUTANEOUS")],
        )
    answer = await svc.resolve_comparison(
        db_session, network_id=NETWORK, treatment_a="Rinvoq", treatment_b="Humira"
    )
    assert answer["status"] == statuses.DIRECT_EVIDENCE_AVAILABLE
    assert answer["is_internal_output"]
    assert "POOLED_ACROSS_MULTIPLE_STUDIES" in answer["flags"]
    assert answer["heterogeneity"]["study_count"] == 3
