"""Phase 3A — adapter contract + extraction pipeline governance.

The named acceptance criteria this file pins:

* a harmonisation proposal is **never persisted as an applied value**
* a proposal contradicting the governing ``approved_time_window`` is **auto-rejected
  without escalation**
* a validation disagreement **blocks** promotion to VERIFIED
* every stage output carries model ID, prompt version and pipeline version
* validation coverage is reported **per licence tier**, not as one figure
* a simulated API failure returns a degraded result and never propagates an exception
"""
from __future__ import annotations

import dataclasses

import httpx
import pytest

from app.evidence import licensing
from app.evidence.extraction import (
    ACCEPTED_BY_PROTOCOL,
    AUTO_REJECTED,
    PIPELINE_VERSION,
    PROPOSED,
    STAGE_EXTRACTION,
    STAGE_HARMONISATION,
    STAGE_VALIDATION,
    ExtractedValue,
    HarmonisationProposal,
    PipelineResult,
    StageProvenance,
    ValidationOutcome,
    screen_proposal,
)
from app.evidence.lifecycles import MAPPED, VERIFIED
from app.evidence.sources import base
from app.models.clinical_study import OutcomeResult


def _prov(stage: str = STAGE_HARMONISATION) -> StageProvenance:
    return StageProvenance(stage=stage, model_id="claude-sonnet-4", prompt_version="v1")


def _proposal(to_week: float, kind: str = "TIMEPOINT") -> HarmonisationProposal:
    return HarmonisationProposal(
        kind=kind,
        from_value=12.0,
        to_value=to_week,
        rationale="Both report the same responder definition.",
        confidence=0.8,
        provenance=_prov(),
        guideline_citation="ISPOR ITC Good Practices Part 2",
    )


# =====================================================================================
# The proposal-only constraint
# =====================================================================================
def test_a_proposal_has_no_way_to_apply_itself():
    """Structural, not conventional: there is no apply() and the type is frozen."""
    proposal = _proposal(16.0)
    assert not hasattr(proposal, "apply")
    assert dataclasses.is_dataclass(proposal)
    with pytest.raises(dataclasses.FrozenInstanceError):
        proposal.to_value = 24.0  # type: ignore[misc]


def test_a_proposal_shares_no_field_names_with_the_persisted_outcome_row():
    """Proposals and persisted values must not share vocabulary.

    A shared name is how the boundary blurs: `rejection_reason` originally existed on
    both, meaning "the protocol auto-rejected this proposal" on one side and "this
    extraction failed verification" on the other. Keeping the namespaces disjoint makes
    that class of confusion a test failure rather than a code review nitpick.
    """
    persisted = {c.name for c in OutcomeResult.__table__.columns}
    proposal_fields = {f.name for f in dataclasses.fields(HarmonisationProposal)}
    collisions = proposal_fields & persisted
    assert not collisions, f"proposal/persisted name collision: {sorted(collisions)}"


def test_a_proposal_outside_the_approved_window_is_auto_rejected():
    screened = screen_proposal(_proposal(24.0), approved_time_window=(14.0, 18.0))
    assert screened.disposition == AUTO_REJECTED
    assert "protocol governs" in screened.auto_rejection_reason


def test_an_auto_rejected_proposal_never_reaches_a_human():
    """No escalation path: routing it to review would invite overruling the protocol."""
    screened = screen_proposal(_proposal(24.0), approved_time_window=(14.0, 18.0))
    assert not screened.is_actionable_by_human

    result = PipelineResult(proposals=[screened])
    assert result.actionable_proposals == []


def test_a_proposal_inside_the_approved_window_survives_screening():
    screened = screen_proposal(_proposal(16.0), approved_time_window=(14.0, 18.0))
    assert screened.disposition == PROPOSED
    assert screened.is_actionable_by_human


def test_window_boundaries_are_inclusive():
    for week in (14.0, 18.0):
        assert screen_proposal(_proposal(week), approved_time_window=(14.0, 18.0)).disposition == PROPOSED


def test_no_governing_protocol_leaves_the_decision_to_a_human():
    """The honest state during development, before any protocol exists."""
    screened = screen_proposal(_proposal(24.0), approved_time_window=None)
    assert screened.disposition == PROPOSED


def test_a_non_numeric_target_is_rejected_rather_than_coerced():
    screened = screen_proposal(_proposal("about week 16"), approved_time_window=(14.0, 18.0))
    assert screened.disposition == AUTO_REJECTED
    assert "not numeric" in screened.auto_rejection_reason


def test_strata_proposals_are_not_screened_by_the_time_window():
    screened = screen_proposal(_proposal(99.0, kind="STRATA"), approved_time_window=(14.0, 18.0))
    assert screened.disposition == PROPOSED


def test_a_guideline_citation_is_context_not_authority():
    """Citing ISPOR does not exempt a proposal from the protocol window."""
    screened = screen_proposal(_proposal(24.0), approved_time_window=(14.0, 18.0))
    assert screened.guideline_citation == "ISPOR ITC Good Practices Part 2"
    assert screened.disposition == AUTO_REJECTED


