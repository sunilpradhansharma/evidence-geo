"""The extraction pipeline, its baseline, and the harness that decides which ships.

Every model call is stubbed. These test the *shape* of the pipeline — that a proposal can
never be applied, that a disagreement blocks promotion, that provenance travels, and that
the verdict is decided by numbers rather than by preference. Whether the agents are
actually more accurate is a measurement, not a test, and it lives in
``scripts/extraction_harness.py`` where it costs real model calls.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.evidence import agents, harness, licensing
from app.evidence.agents import ExtractionTask
from app.evidence.extraction import AUTO_REJECTED, MAPPED, VERIFIED

FIXTURES = Path(__file__).parent / "fixtures"


def _task(**overrides) -> ExtractionTask:
    base = dict(
        source_id="NCT03104400",
        document="ACR20 at week 12: placebo 36.2, upadacitinib 15 mg 70.6.",
        fields=("endpoint", "timepoint_week", "placebo_response_rate"),
        license_class=licensing.PUBLIC_DOMAIN,
    )
    base.update(overrides)
    return ExtractionTask(**base)


def _stub(monkeypatch, *responses):
    """Return successive canned model responses, and record the prompts they answered."""
    calls: list[tuple[str, str]] = []
    queue = list(responses)

    async def _chat_json(system, user, **kwargs):
        calls.append((system, user))
        return queue.pop(0) if queue else {}

    monkeypatch.setattr("app.insights.llm.chat_json", _chat_json)
    return calls


# =====================================================================================
# The baseline
# =====================================================================================
async def test_the_baseline_extracts_values_with_provenance(monkeypatch):
    """``run_baseline`` is referenced in extraction.py's own docstring and did not exist."""
    _stub(monkeypatch, {
        "values": {"endpoint": "ACR20", "timepoint_week": 12, "placebo_response_rate": 36.2},
        "confidence": {"endpoint": 0.95},
        "source_text": {"endpoint": "ACR20 at week 12"},
    })

    result = await agents.run_baseline(_task())

    assert {v.field_name for v in result.values} == {
        "endpoint", "timepoint_week", "placebo_response_rate"
    }
    endpoint = next(v for v in result.values if v.field_name == "endpoint")
    assert endpoint.value == "ACR20"
    assert endpoint.confidence == 0.95
    assert endpoint.source_text == "ACR20 at week 12"
    # Without this a non-deterministic pipeline is unauditable.
    assert endpoint.provenance.stage == "BASELINE"
    assert endpoint.provenance.prompt_version == agents.BASELINE_PROMPT_VERSION
    assert endpoint.provenance.pipeline_version


async def test_a_declined_field_is_absent_rather_than_null(monkeypatch):
    """"Not extracted" and "extracted as nothing" are different facts."""
    _stub(monkeypatch, {"values": {"endpoint": "ACR20", "timepoint_week": None}})

    result = await agents.run_baseline(_task())

    assert {v.field_name for v in result.values} == {"endpoint"}


async def test_a_field_nobody_asked_for_is_dropped(monkeypatch):
    """The prompt's field list is the contract; a volunteered extra would move the target."""
    _stub(monkeypatch, {"values": {"endpoint": "ACR20", "sponsor_favourite_colour": "teal"}})

    result = await agents.run_baseline(_task())

    assert {v.field_name for v in result.values} == {"endpoint"}


async def test_a_provider_failure_degrades_rather_than_raising(monkeypatch):
    async def _boom(system, user, **kwargs):
        raise RuntimeError("bedrock is having a day")

    monkeypatch.setattr("app.insights.llm.chat_json", _boom)

    result = await agents.run_baseline(_task())

    assert result.values == []
    assert result.errors and "bedrock" in result.errors[0]


# =====================================================================================
# Harmonisation proposes and can never apply
# =====================================================================================
async def test_a_proposal_outside_the_approved_window_is_auto_rejected(monkeypatch):
    """The protocol wins without a human, because escalation invites overruling it.

    The proposal here asks to align week 16 onto week 24 — a target the protocol's
    [14, 18] window does not admit. That is the case the screen exists for: the agent may
    argue for it, and the protocol still refuses without anyone being asked.
    """
    _stub(
        monkeypatch,
        {"values": {"timepoint_week": 16}},
        {"proposals": [{
            "kind": "TIMEPOINT", "from_value": 16, "to_value": 24,
            "rationale": "commonly pooled in published PsA NMAs",
            "confidence": 0.8, "guideline_citation": "ISPOR ITC Task Force",
        }]},
        {"checks": []},
    )

    result = await agents.run_pipeline(
        _task(fields=("timepoint_week",), approved_time_window=(14.0, 18.0))
    )

    assert len(result.proposals) == 1
    proposal = result.proposals[0]
    assert proposal.disposition == AUTO_REJECTED
    assert proposal.is_actionable_by_human is False
    assert result.actionable_proposals == []
    # No escalation path: the rejection is terminal and says why.
    assert "the protocol governs" in proposal.auto_rejection_reason


