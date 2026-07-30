"""Evidence-driven recommendations and synthesis (Phase 9).

The mistake this phase exists to prevent has one shape: **answering every finding with
content.** The existing GEO engine finds positioning gaps, which always have a content
remedy, so nothing in it had to ask whether a remedy existed. Phase 8 produces findings that
often have no content remedy at all, and the worst of them looks exactly like one that does:

  *"The model claims Rinvoq beats Drug X and our evidence cannot support that."*

If the comparison is genuinely unavailable, the honest asset says so. If it is unavailable
because **nobody has verified studies we already hold**, then telling a brand team to publish
a comparison table sends them to spend money while the actual fix is an afternoon of
curation. Phase 7 already separates those two; these tests make sure Phase 9 reads it.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.evidence import claims as cl
from app.evidence import lifecycles
from app.evidence import question_generation as qg
from app.models.clinical_study import ClinicalStudy
from app.models.competitor_candidate import ACCEPTED, NEW, CompetitorCandidate
from app.models.database import Base
from app.models.evaluation_claim import EvaluationClaim
from app.models.evidence_network import EvidenceNetwork
from app.models.recommendation import (
    SOURCE_EVIDENCE_GAP,
    SOURCE_POSITIONING_GAP,
    Recommendation,
)
from app.models.response import Response
from app.remediation import evidence_gaps, implications as impl, prompts
from app.services import evidence_synthesis_service as synth

INDICATION = "Psoriatic Arthritis"


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
        recommendation,
        response,
        scoring,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _response(**overrides) -> Response:
    base = {
        "response_id": "R-1", "run_id": "RUN-1", "llm_name": "claude",
        "persona": "Provider", "question_id": "Q-1",
        "question_text": "Is Rinvoq more effective than Humira for psoriatic arthritis?",
        "therapeutic_area": "Rheumatology", "indication": INDICATION, "disease": INDICATION,
        "brand_focus": "Rinvoq", "domain": "Comparative",
        "response_text": "Rinvoq is more effective.", "status": "SUCCESS",
    }
    base.update(overrides)
    return Response(**base)


def _claim(**overrides) -> EvaluationClaim:
    base = {
        "claim_id": "EC-1", "response_id": "R-1", "run_id": "RUN-1",
        "question_id": "Q-1", "llm_name": "claude",
        "claim_text": "Rinvoq is more effective than Humira.",
        "claim_type": cl.DIRECT_COMPARISON_CLAIM,
        "subject": "Rinvoq", "comparator": "Humira", "indication": INDICATION,
        "classification": cl.UNSUPPORTED, "is_adverse": True,
        "reason": "the evidence cannot produce this comparison",
        "dimensions": json.dumps([cl.COMPARATIVE_RANKING_ACCURACY]),
        "evidence_links": json.dumps([]),
        "flags": json.dumps([]),
    }
    base.update(overrides)
    return EvaluationClaim(**base)


# =====================================================================================
# The distinction the phase turns on
# =====================================================================================
def test_a_curation_backlog_is_not_answered_with_content():
    """The core refusal. Publishing a comparison table cannot fix the fact that nobody has
    verified studies we already hold, and proposing one sends a brand team to spend money
    on the wrong problem."""
    result = impl.classify(
        classification=cl.UNSUPPORTED,
        certainty_verdict=None,
        claim_type=cl.DIRECT_COMPARISON_CLAIM,
        gap_attribution=qg.ATTRIBUTION_CURATION,
    )
    assert result.implication == impl.INTERNAL_CURATION_REQUIRED
    assert result.externally_actionable is False
    assert "our own verification backlog" in result.reason
    assert result.evidence_action


def test_a_protocol_window_gap_goes_to_statistical_review_not_content():
    result = impl.classify(
        classification=cl.UNSUPPORTED,
        certainty_verdict=None,
        claim_type=cl.DIRECT_COMPARISON_CLAIM,
        gap_attribution=qg.ATTRIBUTION_PROTOCOL,
    )
    assert result.externally_actionable is False
    assert result.owner == "Statistical review"


def test_a_genuine_evidence_gap_asks_for_evidence_not_a_web_page():
    """A content brief proposing to fill a real evidence gap with a page is how unsupported
    claims get written under our own name."""
    result = impl.classify(
        classification=cl.UNSUPPORTED,
        certainty_verdict=None,
        claim_type=cl.DIRECT_COMPARISON_CLAIM,
        gap_attribution=qg.ATTRIBUTION_EVIDENCE,
        required_evidence="A head-to-head trial of Rinvoq versus Humira.",
    )
    assert result.implication == impl.EVIDENCE_GENERATION_NEEDED
    assert result.externally_actionable is False
    assert "head-to-head" in result.evidence_action


def test_an_unsupported_claim_with_no_attribution_is_still_correctable_publicly():
    """Where we cannot attribute the gap, saying plainly that the comparison is not
    established IS the correction — that one is content."""
    result = impl.classify(
        classification=cl.UNSUPPORTED, certainty_verdict=None,
        claim_type=cl.DIRECT_COMPARISON_CLAIM,
    )
    assert result.implication == impl.MISSING_COMPARATIVE_DATA
    assert result.externally_actionable is True


def test_a_contradiction_of_a_boxed_warning_outranks_everything():
    result = impl.classify(
        classification=cl.CONTRADICTORY, certainty_verdict=None,
        claim_type=cl.SAFETY_WARNING_CLAIM, flags=("SAFETY_CONTRADICTION",),
    )
    assert result.implication == impl.AI_MISINFORMATION_RISK
    assert result.severity == impl.SAFETY_ESCALATION
    assert result.severity > max(
        impl.SEVERITY_OF[name] for name in impl.IMPLICATIONS
    )


def test_underclaiming_is_a_communication_gap_not_a_model_fault():
    """Our evidence is not reaching the model. That is our problem, and it is content."""
    result = impl.classify(
        classification=cl.PARTIALLY_ALIGNED, certainty_verdict=cl.UNDERCLAIMED,
        claim_type=cl.DIRECT_COMPARISON_CLAIM,
    )
    assert result.implication == impl.COMMUNICATION_GAP
    assert result.externally_actionable is True


def test_an_aligned_calibrated_finding_produces_no_work():
    """A recommendation engine that always finds something is not measuring anything."""
    assert impl.classify(
        classification=cl.ALIGNED, certainty_verdict=cl.CALIBRATED,
        claim_type=cl.APPROVAL_CLAIM,
    ) is None


def test_our_own_coverage_gap_is_never_turned_into_an_action():
    assert impl.classify(
        classification=cl.EVIDENCE_UNAVAILABLE, certainty_verdict=None,
        claim_type=cl.APPROVAL_CLAIM,
    ) is None


def test_every_implication_has_an_owner_and_a_severity():
    for name in impl.IMPLICATIONS:
        assert name in impl.OWNER_OF
        assert name in impl.SEVERITY_OF


def test_the_non_content_implications_are_excluded_from_actionable():
    assert impl.INTERNAL_CURATION_REQUIRED not in impl.EXTERNALLY_ACTIONABLE
    assert impl.EVIDENCE_GENERATION_NEEDED not in impl.EXTERNALLY_ACTIONABLE


# =====================================================================================
# Confidence comes from governance, not from a model
# =====================================================================================
def test_a_finding_on_unverified_evidence_cannot_present_itself_as_certain():
    """A recommendation is an instruction to spend money."""
    unverified = impl.confidence_for(
        classification=cl.CONTRADICTORY, verification_states=[None, None]
    )
    verified = impl.confidence_for(
        classification=cl.CONTRADICTORY, verification_states=["VERIFIED", "VERIFIED"]
    )
    assert unverified < verified
    assert verified <= 0.9


def test_an_exploratory_result_caps_confidence():
    """The same rule that stops it grading a response, one layer out."""
    capped = impl.confidence_for(
        classification=cl.CONTRADICTORY, verification_states=["VERIFIED"],
        is_releasable=False,
    )
    assert capped <= 0.4


def test_reporting_our_own_gap_is_confident_about_the_gap_not_a_remedy():
    assert impl.confidence_for(
        classification=cl.EVIDENCE_UNAVAILABLE, verification_states=[]
    ) == 0.5


# =====================================================================================
# The finder
# =====================================================================================
async def test_only_findings_that_carry_an_implication_become_gaps(db_session):
    db_session.add(_response())
    db_session.add(_claim(
        claim_id="EC-aligned", classification=cl.ALIGNED, is_adverse=False,
        certainty_verdict=cl.CALIBRATED,
    ))
    db_session.add(_claim(claim_id="EC-unsupported"))
    await db_session.commit()

    found = await evidence_gaps.find_evidence_gaps(db_session)

    assert [g["claim_id"] for g in found] == ["EC-unsupported"]
    assert found[0]["source_type"] == SOURCE_EVIDENCE_GAP


async def test_a_safety_contradiction_sorts_above_a_calibration_nit(db_session):
    """A page limit must not drop the most consequential finding in the batch."""
    db_session.add(_response())
    db_session.add(_claim(
        claim_id="EC-nit", classification=cl.PARTIALLY_ALIGNED, is_adverse=False,
        certainty_verdict=cl.OVERCLAIMED, claim_type=cl.DIRECT_COMPARISON_CLAIM,
    ))
    db_session.add(_claim(
        claim_id="EC-safety", classification=cl.CONTRADICTORY, is_adverse=True,
        claim_type=cl.SAFETY_WARNING_CLAIM, comparator=None,
        flags=json.dumps(["SAFETY_CONTRADICTION"]),
    ))
    await db_session.commit()

    found = await evidence_gaps.find_evidence_gaps(db_session, limit=1)

    assert [g["claim_id"] for g in found] == ["EC-safety"]
    assert found[0]["strategic_implication"] == impl.AI_MISINFORMATION_RISK


async def test_a_response_with_no_position_score_is_marked_not_assessed(db_session):
    """Distinct from a good position. The two passes are independent and a blank must not
    read as "the brand was positioned fine"."""
    db_session.add(_response())
    db_session.add(_claim())
    await db_session.commit()

    found = await evidence_gaps.find_evidence_gaps(db_session)

    assert found[0]["competitive_position"] == evidence_gaps.POSITION_NOT_ASSESSED


async def test_the_finder_carries_the_grader_reason_verbatim(db_session):
    db_session.add(_response())
    db_session.add(_claim(reason="our evidence gives a risk ratio of 0.959"))
    await db_session.commit()

    found = await evidence_gaps.find_evidence_gaps(db_session)

    assert found[0]["finding_reason"] == "our evidence gives a risk ratio of 0.959"


# =====================================================================================
# The engine keeps them apart
# =====================================================================================
def test_the_evidence_prompt_forbids_overstating_in_our_favour():
    """The finding being answered is that a model over-stated something. An asset that
    over-states in our favour reproduces the fault under our own name."""
    assert "Never propose content that asserts more than the evidence" in prompts.EVIDENCE_SYSTEM
    assert "no head-to-head data exists" in prompts.EVIDENCE_SYSTEM


def test_the_evidence_prompt_supplies_every_fact_the_model_needs():
    """So it never has to reach for a clinical claim of its own."""
    body = prompts.build_evidence_user_prompt({
        "claim_text": "Rinvoq is more effective than Humira.",
        "classification": cl.UNSUPPORTED,
        "finding_reason": "the evidence cannot produce this comparison",
        "strategic_implication": impl.MISSING_COMPARATIVE_DATA,
        "brand_focus": "Rinvoq", "outperforming_competitor": "Humira",
        "indication": INDICATION,
    })
    assert "Rinvoq is more effective than Humira." in body
    assert "the evidence cannot produce this comparison" in body
    assert cl.UNSUPPORTED in body


def test_a_positioning_gap_still_gets_the_positioning_prompt():
    assert "Generative Engine Optimization" in prompts.SYSTEM
    assert prompts.SYSTEM is not prompts.EVIDENCE_SYSTEM


async def test_the_default_source_type_keeps_existing_rows_honest(db_session):
    """Pre-Phase-9 rows all came from the positioning finder, and the column default says
    so without a backfill."""
    db_session.add(Recommendation(
        rec_id="REC-1", batch_id="B-1", competitive_position="SECOND_LINE",
        content_type="FAQ", recommended_action="Publish an FAQ.",
    ))
    await db_session.commit()

    row = (await db_session.execute(select(Recommendation))).scalars().one()
    assert row.source_type == SOURCE_POSITIONING_GAP
    assert row.externally_actionable is True
    assert row.confidence is None


# =====================================================================================
# Synthesis
# =====================================================================================
async def test_synthesis_reports_the_absence_of_a_network_as_a_limitation(db_session):
    report = await synth.synthesise(db_session, indication=INDICATION)

    assert report["what_the_evidence_shows"] == []
    assert any(x["kind"] == "NO_NETWORK" for x in report["limitations"])


async def test_an_unratified_network_is_named_as_a_limitation(db_session):
    """On this corpus the limitations ARE the finding. A page leading with estimates and
    footnoting the governance state would read as settled evidence."""
    db_session.add(EvidenceNetwork(
        network_id="NET-1", indication=INDICATION,
        canonical_outcome_id="PSA_ACR50_W16",
        ratification_status=lifecycles.DRAFT, is_connected=True, has_closed_loops=False,
    ))
    await db_session.commit()

    report = await synth.synthesise(db_session, indication=INDICATION)

    kinds = {x["kind"] for x in report["limitations"]}
    assert "NETWORK_NOT_RATIFIED" in kinds
    assert "NO_CLOSED_LOOPS" in kinds


async def test_evidence_strength_reports_what_review_has_actually_happened(db_session):
    for index, status in enumerate(
        (lifecycles.VERIFIED, lifecycles.EXTRACTED, lifecycles.EXTRACTED)
    ):
        db_session.add(ClinicalStudy(
            study_id=f"S-{index}", indication=INDICATION, verification_status=status,
        ))
    await db_session.commit()

    report = await synth.synthesise(db_session, indication=INDICATION)
    strength = report["evidence_strength"]

    assert strength["studies_total"] == 3
    assert strength["studies_verified"] == 1
    assert strength["verified_fraction"] == pytest.approx(0.3333, abs=1e-3)


async def test_only_accepted_candidates_appear_as_competitor_threats(db_session):
    for status, treatment in ((NEW, "Sonelokimab"), (ACCEPTED, "Bimzelx")):
        db_session.add(CompetitorCandidate(
            candidate_id=f"CC-{treatment}", treatment=treatment, indication=INDICATION,
            review_status=status, has_posted_results=True,
            discovery_reasons=json.dumps(["DIRECTLY_COMPARED_TREATMENT"]),
        ))
    await db_session.commit()

    report = await synth.synthesise(db_session, indication=INDICATION)

    assert report["competitor_landscape"]["accepted_count"] == 1
    assert report["competitor_landscape"]["threats"][0]["treatment"] == "Bimzelx"


async def test_synthesis_rolls_up_ai_alignment_and_implications(db_session):
    db_session.add(_response())
    db_session.add(_claim(claim_id="EC-a", classification=cl.CONTRADICTORY))
    db_session.add(_claim(
        claim_id="EC-b", classification=cl.PARTIALLY_ALIGNED, is_adverse=False,
        certainty_verdict=cl.UNDERCLAIMED,
    ))
    await db_session.commit()

    report = await synth.synthesise(db_session, indication=INDICATION)

    assert report["ai_alignment"]["claims_evaluated"] == 2
    names = {row["implication"] for row in report["strategic_implications"]}
    assert impl.AI_MISINFORMATION_RISK in names
    assert impl.COMMUNICATION_GAP in names
    # Ordered by severity, so the misinformation risk leads.
    assert report["strategic_implications"][0]["implication"] == impl.AI_MISINFORMATION_RISK
