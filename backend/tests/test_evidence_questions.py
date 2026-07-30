"""Evidence-driven question generation (Phase 7).

Two things these tests exist to protect, both of which are ways a question bank can end up
asserting something nobody checked:

1. **A question generated from an unreleasable result.** An exploratory number cannot
   generate an approved question, and the refusal has to live in the constructor rather
   than in a caller that might forget.
2. **A gap question generated from our own backlog.** ``NETWORK_DISCONNECTED`` is a gap
   status whether the network is genuinely disconnected or whether we simply have not
   verified anything yet. Only the first is a fact about the evidence, and asserting *"no
   evidence compares these treatments"* on the strength of the second puts a curation
   ticket into a monitored corpus dressed as a finding.
"""
from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.evidence import lifecycles
from app.evidence import question_generation as qg
from app.models.clinical_study import ClinicalStudy
from app.models.competitor_candidate import ACCEPTED, NEW, CompetitorCandidate
from app.models.database import Base
from app.models.drug_fact import DrugFact
from app.models.evidence_network import EvidenceNetwork
from app.models.harvested_question import HarvestedQuestion
from app.models.question import Question
from app.models.question_evidence import QuestionEvidence
from app.schemas import HarvestPromote, QuestionUpdate
from app.services import evidence_question_service as svc
from app.services import harvest_service, question_service