async def test_a_proposal_never_becomes_a_value(monkeypatch):
    """The load-bearing constraint, asserted end to end rather than trusted to a docstring."""
    _stub(
        monkeypatch,
        {"values": {"timepoint_week": 12}},
        {"proposals": [{
            "kind": "TIMEPOINT", "from_value": 12, "to_value": 16,
            "rationale": "alignable", "confidence": 0.9,
        }]},
        {"checks": []},
    )

    result = await agents.run_pipeline(
        _task(fields=("timepoint_week",), approved_time_window=(10.0, 18.0))
    )

    # The proposal is actionable here (16 is inside 10-18), and still nothing applied it.
    assert result.actionable_proposals
    assert [v.value for v in result.values] == [12]
    assert not hasattr(result.proposals[0], "apply")


async def test_an_unrecognised_proposal_kind_is_dropped(monkeypatch):
    _stub(
        monkeypatch,
        {"values": {"timepoint_week": 12}},
        {"proposals": [{"kind": "DOSE", "from_value": 15, "to_value": 30,
                        "rationale": "same drug", "confidence": 0.9}]},
        {"checks": []},
    )

    result = await agents.run_pipeline(_task(fields=("timepoint_week",)))

    assert result.proposals == []


# =====================================================================================
# Validation blocks, it does not discount
# =====================================================================================
async def test_a_validation_disagreement_blocks_promotion_to_verified(monkeypatch):
    _stub(
        monkeypatch,
        {"values": {"placebo_response_rate": 36.2}},
        {"proposals": []},
        {"checks": [{"field": "placebo_response_rate", "observed": 63.2, "agrees": False,
                     "note": "the table reads 36.2; 63.2 looks transposed"}]},
    )

    result = await agents.run_pipeline(_task(fields=("placebo_response_rate",)))

    assert len(result.disagreements) == 1
    assert result.next_verification_status == MAPPED
    assert result.next_verification_status != VERIFIED


async def test_agreement_permits_promotion(monkeypatch):
    _stub(
        monkeypatch,
        {"values": {"placebo_response_rate": 36.2}},
        {"proposals": []},
        {"checks": [{"field": "placebo_response_rate", "observed": 36.2, "agrees": True}]},
    )

    result = await agents.run_pipeline(_task(fields=("placebo_response_rate",)))

    assert result.disagreements == []
    assert result.next_verification_status == VERIFIED


async def test_validation_reach_is_bounded_by_licence_class(monkeypatch):
    """A restricted source can only be re-checked against the fragment it was allowed to keep."""
    _stub(
        monkeypatch,
        {"values": {"placebo_response_rate": 12.6}},
        {"proposals": []},
        {"checks": [{"field": "placebo_response_rate", "observed": 12.6, "agrees": True}]},
    )

    result = await agents.run_pipeline(_task(
        fields=("placebo_response_rate",), license_class=licensing.RESTRICTED
    ))

    assert result.validations[0].reach == licensing.validation_reach(licensing.RESTRICTED)
    assert result.coverage_report()["license_class"] == licensing.RESTRICTED


async def test_the_documented_descope_is_a_runner_not_a_comment(monkeypatch):
    """Baseline plus validation must be shippable without deleting code."""
    _stub(
        monkeypatch,
        {"values": {"endpoint": "ACR20"}},
        {"checks": [{"field": "endpoint", "observed": "ACR50", "agrees": False}]},
    )

    result = await agents.run_baseline_with_validation(_task(fields=("endpoint",)))

    assert result.proposals == []
    assert result.disagreements
    assert "baseline_with_validation" in agents.RUNNERS


def test_the_default_runner_is_the_measured_descope_not_the_agent_pipeline():
    """The harness verdict, pinned so re-selecting the pipeline has to be deliberate.

    Run on the real corpus with live model calls: baseline 85.7% (12/14), pipeline 71.4%
    (10/14) — wronger *and* abstaining more. The pre-committed rule ships the pipeline only
    if it is strictly more accurate, so the descope applies. Nothing calls a runner in
    production yet, which is exactly why the decision is recorded here: the first caller
    should inherit it rather than re-open a settled question by picking a name.
    """
    assert agents.DEFAULT_RUNNER_NAME == "baseline_with_validation"
    assert agents.DEFAULT_RUNNER is agents.run_baseline_with_validation
    assert agents.DEFAULT_RUNNER is not agents.run_pipeline
    assert agents.DEFAULT_RUNNER_NAME in agents.RUNNERS


