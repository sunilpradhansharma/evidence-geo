"""Claim-level AI-vs-evidence evaluation (Phase 8).

Four ways this phase could produce a confidently wrong finding, and the tests that stop it:

1. **Grading a claim against the wrong authority.** Checking a boxed warning against a
   league table cannot produce a right answer, only a confident one. It raises.
2. **Calling an evidence gap a contradiction.** *"Nothing we hold shows this"* and *"our
   evidence shows the opposite"* send a brand team to do completely different things.
3. **Reading an estimate as a winner without knowing which way is up.** An ACR50 risk ratio
   of 1.4 favours the treatment; an adverse-event risk ratio of 1.4 favours the comparator.
   The arithmetic is identical, so the direction has to be declared, not inferred.
4. **Grading against a number no statistician has approved.** An exploratory result may not
   affect AI scoring, and an alignment dashboard is AI scoring.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import outcomes
from app.evidence import claim_extraction as ce
from app.evidence import claims as cl
from app.evidence import question_generation as qg
from app.models.clinical_study import ClinicalStudy
from app.models.database import Base
from app.models.drug_fact import DrugFact
from app.models.evaluation_claim import EvaluationClaim
from app.models.response import Response
from app.services import claim_evaluation_service as svc
from app.evidence import lifecycles

INDICATION = "Psoriatic Arthritis"
OUTCOME = "PSA_ACR50_W16"


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from app.models import (  # noqa: F401 — register tables on Base.metadata
        audit_log,
        clinical_study,
        competitor_candidate,
        drug_fact,
        evaluation_claim,
        evidence_network,
        nma_result,
        response,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _claim(**overrides) -> cl.ExtractedClaim:
    base = {
        "claim_text": "Rinvoq is more effective than Humira for psoriatic arthritis.",
        "claim_type": cl.DIRECT_COMPARISON_CLAIM,
        "subject": "Rinvoq",
        "comparator": "Humira",
        "indication": INDICATION,
        "direction": cl.SUPERIOR,
        "certainty": cl.DEFINITIVE,
    }
    base.update(overrides)
    return cl.ExtractedClaim(**base)


def _answer(**overrides) -> dict:
    base = {
        "status": "DIRECT_EVIDENCE_AVAILABLE",
        "evidence_level": 1,
        "treatment": "Rinvoq",
        "comparator": "Humira",
        "effect_measure": "risk_ratio",
        "estimate": 1.42,
        "ci_lower": 1.11,
        "ci_upper": 1.82,
        "interval_type": "CI",
        "contributing_studies": ["NCT03104400"],
    }
    base.update(overrides)
    return base


# =====================================================================================
# Routing — the category error
# =====================================================================================
def test_a_safety_claim_cannot_be_graded_against_a_league_table():
    """The plan calls this "an explicit test failure". It raises rather than scoring."""
    with pytest.raises(cl.CategoryError, match="cannot be graded against NMA_RESULT"):
        cl.assert_routable(cl.SAFETY_WARNING_CLAIM, qg.NMA_RESULT)


def test_the_refusal_names_the_authority_that_should_have_been_used():
    with pytest.raises(cl.CategoryError, match="the current regulatory label"):
        cl.assert_routable(cl.APPROVAL_CLAIM, qg.CLINICAL_STUDY)


def test_a_direct_comparison_is_not_answered_by_a_synthesis():
    """A claim that two drugs were compared head-to-head is a claim about a TRIAL. Answering
    it from an indirect estimate would concede the point the claim asserts."""
    assert qg.NMA_RESULT not in cl.authoritative_evidence_for(cl.DIRECT_COMPARISON_CLAIM)
    assert qg.NMA_RESULT in cl.authoritative_evidence_for(cl.RANKING_CLAIM)


def test_every_claim_type_has_a_policy_and_dimensions():
    for claim_type in cl.CLAIM_TYPES:
        assert cl.authoritative_evidence_for(claim_type)
        assert cl.dimensions_for(claim_type)
        assert set(cl.dimensions_for(claim_type)) <= set(cl.DIMENSIONS)


# =====================================================================================
# The extracted claim is validated at the boundary
# =====================================================================================
def test_an_extractor_inventing_a_claim_type_fails_at_the_boundary():
    with pytest.raises(cl.ClaimError, match="claim_type"):
        cl.ExtractedClaim(claim_text="x", claim_type="VIBES_CLAIM", subject="Rinvoq")


def test_a_comparative_claim_with_no_direction_is_not_a_comparative_claim():
    with pytest.raises(cl.ClaimError, match="needs a direction"):
        cl.ExtractedClaim(
            claim_text="Rinvoq and Humira are both used.",
            claim_type=cl.DIRECT_COMPARISON_CLAIM,
            subject="Rinvoq", comparator="Humira",
        )


def test_a_negated_comparison_flips_the_treatment_it_claims_is_better():
    """"Rinvoq is NOT more effective than Humira" asserts the opposite of the same sentence
    without the "not"; reading the direction alone would invert the verdict."""
    asserted = _claim(direction=cl.SUPERIOR, polarity=cl.ASSERTED)
    negated = _claim(direction=cl.SUPERIOR, polarity=cl.NEGATED)
    assert cl.claimed_winner(asserted) == "Rinvoq"
    assert cl.claimed_winner(negated) == "Humira"


# =====================================================================================
# Certainty calibration — the differentiated capability
# =====================================================================================
def test_a_definitive_claim_over_an_interval_that_includes_no_difference_is_overclaimed():
    verdict, reason = cl.calibrate_certainty(cl.DEFINITIVE, interval_crosses_null=True)
    assert verdict == cl.OVERCLAIMED
    assert "includes no difference" in reason


def test_hedging_over_a_clean_interval_is_underclaimed_and_that_is_also_a_finding():
    """Not a harmless conservatism: it usually means our evidence is not reaching the
    model, which is a communication gap Phase 9 acts on. Reporting only over-claiming
    would make this a safety check rather than an alignment measure."""
    verdict, reason = cl.calibrate_certainty(cl.HEDGED, interval_crosses_null=False)
    assert verdict == cl.UNDERCLAIMED
    assert "not reaching the model" in reason


def test_hedging_over_an_interval_that_includes_no_difference_is_correct():
    assert cl.calibrate_certainty(cl.HEDGED, interval_crosses_null=True)[0] == cl.CALIBRATED


def test_no_interval_is_not_calibration():
    """The absence of the input, not a verdict about the response."""
    verdict, reason = cl.calibrate_certainty(cl.DEFINITIVE, interval_crosses_null=None)
    assert verdict == cl.CALIBRATED
    assert "not assessed" in reason


def test_asserting_equivalence_from_a_wide_interval_is_also_an_overclaim():
    """Found on live data: the first run graded "there is definitively no difference" as
    ALIGNED while simultaneously calling it OVERCLAIMED. An interval that includes no
    difference is an absence of evidence, not evidence of equivalence — the same
    distinction the whole evidence-gap category rests on — so a definitive equivalence
    claim over-claims exactly as much as an unsupported winner does."""
    verdict, reason = cl.calibrate_certainty(
        cl.DEFINITIVE, interval_crosses_null=True, claims_a_winner=False
    )
    assert verdict == cl.OVERCLAIMED
    assert "not evidence of equivalence" in reason


def test_reporting_no_clear_difference_without_asserting_equivalence_is_calibrated():
    verdict, _ = cl.calibrate_certainty(
        cl.HEDGED, interval_crosses_null=True, claims_a_winner=False
    )
    assert verdict == cl.CALIBRATED


def test_the_equivalence_overclaim_reaches_the_classification():
    """The verdict and the classification must agree — the live defect was that they did
    not, and a dashboard showing ALIGNED beside OVERCLAIMED is unreadable."""
    crossing = _answer(estimate=0.959, ci_lower=0.778, ci_upper=1.18)
    finding = cl.grade_comparison(
        _claim(direction=cl.SIMILAR, certainty=cl.DEFINITIVE),
        answer=crossing, canonical_outcome_id=OUTCOME,
    )
    assert finding.classification == cl.PARTIALLY_ALIGNED
    assert finding.certainty_verdict == cl.OVERCLAIMED


def test_a_finding_quotes_the_estimate_the_way_the_expected_answer_does():
    """Phase 8 grades against Phase 7's expected answers, so two formatters would make one
    number look like two results."""
    crossing = _answer(estimate=0.9592517401392111, ci_lower=0.7777468676256332,
                       ci_upper=1.183114891570)
    finding = cl.grade_comparison(
        _claim(certainty=cl.HEDGED), answer=crossing, canonical_outcome_id=OUTCOME
    )
    assert "0.959" in finding.reason
    assert "0.9592517" not in finding.reason
    assert qg.describe_estimate(crossing) in finding.reason


def test_a_definitive_claim_resting_on_an_indirect_estimate_is_overclaimed():
    verdict, reason = cl.calibrate_certainty(
        cl.DEFINITIVE, interval_crosses_null=False, is_direct=False
    )
    assert verdict == cl.OVERCLAIMED
    assert "transitivity" in reason


# =====================================================================================
# Reading an estimate as a direction
# =====================================================================================
def test_an_endpoint_that_does_not_declare_its_benefit_direction_is_not_comparable():
    """The bug this guard exists for: an ACR50 risk ratio of 1.4 favours the treatment and
    an adverse-event risk ratio of 1.4 favours the comparator. Same arithmetic."""
    assert cl.favoured_treatment(_answer(), "NOT_A_DECLARED_OUTCOME") is None

    finding = cl.grade_comparison(
        _claim(), answer=_answer(), canonical_outcome_id="NOT_A_DECLARED_OUTCOME"
    )
    assert finding.classification == cl.NOT_COMPARABLE
    assert "benefit_direction" in finding.reason


def test_every_canonical_outcome_declares_its_benefit_direction():
    """A new endpoint cannot be added without saying which way is better."""
    assert outcomes.validate() == []
    for outcome_id in outcomes.outcome_ids():
        assert outcomes.benefit_direction(outcome_id) in outcomes.BENEFIT_DIRECTIONS


def test_an_interval_that_includes_no_difference_favours_nobody():
    crossing = _answer(estimate=1.05, ci_lower=0.88, ci_upper=1.26)
    assert cl.favoured_treatment(crossing, OUTCOME) is None


def test_a_clean_interval_favours_the_arm_the_estimate_points_at():
    assert cl.favoured_treatment(_answer(), OUTCOME) == "Rinvoq"
    flipped = _answer(estimate=0.7, ci_lower=0.55, ci_upper=0.89)
    assert cl.favoured_treatment(flipped, OUTCOME) == "Humira"


# =====================================================================================
# Grading a comparison
# =====================================================================================
def test_an_exploratory_result_may_not_grade_a_response():
    """The execution-mode table is explicit that EXPLORATORY output cannot affect AI
    scoring, and an alignment dashboard is AI scoring."""
    exploratory = _answer(status="EXPLORATORY_RESULT_COMPLETED", evidence_level=3)
    finding = cl.grade_comparison(
        _claim(), answer=exploratory, canonical_outcome_id=OUTCOME
    )
    assert finding.classification == cl.EVIDENCE_UNAVAILABLE
    assert "not releasable" in finding.reason


def test_a_gap_makes_an_asserted_winner_unsupported_never_contradictory():
    """The distinction the plan insists on. "We have nothing" is not "you are wrong"."""
    gap = _answer(status="NETWORK_DISCONNECTED", evidence_level=4,
                  estimate=None, ci_lower=None, ci_upper=None)
    finding = cl.grade_comparison(_claim(), answer=gap, canonical_outcome_id=OUTCOME,
                                  network_id="NET-1")
    assert finding.classification == cl.UNSUPPORTED
    assert "absence of evidence, not a contradiction" in finding.reason
    assert finding.is_adverse


def test_a_gap_finding_carries_the_resolvers_reason_not_the_generic_description():
    """Found on prod. `NETWORK_DISCONNECTED` reads identically whether nobody studied the
    drug or our own protocol window excluded its only trial, and the alignment dashboard
    was printing the status constant — telling a reader "no path of shared comparators
    connects the two treatments" while the same payload held the study id and the window
    that caused it. Those send a reader to two different places and only one is true."""
    excluded = _answer(
        status="NETWORK_DISCONNECTED", evidence_level=4,
        estimate=None, ci_lower=None, ci_upper=None,
        reason=(
            "Rinvoq is randomised by NCT03104400 but contributed no usable row to this "
            "analysis (reports week 12, outside the approved window [14, 18]). No path "
            "of shared comparators can exist without it."
        ),
    )
    finding = cl.grade_comparison(
        _claim(comparator="Tremfya"), answer=excluded,
        canonical_outcome_id=OUTCOME, network_id="NET-1",
    )
    assert finding.classification == cl.UNSUPPORTED
    assert "NCT03104400" in finding.reason
    assert "week 12" in finding.reason
    # The status stays greppable, and the gap is still not a contradiction.
    assert "NETWORK_DISCONNECTED" in finding.reason
    assert "absence of evidence, not a contradiction" in finding.reason
    # The generic line must not be what a reviewer reads.
    assert "No path of shared comparators connects the two treatments." not in finding.reason


def test_a_gap_without_a_resolver_reason_falls_back_to_the_status_description():
    """A caller that supplies no reason must still produce a readable finding rather than
    an empty parenthesis."""
    bare = _answer(status="NETWORK_DISCONNECTED", evidence_level=4,
                   estimate=None, ci_lower=None, ci_upper=None, reason="")
    finding = cl.grade_comparison(_claim(), answer=bare, canonical_outcome_id=OUTCOME)
    assert "No path of shared comparators connects the two treatments" in finding.reason


def test_declining_to_name_a_winner_where_the_evidence_cannot_is_aligned():
    gap = _answer(status="NETWORK_DISCONNECTED", evidence_level=4, estimate=None)
    finding = cl.grade_comparison(
        _claim(direction=cl.SIMILAR, certainty=cl.UNCERTAIN),
        answer=gap, canonical_outcome_id=OUTCOME,
    )
    assert finding.classification == cl.ALIGNED


def test_the_estimate_agreeing_with_the_claim_is_aligned():
    finding = cl.grade_comparison(
        _claim(certainty=cl.PROBABLE), answer=_answer(), canonical_outcome_id=OUTCOME
    )
    assert finding.classification == cl.ALIGNED
    assert "1.42" in finding.reason
    assert finding.certainty_verdict == cl.CALIBRATED


def test_the_estimate_favouring_the_other_arm_is_contradictory():
    flipped = _answer(estimate=0.7, ci_lower=0.55, ci_upper=0.89)
    finding = cl.grade_comparison(
        _claim(certainty=cl.PROBABLE), answer=flipped, canonical_outcome_id=OUTCOME
    )
    assert finding.classification == cl.CONTRADICTORY
    assert "favouring Humira" in finding.reason


def test_asserting_a_winner_where_the_interval_crosses_null_is_partially_aligned():
    """The headline calibration case, and the exact shape of the live PsA result."""
    crossing = _answer(estimate=0.959, ci_lower=0.778, ci_upper=1.18)
    finding = cl.grade_comparison(
        _claim(certainty=cl.DEFINITIVE), answer=crossing, canonical_outcome_id=OUTCOME
    )
    assert finding.classification == cl.PARTIALLY_ALIGNED
    assert finding.certainty_verdict == cl.OVERCLAIMED


def test_a_generic_name_is_not_a_contradiction():
    """A model saying "upadacitinib" is talking about Rinvoq. A string compare here would
    report a contradiction every time a model used the generic name."""
    finding = cl.grade_comparison(
        _claim(subject="upadacitinib", certainty=cl.PROBABLE),
        answer=_answer(), canonical_outcome_id=OUTCOME,
    )
    assert finding.classification == cl.ALIGNED


# =====================================================================================
# Labels
# =====================================================================================
def test_a_label_that_lists_the_indication_aligns_with_an_approval_claim():
    finding = cl.grade_approval(
        _claim(claim_type=cl.APPROVAL_CLAIM, direction=cl.NO_DIRECTION, comparator=None),
        fact_id="F1", approved_indications=[INDICATION, "Rheumatoid Arthritis"],
    )
    assert finding.classification == cl.ALIGNED


def test_a_label_that_does_not_list_it_contradicts_the_claim():
    finding = cl.grade_approval(
        _claim(claim_type=cl.APPROVAL_CLAIM, direction=cl.NO_DIRECTION, comparator=None),
        fact_id="F1", approved_indications=["Rheumatoid Arthritis"],
    )
    assert finding.classification == cl.CONTRADICTORY
    assert finding.is_adverse


def test_an_unextracted_indication_list_cannot_answer_an_approval_claim_either_way():
    """An empty list is our parsing gap. Reading it as "not approved" would turn that into
    a claim about the FDA."""
    finding = cl.grade_approval(
        _claim(claim_type=cl.APPROVAL_CLAIM, direction=cl.NO_DIRECTION, comparator=None),
        fact_id="F1", approved_indications=[],
    )
    assert finding.classification == cl.EVIDENCE_UNAVAILABLE
    assert not finding.is_adverse


def test_denying_a_boxed_warning_that_exists_is_flagged():
    finding = cl.grade_safety(
        _claim(claim_type=cl.SAFETY_WARNING_CLAIM, direction=cl.NO_DIRECTION,
               comparator=None, polarity=cl.NEGATED),
        fact_id="F1", boxed_warnings=["Serious infections", "Malignancy"],
        has_boxed_warning=True,
    )
    assert finding.classification == cl.CONTRADICTORY
    assert "SAFETY_CONTRADICTION" in finding.flags
    assert cl.SAFETY_ACCURACY in finding.dimensions


def test_a_self_contradicting_label_row_cannot_grade_a_safety_claim():
    finding = cl.grade_safety(
        _claim(claim_type=cl.SAFETY_WARNING_CLAIM, direction=cl.NO_DIRECTION,
               comparator=None),
        fact_id="F1", boxed_warnings=["Serious infections"], has_boxed_warning=False,
    )
    assert finding.classification == cl.EVIDENCE_UNAVAILABLE
    assert "contradicts itself" in finding.reason


# =====================================================================================
# Citations
# =====================================================================================
def test_a_citation_we_cannot_resolve_is_unverifiable_not_hallucinated():
    """Our corpus holds only curated full-depth drugs, so "not in our store" overwhelmingly
    means "not ingested". Calling it a hallucination would be a confident accusation built
    on our own coverage gap."""
    finding = cl.grade_citations(
        _claim(cited_identifiers=("NCT03104400", "NCT99999999")),
        resolvable=("NCT03104400",), unresolvable=("NCT99999999",),
    )
    assert finding.classification == cl.EVIDENCE_UNAVAILABLE
    assert "UNVERIFIABLE_CITATION" in finding.flags
    assert not finding.is_adverse
    assert cl.HALLUCINATED_STUDIES not in finding.dimensions


def test_a_claim_citing_nothing_produces_no_citation_finding():
    assert cl.grade_citations(_claim(), resolvable=(), unresolvable=()) is None


# =====================================================================================
# Rollup
# =====================================================================================
def test_our_own_coverage_gaps_do_not_lower_a_models_alignment_score():
    """Otherwise alignment would fall as the evidence base thins — exactly backwards, and
    on today's prod corpus it would mark almost every response wrong."""
    findings = [
        cl.Finding(cl.ALIGNED, "ok", dimensions=(cl.FACTUAL_ACCURACY,)),
        cl.Finding(cl.EVIDENCE_UNAVAILABLE, "we hold nothing", dimensions=(cl.EVIDENCE_COVERAGE,)),
        cl.Finding(cl.EVIDENCE_UNAVAILABLE, "we hold nothing", dimensions=(cl.EVIDENCE_COVERAGE,)),
    ]
    summary = cl.roll_up(findings)
    assert summary["alignment_score"] == 1.0
    assert summary["checkable_count"] == 1
    assert summary["coverage"] < 0.5


