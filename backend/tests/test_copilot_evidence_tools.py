"""Copilot (Ema) tool backfill tests, round 2 — Clinical Evidence, Activation & Impact,
the Influence Graph, comparison curation and harvest run-to-pipeline.

Same shape as ``test_copilot_tools.py``: the tools open their OWN ``AsyncSessionLocal``,
so the fixture monkeypatches that factory on every tool module to one shared in-memory
SQLite DB (``StaticPool`` keeps the seeding session and the tool's session on the same
connection).

The value of the "every no-id view executes on an empty DB" parametrization is drift
detection: these tools call ~20 services by keyword, and a renamed kwarg surfaces here
rather than in a chat turn.
"""
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.database import Base

# Import every model so the full schema registers before create_all (mirrors init_db).
from app.models import (  # noqa: F401
    alert,
    analysis_protocol,
    audit_log,
    clinical_study,
    competitor_candidate,
    consensus,
    digest,
    drug_fact,
    evaluation_claim,
    evidence_network,
    harvested_question,
    intervention,
    intervention_event,
    intervention_result,
    measurement_snapshot,
    nma_result,
    preferred_source,
    question,
    question_evidence,
    recommendation,
    recommendation_review,
    response,
    response_citation,
    run,
    scoring,
    source_domain,
    source_payload,
    theme,
)
from app.copilot.nodes import tool_executor
from app.copilot.tools import activation_tools, evidence_tools, question_tools, read_tools
from app.copilot.tools.registry import TOOLS, anthropic_tool_schemas

NEW_READ_TOOLS = [
    "get_evidence",
    "get_evidence_comparison",
    "get_evidence_governance",
    "get_evidence_alignment",
    "get_competitor_discovery",
    "get_evidence_synthesis",
    "get_interventions",
    "get_curation_coverage",
]
NEW_ACTION_TOOLS = [
    "run_evidence_ingest",
    "evaluate_claims",
    "generate_evidence_questions",
    "curate_evidence",
    "review_evidence",
    "manage_intervention",
    "generate_curation_questions",
    "run_questions_to_pipeline",
]
NEW_TOOLS = [*NEW_READ_TOOLS, *NEW_ACTION_TOOLS]

# Governance tools must be BOTH mutating and governance, or the executor will happily
# propose an unnamed sign-off.
GOVERNANCE_TOOLS = {
    "curate_evidence": "/evidence/studies",
    "review_evidence": "/evidence/governance",
    "manage_intervention": "/dashboard/activation-impact",
    "run_questions_to_pipeline": "/run-analysis",
}