INDICATION = "Psoriatic Arthritis"
OUTCOME = "PSA_ACR50_W16"
NETWORK = "NET-PSA-TEST"


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from app.models import (  # noqa: F401 — register tables on Base.metadata
        audit_log,
        clinical_study,
        competitor_candidate,
        drug_fact,
        evidence_network,
        harvested_question,
        nma_result,
        prompt_volume,
        question,
        question_evidence,
        question_variation,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


# =====================================================================================
# Fixtures shaped like the real payloads
# =====================================================================================
def _answer(**overrides) -> dict:
    """A releasable Level-1 answer, in the shape ``resolve_comparison`` returns."""
    base = {
        "status": "DIRECT_EVIDENCE_AVAILABLE",
        "evidence_level": 1,
        "treatment": "Rinvoq",
        "comparator": "Humira",
        "reason": "One trial randomised both treatments.",
        "is_success": True,
        "is_releasable": True,
        "is_internal_output": False,
        "effect_measure": "risk_ratio",
        "estimate": 1.42,
        "ci_lower": 1.11,
        "ci_upper": 1.82,
        "interval_type": "CI",
        "contributing_studies": ["NCT03104400"],
        "flags": [],
        "heterogeneity": None,
        "anchor": None,
    }
    base.update(overrides)
    return base


def _gap(**overrides) -> dict:
    base = {
        "status": "NETWORK_DISCONNECTED",
        "evidence_level": 4,
        "treatment": "Rinvoq",
        "comparator": "Tremfya",
        "reason": "No path of shared comparators connects the two treatments.",
        "is_success": False,
        "is_releasable": False,
        "is_internal_output": False,
        "flags": [],
    }
    base.update(overrides)
    return base


# =====================================================================================
# The role enum, structurally
# =====================================================================================
def test_a_reference_cannot_carry_a_role_outside_the_enum():
    """The point of one required value is that the invalid state is not constructible."""
    with pytest.raises(qg.GenerationError, match="relationship_role"):
        qg.EvidenceRef(qg.CLINICAL_STUDY, "NCT1", "SUPPORTS_AND_CONTRADICTS")


def test_a_reference_cannot_name_an_evidence_family_that_does_not_exist():
    with pytest.raises(qg.GenerationError, match="evidence_type"):
        qg.EvidenceRef("SOMEONES_SPREADSHEET", "row-4", qg.SUPPORTS_EXPECTED_ANSWER)


def test_a_reference_cannot_be_anonymous():
    with pytest.raises(qg.GenerationError, match="evidence_id"):
        qg.EvidenceRef(qg.CLINICAL_STUDY, "  ", qg.SUPPORTS_EXPECTED_ANSWER)


# =====================================================================================
# Releasability
# =====================================================================================
def test_an_exploratory_result_cannot_generate_a_question():
    """The single rule Phase 6 built `is_releasable` to carry into Phase 7."""
    exploratory = _answer(
        status="EXPLORATORY_RESULT_COMPLETED", evidence_level=3, is_releasable=False
    )
    with pytest.raises(qg.GenerationError, match="not releasable"):
        qg.comparative_question(
            exploratory, indication=INDICATION, canonical_outcome_id=OUTCOME
        )


def test_a_governed_result_generates_a_question_naming_its_trial():
    q = qg.comparative_question(
        _answer(), indication=INDICATION, canonical_outcome_id=OUTCOME,
        network_id=NETWORK,
    )
    assert q.category == qg.COMPARATIVE_EFFICACY
    assert "Rinvoq" in q.question_text and "Humira" in q.question_text
    assert INDICATION in q.question_text
    # Every number in the expected answer is one the resolver produced.
    assert "1.42" in q.expected_answer
    assert "1.11 to 1.82" in q.expected_answer
    assert "NCT03104400" in q.expected_answer
    assert any(r.evidence_id == "NCT03104400" for r in q.evidence)


def test_a_pooled_direct_estimate_carries_the_internal_output_label():
    """Our meta-analysis of three trials is not the same object as one trial's result."""
    q = qg.comparative_question(
        _answer(is_internal_output=True, flags=["POOLED_ACROSS_MULTIPLE_STUDIES"]),
        indication=INDICATION, canonical_outcome_id=OUTCOME,
    )
    assert qg.INTERNAL_OUTPUT_LABEL in q.expected_answer
    assert "POOLED_ACROSS_MULTIPLE_STUDIES" in q.flags


def test_an_interval_crossing_no_effect_never_claims_a_winner():
    """The expected answer may not be stronger than the interval it came from.

    This is the case Phase 8's certainty calibration grades against, so if the expected
    answer itself over-claimed, the grader would mark a correctly-hedged model wrong.
    """
    q = qg.comparative_question(
        _answer(estimate=1.05, ci_lower=0.88, ci_upper=1.26),
        indication=INDICATION, canonical_outcome_id=OUTCOME,
    )
    assert "Neither is shown to be more effective" in q.expected_answer


def test_no_interval_is_not_the_same_as_an_interval_that_excludes_the_null():
    assert qg.crosses_no_effect(_answer(ci_lower=None, ci_upper=None)) is None
    assert qg.crosses_no_effect(_answer()) is False
    assert qg.crosses_no_effect(_answer(ci_lower=0.9, ci_upper=1.2)) is True


def test_a_risk_difference_is_tested_against_zero_not_one():
    """Reading a difference against 1.0 would call every real effect null."""
    difference = _answer(effect_measure="risk_difference", estimate=0.12,
                         ci_lower=0.04, ci_upper=0.20)
    assert qg.crosses_no_effect(difference) is False


# =====================================================================================
# Gap attribution — the finding this phase turns on
# =====================================================================================
def test_an_unverified_corpus_is_a_curation_gap_not_an_evidence_gap():
    scoping = {"skipped": [
        {"study_id": "NCT1", "reason": "verification_status is EXTRACTED"},
        {"study_id": "NCT2", "reason": "verification_status is EXTRACTED"},
    ]}
    attribution, reason = qg.attribute_gap("NETWORK_DISCONNECTED", scoping)
    assert attribution == qg.ATTRIBUTION_CURATION
    assert "our own process" in reason


def test_a_gap_created_by_the_approved_window_is_a_protocol_finding():
    """Issue 1, surfaced where a question would otherwise have been generated from it."""
    scoping = {"skipped": [
        {"study_id": "NCT03104400",
         "reason": "reports week 12, outside the approved window [14, 18]"},
    ]}
    attribution, reason = qg.attribute_gap("NETWORK_DISCONNECTED", scoping)
    assert attribution == qg.ATTRIBUTION_PROTOCOL
    assert "the evidence exists" in reason


def test_curation_outranks_the_protocol_because_it_is_checked_first_upstream():
    """An unverified study never reaches the window check, so it cannot yet be known
    whether the protocol would have excluded anything."""
    scoping = {"skipped": [
        {"study_id": "NCT1", "reason": "verification_status is EXTRACTED"},
        {"study_id": "NCT2", "reason": "reports week 12, outside the approved window [14, 18]"},
    ]}
    assert qg.attribute_gap("NETWORK_DISCONNECTED", scoping)[0] == qg.ATTRIBUTION_CURATION


def test_a_genuinely_thin_corpus_is_an_evidence_gap():
    attribution, _ = qg.attribute_gap("NETWORK_DISCONNECTED", {"skipped": []})
    assert attribution == qg.ATTRIBUTION_EVIDENCE


def test_a_gap_question_is_refused_when_the_gap_is_our_backlog():
    scoping = {"skipped": [{"study_id": "NCT1", "reason": "verification_status is EXTRACTED"}]}
    with pytest.raises(qg.GenerationError, match="attributable to CURATION"):
        qg.evidence_gap_question(
            _gap(), indication=INDICATION, canonical_outcome_id=OUTCOME, scoping=scoping
        )


def test_a_real_gap_asks_the_question_a_person_asks_and_answers_it_honestly():
    q = qg.evidence_gap_question(
        _gap(), indication=INDICATION, canonical_outcome_id=OUTCOME,
        scoping={"skipped": []}, network_id=NETWORK,
    )
    # The text sent to a model must be the ordinary question — that is the only way a
    # model asserting an unsupported ranking gets caught.
    assert "more effective than" in q.question_text
    assert "what evidence would be required" not in q.question_text.lower()
    assert "absence of evidence, not evidence of equivalence" in q.expected_answer
    assert q.required_evidence and "published synthesis containing both" in q.required_evidence
    assert q.evidence[0].relationship_role == qg.DEFINES_EVIDENCE_GAP


# =====================================================================================
# A node set is not a question set
# =====================================================================================
def test_placebo_is_an_anchor_not_a_question():
    """Found on the live dev corpus: the first generation produced *"is Bimzelx or Placebo
    more effective?"*. Placebo is in almost every network because every indirect estimate
    chains through it, so without this screen a majority of generated questions would be
    contrasts nobody asks and every one would cost a model call per run."""
    monitorable, reason = qg.is_monitorable_pair("Bimzelx", "Placebo")
    assert monitorable is False
    assert "anchor" in reason


def test_a_drug_class_node_is_not_a_product_question():
    monitorable, reason = qg.is_monitorable_pair("Rinvoq", "TNFi")
    assert monitorable is False
    assert "drug class" in reason


def test_two_real_products_are_monitorable():
    assert qg.is_monitorable_pair("Rinvoq", "Humira") == (True, None)


# =====================================================================================
# Drug facts and safety — refusing rather than inferring from silence
# =====================================================================================
def test_an_unextracted_indication_list_supports_no_approval_claim_either_way():
    """An empty list is a parsing failure, not a regulatory fact about non-approval."""
    with pytest.raises(qg.GenerationError, match="no extracted approved-indication list"):
        qg.drug_fact_question(
            brand="Rinvoq", indication=INDICATION, fact_id="F1", approved_indications=[]
        )


def test_a_label_that_does_not_list_the_indication_answers_no_and_says_what_it_does_list():
    q = qg.drug_fact_question(
        brand="Skyrizi", indication="Ankylosing Spondylitis", fact_id="F2",
        approved_indications=["Plaque Psoriasis", "Psoriatic Arthritis"],
        label_updated_at=date(2025, 3, 1),
    )
    assert q.expected_answer.startswith("No.")
    assert "Plaque Psoriasis" in q.expected_answer
    assert q.expected_evidence_type == qg.REGULATORY_LABEL


def test_a_drug_fact_that_contradicts_itself_cannot_answer_a_safety_question():
    """Boxed-warning text with the flag unset is a defect in the row, not a call to make."""
    with pytest.raises(qg.GenerationError, match="contradicts itself"):
        qg.safety_question(
            brand="Rinvoq", indication=INDICATION, fact_id="F3",
            boxed_warnings=["Serious infections"], has_boxed_warning=False,
        )


def test_a_recorded_warning_with_no_extracted_text_still_answers_yes():
    q = qg.safety_question(
        brand="Rinvoq", indication=INDICATION, fact_id="F4",
        boxed_warnings=[], has_boxed_warning=True,
    )
    assert q.expected_answer.startswith("Yes.")
    assert "not extracted" in q.expected_answer


# =====================================================================================
# Population — the axis networks can actually be split on
# =====================================================================================
def test_a_population_the_store_cannot_filter_to_is_refused():
    with pytest.raises(qg.GenerationError, match="not a canonical population stratum"):
        qg.population_question(
            brand="Rinvoq", comparator=None, indication=INDICATION,
            stratum_id="BSA >= 3%", stratum_label="BSA at least 3%", study_ids=["NCT1"],
        )


# =====================================================================================
# Dedupe
# =====================================================================================
def test_the_same_evidence_generates_the_same_key_twice():
    first = qg.comparative_question(_answer(), indication=INDICATION, canonical_outcome_id=OUTCOME)
    second = qg.comparative_question(_answer(), indication=INDICATION, canonical_outcome_id=OUTCOME)
    assert first.dedupe_key == second.dedupe_key


def test_a_different_comparator_is_a_different_question():
    first = qg.comparative_question(_answer(), indication=INDICATION, canonical_outcome_id=OUTCOME)
    other = qg.comparative_question(
        _answer(comparator="Tremfya"), indication=INDICATION, canonical_outcome_id=OUTCOME
    )
    assert first.dedupe_key != other.dedupe_key


# =====================================================================================
# Staging
# =====================================================================================
async def _generated() -> qg.GeneratedQuestion:
    return qg.comparative_question(
        _answer(), indication=INDICATION, canonical_outcome_id=OUTCOME,
        therapeutic_area="Rheumatology", network_id=NETWORK,
    )


async def test_a_dry_run_stages_nothing_and_rolls_nothing_back(db_session):
    """A reported dry run that had written would be issue 4 again, in mirror image."""
    db_session.add(ClinicalStudy(study_id="NCT03104400", indication=INDICATION))
    await db_session.flush()

    report = await svc.stage(db_session, [await _generated()], commit=False)

    assert report["staged_created"] == 1
    assert (await db_session.execute(select(HarvestedQuestion))).scalars().all() == []
    # The caller's queued work survived — the dry run did not roll it back.
    assert await db_session.get(ClinicalStudy, "NCT03104400") is not None


async def test_staging_lands_in_the_shared_review_queue_as_classified(db_session):
    await svc.stage(db_session, [await _generated()], commit=True)

    row = (await db_session.execute(select(HarvestedQuestion))).scalars().one()
    assert row.source == svc.SOURCE
    assert row.status == "CLASSIFIED"
    assert row.therapeutic_area == "Rheumatology"
    payload = json.loads(row.evidence_payload)
    assert payload["category"] == qg.COMPARATIVE_EFFICACY
    assert payload["expected_answer"]


async def test_a_decided_row_is_never_overwritten_by_a_re_run(db_session):
    """The rule ingestion and competitor discovery both keep."""
    await svc.stage(db_session, [await _generated()], commit=True)
    row = (await db_session.execute(select(HarvestedQuestion))).scalars().one()
    row.status = "REJECTED"
    await db_session.commit()

    report = await svc.stage(db_session, [await _generated()], commit=True)

    assert report["staged_created"] == 0 and report["staged_refreshed"] == 0
    assert len(report["staged_skipped"]) == 1
    refreshed = (await db_session.execute(select(HarvestedQuestion))).scalars().one()
    assert refreshed.status == "REJECTED"


async def test_an_undecided_row_is_refreshed_rather_than_duplicated(db_session):
    await svc.stage(db_session, [await _generated()], commit=True)
    report = await svc.stage(db_session, [await _generated()], commit=True)

    assert report["staged_refreshed"] == 1
    assert len((await db_session.execute(select(HarvestedQuestion))).scalars().all()) == 1


# =====================================================================================
# Promotion + the approval invariant
# =====================================================================================
async def _promote(db, *, approve: bool = False) -> Question:
    item = (await db.execute(select(HarvestedQuestion))).scalars().one()
    return await harvest_service.promote(
        db, item.id,
        HarvestPromote(persona="Provider", therapeutic_area="Rheumatology",
                       brand_focus="Rinvoq", domain="Comparative"),
        approve=approve,
    )


async def test_promotion_materialises_the_proposal_into_associations(db_session):
    db_session.add(ClinicalStudy(study_id="NCT03104400", indication=INDICATION))
    await svc.stage(db_session, [await _generated()], commit=True)

    question = await _promote(db_session)

    links = (await db_session.execute(
        select(QuestionEvidence).where(QuestionEvidence.question_id == question.question_id)
    )).scalars().all()
    assert {row.evidence_id for row in links} == {"NCT03104400", NETWORK}
    assert question.generation_method == svc.GENERATION_METHOD
    assert question.approval_status == "PENDING"
    # The indication travelled, so the question can be graded against the same network.
    assert question.disease == INDICATION


async def test_an_evidence_question_cannot_be_approved_over_unverified_evidence(db_session):
    db_session.add(ClinicalStudy(
        study_id="NCT03104400", indication=INDICATION,
        verification_status=lifecycles.EXTRACTED,
    ))
    await svc.stage(db_session, [await _generated()], commit=True)
    question = await _promote(db_session)

    with pytest.raises(question_service.QuestionApprovalBlocked) as excinfo:
        await question_service.update_question(
            db_session, question.id,
            QuestionUpdate(approval_status="APPROVED", approver_name="Dr Reviewer"),
        )
    assert "no verified evidence association" in str(excinfo.value)


async def test_verifying_the_study_unblocks_the_same_approval(db_session):
    """The invariant is a gate, not a wall: it opens when the curation is done."""
    db_session.add(ClinicalStudy(
        study_id="NCT03104400", indication=INDICATION,
        verification_status=lifecycles.EXTRACTED,
    ))
    await svc.stage(db_session, [await _generated()], commit=True)
    question = await _promote(db_session)

    study = await db_session.get(ClinicalStudy, "NCT03104400")
    study.verification_status = lifecycles.VERIFIED
    await db_session.commit()

    approved = await question_service.update_question(
        db_session, question.id,
        QuestionUpdate(approval_status="APPROVED", approver_name="Dr Reviewer"),
    )
    assert approved.approval_status == "APPROVED"


async def test_the_invariant_does_not_touch_the_manual_question_bank(db_session):
    """Applying it everywhere would block every question that already exists."""
    manual = Question(
        question_id="Q-manual", question_text="Is Rinvoq once daily?", persona="Patient",
        therapeutic_area="Rheumatology", brand_focus="Rinvoq", domain="General",
        approval_status="PENDING",
    )
    db_session.add(manual)
    await db_session.commit()

    assert await svc.approval_blockers(db_session, manual) == []
    updated = await question_service.update_question(
        db_session, manual.id,
        QuestionUpdate(approval_status="APPROVED", approver_name="MA"),
    )
    assert updated.approval_status == "APPROVED"


async def test_the_auto_approve_shortcut_cannot_route_around_the_invariant(db_session):
    """Run-to-Pipeline sets the column directly, so it needs the check reached its own way."""
    from fastapi import HTTPException

    db_session.add(ClinicalStudy(
        study_id="NCT03104400", indication=INDICATION,
        verification_status=lifecycles.EXTRACTED,
    ))
    await svc.stage(db_session, [await _generated()], commit=True)

    with pytest.raises(HTTPException) as excinfo:
        await _promote(db_session, approve=True)
    assert excinfo.value.status_code == 422
    assert "auto-approve an evidence-generated question" in str(excinfo.value.detail)


async def test_an_association_to_a_missing_row_is_reported_as_missing_not_verified(db_session):
    """A dangling reference is the cost of a polymorphic key, so it is named, not hidden."""
    db_session.add(Question(
        question_id="Q-ev", question_text="?", persona="Provider",
        therapeutic_area="Rheumatology", brand_focus="Rinvoq", domain="Comparative",
        generation_method=svc.GENERATION_METHOD,
    ))
    db_session.add(QuestionEvidence(
        question_id="Q-ev", evidence_type=qg.CLINICAL_STUDY, evidence_id="NCT-gone",
        relationship_role=qg.SUPPORTS_EXPECTED_ANSWER,
    ))
    await db_session.commit()

    links = await svc.associations(db_session, "Q-ev")
    assert links[0]["exists"] is False and links[0]["is_verified"] is False


async def test_a_gap_question_needs_a_ratified_network_because_it_asserts_an_absence(db_session):
    """An absence claim rests on the evidence set being complete, and ratification is the
    review that says so. A DRAFT network cannot back "nothing shows this"."""
    db_session.add(EvidenceNetwork(
        network_id=NETWORK, indication=INDICATION, canonical_outcome_id=OUTCOME,
        ratification_status=lifecycles.DRAFT,
    ))
    gap = qg.evidence_gap_question(
        _gap(), indication=INDICATION, canonical_outcome_id=OUTCOME,
        scoping={"skipped": []}, therapeutic_area="Rheumatology", network_id=NETWORK,
    )
    await svc.stage(db_session, [gap], commit=True)
    question = await _promote(db_session)

    assert await svc.approval_blockers(db_session, question)

    network = (await db_session.execute(
        select(EvidenceNetwork).where(EvidenceNetwork.network_id == NETWORK)
    )).scalar_one()
    network.ratification_status = lifecycles.RATIFIED
    await db_session.commit()

    assert await svc.approval_blockers(db_session, question) == []


# =====================================================================================
# Competitor discovery — an undecided candidate is a proposal, not evidence
# =====================================================================================
async def test_only_accepted_candidates_generate_questions(db_session):
    refused: list[dict] = []
    for status, treatment in ((NEW, "Sonelokimab"), (ACCEPTED, "Bimzelx")):
        db_session.add(CompetitorCandidate(
            candidate_id=f"CC-{treatment}", treatment=treatment, indication=INDICATION,
            discovery_reasons=json.dumps(["DIRECTLY_COMPARED_TREATMENT"]),
            review_status=status,
        ))
    await db_session.commit()

    generated = await svc._from_competitors(db_session, INDICATION, "Rheumatology", refused)

    assert [q.brand for q in generated] == ["Bimzelx"]


async def test_only_verified_drug_facts_generate_questions(db_session):
    refused: list[dict] = []
    db_session.add(DrugFact(
        fact_id="F-unverified", brand="Rinvoq",
        approved_indications=json.dumps([INDICATION]),
        verification_status=lifecycles.EXTRACTED,
    ))
    db_session.add(DrugFact(
        fact_id="F-verified", brand="Skyrizi",
        approved_indications=json.dumps([INDICATION]),
        has_boxed_warning=False,
        verification_status=lifecycles.VERIFIED,
    ))
    await db_session.commit()

    generated = await svc._from_drug_facts(db_session, INDICATION, "Rheumatology", refused)

    assert {q.brand for q in generated} == {"Skyrizi"}
    assert {q.category for q in generated} == {qg.DRUG_FACT, qg.SAFETY}


async def test_a_malformed_stored_json_column_refuses_rather_than_500s(db_session):
    refused: list[dict] = []
    db_session.add(DrugFact(
        fact_id="F-broken", brand="Rinvoq", approved_indications="{not json",
        verification_status=lifecycles.VERIFIED,
    ))
    await db_session.commit()

    generated = await svc._from_drug_facts(db_session, INDICATION, "Rheumatology", refused)

    assert not [q for q in generated if q.category == qg.DRUG_FACT]
    assert any("approved-indication list" in r["reason"] for r in refused)