# =====================================================================================
# Validation blocks promotion
# =====================================================================================
def _validation(agrees: bool) -> ValidationOutcome:
    return ValidationOutcome(
        field_name="events",
        agrees=agrees,
        expected=120,
        observed=120 if agrees else 118,
        reach="FULL_SOURCE",
        provenance=_prov(STAGE_VALIDATION),
    )


def test_a_validation_disagreement_blocks_promotion_to_verified():
    """Not a warning and not a confidence penalty — the row stays MAPPED."""
    result = PipelineResult(validations=[_validation(True), _validation(False)])
    assert result.next_verification_status == MAPPED
    assert len(result.disagreements) == 1


def test_full_agreement_permits_promotion():
    result = PipelineResult(validations=[_validation(True), _validation(True)])
    assert result.next_verification_status == VERIFIED


# =====================================================================================
# Auditability
# =====================================================================================
def test_every_stage_output_carries_model_prompt_and_pipeline_version():
    """A non-deterministic pipeline without this is unauditable."""
    for produced in (
        ExtractedValue("events", 120, "120/200", 0.9, _prov(STAGE_EXTRACTION)),
        _proposal(16.0),
        _validation(True),
    ):
        provenance = produced.provenance
        assert provenance.model_id
        assert provenance.prompt_version
        assert provenance.pipeline_version == PIPELINE_VERSION
        assert set(provenance.as_dict()) >= {"model_id", "prompt_version", "pipeline_version"}


# =====================================================================================
# Coverage is per licence tier
# =====================================================================================
def test_validation_coverage_is_reported_per_licence_tier():
    restricted = PipelineResult(license_class=licensing.RESTRICTED, validations=[_validation(True)])
    public = PipelineResult(license_class=licensing.PUBLIC_DOMAIN, validations=[_validation(True)])

    assert restricted.validation_reach == "FRAGMENT"
    assert public.validation_reach == "FULL_SOURCE"
    assert restricted.coverage_report()["license_class"] == licensing.RESTRICTED


def test_coverage_report_counts_auto_rejected_proposals_separately():
    result = PipelineResult(
        license_class=licensing.PUBLIC_DOMAIN,
        proposals=[
            screen_proposal(_proposal(24.0), approved_time_window=(14.0, 18.0)),
            screen_proposal(_proposal(16.0), approved_time_window=(14.0, 18.0)),
        ],
    )
    report = result.coverage_report()
    assert report["proposals_total"] == 2
    assert report["proposals_auto_rejected"] == 1


# =====================================================================================
# Adapter contract — never raises
# =====================================================================================
@pytest.fixture
def _no_network(monkeypatch):
    """Force every httpx GET to fail, the way a real outage would."""
    async def _boom(self, *a, **k):  # noqa: ANN001, ARG001
        raise httpx.ConnectError("simulated outage")

    monkeypatch.setattr(httpx.AsyncClient, "get", _boom)


async def test_a_transport_failure_degrades_and_never_raises(_no_network):
    result = await base.get_json(
        "https://example.invalid/x", source_type="CLINICALTRIALS_GOV", source_identifier="NCT01"
    )
    assert result.ok is False
    assert "transport error" in result.reason
    assert result.payload is None


async def test_a_404_is_information_not_breakage(monkeypatch):
    async def _missing(self, *a, **k):  # noqa: ANN001, ARG001
        return httpx.Response(404, request=httpx.Request("GET", "https://example.invalid/x"))

    monkeypatch.setattr(httpx.AsyncClient, "get", _missing)
    result = await base.get_json(
        "https://example.invalid/x", source_type="PUBMED", source_identifier="99999999"
    )
    assert result.ok is False
    assert result.reason == "not found"
    assert result.status_code == 404


async def test_malformed_json_is_caught(monkeypatch):
    async def _garbage(self, *a, **k):  # noqa: ANN001, ARG001
        return httpx.Response(
            200, text="<html>not json</html>", request=httpx.Request("GET", "https://example.invalid/x")
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", _garbage)
    result = await base.get_json(
        "https://example.invalid/x", source_type="OPENFDA", source_identifier="abc"
    )
    assert result.ok is False
    assert "malformed JSON" in result.reason


async def test_a_successful_fetch_classifies_its_own_licence(monkeypatch):
    async def _ok(self, *a, **k):  # noqa: ANN001, ARG001
        return httpx.Response(
            200, json={"studies": []}, request=httpx.Request("GET", "https://example.invalid/x")
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", _ok)
    result = await base.get_json(
        "https://example.invalid/x", source_type="CLINICALTRIALS_GOV", source_identifier="NCT01"
    )
    assert result.ok
    assert result.license_class == licensing.PUBLIC_DOMAIN
    assert result.may_retain_document


def test_an_unknown_source_defaults_to_restricted_retention():
    result = base.FetchResult(ok=True, source_type="SOME_JOURNAL", source_identifier="doi:10.1/x")
    assert result.license_class == licensing.RESTRICTED
    assert not result.may_retain_document
