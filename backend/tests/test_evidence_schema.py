"""Phase 2 — canonical evidence schema, lifecycles and licence-aware retention.

Every test here maps to a named acceptance criterion in the plan. The four that matter
most, because each guards a rule that is invisible at runtime until it has already been
violated:

* a RESTRICTED source cannot persist a full document
* a VERIFIED study can be INCLUDED in one network and EXCLUDED from another at once
* excluding a study requires a reason
* a network cannot reach RATIFIED without BOTH review transitions, in order
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.evidence import lifecycles as lc
from app.evidence import licensing, statuses
from app.models.clinical_study import ClinicalStudy, OutcomeResult, StudyArm
from app.models.database import Base
from app.models.drug_fact import DrugFact
from app.models.evidence_network import EvidenceNetwork, NetworkMembership
from app.models.nma_result import COMPUTED, EXPLORATORY, PUBLISHED, NMAResult
from app.models.source_payload import SourcePayload, checksum_of


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from app.models import (  # noqa: F401 — register the Phase 2 tables on Base.metadata
        clinical_study,
        drug_fact,
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


# =====================================================================================
# Licence-aware retention
# =====================================================================================
def test_restricted_source_cannot_retain_a_full_document():
    """The load-bearing guarantee of app.evidence.licensing."""
    decision = licensing.enforce(
        source_type="COCHRANE",
        raw_payload="<html>the entire paywalled review</html>",
        retained_fragment="ACR50 at week 24: 38% vs 19%",
    )
    assert decision.license_class == licensing.RESTRICTED
    assert decision.retention_policy == licensing.FRAGMENT_ONLY
    assert decision.raw_payload is None
    assert decision.dropped_fields == ("raw_payload",)
    assert decision.retained_fragment == "ACR50 at week 24: 38% vs 19%"


def test_public_domain_source_retains_everything_indefinitely():
    decision = licensing.enforce(source_type="CLINICALTRIALS_GOV", raw_payload='{"nct": "NCT01"}')
    assert decision.license_class == licensing.PUBLIC_DOMAIN
    assert decision.raw_payload == '{"nct": "NCT01"}'
    assert decision.expires_at is None
    assert decision.dropped_fields == ()


def test_open_access_retains_full_text_but_with_an_expiry():
    """OA grants are revocable in practice, so full text carries a re-check date."""
    decision = licensing.enforce(source_type="PMC_OA", raw_payload="full article text")
    assert decision.retention_policy == licensing.FULL_WHILE_LICENSED
    assert decision.raw_payload == "full article text"
    assert decision.expires_at is not None


def test_unknown_source_defaults_to_the_most_conservative_class():
    """An unknown source is the one case where guessing wrong is unrecoverable."""
    decision = licensing.enforce(source_type="SOME_NEW_PUBLISHER", raw_payload="doc")
    assert decision.license_class == licensing.RESTRICTED
    assert decision.raw_payload is None


def test_general_pmc_is_not_treated_as_the_open_access_subset():
    """PMC membership does not imply an OA licence — a genuinely easy mistake."""
    assert licensing.license_for_source("PMC") == licensing.RESTRICTED
    assert licensing.license_for_source("PMC_OA") == licensing.OPEN_ACCESS


def test_a_reviewer_upload_does_not_create_a_right_to_retain():
    decision = licensing.enforce(source_type="MANUAL_UPLOAD", raw_payload="uploaded PDF bytes")
    assert decision.raw_payload is None


def test_an_invalid_explicit_license_override_does_not_become_permissive():
    decision = licensing.enforce(
        source_type="CLINICALTRIALS_GOV", raw_payload="doc", license_class="TOTALLY_FINE_HONEST"
    )
    assert decision.license_class == licensing.RESTRICTED
    assert decision.raw_payload is None


def test_validation_reach_is_reported_per_licence_tier():
    """Coverage is per tier, never claimed as a single figure."""
    assert licensing.validation_reach(licensing.PUBLIC_DOMAIN) == "FULL_SOURCE"
    assert licensing.validation_reach(licensing.RESTRICTED) == "FRAGMENT"


def test_source_payload_record_enforces_retention_and_keeps_the_checksum():
    """A checksum is a fact ABOUT a document, not a copy of it, so it always survives."""
    payload = SourcePayload.record(
        payload_id="SP-1",
        source_type="HTA",
        source_identifier="TA803",
        raw_payload="<full NICE submission>",
        retained_fragment="Committee accepted the company's base case.",
        citation="NICE TA803",
        page_provenance="p. 42, Table 12",
    )
    assert payload.raw_payload is None
    assert payload.license_class == licensing.RESTRICTED
    assert json.loads(payload.dropped_fields) == ["raw_payload"]
    assert payload.checksum == checksum_of("<full NICE submission>")
    assert payload.page_provenance == "p. 42, Table 12"


# =====================================================================================
# Lifecycle 1 — evidence verification (row-level)
# =====================================================================================
def test_verification_follows_extracted_mapped_verified():
    assert lc.can_transition("verification", lc.EXTRACTED, lc.MAPPED)
    assert lc.can_transition("verification", lc.MAPPED, lc.VERIFIED)


def test_verification_cannot_skip_mapping():
    with pytest.raises(lc.LifecycleError, match="cannot move"):
        lc.assert_transition("verification", lc.EXTRACTED, lc.VERIFIED)


def test_verified_and_rejected_are_terminal():
    """Re-opening a decided row would rewrite history; corrections make a new version."""
    assert lc.is_terminal("verification", lc.VERIFIED)
    assert lc.is_terminal("verification", lc.REJECTED)


def test_rejection_is_reachable_from_any_live_state():
    assert lc.can_transition("verification", lc.EXTRACTED, lc.REJECTED)
    assert lc.can_transition("verification", lc.MAPPED, lc.REJECTED)


# =====================================================================================
# Lifecycle 2 — network membership (per network + protocol)
# =====================================================================================
def test_excluding_a_study_requires_a_reason():
    """An unexplained exclusion is indistinguishable from a mistake."""
    with pytest.raises(lc.LifecycleError, match="requires a reason"):
        lc.assert_transition("membership", lc.PROPOSED, lc.EXCLUDED)
    assert lc.assert_transition(
        "membership", lc.PROPOSED, lc.EXCLUDED, reason="Week 24 outside approved window"
    ) == lc.EXCLUDED


def test_blank_exclusion_reason_is_not_a_reason():
    with pytest.raises(lc.LifecycleError, match="requires a reason"):
        lc.assert_transition("membership", lc.INCLUDED, lc.EXCLUDED, reason="   ")


def test_membership_is_revisable_in_both_directions():
    """Unlike verification, this is a re-judgement of fit, not a rewrite of fact."""
    assert lc.can_transition("membership", lc.INCLUDED, lc.EXCLUDED)
    assert lc.can_transition("membership", lc.EXCLUDED, lc.INCLUDED)


# =====================================================================================
# Lifecycle 3 — network ratification
# =====================================================================================
def test_network_cannot_reach_ratified_without_both_reviews():
    assert not lc.can_transition("ratification", lc.DRAFT, lc.RATIFIED)
    assert not lc.can_transition("ratification", lc.PENDING_MEDICAL_REVIEW, lc.RATIFIED)
    assert lc.can_transition("ratification", lc.PENDING_STATISTICAL_REVIEW, lc.RATIFIED)


def test_ratification_review_order_is_enforced():
    """Statistical review is only reachable after medical review."""
    with pytest.raises(lc.LifecycleError):
        lc.assert_transition("ratification", lc.DRAFT, lc.PENDING_STATISTICAL_REVIEW)


def test_full_ratification_path_is_walkable():
    state = lc.DRAFT
    for target in (lc.PENDING_MEDICAL_REVIEW, lc.PENDING_STATISTICAL_REVIEW, lc.RATIFIED):
        state = lc.assert_transition("ratification", state, target)
    assert state == lc.RATIFIED
    assert lc.is_computable(state)


def test_only_a_ratified_network_is_computable():
    for state in (lc.DRAFT, lc.PENDING_MEDICAL_REVIEW, lc.PENDING_STATISTICAL_REVIEW):
        assert not lc.is_computable(state)


def test_unknown_lifecycle_is_an_error_not_a_silent_pass():
    with pytest.raises(lc.LifecycleError, match="unknown lifecycle"):
        lc.can_transition("nonsense", "A", "B")


# =====================================================================================
# Structured statuses
# =====================================================================================
def test_exploratory_results_are_never_releasable():
    """The single predicate every downstream consumer routes through."""
    assert statuses.is_success(statuses.EXPLORATORY_RESULT_COMPLETED)
    assert not statuses.is_releasable(statuses.EXPLORATORY_RESULT_COMPLETED)
    assert statuses.is_releasable(statuses.GOVERNED_SYNTHESIS_COMPLETED)


def test_evidence_gaps_are_findings_not_failures():
    assert statuses.is_gap(statuses.NETWORK_DISCONNECTED)
    assert statuses.is_gap(statuses.ROUTE_MIXING_NOT_ESTIMABLE)
    assert not statuses.is_success(statuses.NETWORK_DISCONNECTED)


def test_every_status_has_a_human_readable_description():
    for status in statuses.ALL_STATUSES:
        assert statuses.describe(status) != "Unknown comparison status."


def test_describe_is_safe_for_an_unknown_status():
    assert statuses.describe("WHO_KNOWS") == "Unknown comparison status."


# =====================================================================================
# Persistence — the schema actually round-trips
# =====================================================================================
async def test_a_verified_study_can_be_included_here_and_excluded_there(db_session):
    """The reason membership is not a column on ClinicalStudy.

    The same correctly-extracted study is appropriate for an ACR50 network and
    inappropriate for an ACR20 one. A single row-level flag cannot express that.
    """
    study = ClinicalStudy(
        study_id="ST-1",
        registry_id="NCT03104400",
        acronym="SELECT-PsA 1",
        indication="Psoriatic Arthritis",
        treatment_phase="PRIMARY",
        verification_status=lc.VERIFIED,
    )
    acr50 = EvidenceNetwork(
        network_id="NET-ACR50",
        indication="Psoriatic Arthritis",
        canonical_outcome_id="PSA_ACR50_W16",
        protocol_id="PSA_ACR50_PRIMARY",
    )
    acr20 = EvidenceNetwork(
        network_id="NET-ACR20",
        indication="Psoriatic Arthritis",
        canonical_outcome_id="PSA_ACR20_W16",
        protocol_id="PSA_ACR20_PRIMARY",
    )
    db_session.add_all([study, acr50, acr20])
    await db_session.flush()

    db_session.add_all([
        NetworkMembership(
            membership_id="M-1", network_id="NET-ACR50", study_id="ST-1",
            protocol_id="PSA_ACR50_PRIMARY", membership_status=lc.INCLUDED,
        ),
        NetworkMembership(
            membership_id="M-2", network_id="NET-ACR20", study_id="ST-1",
            protocol_id="PSA_ACR20_PRIMARY", membership_status=lc.EXCLUDED,
            exclusion_reason="ACR20 not reported at the protocol timepoint",
        ),
    ])
    await db_session.commit()

    rows = (await db_session.execute(
        select(NetworkMembership).where(NetworkMembership.study_id == "ST-1")
    )).scalars().all()
    by_network = {r.network_id: r for r in rows}

    assert study.verification_status == lc.VERIFIED
    assert by_network["NET-ACR50"].membership_status == lc.INCLUDED
    assert by_network["NET-ACR20"].membership_status == lc.EXCLUDED
    assert by_network["NET-ACR20"].exclusion_reason


async def test_arm_level_results_round_trip_with_multi_arm_structure(db_session):
    """Three arms stay three arms. Flattening here would lose the correlation the NMA needs."""
    study = ClinicalStudy(
        study_id="ST-2", registry_id="NCT02", indication="Psoriatic Arthritis",
        treatment_phase="PRIMARY",
    )
    db_session.add(study)
    await db_session.flush()

    for arm_id, treatment, route, events in (
        ("A1", "Rinvoq", "ORAL", 120),
        ("A2", "Placebo", "ORAL", 60),
        ("A3", "Humira", "SC", 100),
    ):
        db_session.add(StudyArm(
            arm_id=arm_id, study_id="ST-2", treatment=treatment,
            administration_route=route, sample_size=200,
            is_placebo=(treatment == "Placebo"),
        ))
        db_session.add(OutcomeResult(
            result_id=f"R-{arm_id}", study_id="ST-2", arm_id=arm_id,
            canonical_outcome_id="PSA_ACR50_W16", endpoint="ACR50",
            timepoint_week=16, outcome_type="binary", events=events, sample_size=200,
        ))
    await db_session.commit()

    loaded = (await db_session.execute(
        select(ClinicalStudy).where(ClinicalStudy.study_id == "ST-2")
    )).scalar_one()
    assert len(loaded.arms) == 3
    assert len(loaded.outcomes) == 3
    assert {a.administration_route for a in loaded.arms} == {"ORAL", "SC"}


async def test_computed_results_carry_protocol_hash_and_engine_versions(db_session):
    """Without these a result cannot answer 'under what rules was this produced?'."""
    result = NMAResult(
        result_id="NMA-1", source=COMPUTED, indication="Psoriatic Arthritis",
        canonical_outcome_id="PSA_ACR50_W16", network_id="NET-ACR50", network_version=3,
        engine="NETMETA", engine_version="1.0.0", package_version="netmeta 7.0-0",
        protocol_id="PSA_ACR50_PRIMARY", protocol_hash="sha256:abc123",
        execution_mode=EXPLORATORY, status=statuses.EXPLORATORY_RESULT_COMPLETED,
        is_route_mixed=True, placebo_response_policy="SENSITIVITY_REQUIRED",
        administration_routes=json.dumps({"Rinvoq": "ORAL", "Humira": "SC"}),
    )
    db_session.add(result)
    await db_session.commit()

    loaded = (await db_session.execute(
        select(NMAResult).where(NMAResult.result_id == "NMA-1")
    )).scalar_one()
    assert loaded.protocol_hash and loaded.engine_version and loaded.package_version
    assert loaded.is_internal_output
    assert not statuses.is_releasable(loaded.status)
    assert json.loads(loaded.administration_routes)["Rinvoq"] == "ORAL"


async def test_published_and_computed_share_a_table_but_not_an_identity(db_session):
    published = NMAResult(
        result_id="NMA-PUB", source=PUBLISHED, indication="Plaque Psoriasis",
        canonical_outcome_id="PSO_PASI90_W16", citation="Armstrong et al. 2024",
        status=statuses.PUBLISHED_RESULT_AVAILABLE, source_is_citable=True,
        included_studies=json.dumps(["NCT01", "NCT02"]), included_studies_recoverable=True,
    )
    db_session.add(published)
    await db_session.commit()

    assert not published.is_internal_output
    assert published.source_is_citable
    # Citability and external approval are independent properties.
    assert not published.claim_is_approved_for_external_use


async def test_drug_facts_default_to_unapproved_for_external_use(db_session):
    """A published label is citable; our extracted interpretation of it is not approved."""
    fact = DrugFact(
        fact_id="DF-1", brand="Rinvoq", generic="upadacitinib",
        drug_class="JAK inhibitor", administration_route="ORAL",
        has_boxed_warning=True, regulatory_source="FDA",
    )
    db_session.add(fact)
    await db_session.commit()

    assert fact.verification_status == lc.EXTRACTED
    assert fact.source_is_citable
    assert not fact.claim_is_approved_for_external_use