def test_the_rollup_separates_certainty_verdicts_from_classifications():
    findings = [
        cl.Finding(cl.PARTIALLY_ALIGNED, "over", dimensions=(cl.CERTAINTY_CALIBRATION,),
                   certainty_verdict=cl.OVERCLAIMED),
        cl.Finding(cl.ALIGNED, "fine", dimensions=(cl.CERTAINTY_CALIBRATION,),
                   certainty_verdict=cl.CALIBRATED),
    ]
    summary = cl.roll_up(findings)
    assert summary["certainty_calibration"] == {cl.OVERCLAIMED: 1, cl.CALIBRATED: 1}
    assert summary["alignment_score"] == 0.75


# =====================================================================================
# Extraction parsing — pure, no network
# =====================================================================================
def test_a_well_formed_extraction_parses():
    parsed, rejected = ce.parse_claims({"claims": [{
        "claim_text": "Rinvoq is approved for psoriatic arthritis.",
        "claim_type": "APPROVAL_CLAIM", "subject": "Rinvoq",
        "indication": "Psoriatic Arthritis", "certainty": "DEFINITIVE",
    }]})
    assert not rejected
    assert parsed[0].claim_type == cl.APPROVAL_CLAIM


def test_a_malformed_claim_is_reported_not_silently_dropped():
    """An extractor that has quietly stopped working must not look like a corpus with
    nothing to say."""
    parsed, rejected = ce.parse_claims({"claims": [
        {"claim_text": "x", "claim_type": "DIRECT_COMPARISON_CLAIM", "subject": "Rinvoq"},
        {"claim_text": "ok", "claim_type": "MECHANISM_CLAIM", "subject": "Rinvoq"},
    ]})
    assert len(parsed) == 1
    assert len(rejected) == 1
    assert "direction" in rejected[0]["reason"]