# =====================================================================================
# Grading
# =====================================================================================
def test_a_miss_is_graded_apart_from_a_wrong_answer():
    """An abstainer must not be ranked level with a confident fabricator."""
    assert harness.grade_field("ACR20", "ACR20") == harness.CORRECT
    assert harness.grade_field("ACR20", "ACR50") == harness.WRONG
    assert harness.grade_field("ACR20", None) == harness.MISSED


def test_a_number_returned_as_a_string_still_counts():
    """Otherwise the harness measures JSON formatting rather than extraction."""
    assert harness.grade_field(12, "12") == harness.CORRECT
    assert harness.grade_field(36.2, "36.2") == harness.CORRECT
    assert harness.grade_field(36.2, "not a number") == harness.WRONG


def test_grading_ignores_case_and_whitespace():
    assert harness.grade_field("SELECT-PsA 1", "select-psa  1") == harness.CORRECT


# =====================================================================================
# The verdict
# =====================================================================================
def _report(name: str, grades: dict[str, str], tier=licensing.PUBLIC_DOMAIN):
    return harness.HarnessReport(
        runner=name,
        scores=[harness.CaseScore(case_id="c1", license_class=tier, grades=grades)],
    )


def test_a_strictly_better_pipeline_ships():
    baseline = _report("baseline", {"a": harness.CORRECT, "b": harness.WRONG})
    pipeline = _report("pipeline", {"a": harness.CORRECT, "b": harness.CORRECT})

    decision = harness.verdict(baseline, pipeline)

    assert decision["verdict"] == harness.SHIP_PIPELINE
    assert decision["improvement"] == pytest.approx(0.5)


def test_a_tie_ships_the_baseline():
    """Equal accuracy for three model calls instead of one is a cost with no return."""
    grades = {"a": harness.CORRECT, "b": harness.WRONG}
    decision = harness.verdict(_report("baseline", grades), _report("pipeline", grades))

    assert decision["verdict"] == harness.SHIP_BASELINE
    assert "not more accurate" in decision["reason"]


def test_an_unmeasurable_corpus_ships_the_simpler_runner():
    """Absence of a measurement is not evidence for the more complex option."""
    decision = harness.verdict(_report("baseline", {}), _report("pipeline", {}))

    assert decision["verdict"] == harness.SHIP_BASELINE
    assert "no gradeable fields" in decision["reason"]


def test_accuracy_is_reported_per_licence_tier():
    """One headline figure would average a re-derivable source with one that is not."""
    report = harness.HarnessReport(runner="baseline", scores=[
        harness.CaseScore("public", licensing.PUBLIC_DOMAIN,
                          {"a": harness.CORRECT, "b": harness.CORRECT}),
        harness.CaseScore("restricted", licensing.RESTRICTED,
                          {"a": harness.CORRECT, "b": harness.WRONG}),
    ])

    data = report.as_dict()

    assert data["overall"]["accuracy"] == pytest.approx(0.75)
    assert data["by_license_class"][licensing.PUBLIC_DOMAIN]["accuracy"] == pytest.approx(1.0)
    assert data["by_license_class"][licensing.RESTRICTED]["accuracy"] == pytest.approx(0.5)


# =====================================================================================
# The corpus itself
# =====================================================================================
def test_the_committed_corpus_loads_and_covers_more_than_one_licence_tier():
    corpus = harness.load_corpus(FIXTURES / "extraction_corpus.json")

    assert len(corpus) >= 3
    assert {c.license_class for c in corpus} == {
        licensing.PUBLIC_DOMAIN, licensing.RESTRICTED
    }
    assert all(c.expected for c in corpus)


def test_every_corpus_label_is_present_in_its_own_document():
    """Guards the property that makes these labels ground truth rather than opinion.

    A label the source text does not contain would be a judgement smuggled in as a
    transcription, and the harness would then be scoring models against our beliefs.
    """
    for case in harness.load_corpus(FIXTURES / "extraction_corpus.json"):
        for name, value in case.expected.items():
            assert str(value) in case.document, (
                f"{case.case_id}: expected {name}={value!r} does not appear in the document"
            )


async def test_the_harness_runs_end_to_end_against_a_stub(monkeypatch):
    corpus = harness.load_corpus(FIXTURES / "extraction_corpus.json")

    async def _chat_json(system, user, **kwargs):
        # Answer every case perfectly by echoing the label the document contains.
        case = next(c for c in corpus if c.case_id in user or c.document[:40] in user)
        return {"values": dict(case.expected)}

    monkeypatch.setattr("app.insights.llm.chat_json", _chat_json)

    report = await harness.evaluate(agents.run_baseline, corpus, name="baseline")

    assert report.accuracy == pytest.approx(1.0)
    assert report.as_dict()["cases"] == len(corpus)