@pytest.fixture
async def maker(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    for module in (evidence_tools, activation_tools, question_tools, read_tools):
        monkeypatch.setattr(module, "AsyncSessionLocal", factory)
    yield factory
    await engine.dispose()


# =================================================================================
# Registry / schema
# =================================================================================
def test_registry_exposes_new_tools():
    for name in NEW_TOOLS:
        assert name in TOOLS, f"{name} not registered"


def test_anthropic_tool_schemas_build():
    schemas = anthropic_tool_schemas()
    names = {s["name"] for s in schemas}
    for name in NEW_TOOLS:
        assert name in names
    for s in schemas:
        assert s["description"] and isinstance(s["input_schema"], dict)


@pytest.mark.parametrize("name,nav", sorted(GOVERNANCE_TOOLS.items()))
def test_governance_tools_are_gated(name, nav):
    spec = TOOLS[name]
    assert spec.mutating is True, f"{name} must be intercepted for confirmation"
    assert spec.governance is True, f"{name} must require a named decider"
    assert spec.nav_target == nav


@pytest.mark.parametrize("name", ["run_evidence_ingest", "evaluate_claims",
                                  "generate_evidence_questions", "generate_curation_questions"])
def test_billed_tools_are_mutating(name):
    """Read-shaped but billed/external work still has to reach a Confirm card."""
    assert TOOLS[name].mutating is True


@pytest.mark.parametrize("name", NEW_READ_TOOLS)
def test_read_tools_are_not_mutating(name):
    spec = TOOLS[name]
    assert spec.mutating is False and spec.governance is False


# =================================================================================
# The reviewer-name vocabulary (the deadlock guard)
# =================================================================================
@pytest.mark.parametrize("key", [
    "verified_by", "rejected_by", "decided_by", "submitted_by", "reopened_by",
    "revoked_by", "applied_by", "reviewer", "actor_name", "owner_name",
    "approver_name", "reviewer_name", "scored_by",
])
def test_executor_recognises_every_reviewer_key(key):
    """A governance tool whose name key is missing here can NEVER be proposed."""
    assert tool_executor._has_reviewer({key: "Dr Smith"}) is True


def test_executor_rejects_blank_reviewer():
    assert tool_executor._has_reviewer({"verified_by": "   "}) is False
    assert tool_executor._has_reviewer({}) is False


def test_every_governance_tool_has_a_recognised_name_field():
    """Cross-check the specs against _REVIEWER_KEYS so the two cannot drift apart."""
    for name in GOVERNANCE_TOOLS:
        fields = set(TOOLS[name].input_schema.model_fields)
        assert fields & set(tool_executor._REVIEWER_KEYS), (
            f"{name} is governance-gated but has no field in _REVIEWER_KEYS"
        )


# =================================================================================
# Confirm-card previews
# =================================================================================
def test_preview_summary_states_the_model_call_cost():
    spec = TOOLS["evaluate_claims"]
    summary = tool_executor._preview_summary(spec, {"scope": "run", "run_id": "R-1", "limit": 50})
    assert "R-1" in summary and "50" in summary and "model call" in summary


def test_preview_summary_warns_that_run_to_pipeline_bypasses_review():
    spec = TOOLS["run_questions_to_pipeline"]
    summary = tool_executor._preview_summary(spec, {"item_ids": [1, 2, 3], "reviewer_name": "MA"})
    assert "3" in summary and "bypass" in summary.lower()


def test_preview_summary_distinguishes_preview_from_commit():
    spec = TOOLS["run_evidence_ingest"]
    preview = tool_executor._preview_summary(spec, {"action": "trials", "indication": "PsA", "commit": False})
    commit = tool_executor._preview_summary(spec, {"action": "trials", "indication": "PsA", "commit": True})
    assert preview.startswith("Preview") and commit.startswith("Commit")


def test_identifiers_are_not_editable_on_the_card():
    spec = TOOLS["curate_evidence"]
    fields = tool_executor._preview_fields(spec, {"action": "study_check", "study_id": "S-1", "verified_by": "Ana"})
    by_key = {f["key"]: f for f in fields}
    assert by_key["study_id"]["editable"] is False
    assert by_key["verified_by"]["editable"] is True


def test_build_pending_action_carries_governance_and_presets():
    pa = tool_executor.build_pending_action(
        TOOLS["run_evidence_ingest"], {"action": "trials", "indication": "PsA"}, "trace-1"
    )
    assert pa["governance"] is False
    assert [p["label"] for p in pa["presets"]] == ["Preview", "Commit"]
    gov = tool_executor.build_pending_action(
        TOOLS["review_evidence"], {"action": "network_submit", "network_id": "N-1"}, "trace-1"
    )
    assert gov["governance"] is True


# =================================================================================
# get_evidence
# =================================================================================
async def test_evidence_overview_ok_on_empty_corpus(maker):
    res = await evidence_tools.get_evidence(evidence_tools.GetEvidenceInput(view="overview"))
    assert res.ok is True
    assert res.data["view"] == "overview"


async def test_evidence_unknown_view_errors(maker):
    res = await evidence_tools.get_evidence(evidence_tools.GetEvidenceInput(view="nope"))
    assert res.ok is False
    assert "view must be one of" in (res.error or "")


@pytest.mark.parametrize("view,arg", [
    ("network", "network_id"), ("study", "study_id"), ("drug_fact", "brand"),
])
async def test_evidence_id_views_require_their_id(maker, view, arg):
    res = await evidence_tools.get_evidence(evidence_tools.GetEvidenceInput(view=view))
    assert res.ok is False
    assert arg in (res.error or "")


async def test_evidence_missing_network_is_not_found(maker):
    res = await evidence_tools.get_evidence(
        evidence_tools.GetEvidenceInput(view="network", network_id="NET-NOPE")
    )
    assert res.ok is False
    assert "NET-NOPE" in (res.error or "")


@pytest.mark.parametrize("view", ["overview", "networks", "studies", "drug_facts"])
async def test_evidence_all_simple_views_ok(maker, view):
    res = await evidence_tools.get_evidence(evidence_tools.GetEvidenceInput(view=view))
    assert res.ok is True, f"{view}: {res.error}"


# =================================================================================
# get_evidence_comparison
# =================================================================================
async def test_comparison_unknown_view_errors(maker):
    res = await evidence_tools.get_evidence_comparison(
        evidence_tools.GetEvidenceComparisonInput(view="bogus", network_id="N-1")
    )
    assert res.ok is False
    assert "view must be one of" in (res.error or "")


async def test_comparison_rejects_unknown_execution_mode(maker):
    res = await evidence_tools.get_evidence_comparison(
        evidence_tools.GetEvidenceComparisonInput(view="matrix", network_id="N-1", execution_mode="YOLO")
    )
    assert res.ok is False
    assert "execution_mode" in (res.error or "")


async def test_comparison_resolve_needs_both_treatments(maker):
    res = await evidence_tools.get_evidence_comparison(
        evidence_tools.GetEvidenceComparisonInput(view="resolve", network_id="N-1", treatment_a="Rinvoq")
    )
    assert res.ok is False
    assert "treatment_b" in (res.error or "")


async def test_comparison_missing_network_is_reported(maker):
    res = await evidence_tools.get_evidence_comparison(
        evidence_tools.GetEvidenceComparisonInput(view="matrix", network_id="NET-NOPE")
    )
    assert res.ok is False


# =================================================================================
# get_evidence_governance
# =================================================================================
async def test_governance_vocabulary_is_served_from_the_module(maker):
    res = await evidence_tools.get_evidence_governance(
        evidence_tools.GetEvidenceGovernanceInput(view="vocabulary")
    )
    assert res.ok is True
    result = res.data["result"]
    assert result["approval_roles"] and result["ratification_states"]
    # PROPOSED is the builder's write, never a reviewer's decision.
    assert "PROPOSED" not in result["membership_decisions"]


async def test_governance_unknown_view_errors(maker):
    res = await evidence_tools.get_evidence_governance(
        evidence_tools.GetEvidenceGovernanceInput(view="zzz")
    )
    assert res.ok is False
    assert "view must be one of" in (res.error or "")


@pytest.mark.parametrize("view,arg", [
    ("protocol", "protocol_id"), ("network_gate", "network_id"),
    ("memberships", "network_id"), ("study_source_check", "study_id"),
    ("drug_fact_source_check", "fact_id"), ("question_evidence", "question_id"),
    ("approval_blockers", "question_id"),
])
async def test_governance_id_views_require_their_id(maker, view, arg):
    res = await evidence_tools.get_evidence_governance(
        evidence_tools.GetEvidenceGovernanceInput(view=view)
    )
    assert res.ok is False
    assert arg in (res.error or "")


@pytest.mark.parametrize("view", ["protocols", "curation_queue", "drug_facts_queue", "vocabulary"])
async def test_governance_all_simple_views_ok(maker, view):
    res = await evidence_tools.get_evidence_governance(
        evidence_tools.GetEvidenceGovernanceInput(view=view)
    )
    assert res.ok is True, f"{view}: {res.error}"


# =================================================================================
# get_evidence_alignment
# =================================================================================
async def test_alignment_report_ok_on_empty_db(maker):
    res = await evidence_tools.get_evidence_alignment(
        evidence_tools.GetEvidenceAlignmentInput(view="alignment")
    )
    assert res.ok is True
    # The honesty rule: coverage is named in the summary, not just the payload.
    assert "coverage" in res.summary.lower()


async def test_alignment_vocabulary_lists_claim_types(maker):
    res = await evidence_tools.get_evidence_alignment(
        evidence_tools.GetEvidenceAlignmentInput(view="vocabulary")
    )
    assert res.ok is True
    assert res.data["result"]["claim_types"]


async def test_alignment_response_claims_requires_response_id(maker):
    res = await evidence_tools.get_evidence_alignment(
        evidence_tools.GetEvidenceAlignmentInput(view="response_claims")
    )
    assert res.ok is False
    assert "response_id" in (res.error or "")


async def test_alignment_unknown_view_errors(maker):
    res = await evidence_tools.get_evidence_alignment(
        evidence_tools.GetEvidenceAlignmentInput(view="huh")
    )
    assert res.ok is False
    assert "view must be one of" in (res.error or "")


# =================================================================================
# get_competitor_discovery
# =================================================================================
async def test_competitor_reasons_expose_the_weights(maker):
    res = await evidence_tools.get_competitor_discovery(
        evidence_tools.GetCompetitorDiscoveryInput(view="reasons")
    )
    assert res.ok is True
    assert res.data["result"]["reasons"]


async def test_competitor_rejects_unknown_review_status(maker):
    res = await evidence_tools.get_competitor_discovery(
        evidence_tools.GetCompetitorDiscoveryInput(view="candidates", review_status="MAYBE")
    )
    assert res.ok is False
    assert "review_status" in (res.error or "")


async def test_competitor_class_map_requires_indication(maker):
    res = await evidence_tools.get_competitor_discovery(
        evidence_tools.GetCompetitorDiscoveryInput(view="class_map")
    )
    assert res.ok is False
    assert "indication" in (res.error or "")


@pytest.mark.parametrize("view", ["candidates", "reasons", "config_proposal"])
async def test_competitor_simple_views_ok(maker, view):
    res = await evidence_tools.get_competitor_discovery(
        evidence_tools.GetCompetitorDiscoveryInput(view=view)
    )
    assert res.ok is True, f"{view}: {res.error}"


# =================================================================================
# get_evidence_synthesis
# =================================================================================
async def test_synthesis_requires_indication(maker):
    res = await evidence_tools.get_evidence_synthesis(
        evidence_tools.GetEvidenceSynthesisInput(view="synthesis")
    )
    assert res.ok is False
    assert "indication" in (res.error or "")


async def test_synthesis_published_list_ok_empty(maker):
    res = await evidence_tools.get_evidence_synthesis(
        evidence_tools.GetEvidenceSynthesisInput(view="published")
    )
    assert res.ok is True
    assert res.data["result"] == []


async def test_synthesis_assess_needs_both_treatments(maker):
    res = await evidence_tools.get_evidence_synthesis(
        evidence_tools.GetEvidenceSynthesisInput(view="assess", indication="Psoriatic Arthritis")
    )
    assert res.ok is False
    assert "treatment_a" in (res.error or "")


async def test_synthesis_unknown_view_errors(maker):
    res = await evidence_tools.get_evidence_synthesis(
        evidence_tools.GetEvidenceSynthesisInput(view="nah")
    )
    assert res.ok is False
    assert "view must be one of" in (res.error or "")


# =================================================================================
# Activation & Impact
# =================================================================================
async def test_interventions_list_ok_empty(maker):
    res = await activation_tools.get_interventions(activation_tools.GetInterventionsInput(view="list"))
    assert res.ok is True
    assert res.data["result"]["count"] == 0


async def test_interventions_rejects_unknown_status(maker):
    res = await activation_tools.get_interventions(
        activation_tools.GetInterventionsInput(view="list", status="SHIPPED")
    )
    assert res.ok is False
    assert "status must be one of" in (res.error or "")


@pytest.mark.parametrize("view", ["detail", "result", "timeline"])
async def test_interventions_detail_views_need_an_id(maker, view):
    res = await activation_tools.get_interventions(activation_tools.GetInterventionsInput(view=view))
    assert res.ok is False
    assert "intervention_id" in (res.error or "")


async def test_interventions_unknown_view_errors(maker):
    res = await activation_tools.get_interventions(activation_tools.GetInterventionsInput(view="what"))
    assert res.ok is False
    assert "view must be one of" in (res.error or "")


async def test_manage_intervention_unknown_action_errors(maker):
    res = await activation_tools.manage_intervention(
        activation_tools.ManageInterventionInput(action="delete", intervention_id="I-1")
    )
    assert res.ok is False
    assert "action must be one of" in (res.error or "")


async def test_manage_intervention_create_needs_a_recommendation(maker):
    res = await activation_tools.manage_intervention(
        activation_tools.ManageInterventionInput(action="create_from_recommendation")
    )
    assert res.ok is False
    assert "recommendation_id" in (res.error or "")


async def test_manage_intervention_publish_needs_a_url(maker):
    res = await activation_tools.manage_intervention(
        activation_tools.ManageInterventionInput(action="publish", intervention_id="I-1")
    )
    assert res.ok is False
    assert "publication_url" in (res.error or "")


async def test_manage_intervention_missing_row_is_reported(maker):
    res = await activation_tools.manage_intervention(
        activation_tools.ManageInterventionInput(action="measure_now", intervention_id="I-NOPE")
    )
    assert res.ok is False


# =================================================================================
# Curation coverage + generation
# =================================================================================
async def test_curation_coverage_reports_gaps(maker):
    res = await question_tools.get_curation_coverage(
        question_tools.GetCurationCoverageInput(limit=5)
    )
    assert res.ok is True
    result = res.data["result"]
    assert "gaps" in result and "estimated_model_calls" in result
    assert "comparison" in res.summary


async def test_generate_curation_questions_dry_run_does_not_bill(maker, monkeypatch):
    called = {}

    async def _fake_generate(db, **kwargs):
        called.update(kwargs)
        return {"generated": 0, "cells": 0, "committed": kwargs.get("commit")}

    monkeypatch.setattr("app.curation.service.generate", _fake_generate)
    res = await question_tools.generate_curation_questions(
        question_tools.GenerateCurationQuestionsInput(limit=5)
    )
    assert res.ok is True
    assert called["commit"] is False
    assert "DRY RUN" in res.summary and "nothing was billed" in res.summary


async def test_generate_curation_questions_clamps_the_batch(maker, monkeypatch):
    from app.curation import service as curation_service

    called = {}

    async def _fake_generate(db, **kwargs):
        called.update(kwargs)
        return {"generated": 0}

    monkeypatch.setattr("app.curation.service.generate", _fake_generate)
    await question_tools.generate_curation_questions(
        question_tools.GenerateCurationQuestionsInput(limit=9999)
    )
    assert called["limit"] == curation_service.MAX_CELLS_PER_RUN


# =================================================================================
# Harvest run-to-pipeline
# =================================================================================
async def test_run_to_pipeline_rejects_an_empty_selection(maker):
    res = await question_tools.run_questions_to_pipeline(
        question_tools.RunQuestionsToPipelineInput(item_ids=[], reviewer_name="MA")
    )
    assert res.ok is False


async def test_run_to_pipeline_reports_when_everything_was_skipped(maker, monkeypatch):
    async def _all_skipped(db, item_ids, *, reviewer_name=None):
        return {
            "question_ids": [],
            "promoted": [],
            "skipped": [{"id": 1, "question_text": "…", "reason": "adverse event"}],
        }

    monkeypatch.setattr("app.services.harvest_service.promote_and_approve_batch", _all_skipped)
    res = await question_tools.run_questions_to_pipeline(
        question_tools.RunQuestionsToPipelineInput(item_ids=[1], reviewer_name="MA")
    )
    assert res.ok is False
    assert "adverse event" in (res.error or "")


# =================================================================================
# Evidence actions
# =================================================================================
async def test_run_evidence_ingest_unknown_action_errors(maker):
    res = await evidence_tools.run_evidence_ingest(
        evidence_tools.RunEvidenceIngestInput(action="teleport")
    )
    assert res.ok is False
    assert "action must be one of" in (res.error or "")


async def test_run_evidence_ingest_trials_requires_an_indication(maker):
    res = await evidence_tools.run_evidence_ingest(
        evidence_tools.RunEvidenceIngestInput(action="trials")
    )
    assert res.ok is False
    assert "indication" in (res.error or "")


async def test_run_evidence_ingest_inherits_the_route_validation(maker):
    """Reaching the router means an undeclared indication is refused with the known set."""
    res = await evidence_tools.run_evidence_ingest(
        evidence_tools.RunEvidenceIngestInput(action="trials", indication="Not A Real Disease")
    )
    assert res.ok is False
    assert "not a declared indication" in (res.error or "").lower()


async def test_run_evidence_ingest_inherits_the_single_slot_guard(maker, monkeypatch):
    """Only one ingestion job runs at a time — a re-parse racing an ingest fights over rows."""
    from app.api import evidence_ingestion as ing

    monkeypatch.setitem(ing._JOB, "running", True)
    monkeypatch.setitem(ing._JOB, "kind", "trials")
    res = await evidence_tools.run_evidence_ingest(
        evidence_tools.RunEvidenceIngestInput(action="reparse")
    )
    assert res.ok is False
    assert "already running" in (res.error or "").lower()


async def test_competitor_sweep_defaults_to_preview(maker, monkeypatch):
    called = {}

    async def _fake_discover(db, *, indication=None, commit=True):
        called.update(indication=indication, commit=commit)
        return {"candidates": 0, "committed": commit}

    monkeypatch.setattr("app.services.competitor_discovery_service.discover", _fake_discover)
    res = await evidence_tools.run_evidence_ingest(
        evidence_tools.RunEvidenceIngestInput(action="competitor_sweep")
    )
    assert res.ok is True
    assert called["commit"] is False
    assert res.nav_target == "/evidence/competitors"


async def test_evaluate_claims_unknown_scope_errors(maker):
    res = await evidence_tools.evaluate_claims(evidence_tools.EvaluateClaimsInput(scope="everything"))
    assert res.ok is False
    assert "scope must be" in (res.error or "")


async def test_evaluate_claims_run_requires_run_id(maker):
    res = await evidence_tools.evaluate_claims(evidence_tools.EvaluateClaimsInput(scope="run"))
    assert res.ok is False
    assert "run_id" in (res.error or "")


async def test_evaluate_claims_unknown_response_is_reported(maker):
    res = await evidence_tools.evaluate_claims(
        evidence_tools.EvaluateClaimsInput(scope="response", response_id="R-NOPE")
    )
    assert res.ok is False
    assert "R-NOPE" in (res.error or "")


async def test_evaluate_claims_clamps_the_limit(maker, monkeypatch):
    called = {}

    async def _fake_run(db, run_id, *, limit=200):
        called.update(run_id=run_id, limit=limit)
        return {"evaluated": 2, "finding_count": 1}

    monkeypatch.setattr("app.services.claim_evaluation_service.evaluate_run", _fake_run)
    res = await evidence_tools.evaluate_claims(
        evidence_tools.EvaluateClaimsInput(scope="run", run_id="RUN-1", limit=99999)
    )
    assert res.ok is True
    assert called["limit"] == 1000  # the route's own ceiling
    assert res.nav_target == "/evidence/alignment"


async def test_generate_evidence_questions_dry_run_by_default(maker, monkeypatch):
    called = {}

    async def _fake_generate(db, *, network_id, commit=False):
        called.update(network_id=network_id, commit=commit)
        return {"generated": 4}

    monkeypatch.setattr("app.services.evidence_question_service.generate_for_network", _fake_generate)
    res = await evidence_tools.generate_evidence_questions(
        evidence_tools.GenerateEvidenceQuestionsInput(network_id="NET-1")
    )
    assert res.ok is True
    assert called["commit"] is False
    assert "PREVIEW" in res.summary and "nothing was staged" in res.summary


async def test_curate_evidence_unknown_action_errors(maker):
    res = await evidence_tools.curate_evidence(evidence_tools.CurateEvidenceInput(action="bless"))
    assert res.ok is False
    assert "action must be one of" in (res.error or "")


@pytest.mark.parametrize("action,arg", [
    ("study_check", "study_id"), ("drug_fact_check", "fact_id"),
])
async def test_curate_evidence_requires_its_subject(maker, action, arg):
    res = await evidence_tools.curate_evidence(
        evidence_tools.CurateEvidenceInput(action=action, verified_by="Ana")
    )
    assert res.ok is False
    assert arg in (res.error or "")


async def test_curate_evidence_reject_requires_a_reason(maker):
    res = await evidence_tools.curate_evidence(
        evidence_tools.CurateEvidenceInput(action="study_reject", study_id="S-1", rejected_by="Ana")
    )
    assert res.ok is False
    assert "reason" in (res.error or "")


async def test_curate_evidence_membership_requires_the_full_triple(maker):
    res = await evidence_tools.curate_evidence(
        evidence_tools.CurateEvidenceInput(action="membership_decision", network_id="N-1", decided_by="Ana")
    )
    assert res.ok is False
    assert "study_id" in (res.error or "")


async def test_curate_evidence_refusal_is_reported_not_raised(maker):
    """An unverifiable study is a refusal with a reason, never an exception."""
    res = await evidence_tools.curate_evidence(
        evidence_tools.CurateEvidenceInput(action="study_check", study_id="S-NOPE", verified_by="Ana")
    )
    assert res.ok is False
    assert res.error


async def test_review_evidence_unknown_action_errors(maker):
    res = await evidence_tools.review_evidence(evidence_tools.ReviewEvidenceInput(action="bless"))
    assert res.ok is False
    assert "action must be one of" in (res.error or "")


async def test_review_evidence_network_actions_need_a_network(maker):
    res = await evidence_tools.review_evidence(
        evidence_tools.ReviewEvidenceInput(action="network_submit", submitted_by="Ana")
    )
    assert res.ok is False
    assert "network_id" in (res.error or "")


async def test_review_evidence_requires_an_approve_decision(maker):
    res = await evidence_tools.review_evidence(
        evidence_tools.ReviewEvidenceInput(action="network_medical", network_id="N-1", reviewer="Ana")
    )
    assert res.ok is False
    assert "approve" in (res.error or "")


async def test_review_evidence_rejection_requires_a_note(maker):
    res = await evidence_tools.review_evidence(
        evidence_tools.ReviewEvidenceInput(
            action="network_medical", network_id="N-1", reviewer="Ana", approve=False
        )
    )
    assert res.ok is False
    assert "note" in (res.error or "")


async def test_review_evidence_reopen_requires_a_reason(maker):
    res = await evidence_tools.review_evidence(
        evidence_tools.ReviewEvidenceInput(action="network_reopen", network_id="N-1", reopened_by="Ana")
    )
    assert res.ok is False
    assert "reason" in (res.error or "")


async def test_review_evidence_protocol_decision_needs_role_and_decision(maker):
    res = await evidence_tools.review_evidence(
        evidence_tools.ReviewEvidenceInput(
            action="protocol_decision", protocol_id="P-1", reviewer="Ana"
        )
    )
    assert res.ok is False
    assert "approval_role" in (res.error or "")


async def test_review_evidence_config_applied_needs_candidates(maker):
    res = await evidence_tools.review_evidence(
        evidence_tools.ReviewEvidenceInput(action="competitor_config_applied", applied_by="Ana")
    )
    assert res.ok is False
    assert "candidate_ids" in (res.error or "")


# =================================================================================
# Influence Graph (folded into the existing Source Authority tool)
# =================================================================================
async def test_influence_graph_view_ok(maker):
    res = await read_tools.get_source_authority(
        read_tools.GetSourceAuthorityInput(view="influence_graph")
    )
    assert res.ok is True
    assert res.data["view"] == "influence_graph"


async def test_influence_node_evidence_requires_node_type(maker):
    res = await read_tools.get_source_authority(
        read_tools.GetSourceAuthorityInput(view="influence_node_evidence", key="Efficacy")
    )
    assert res.ok is False
    assert "node_type" in (res.error or "")


async def test_influence_node_evidence_requires_key(maker):
    res = await read_tools.get_source_authority(
        read_tools.GetSourceAuthorityInput(view="influence_node_evidence", node_type="theme")
    )
    assert res.ok is False
    assert "key" in (res.error or "")


async def test_influence_node_evidence_ok_with_both(maker):
    res = await read_tools.get_source_authority(
        read_tools.GetSourceAuthorityInput(
            view="influence_node_evidence", node_type="theme", key="Efficacy"
        )
    )
    assert res.ok is True