def test_extraction_is_bounded():
    payload = {"claims": [
        {"claim_text": f"claim {i}", "claim_type": "MECHANISM_CLAIM", "subject": "Rinvoq"}
        for i in range(50)
    ]}
    parsed, _ = ce.parse_claims(payload)
    assert len(parsed) == ce.MAX_CLAIMS


def test_a_response_that_is_not_json_is_a_rejection_not_a_crash():
    parsed, rejected = ce.parse_claims("I could not do that")
    assert parsed == []
    assert rejected


# =====================================================================================
# Service — routing against the database
# =====================================================================================
async def test_an_unverified_label_cannot_produce_an_approval_finding(db_session):
    db_session.add(DrugFact(
        fact_id="F-unverified", brand="Rinvoq",
        approved_indications=json.dumps([INDICATION]),
        verification_status=lifecycles.EXTRACTED,
    ))
    await db_session.commit()

    finding = await svc.grade_claim(
        db_session,
        _claim(claim_type=cl.APPROVAL_CLAIM, direction=cl.NO_DIRECTION, comparator=None),
        indication=INDICATION,
    )
    assert finding.classification == cl.EVIDENCE_UNAVAILABLE
    assert "no verified drug facts" in finding.reason


async def test_a_verified_label_grades_the_same_claim(db_session):
    db_session.add(DrugFact(
        fact_id="F-verified", brand="Rinvoq",
        approved_indications=json.dumps([INDICATION]),
        verification_status=lifecycles.VERIFIED,
    ))
    await db_session.commit()

    finding = await svc.grade_claim(
        db_session,
        _claim(claim_type=cl.APPROVAL_CLAIM, direction=cl.NO_DIRECTION, comparator=None),
        indication=INDICATION,
    )
    assert finding.classification == cl.ALIGNED


