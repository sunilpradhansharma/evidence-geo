"""Tier A competitor discovery (Phase 5). No network in any test.

The rules are pure, so most of this file needs no session. What it pins:

* **Placebo is never a competitor** — it is the most common node in every network built here,
  so a sweep without the guard would propose it as the leading competitor in all eight
  indications. Same for ``Total``, ``Arm B`` and ``bDMARD``.
* **Discovery proposes, a human commits.** No route and no service call writes ``brands.yaml``;
  acceptance yields a rendered fragment, and an uncurated molecule's class is emitted
  **commented out** so a paste cannot ship a placeholder as a real value.
* **A decided candidate is never overwritten** by a re-sweep, so a rejection is remembered.
* **Confidence is a function of reasons alone** — volume must not outrank a head-to-head.
* **Class is copied from curation or left null**, never inferred, and the class map reports
  how much of the network has no class at all.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api import competitor_discovery as cd_api
from app.evidence import discovery
from app.models.clinical_study import ClinicalStudy, StudyArm
from app.models.competitor_candidate import (
    ACCEPTED,
    DEFERRED,
    NEW,
    REJECTED,
    CompetitorCandidate,
)
from app.models.database import Base, get_db
from app.models.drug_fact import DrugFact
from app.services import competitor_discovery_service as svc

# Real config, not fixtures. Psoriatic Arthritis is the strongest network and the only
# indication holding all four focus drugs, so the curated "already tracked" set is genuinely
# populated (16 names) and the two gaps below are the real ones:
#
#   TRACKED       already a curated PsA competitor, so never a discovery
#   CURATED_ONLY  in `drug_catalog` with a real class, absent from the PsA competitor list
#   UNCURATED     in neither, so nothing may be asserted about its class
#
# Picking these from live config rather than inventing names is deliberate: a fixture drug
# would let the "class is never inferred" test pass without the curated table being consulted.
INDICATION = "Psoriatic Arthritis"
TRACKED = "Humira"
CURATED_ONLY = "Sotyktu"
UNCURATED = "Izokibep"


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from app.models import (  # noqa: F401 — register tables on Base.metadata
        audit_log,
        clinical_study,
        competitor_candidate,
        drug_fact,
        nma_result,
        source_payload,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.fixture
async def api():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    from app.models import (  # noqa: F401
        audit_log,
        clinical_study,
        competitor_candidate,
        drug_fact,
        nma_result,
        source_payload,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _override():
        async with maker() as s:
            yield s

    app = FastAPI()
    app.include_router(cd_api.router)
    app.dependency_overrides[get_db] = _override
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, maker
    await engine.dispose()


async def _trial(
    db,
    study_id: str,
    treatments: list[str],
    *,
    indication: str = INDICATION,
    phase: str | None = "PHASE3",
    posted: date | None = None,
    started: date | None = None,
    sponsor: str | None = "Some Sponsor",
    routes: dict[str, str] | None = None,
) -> None:
    db.add(ClinicalStudy(
        study_id=study_id, indication=indication, phase=phase, is_randomised=True,
        results_first_posted=posted, start_date=started, sponsor=sponsor,
    ))
    for index, treatment in enumerate(treatments):
        db.add(StudyArm(
            arm_id=f"{study_id}:A{index}", study_id=study_id, treatment=treatment,
            is_placebo=treatment == "Placebo",
            administration_route=(routes or {}).get(treatment),
        ))
    await db.commit()


def _observation(treatment: str, **kwargs) -> discovery.TreatmentObservation:
    return discovery.TreatmentObservation(
        treatment=treatment, indication=INDICATION, **kwargs
    )


# =====================================================================================
# The guard: what can never be a competitor
# =====================================================================================
@pytest.mark.parametrize("label", [
    "Placebo",
    "PBO",
    "Placebo 15 mg",          # placebo with dose noise is still placebo
    "Total",                  # a sum across arms, not an arm
    "All Participants Randomized",
    "B",                      # an enumerator naming no molecule
    "2",
    "",
    "bDMARD",                 # a class, not a molecule
    "anti-TNF",
    "Standard Care",
])
def test_a_label_that_names_no_competing_molecule_is_never_discoverable(label):
    """Placebo is the one that matters most.

    It is the anchor of every star network built here, so a sweep missing this guard would
    rank "Placebo" as the top competitor in all eight indications.
    """
    assert discovery.is_discoverable(label) is False


@pytest.mark.parametrize("label", ["Bimzelx", "Sotyktu", "izokibep", "ABT-494"])
def test_a_molecule_label_is_discoverable(label):
    assert discovery.is_discoverable(label) is True


def test_the_guard_delegates_rather_than_restating_what_an_arm_label_means(monkeypatch):
    """One opinion about arm labels.

    A second implementation is how a fabricated node reaches a curated config file, so the
    predicate must be the shared one — not a copy that drifts.
    """
    calls: list[str] = []
    from app.evidence import treatments as treatment_labels

    real = treatment_labels.is_placebo
    monkeypatch.setattr(
        discovery.treatment_labels,
        "is_placebo",
        lambda label: calls.append(label) or real(label),
    )
    discovery.is_discoverable("Bimzelx")
    assert calls == ["Bimzelx"]


def test_a_class_level_arm_disqualifies_the_whole_study_not_just_that_arm():
    """A withdrawal trial's remaining arms all resolve to clean molecule names.

    Screening only the offending arm would leave the rest looking like a randomised
    comparison between the drugs beside it — which is precisely the comparison a trial of
    *interrupt vs continue* did not run.
    """
    withdrawal = ["Rinvoq", "Orencia", "Xeljanz", "TNFi", "CAN"]
    assert discovery.is_strategy_trial(withdrawal) is True

    # Why the study-level test has to exist: the arm that reached the live review queue
    # passes the per-treatment guard cleanly, because "CAN" names no class and no strategy.
    assert discovery.is_discoverable("CAN") is True

    assert discovery.is_strategy_trial(["Rinvoq", TRACKED, "Placebo"]) is False


# =====================================================================================
# Reasons and ranking
# =====================================================================================
def test_being_randomised_against_a_treatment_we_monitor_is_the_strongest_reason():
    monitored = discovery.normalise_names({"Rinvoq", "Humira"})
    candidate = discovery.assess(
        _observation("Bimzelx", study_ids=("S1",), co_arm_treatments=("Humira", "Placebo"),
                     comparators=("Humira", "Placebo"), has_posted_results=True),
        monitored=monitored,
        our_comparators=discovery.normalise_names({"Placebo"}),
    )
    assert candidate is not None
    assert discovery.DIRECTLY_COMPARED_TREATMENT in candidate.reasons
    assert candidate.compared_with == ("Humira",)
    assert candidate.direct_comparison_count == 1
    # A head-to-head subsumes a shared anchor, so the weaker reason is not also claimed —
    # reporting both would double-count one fact.
    assert discovery.SHARED_COMPARATOR_TREATMENT not in candidate.reasons


def test_a_shared_anchor_is_reported_when_there_is_no_head_to_head():
    """Network topology: it was tested against placebo, and so were ours."""
    candidate = discovery.assess(
        _observation("Sotyktu", study_ids=("S9",), co_arm_treatments=("Placebo",),
                     comparators=("Placebo",), has_posted_results=True),
        monitored=discovery.normalise_names({"Rinvoq"}),
        our_comparators=discovery.normalise_names({"Placebo"}),
    )
    assert candidate is not None
    assert candidate.reasons == (discovery.SHARED_COMPARATOR_TREATMENT,)
    assert candidate.shared_comparators == ("Placebo",)


def test_confidence_is_a_function_of_reasons_not_of_study_volume():
    """Ten trials of a drug nobody randomised against ours is still weak evidence.

    Letting volume raise the score would rank a well-studied irrelevance above a single
    head-to-head trial, which is the one thing that actually establishes competition.
    """
    monitored = discovery.normalise_names({"Rinvoq"})
    anchors = discovery.normalise_names({"Placebo"})

    head_to_head = discovery.assess(
        _observation("Bimzelx", study_ids=("S1",), co_arm_treatments=("Rinvoq",),
                     comparators=("Rinvoq",), has_posted_results=True),
        monitored=monitored, our_comparators=anchors,
    )
    well_studied = discovery.assess(
        _observation(
            "Sotyktu", study_ids=tuple(f"S{n}" for n in range(10)),
            co_arm_treatments=("Placebo",), comparators=("Placebo",),
            has_posted_results=True,
        ),
        monitored=monitored, our_comparators=anchors,
    )
    assert well_studied.evidence_count == 10
    assert head_to_head.evidence_count == 1
    assert head_to_head.discovery_confidence > well_studied.discovery_confidence


def test_confidence_is_the_summed_reason_weights_and_nothing_else():
    """Plain arithmetic, so a reviewer working the queue can be told why in one sentence."""
    reasons = (discovery.DIRECTLY_COMPARED_TREATMENT, discovery.PUBLISHED_NMA_TREATMENT)
    assert discovery.confidence_for(reasons) == pytest.approx(0.60)
    assert discovery.confidence_for(()) == 0.0
    # Capped, so a treatment satisfying everything cannot exceed 1.0.
    assert discovery.confidence_for(discovery.DISCOVERY_REASONS) == 1.0


def test_a_treatment_we_already_track_is_not_a_discovery():
    """Known is not discovered. A queue full of drugs we watch buries the real finds."""
    assert discovery.assess(
        _observation("Rinvoq", study_ids=("S1",), co_arm_treatments=("Humira",),
                     comparators=("Humira",), has_posted_results=True),
        monitored=discovery.normalise_names({"Rinvoq", "Humira"}),
        our_comparators=discovery.normalise_names({"Placebo"}),
    ) is None


def test_no_tier_a_reason_means_no_candidate():
    """Tier A ships only what is mechanically derivable. No signal, nothing to show."""
    assert discovery.assess(
        _observation("Unrelated", study_ids=("S1",), co_arm_treatments=("AlsoUnrelated",),
                     comparators=("AlsoUnrelated",), has_posted_results=True),
        monitored=discovery.normalise_names({"Rinvoq"}),
        our_comparators=discovery.normalise_names({"Placebo"}),
    ) is None


def test_an_unposted_trial_reads_as_pipeline_rather_than_approved():
    candidate = discovery.assess(
        _observation("Sonelokimab", study_ids=("S1",), co_arm_treatments=("Rinvoq",),
                     comparators=("Rinvoq",), has_posted_results=False,
                     development_phase="PHASE2"),
        monitored=discovery.normalise_names({"Rinvoq"}),
        our_comparators=discovery.normalise_names({"Placebo"}),
    )
    assert discovery.PIPELINE_INDICATION_COMPETITOR in candidate.reasons
    assert discovery.APPROVED_INDICATION_COMPETITOR not in candidate.reasons
    assert candidate.development_phase == "PHASE2"


def test_a_label_naming_the_indication_is_its_own_reason():
    candidate = discovery.assess(
        _observation("Cosentyx", study_ids=("S1",), co_arm_treatments=("Placebo",),
                     comparators=("Placebo",), has_posted_results=True,
                     label_names_indication=True),
        monitored=discovery.normalise_names({"Rinvoq"}),
        our_comparators=discovery.normalise_names({"Placebo"}),
    )
    assert discovery.APPROVED_INDICATION_COMPETITOR in candidate.reasons


# =====================================================================================
# The sweep
# =====================================================================================
async def test_a_sweep_finds_an_untracked_competitor_and_never_proposes_placebo(db_session):
    """The productised form of the uncurated-arm-label count Phase 0 measured."""
    await _trial(db_session, "S1", [CURATED_ONLY, TRACKED, "Placebo"],
                 posted=date(2024, 1, 1))

    report = await svc.discover(db_session, indication=INDICATION)

    names = {c["treatment"] for c in report["candidates"]}
    # Curated as a molecule, absent from the PsA competitor list — the gap worth surfacing.
    assert CURATED_ONLY in names
    assert "Placebo" not in names
    # Already a curated PsA brand, so tracked rather than discovered.
    assert TRACKED not in names
    assert report["indications"][0]["treatments_observed"] == 3
    assert report["indications"][0]["already_tracked"] == 1


async def test_a_strategy_trials_co_arms_never_reach_the_review_queue(db_session):
    """Found by the first live sweep, on real prod data.

    NCT05080218 randomises *interrupt* against *continue* on whichever biologic each
    patient was already taking. Eight of its nine arms resolve to real molecules, so with
    no study-level screen each one reads as a head-to-head against the others, and the one
    the drug catalog could not resolve — ``"Treatment Interruption - CAN"`` — was proposed
    as a competitor on the strength of a comparison that never happened.
    """
    await _trial(db_session, "NCT05080218", ["CAN", "TNFi", TRACKED],
                 posted=date(2024, 1, 1))
    await _trial(db_session, "S2", [CURATED_ONLY, TRACKED], posted=date(2024, 1, 1))

    report = await svc.discover(db_session, indication=INDICATION)

    names = {c["treatment"] for c in report["candidates"]}
    assert "CAN" not in names
    # Study-level, not a blanket mute: a genuine head-to-head in another trial still lands.
    assert CURATED_ONLY in names
    # Screened studies are disclosed, so "37 scanned" cannot quietly mean 34 used.
    assert report["indications"][0]["strategy_trials_screened"] == 1


async def test_a_sweep_records_that_it_wrote_no_configuration(db_session):
    """The audit entry has to say so, because the alternative is unfalsifiable."""
    from app.models.audit_log import AuditLog

    await _trial(db_session, "S1", [CURATED_ONLY, TRACKED], posted=date(2024, 1, 1))
    await svc.discover(db_session, indication=INDICATION)

    entry = (await db_session.execute(
        select(AuditLog).where(AuditLog.event == "COMPETITOR_DISCOVERY_SWEEP")
    )).scalars().first()
    assert entry is not None
    assert '"config_written": false' in (entry.context or "").lower()


async def test_a_dry_sweep_reports_without_persisting(db_session):
    await _trial(db_session, "S1", [CURATED_ONLY, TRACKED], posted=date(2024, 1, 1))

    report = await svc.discover(db_session, indication=INDICATION, commit=False)
    assert report["created"] == 1
    await db_session.rollback()

    assert (await db_session.execute(select(CompetitorCandidate))).scalars().all() == []


async def test_a_resweep_updates_an_undecided_candidate_in_place(db_session):
    """Ids are derived from (indication, treatment), so nothing accumulates."""
    await _trial(db_session, "S1", [CURATED_ONLY, TRACKED], posted=date(2024, 1, 1))
    first = await svc.discover(db_session, indication=INDICATION)
    assert first["created"] == 1

    await _trial(db_session, "S2", [CURATED_ONLY, "Rinvoq"], posted=date(2024, 6, 1))
    second = await svc.discover(db_session, indication=INDICATION)
    assert second["created"] == 0
    assert second["updated"] == 1

    rows = list((await db_session.execute(
        select(CompetitorCandidate).where(
            CompetitorCandidate.treatment == CURATED_ONLY
        )
    )).scalars().all())
    assert len(rows) == 1
    assert rows[0].evidence_count == 2
    assert sorted(json.loads(rows[0].compared_with)) == [TRACKED, "Rinvoq"]


async def test_a_rejection_is_remembered_across_sweeps(db_session):
    """Otherwise every sweep re-proposes a molecule someone already ruled out.

    Same rule as ingestion's "a decided study is never overwritten": a re-run is not new
    information about a judgement a person already made.
    """
    await _trial(db_session, "S1", [CURATED_ONLY, TRACKED], posted=date(2024, 1, 1))
    await svc.discover(db_session, indication=INDICATION)
    candidate_id = svc.candidate_id_for(INDICATION, CURATED_ONLY)
    await svc.review_candidate(
        db_session, candidate_id, decision=REJECTED, reviewer="Curator",
        note="out of scope for this indication",
    )

    again = await svc.discover(db_session, indication=INDICATION)
    assert again["skipped_decided"] == 1
    assert again["updated"] == 0

    row = (await db_session.execute(
        select(CompetitorCandidate).where(
            CompetitorCandidate.candidate_id == candidate_id
        )
    )).scalar_one()
    assert row.review_status == REJECTED
    assert row.reviewed_by == "Curator"


async def test_a_deferred_candidate_is_still_refreshed(db_session):
    """DEFERRED means "not yet", not "decided" — new evidence should reach it."""
    await _trial(db_session, "S1", [CURATED_ONLY, TRACKED], posted=date(2024, 1, 1))
    await svc.discover(db_session, indication=INDICATION)
    candidate_id = svc.candidate_id_for(INDICATION, CURATED_ONLY)
    await svc.review_candidate(
        db_session, candidate_id, decision=DEFERRED, reviewer="Curator"
    )

    await _trial(db_session, "S2", [CURATED_ONLY, "Rinvoq"], posted=date(2024, 6, 1))
    again = await svc.discover(db_session, indication=INDICATION)
    assert again["updated"] == 1
    assert again["skipped_decided"] == 0


async def test_a_published_synthesis_node_is_its_own_signal(db_session):
    """Membership of an ingested NMA network, read back through the synthesis service."""
    from app.models.nma_result import PUBLISHED, NMAResult

    await _trial(db_session, "S1", ["Sotyktu", "Placebo"], posted=date(2024, 1, 1))
    db_session.add(NMAResult(
        result_id="NMA-1", source=PUBLISHED, indication=INDICATION,
        status="MEDICAL_REVIEW_REQUIRED",
        estimates=(
            '[{"treatment": "Sotyktu", "comparator": "Placebo", "effect_estimate": 1.4}]'
        ),
    ))
    await db_session.commit()

    report = await svc.discover(db_session, indication=INDICATION)
    found = next(c for c in report["candidates"] if c["treatment"] == "Sotyktu")
    assert discovery.PUBLISHED_NMA_TREATMENT in found["reasons"]
    assert report["indications"][0]["published_syntheses_scanned"] == 1


async def test_a_recent_trial_reads_as_newly_active(db_session):
    await _trial(
        db_session, "S1", [UNCURATED, "Rinvoq"],
        started=date.today() - timedelta(days=30), posted=None,
    )
    report = await svc.discover(db_session, indication=INDICATION)
    found = next(c for c in report["candidates"] if c["treatment"] == UNCURATED)
    assert discovery.NEWLY_ACTIVE_TRIAL_TREATMENT in found["reasons"]


async def test_a_single_arm_study_contributes_no_comparison(db_session):
    """One arm is not a comparison, so it cannot establish competition."""
    await _trial(db_session, "S1", [CURATED_ONLY], posted=date(2024, 1, 1))
    report = await svc.discover(db_session, indication=INDICATION)
    assert report["candidates"] == []
    assert report["indications"][0]["treatments_observed"] == 0


async def test_class_and_route_are_copied_from_curation_never_inferred(db_session):
    """An uncurated molecule keeps NULLs, which is what says it needs characterising."""
    await _trial(db_session, "S1", [CURATED_ONLY, UNCURATED, TRACKED],
                 posted=date(2024, 1, 1))
    await svc.discover(db_session, indication=INDICATION)

    rows = {
        r.treatment: r for r in (await db_session.execute(
            select(CompetitorCandidate)
        )).scalars().all()
    }
    # Curated in drug_catalog, so the real class and route are copied across.
    assert rows[CURATED_ONLY].is_curated_drug is True
    assert rows[CURATED_ONLY].drug_class == "TYK2 inhibitor"
    assert rows[CURATED_ONLY].administration_route == "ORAL"
    # Curated nowhere, and nothing was guessed for it. Open-set class inference is the
    # Tier B2 capability that stays out of scope.
    assert rows[UNCURATED].is_curated_drug is False
    assert rows[UNCURATED].drug_class is None
    assert rows[UNCURATED].administration_route is None


# =====================================================================================
# Review + config proposal
# =====================================================================================
async def test_a_decision_needs_a_named_reviewer(db_session):
    await _trial(db_session, "S1", [CURATED_ONLY, TRACKED], posted=date(2024, 1, 1))
    await svc.discover(db_session, indication=INDICATION)
    candidate_id = svc.candidate_id_for(INDICATION, CURATED_ONLY)

    with pytest.raises(svc.DiscoveryError, match="named reviewer"):
        await svc.review_candidate(
            db_session, candidate_id, decision=ACCEPTED, reviewer="  "
        )
    with pytest.raises(svc.DiscoveryError, match="decision must be one of"):
        await svc.review_candidate(
            db_session, candidate_id, decision="APPROVE_PLEASE", reviewer="Curator"
        )


async def test_accepting_a_candidate_changes_no_configuration(db_session):
    """It records a decision and yields a fragment. brands.yaml stays hand-authored.

    The whole argument for the curated class/route table is that it is a *reviewable
    artefact*; a queue that edited it would make it the unreviewed kind, which is the Tier B2
    capability explicitly out of scope.
    """
    from app.config import taxonomy

    await _trial(db_session, "S1", [CURATED_ONLY, TRACKED], posted=date(2024, 1, 1))
    await svc.discover(db_session, indication=INDICATION)
    before = taxonomy.competitors_for_disease(INDICATION)

    await svc.review_candidate(
        db_session, svc.candidate_id_for(INDICATION, CURATED_ONLY),
        decision=ACCEPTED, reviewer="Curator", note="real PsA comparator",
    )
    assert taxonomy.competitors_for_disease(INDICATION) == before

    proposal = await svc.config_proposal(db_session)
    assert proposal["accepted_pending_commit"] == 1
    assert CURATED_ONLY in proposal["yaml"]
    assert INDICATION in proposal["yaml"]
    assert "discovery never edits it" in proposal["yaml"]


async def test_an_uncurated_molecule_is_proposed_commented_out(db_session):
    """A pasteable placeholder would let ``drug_class: REVIEW_REQUIRED`` ship as a value.

    A YAML comment cannot, so the missing fields are named in an inert block a human has to
    fill in and uncomment.
    """
    await _trial(db_session, "S1", [UNCURATED, "Rinvoq"], posted=date(2024, 1, 1))
    await svc.discover(db_session, indication=INDICATION)
    await svc.review_candidate(
        db_session, svc.candidate_id_for(INDICATION, UNCURATED),
        decision=ACCEPTED, reviewer="Curator",
    )

    proposal = await svc.config_proposal(db_session)
    assert proposal["needs_characterising"] == [UNCURATED]
    catalog_lines = [
        line for line in proposal["yaml"].splitlines() if f"{UNCURATED}:" in line
    ]
    assert catalog_lines, "the molecule should appear in the drug_catalog block"
    # Every line naming it inside the catalog block is a comment.
    assert all(line.lstrip().startswith("#") for line in catalog_lines)
    assert "drug_catalog:" not in [
        line.strip() for line in proposal["yaml"].splitlines()
    ], "the catalog block itself must stay commented out"


async def test_only_an_accepted_candidate_can_be_marked_applied(db_session):
    await _trial(db_session, "S1", [CURATED_ONLY, TRACKED], posted=date(2024, 1, 1))
    await svc.discover(db_session, indication=INDICATION)
    candidate_id = svc.candidate_id_for(INDICATION, CURATED_ONLY)

    with pytest.raises(svc.DiscoveryError, match="only accepted candidates"):
        await svc.mark_config_applied(db_session, [candidate_id], applied_by="Curator")

    await svc.review_candidate(
        db_session, candidate_id, decision=ACCEPTED, reviewer="Curator"
    )
    result = await svc.mark_config_applied(
        db_session, [candidate_id], applied_by="Curator"
    )
    assert result["applied"] == [candidate_id]
    # Committed, so it drops out of the outstanding proposal.
    assert (await svc.config_proposal(db_session))["accepted_pending_commit"] == 0


async def test_acceptance_and_committing_the_config_are_separate_facts(db_session):
    """One is a judgement about a molecule, the other is a change to the taxonomy.

    Collapsing them would make the queue claim a config change nobody made.
    """
    await _trial(db_session, "S1", [CURATED_ONLY, TRACKED], posted=date(2024, 1, 1))
    await svc.discover(db_session, indication=INDICATION)
    candidate_id = svc.candidate_id_for(INDICATION, CURATED_ONLY)
    await svc.review_candidate(
        db_session, candidate_id, decision=ACCEPTED, reviewer="Curator"
    )

    row = (await db_session.execute(
        select(CompetitorCandidate).where(
            CompetitorCandidate.candidate_id == candidate_id
        )
    )).scalar_one()
    assert row.review_status == ACCEPTED
    assert row.config_applied is False


# =====================================================================================
# Cross-class map (B1 presentation)
# =====================================================================================
def test_grouping_reports_what_it_could_not_characterise():
    """A class map that dropped uncurated molecules would look complete while hiding most
    of the network — Phase 0 measured curated coverage at 12-26% of nodes."""
    groups, uncharacterised = discovery.group_by_class([
        ("Skyrizi", "IL-23 inhibitor", "SC"),
        ("Tremfya", "IL-23 inhibitor", "SC"),
        ("Rinvoq", "JAK inhibitor", "ORAL"),
        ("Sonelokimab", None, None),
    ])
    assert [g.drug_class for g in groups] == ["IL-23 inhibitor", "JAK inhibitor"]
    assert groups[0].treatments == ["Skyrizi", "Tremfya"]
    assert groups[1].routes == {"Rinvoq": "ORAL"}
    assert uncharacterised == ["Sonelokimab"]


async def test_the_class_map_discloses_route_mixing_and_coverage(db_session):
    """Route mixing is visible from the class map before any resolve runs."""
    await _trial(
        db_session, "S1", ["Rinvoq", "Skyrizi", UNCURATED, "Placebo"],
        posted=date(2024, 1, 1),
        routes={"Rinvoq": "ORAL", "Skyrizi": "SC"},
    )

    body = await svc.class_map(db_session, indication=INDICATION)
    assert body["is_route_mixed"] is True
    assert sorted(body["routes_present"]) == ["ORAL", "SC"]
    # Placebo is not a treatment node in this view either.
    assert "Placebo" not in body["uncharacterised"]
    assert body["uncharacterised"] == [UNCURATED]
    assert body["characterised_pct"] < 100.0
    monitored = {t for group in body["classes"] for t in group["monitored"]}
    assert {"Rinvoq", "Skyrizi"} <= monitored


# =====================================================================================
# API contract
# =====================================================================================
async def test_the_reason_vocabulary_is_served_from_one_place(api):
    """So a queue UI does not hardcode an enum or invent its own weights."""
    client, _ = api
    body = (await client.get("/competitor-discovery/reasons")).json()
    codes = [r["code"] for r in body["reasons"]]
    assert codes == list(discovery.DISCOVERY_REASONS)
    assert body["reasons"][0]["weight"] == discovery.REASON_WEIGHTS[codes[0]]
    assert "never inferred" in body["tier_b2_out_of_scope"]


async def test_the_queue_round_trips_through_the_api(api):
    client, maker = api
    async with maker() as db:
        await _trial(db, "S1", [CURATED_ONLY, TRACKED], posted=date(2024, 1, 1))

    swept = (await client.post(
        "/competitor-discovery/sweep", params={"indication": INDICATION}
    )).json()
    assert swept["created"] == 1

    queue = (await client.get("/competitor-discovery/candidates")).json()
    assert queue["total"] == 1
    candidate = queue["candidates"][0]
    assert candidate["treatment"] == CURATED_ONLY
    assert candidate["review_status"] == NEW
    # The queue explains itself: reason labels travel with the row.
    assert candidate["reason_labels"]
    assert queue["counts_by_status"] == {NEW: 1}

    reviewed = await client.post(
        f"/competitor-discovery/candidates/{candidate['candidate_id']}/review",
        json={"decision": ACCEPTED, "reviewer": "Curator", "note": "real comparator"},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["review_status"] == ACCEPTED

    proposal = (await client.get("/competitor-discovery/config-proposal")).json()
    assert proposal["accepted_pending_commit"] == 1

    applied = await client.post(
        "/competitor-discovery/config-applied",
        json={"candidate_ids": [candidate["candidate_id"]], "applied_by": "Curator"},
    )
    assert applied.status_code == 200
    assert (await client.get(
        "/competitor-discovery/config-proposal"
    )).json()["accepted_pending_commit"] == 0


async def test_an_unknown_review_status_is_rejected(api):
    client, _ = api
    response = await client.get(
        "/competitor-discovery/candidates", params={"review_status": "MAYBE"}
    )
    assert response.status_code == 400


async def test_reviewing_an_unknown_candidate_is_a_400(api):
    client, _ = api
    response = await client.post(
        "/competitor-discovery/candidates/CC-NOPE/review",
        json={"decision": ACCEPTED, "reviewer": "Curator"},
    )
    assert response.status_code == 400