async def test_a_comparison_with_no_network_is_uncheckable_not_wrong(db_session):
    finding = await svc.grade_claim(db_session, _claim(), indication=INDICATION)
    assert finding.classification == cl.EVIDENCE_UNAVAILABLE
    assert not finding.is_adverse


async def test_an_unwired_claim_type_raises_rather_than_passing_silently(db_session):
    """There is deliberately no `else: return ALIGNED` in the router."""
    class Sneaky(cl.ExtractedClaim):
        pass

    claim = _claim(claim_type=cl.TRIAL_RESULT_CLAIM, direction=cl.NO_DIRECTION,
                   comparator=None)
    object.__setattr__(claim, "claim_type", "UNWIRED_CLAIM")
    with pytest.raises(cl.ClaimError, match="no grader is wired"):
        await svc.grade_claim(db_session, claim, indication=INDICATION)


async def test_identifiers_are_resolved_against_both_internal_and_registry_ids(db_session):
    db_session.add(ClinicalStudy(
        study_id="S-1", registry_id="NCT03104400", indication=INDICATION,
    ))
    await db_session.commit()

    resolvable, unresolvable = await svc._resolve_identifiers(
        db_session, ("NCT03104400", "NCT99999999")
    )
    assert resolvable == ("NCT03104400",)
    assert unresolvable == ("NCT99999999",)


async def test_the_alignment_report_separates_score_from_coverage(db_session):
    for index, (classification, adverse) in enumerate((
        (cl.ALIGNED, False), (cl.CONTRADICTORY, True), (cl.EVIDENCE_UNAVAILABLE, False),
    )):
        db_session.add(EvaluationClaim(
            claim_id=f"EC-{index}", response_id="R-1", run_id="RUN-1",
            llm_name="claude", claim_text="x", claim_type=cl.APPROVAL_CLAIM,
            subject="Rinvoq", classification=classification, is_adverse=adverse,
            dimensions=json.dumps([cl.FACTUAL_ACCURACY]),
        ))
    await db_session.commit()

    report = await svc.alignment_report(db_session, run_id="RUN-1")

    assert report["overall"]["claim_count"] == 3
    assert report["overall"]["checkable_count"] == 2
    assert report["overall"]["alignment_score"] == 0.5
    assert report["by_model"]["claude"]["adverse_count"] == 1
    assert len(report["adverse_examples"]) == 1
