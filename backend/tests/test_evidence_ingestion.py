"""Ingestion + network construction. No network in any test.

The ingestion tests run against the same committed registry fixture the Phase 3B adapter
tests use; only ``fetch``/``search`` touch the wire and neither is called here.

What is pinned, and why each one matters:

* **ingestion never marks its own output VERIFIED** — otherwise the verification lifecycle
  is decorative and the resolver's refusal to compute on unverified rows means nothing
* **a decided study is never overwritten** by a re-harvest
* **retention runs through the licence matrix**, so provenance is a property of the source
* **topology comes from the shared module**, so a within-study triangle is not counted as
  independent evidence
* **the builder proposes, never includes** — inclusion is a per-analysis human judgement
* **a build report discloses both topologies** — the endpoint-level graph it stores AND
  what the governing protocol's approved window leaves answerable, without the builder
  applying that window to anything
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.evidence import lifecycles
from app.evidence.sources import clinicaltrials as ctg
from app.evidence.sources.base import FetchResult
from app.models.clinical_study import BINARY, ClinicalStudy, OutcomeResult, StudyArm
from app.models.database import Base
from app.models.evidence_network import EvidenceNetwork, NetworkMembership
from app.models.source_payload import SourcePayload
from app.services import evidence_ingestion_service as ingest
from app.services import network_builder_service as builder

FIXTURES = Path(__file__).parent / "fixtures"
INDICATION = "Psoriatic Arthritis"
# Real config, not a fixture: PSA_ACR50_W16 allows weeks 12-24 and this protocol narrows to
# [14, 18]. That legal narrowing is what makes the two topologies differ.
PROTOCOL = "PSA_ACR50_W16_PRIMARY"


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


def _payload() -> dict:
    return json.loads((FIXTURES / "ctg_select_psa_1.json").read_text(encoding="utf-8"))


def _parsed(payload: dict | None = None) -> ctg.ParsedStudy:
    result = FetchResult(
        ok=True,
        source_type=ctg.SOURCE_TYPE,
        source_identifier="NCT03104400",
        payload=payload or _payload(),
        raw_text=json.dumps(payload or _payload()),
    )
    parsed = ctg.parse(result, indication=INDICATION)
    assert parsed is not None
    return parsed


# =====================================================================================
# Ingestion
# =====================================================================================
async def test_a_parsed_study_becomes_rows(db_session):
    outcome = await ingest.ingest_study(
        db_session, _parsed(), raw_payload=json.dumps(_payload())
    )

    assert outcome.action == "INGESTED"
    assert outcome.study_id == "NCT03104400"
    assert outcome.arm_count == 4
    assert outcome.outcome_count > 0

    study = (await db_session.execute(
        select(ClinicalStudy).where(ClinicalStudy.study_id == "NCT03104400")
    )).scalar_one()
    assert study.indication == INDICATION
    assert study.is_randomised
    assert study.acronym == "SELECT-PsA 1"

    arms = (await db_session.execute(
        select(StudyArm).where(StudyArm.study_id == "NCT03104400")
    )).scalars().all()
    assert {a.treatment for a in arms} == {"Placebo", "Rinvoq", "Humira"}


async def test_ingestion_never_marks_its_own_output_verified(db_session):
    """The whole verification lifecycle is decorative if the pipeline can self-certify.

    It also makes the resolver's refusal to compute on unverified rows meaningless, since
    nothing would ever be unverified.
    """
    outcome = await ingest.ingest_study(
        db_session, _parsed(), raw_payload=json.dumps(_payload())
    )
    assert outcome.verification_status != lifecycles.VERIFIED
    assert outcome.verification_status in (lifecycles.EXTRACTED, lifecycles.MAPPED)


async def test_the_full_registry_payload_is_retained_because_the_source_is_public_domain(
    db_session,
):
    """A property of the licence, not of this module. A restricted source keeps a fragment."""
    raw = json.dumps(_payload())
    await ingest.ingest_study(db_session, _parsed(), raw_payload=raw)

    payload = (await db_session.execute(select(SourcePayload))).scalar_one()
    assert payload.license_class == "PUBLIC_DOMAIN"
    assert payload.retention_policy == "FULL_INDEFINITE"
    assert payload.raw_payload == raw
    assert payload.checksum
    assert payload.dropped_fields is None


async def test_every_stored_row_carries_its_payload_provenance(db_session):
    """A value nobody can trace back to a document is not evidence."""
    await ingest.ingest_study(db_session, _parsed(), raw_payload=json.dumps(_payload()))

    study = (await db_session.execute(select(ClinicalStudy))).scalar_one()
    rows = (await db_session.execute(select(OutcomeResult))).scalars().all()
    assert study.source_payload_id
    assert rows
    assert all(r.source_payload_id == study.source_payload_id for r in rows)


async def test_reingesting_an_undecided_study_updates_instead_of_duplicating(db_session):
    await ingest.ingest_study(db_session, _parsed(), raw_payload=json.dumps(_payload()))
    second = await ingest.ingest_study(
        db_session, _parsed(), raw_payload=json.dumps(_payload())
    )

    assert second.action == "UPDATED"
    studies = (await db_session.execute(select(ClinicalStudy))).scalars().all()
    assert len(studies) == 1
    arms = (await db_session.execute(select(StudyArm))).scalars().all()
    assert len(arms) == 4  # not 8


async def test_an_unchanged_refetch_reuses_its_payload_instead_of_storing_it_again(
    db_session,
):
    """Studies deduplicated on re-harvest; the retained document did not.

    Every run minted a fresh ``uuid4()`` payload, so an unchanged registry record was
    stored in full again and the previous row was left orphaned — repointed away from, but
    never marked ``superseded_by``. A live PsA run showed it plainly: 66 payload rows
    carrying only 33 distinct checksums.
    """
    raw = json.dumps(_payload())
    await ingest.ingest_study(db_session, _parsed(), raw_payload=raw)
    await ingest.ingest_study(db_session, _parsed(), raw_payload=raw)

    payloads = (await db_session.execute(select(SourcePayload))).scalars().all()
    assert len(payloads) == 1, "a byte-identical refetch is not a second document"

    # Reuse is only correct if the provenance pointer still resolves.
    study = (await db_session.execute(select(ClinicalStudy))).scalar_one()
    assert study.source_payload_id == payloads[0].payload_id
    rows = (await db_session.execute(select(OutcomeResult))).scalars().all()
    assert {r.source_payload_id for r in rows} == {payloads[0].payload_id}


async def test_a_changed_record_still_gets_its_own_payload(db_session):
    """The other half of the contract, so dedup cannot silently swallow a real revision."""
    await ingest.ingest_study(db_session, _parsed(), raw_payload=json.dumps(_payload()))

    revised = _payload()
    revised["__revised_by_the_registry"] = True
    await ingest.ingest_study(db_session, _parsed(), raw_payload=json.dumps(revised))

    payloads = (await db_session.execute(select(SourcePayload))).scalars().all()
    assert len(payloads) == 2
    assert len({p.checksum for p in payloads}) == 2


async def test_reingesting_a_verified_study_is_refused_rather_than_overwriting_it(
    db_session,
):
    """Overwriting would silently rewrite a fact someone signed for.

    The lifecycle has no edge out of VERIFIED for exactly this reason — a correction has to
    create a new version, which leaves the original readable.
    """
    await ingest.ingest_study(db_session, _parsed(), raw_payload=json.dumps(_payload()))
    await ingest.verify_study(db_session, "NCT03104400", verified_by="Dr Reviewer")

    again = await ingest.ingest_study(
        db_session, _parsed(), raw_payload=json.dumps(_payload())
    )
    assert again.action == "SKIPPED"
    assert "decided row" in (again.reason or "")

    study = (await db_session.execute(select(ClinicalStudy))).scalar_one()
    assert study.verification_status == lifecycles.VERIFIED
    assert study.verified_by == "Dr Reviewer"


async def test_verification_requires_a_named_person(db_session):
    await ingest.ingest_study(db_session, _parsed(), raw_payload=json.dumps(_payload()))
    with pytest.raises(ingest.IngestionError, match="not auditable"):
        await ingest.verify_study(db_session, "NCT03104400", verified_by="   ")


async def test_verifying_a_study_verifies_its_outcome_rows(db_session):
    """A verified study whose rows are still EXTRACTED would be filtered out downstream."""
    await ingest.ingest_study(db_session, _parsed(), raw_payload=json.dumps(_payload()))
    await ingest.verify_study(db_session, "NCT03104400", verified_by="Dr Reviewer")

    rows = (await db_session.execute(select(OutcomeResult))).scalars().all()
    assert rows
    assert all(r.verification_status == lifecycles.VERIFIED for r in rows)


async def test_verifying_an_unknown_study_is_refused(db_session):
    with pytest.raises(ingest.IngestionError, match="unknown study"):
        await ingest.verify_study(db_session, "NCT00000000", verified_by="Dr Reviewer")


async def test_an_unparseable_payload_is_refused(db_session):
    with pytest.raises(ingest.IngestionError, match="could not be parsed"):
        await ingest.ingest_payload(db_session, {}, indication=INDICATION)


def _stub_registry(monkeypatch) -> None:
    """Serve the committed fixture for both wire boundaries. Still no network."""
    payload = json.loads((FIXTURES / "ctg_select_psa_1.json").read_text(encoding="utf-8"))

    async def _search(**_kwargs):
        return FetchResult(
            ok=True, source_type=ctg.SOURCE_TYPE, source_identifier="search",
            payload={"studies": [payload]},
        )

    async def _fetch(nct):
        return FetchResult(
            ok=True, source_type=ctg.SOURCE_TYPE, source_identifier=nct,
            payload=payload, raw_text=json.dumps(payload),
        )

    monkeypatch.setattr(ctg, "search", _search)
    monkeypatch.setattr(ctg, "fetch", _fetch)


async def test_a_dry_run_writes_nothing(db_session, monkeypatch):
    """It used to write everything while printing "DRY RUN - nothing written".

    ``ingest_indication`` committed unconditionally, so the CLI's ``rollback()`` ran against
    a fresh empty transaction and undid nothing. The caller owns the commit now.
    """
    _stub_registry(monkeypatch)

    report = await ingest.ingest_indication(
        db_session, INDICATION, drugs=["Rinvoq"], commit=False,
    )
    assert report.ingested == 1, "the run must still do its work and report on it"

    await db_session.rollback()          # exactly what the CLI does for a dry run
    assert (await db_session.execute(select(ClinicalStudy))).scalars().all() == []
    assert (await db_session.execute(select(StudyArm))).scalars().all() == []


async def test_a_committed_run_persists(db_session, monkeypatch):
    """The other half of the contract: --commit must still write."""
    _stub_registry(monkeypatch)

    await ingest.ingest_indication(
        db_session, INDICATION, drugs=["Rinvoq"], commit=True,
    )

    await db_session.rollback()
    studies = (await db_session.execute(select(ClinicalStudy))).scalars().all()
    assert [s.study_id for s in studies] == ["NCT03104400"]


# =====================================================================================
# Progress reporting — additive, so the script path must be untouched
# =====================================================================================
async def test_the_script_path_is_unchanged_when_no_progress_dict_is_passed(
    db_session, monkeypatch
):
    """Every CLI caller passes nothing, so ``progress=None`` has to behave identically.

    A progress hook that changed what a run *does* would be a second code path to test, and
    the runner scripts are the ones with the longest history of correct output.
    """
    _stub_registry(monkeypatch)

    report = await ingest.ingest_indication(
        db_session, INDICATION, drugs=["Rinvoq"], commit=False,
    )
    await db_session.rollback()
    assert report.ingested == 1
    assert report.discovered == 1


async def test_a_supplied_progress_dict_is_filled_in_place(db_session, monkeypatch):
    """The contract ``harvest.pipeline.harvest`` already uses: the CALLER owns the dict.

    It is read from another coroutine while the run is in flight, which is the only way a
    minutes-long throttled harvest can report anything other than "still going".
    """
    _stub_registry(monkeypatch)
    progress: dict = {}

    report = await ingest.ingest_indication(
        db_session, INDICATION, drugs=["Rinvoq", "Skyrizi"], commit=False,
        progress=progress,
    )
    await db_session.rollback()

    assert progress["phase"] == "done"
    assert progress["drugs_total"] == progress["drugs_done"] == 2
    assert progress["discovered"] == report.discovered
    # `studies_total` is the CAPPED list, not `discovered`: a limited smoke run that reported
    # "1 of 37" would look stalled at the moment it finished.
    assert progress["studies_total"] == progress["studies_done"] == 1
    assert progress["ingested"] == report.ingested


# =====================================================================================
# Re-parse — the offline path for a stale extraction
# =====================================================================================
async def test_reparse_reads_the_stored_document_and_reuses_its_provenance(db_session):
    """A stale parse is our defect, so the fix must not also change the source.

    Re-harvesting would move two variables at once and make the delta unattributable. The
    payload row is reused rather than re-minted, so re-parsing does not accumulate a second
    document per fetch — the checksum still matches because the original bytes are passed
    through unmodified rather than re-serialised.
    """
    raw = json.dumps(_payload())
    await ingest.ingest_study(db_session, _parsed(), raw_payload=raw)
    before = (await db_session.execute(select(SourcePayload))).scalars().all()

    outcome = await ingest.reparse_study(db_session, "NCT03104400")

    assert outcome.action == "UPDATED"
    after = (await db_session.execute(select(SourcePayload))).scalars().all()
    assert len(after) == len(before) == 1
    study = (await db_session.execute(
        select(ClinicalStudy).where(ClinicalStudy.study_id == "NCT03104400")
    )).scalar_one()
    assert study.source_payload_id == before[0].payload_id
    assert study.indication == INDICATION


async def test_reparse_restores_a_row_a_stale_parse_left_orphaned(db_session):
    """The defect this path exists for: 664 live rows held no arm, and ``_treatments_of``
    skips those silently, so they were absent from every network with no warning."""
    await ingest.ingest_study(db_session, _parsed(), raw_payload=json.dumps(_payload()))
    row = (await db_session.execute(
        select(OutcomeResult).where(OutcomeResult.arm_id.is_not(None)).limit(1)
    )).scalar_one()
    result_id, expected_arm = row.result_id, row.arm_id
    row.arm_id = None
    await db_session.commit()

    await ingest.reparse_study(db_session, "NCT03104400")

    repaired = (await db_session.execute(
        select(OutcomeResult).where(OutcomeResult.result_id == result_id)
    )).scalar_one()
    assert repaired.arm_id == expected_arm


async def test_reparse_has_no_privileged_access_to_a_verified_row(db_session):
    """A maintenance routine does not get to step around the verification lifecycle.

    If it did, "a decided study is never overwritten" would hold only for the code paths
    that happened to remember it, which is not a guarantee.
    """
    await ingest.ingest_study(db_session, _parsed(), raw_payload=json.dumps(_payload()))
    await ingest.verify_study(db_session, "NCT03104400", verified_by="Dr Reviewer")

    outcome = await ingest.reparse_study(db_session, "NCT03104400")

    assert outcome.action == "SKIPPED"
    assert "already VERIFIED" in (outcome.reason or "")
    study = (await db_session.execute(
        select(ClinicalStudy).where(ClinicalStudy.study_id == "NCT03104400")
    )).scalar_one()
    assert study.verified_by == "Dr Reviewer"


async def test_a_study_with_no_retained_document_cannot_be_reparsed(db_session):
    """A FRAGMENT_ONLY licence keeps no document, so there is nothing to re-read.

    Reported with the licence named, because "nothing to re-read" and "nothing changed" are
    different outcomes and only one of them means re-harvesting is the only remedy.
    """
    await ingest.ingest_study(db_session, _parsed(), raw_payload=json.dumps(_payload()))
    payload = (await db_session.execute(select(SourcePayload))).scalar_one()
    payload.raw_payload = None
    await db_session.commit()

    outcome = await ingest.reparse_study(db_session, "NCT03104400")

    assert outcome.action == "SKIPPED"
    assert "can only be re-harvested" in (outcome.reason or "")
    assert payload.license_class in (outcome.reason or "")


async def test_reparse_flags_a_study_current_screening_would_now_reject(db_session):
    """Screening runs at ingest, so a rule tightened later never revisits stored rows.

    Re-parse does not delete studies — removing evidence is a decision, not a maintenance
    side effect — but leaving one silently inside a corpus current rules would refuse is the
    same class of defect as the orphan skip above.
    """
    payload = _payload()
    design = payload["protocolSection"].setdefault("designModule", {})
    design.setdefault("designInfo", {})["allocation"] = "NON_RANDOMIZED"
    raw = json.dumps(payload)
    await ingest.ingest_study(db_session, _parsed(payload), raw_payload=raw)

    outcome = await ingest.reparse_study(db_session, "NCT03104400")

    assert outcome.action == "UPDATED"
    assert any("not randomised" in w for w in outcome.warnings)
    assert any("needs a decision" in w for w in outcome.warnings)


async def test_reparse_studies_scopes_to_one_indication(db_session):
    await ingest.ingest_study(db_session, _parsed(), raw_payload=json.dumps(_payload()))

    assert await ingest.reparse_studies(db_session, indication="Atopic Dermatitis") == []
    results = await ingest.reparse_studies(db_session, indication=INDICATION)
    assert [r.study_id for r in results] == ["NCT03104400"]


# =====================================================================================
# Network construction
# =====================================================================================
async def _study(
    db,
    study_id: str,
    arms: list[tuple[str, int, int]],
    *,
    outcome_id: str = "PSA_ACR50_W16",
    week: float = 16,
    phase: str = "PRIMARY",
    stratum: str | None = None,
    randomised: bool = True,
    verification: str = lifecycles.VERIFIED,
) -> None:
    db.add(ClinicalStudy(
        study_id=study_id, indication=INDICATION, treatment_phase=phase,
        is_randomised=randomised, verification_status=verification,
        population_stratum=stratum,
    ))
    for index, (treatment, events, n) in enumerate(arms):
        arm_id = f"{study_id}:A{index}"
        db.add(StudyArm(
            arm_id=arm_id, study_id=study_id, treatment=treatment,
            is_placebo=treatment == "Placebo", sample_size=n,
            administration_route="ORAL" if treatment == "Rinvoq" else "SUBCUTANEOUS",
        ))
        db.add(OutcomeResult(
            result_id=f"{study_id}:R{index}", study_id=study_id, arm_id=arm_id,
            canonical_outcome_id=outcome_id, endpoint="ACR50", timepoint_week=week,
            treatment_phase=phase, population_stratum=stratum,
            outcome_type=BINARY, events=events, sample_size=n,
        ))
    await db.commit()


async def test_the_network_id_is_derived_from_the_whole_scope():
    """Derived so re-running updates one network instead of accumulating near-duplicates."""
    base = builder.network_id_for(
        indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16"
    )
    assert base == "NET-PSORIATIC-ARTHRITIS-PSA_ACR50_W16"

    # Any scope change is a DIFFERENT network, not a variant of the same one.
    assert builder.network_id_for(
        indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
        treatment_phase="INDUCTION",
    ) != base
    assert builder.network_id_for(
        indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
        population_stratum="BIO_NAIVE",
    ) != base


async def test_a_dry_run_build_reports_the_graph_but_writes_nothing(db_session):
    """The builder had the same unconditional commit ingestion did.

    A reviewer needs to see the graph the data produces BEFORE agreeing to store it, so the
    report must be fully populated while the rows stay uncommitted.
    """
    await _study(db_session, "S1", [("Rinvoq", 45, 100), ("Placebo", 20, 100)])
    await _study(db_session, "S2", [("Humira", 35, 100), ("Placebo", 18, 100)])

    report = await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
        commit=False,
    )
    assert report.topology_summary["node_count"] == 3
    assert sorted(report.proposed_studies) == ["S1", "S2"]

    await db_session.rollback()
    assert (await db_session.execute(select(EvidenceNetwork))).scalars().all() == []
    assert (await db_session.execute(select(NetworkMembership))).scalars().all() == []


async def test_building_a_star_network_records_its_topology(db_session):
    await _study(db_session, "S1", [("Rinvoq", 45, 100), ("Placebo", 20, 100)])
    await _study(db_session, "S2", [("Humira", 35, 100), ("Placebo", 18, 100)])

    report = await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
    )

    assert report.created
    network = (await db_session.execute(select(EvidenceNetwork))).scalar_one()
    assert json.loads(network.treatment_nodes) == ["Humira", "Placebo", "Rinvoq"]
    assert network.is_connected
    assert not network.has_closed_loops
    assert not network.has_multi_arm_studies
    assert json.loads(network.administration_routes)["Rinvoq"] == "ORAL"


async def test_a_within_study_triangle_is_not_independent_evidence(db_session):
    """A three-arm trial forms a triangle, but its comparisons share a control group.

    Counting it as a closed loop available for inconsistency testing would claim the network
    can be checked for disagreement when there is nothing to disagree with.
    """
    await _study(
        db_session, "MULTI",
        [("Rinvoq", 45, 100), ("Humira", 35, 100), ("Placebo", 20, 100)],
    )
    report = await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
    )

    assert report.topology_summary["has_multi_arm_studies"]
    assert report.topology_summary["loop_count"] == 1
    assert report.topology_summary["independent_loop_count"] == 0


async def test_the_builder_proposes_and_never_includes(db_session):
    """Inclusion is a per-analysis clinical judgement, so proposing it is as far as a
    pipeline can honestly go."""
    await _study(db_session, "S1", [("Rinvoq", 45, 100), ("Placebo", 20, 100)])
    await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
    )

    memberships = (await db_session.execute(select(NetworkMembership))).scalars().all()
    assert memberships
    assert all(m.membership_status == lifecycles.PROPOSED for m in memberships)
    assert all(m.proposal_rationale for m in memberships)

    network = (await db_session.execute(select(EvidenceNetwork))).scalar_one()
    assert network.ratification_status == lifecycles.DRAFT


async def test_rebuilding_does_not_downgrade_an_existing_membership_decision(db_session):
    """The builder re-running is not new information about a judgement already made."""
    await _study(db_session, "S1", [("Rinvoq", 45, 100), ("Placebo", 20, 100)])
    await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
    )
    membership = (await db_session.execute(select(NetworkMembership))).scalar_one()
    membership.membership_status = lifecycles.INCLUDED
    await db_session.commit()

    await _study(db_session, "S2", [("Humira", 35, 100), ("Placebo", 18, 100)])
    await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
    )

    rows = {
        m.study_id: m.membership_status
        for m in (await db_session.execute(select(NetworkMembership))).scalars().all()
    }
    assert rows["S1"] == lifecycles.INCLUDED   # preserved
    assert rows["S2"] == lifecycles.PROPOSED   # newly proposed


async def test_a_study_reporting_another_outcome_is_excluded_with_a_reason(db_session):
    await _study(db_session, "S1", [("Rinvoq", 45, 100), ("Placebo", 20, 100)])
    await _study(
        db_session, "OTHER", [("Humira", 35, 100), ("Placebo", 18, 100)],
        outcome_id="PSA_ACR20_W16",
    )

    report = await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
    )
    assert report.proposed_studies == ["S1"]
    assert any(
        s == "OTHER" and "PSA_ACR50_W16" in reason for s, reason in report.excluded
    )


async def test_two_studies_whose_arms_are_called_a_and_b_do_not_merge_into_shared_nodes(
    db_session,
):
    """Taken from the live PsA run: "A" and "B" appeared in NCT00646178 AND NCT00646386.

    A node is identified by its label, so admitting these would pool two unrelated trials
    onto a comparator that does not exist and close a loop the evidence never contained —
    the same fabrication that justifies screening "Standard Care", with even less to inspect.
    """
    await _study(db_session, "OLD1", [("A", 40, 100), ("B", 20, 100)])
    await _study(db_session, "OLD2", [("A", 33, 100), ("B", 18, 100)])

    report = await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
    )

    assert report.proposed_studies == []
    assert {s for s, _r in report.excluded} == {"OLD1", "OLD2"}
    assert all("names no treatment" in reason for _s, reason in report.excluded)
    network = (await db_session.execute(select(EvidenceNetwork))).scalar_one()
    assert json.loads(network.treatment_nodes) == []


async def test_one_unidentifiable_arm_disqualifies_the_whole_study(db_session):
    """A contrast needs both sides named. The identifiable arm is not a network on its own."""
    await _study(db_session, "MIXED", [("Rinvoq", 45, 100), ("Group B", 20, 100)])

    report = await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
    )

    assert report.proposed_studies == []
    network = (await db_session.execute(select(EvidenceNetwork))).scalar_one()
    assert json.loads(network.treatment_nodes) == []


async def test_a_properly_labelled_study_is_still_admitted(db_session):
    """The guard above must not be so broad that it starts refusing real evidence."""
    await _study(db_session, "GOOD", [("Rinvoq", 45, 100), ("Placebo", 20, 100)])

    report = await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
    )

    assert report.proposed_studies == ["GOOD"]
    network = (await db_session.execute(select(EvidenceNetwork))).scalar_one()
    assert set(json.loads(network.treatment_nodes)) == {"Rinvoq", "Placebo"}


async def test_a_study_already_in_the_database_cannot_smuggle_a_strategy_arm_into_a_network(
    db_session,
):
    """Screening at ingestion cannot protect a network by itself.

    ``build_network`` reads every study for the indication, not only the current run's. The
    four PsA strategy trials were ingested BEFORE the screen existed, so they are still in
    the table and "Standard Care" would still become a node — pooling with every other
    trial's "Standard Care" on nothing but a shared label.
    """
    await _study(db_session, "STRATEGY", [("Rinvoq", 45, 100), ("Standard Care", 20, 100)])

    report = await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
    )

    assert report.proposed_studies == []
    assert any("class" in reason or "strategy" in reason for _s, reason in report.excluded)
    network = (await db_session.execute(select(EvidenceNetwork))).scalar_one()
    assert json.loads(network.treatment_nodes) == []


async def test_a_non_randomised_study_is_excluded(db_session):
    """Admitting one would invent network edges that no randomisation supports."""
    await _study(
        db_session, "OBS", [("Rinvoq", 45, 100), ("Placebo", 20, 100)], randomised=False
    )
    report = await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
    )
    assert report.proposed_studies == []
    assert any("not randomised" in reason for _s, reason in report.excluded)


async def test_a_phase_mismatch_is_excluded_and_names_the_phase_found(db_session):
    """Maintenance cohorts are re-randomised induction responders, so this is a hard gate."""
    await _study(
        db_session, "MAINT", [("Rinvoq", 45, 100), ("Placebo", 20, 100)],
        phase="MAINTENANCE",
    )
    report = await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
        treatment_phase="PRIMARY",
    )
    assert report.proposed_studies == []
    assert any("MAINTENANCE" in reason for _s, reason in report.excluded)


# =====================================================================================
# The two topologies: endpoint-level, and what the protocol leaves answerable
# =====================================================================================
async def _psa_network_with_one_early_reader(db) -> None:
    """A star network where Rinvoq reports at week 12, outside PROTOCOL's [14, 18]."""
    await _study(db, "EARLY", [("Rinvoq", 45, 100), ("Placebo", 20, 100)], week=12)
    await _study(db, "S2", [("Humira", 35, 100), ("Placebo", 18, 100)], week=16)
    await _study(db, "S3", [("Tremfya", 30, 100), ("Placebo", 17, 100)], week=16)


async def test_a_build_report_discloses_what_the_protocol_window_leaves_answerable(
    db_session,
):
    """The live PsA run announced 8 connected nodes including Rinvoq; every
    protocol-scoped resolve saw 6 without it, and the report said nothing about the gap.

    SELECT-PsA 1 really does report ACR50 at week 12 while ``PSA_ACR50_W16_PRIMARY``
    approves [14, 18], so the node is in the network and is not answerable under it.
    """
    await _psa_network_with_one_early_reader(db_session)

    report = await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
        protocol_id=PROTOCOL,
    )

    # Endpoint-level: the whole of the outcome's own window, so Rinvoq is a node.
    assert report.topology_summary["node_count"] == 4
    assert report.topology_summary["is_connected"]

    scope = report.protocol_scope
    assert scope is not None
    assert scope.protocol_id == PROTOCOL
    assert scope.approved_time_window == (14.0, 18.0)
    assert scope.topology_summary["node_count"] == 3
    assert "Rinvoq" not in scope.topology_summary["nodes"]
    assert scope.nodes_lost == ("Rinvoq",)
    assert scope.studies_out_of_window == ("EARLY",)
    assert scope.narrows
    assert report.overstates_answerable


async def test_the_protocol_window_is_disclosed_and_never_applied(db_session):
    """Disclosure, not enforcement.

    Applying the window here would put one judgement in the builder AND the resolver, which
    is the defect this disclosure exists to reveal. An approval can widen the window without
    re-harvesting anything, so the out-of-window study stays a proposed member and the
    stored topology stays endpoint-level.
    """
    await _psa_network_with_one_early_reader(db_session)

    report = await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
        protocol_id=PROTOCOL,
    )

    assert "EARLY" in report.proposed_studies
    assert not any(s == "EARLY" for s, _r in report.excluded)

    memberships = {
        m.study_id: m.membership_status
        for m in (await db_session.execute(select(NetworkMembership))).scalars().all()
    }
    assert memberships["EARLY"] == lifecycles.PROPOSED

    network = (await db_session.execute(select(EvidenceNetwork))).scalar_one()
    assert "Rinvoq" in json.loads(network.treatment_nodes)
    assert network.is_connected


async def test_without_a_protocol_there_is_no_scoped_topology_to_report(db_session):
    """No approved window means nothing narrows it. Reporting an empty scoped graph would
    claim nothing is answerable, which is a different and worse lie."""
    await _psa_network_with_one_early_reader(db_session)

    report = await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
    )

    assert report.protocol_scope is None
    assert not report.overstates_answerable
    assert report.topology_summary["node_count"] == 4


async def test_a_window_that_admits_everything_narrows_nothing(db_session):
    """The disclosure must not cry wolf, or a real narrowing stops being visible."""
    await _study(db_session, "S1", [("Rinvoq", 45, 100), ("Placebo", 20, 100)], week=16)
    await _study(db_session, "S2", [("Humira", 35, 100), ("Placebo", 18, 100)], week=16)

    report = await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
        protocol_id=PROTOCOL,
    )

    scope = report.protocol_scope
    assert scope is not None
    assert scope.nodes_lost == ()
    assert scope.studies_out_of_window == ()
    assert not scope.narrows
    assert not report.overstates_answerable
    assert scope.topology_summary["node_count"] == report.topology_summary["node_count"]


async def test_a_rebuild_discloses_against_the_protocol_already_on_the_network(db_session):
    """A rebuild that names no protocol does not un-govern the network.

    The resolver reads ``network.protocol_id``, so scoping the disclosure to the passed
    argument would report "nothing narrows this" about a network a protocol still governs.
    """
    await _psa_network_with_one_early_reader(db_session)
    await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
        protocol_id=PROTOCOL,
    )

    report = await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
    )

    assert report.protocol_id is None            # nothing was passed in
    assert report.protocol_scope is not None     # but one still governs
    assert report.protocol_scope.protocol_id == PROTOCOL
    assert report.protocol_scope.nodes_lost == ("Rinvoq",)


async def test_an_undefined_protocol_is_refused(db_session):
    with pytest.raises(builder.NetworkBuildError, match="not defined"):
        await builder.build_network(
            db_session, indication=INDICATION,
            canonical_outcome_id="PSA_ACR50_W16", protocol_id="NO_SUCH_PROTOCOL",
        )


async def test_rebuilding_a_ratified_network_is_refused(db_session):
    """Changing the evidence set under a ratified network would invalidate the review it
    passed while leaving it looking approved."""
    await _study(db_session, "S1", [("Rinvoq", 45, 100), ("Placebo", 20, 100)])
    await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
    )
    network = (await db_session.execute(select(EvidenceNetwork))).scalar_one()
    network.ratification_status = lifecycles.RATIFIED
    await db_session.commit()

    with pytest.raises(builder.NetworkBuildError, match="RATIFIED"):
        await builder.build_network(
            db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
        )


@pytest.mark.parametrize(
    "status", (lifecycles.PENDING_MEDICAL_REVIEW, lifecycles.PENDING_STATISTICAL_REVIEW)
)
async def test_rebuilding_a_network_under_review_is_refused(db_session, status):
    """A review in progress is as untouchable as a finished one.

    This refused only on ``RATIFIED``, so a rebuild could rewrite the nodes and edges under
    a reviewer who was part-way through reading them — the graph they approve would not be
    the graph they read, and nothing would say so. ``scripts/reparse_dev_pilot.py`` already
    blocked all three states; the builder now shares that one rule rather than holding a
    narrower opinion of it.
    """
    await _study(db_session, "S1", [("Rinvoq", 45, 100), ("Placebo", 20, 100)])
    await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
    )
    network = (await db_session.execute(select(EvidenceNetwork))).scalar_one()
    network.ratification_status = status
    await db_session.commit()

    with pytest.raises(builder.NetworkBuildError, match=status):
        await builder.build_network(
            db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
        )


async def test_a_superseded_network_is_still_rebuildable(db_session):
    """The frozen set is deliberately not the complement of DRAFT.

    A retired network is nobody's live evidence set, so nothing is protected by refusing to
    touch it. Folding SUPERSEDED into the guard would look tidier and would block the one
    operation a superseded row might still legitimately need.
    """
    await _study(db_session, "S1", [("Rinvoq", 45, 100), ("Placebo", 20, 100)])
    await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
    )
    network = (await db_session.execute(select(EvidenceNetwork))).scalar_one()
    network.ratification_status = lifecycles.SUPERSEDED
    await db_session.commit()

    report = await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
    )
    assert report.network_id


# =====================================================================================
# End to end: ingest -> verify -> build -> resolve
# =====================================================================================
async def test_the_resolver_answers_on_ingested_data(db_session):
    """The point of the whole slice. Until this passes, Phase 6 has no fuel.

    SELECT-PsA 1 randomised Rinvoq against Humira directly, so the honest answer is Level 1
    — the trial's own result, releasable without our governance.
    """
    from app.services import comparison_service

    await ingest.ingest_study(db_session, _parsed(), raw_payload=json.dumps(_payload()))
    await ingest.verify_study(db_session, "NCT03104400", verified_by="Dr Reviewer")

    study = (await db_session.execute(select(ClinicalStudy))).scalar_one()
    outcome_id = next(
        (o.canonical_outcome_id for o in study.outcomes if o.canonical_outcome_id), None
    )
    assert outcome_id, "the fixture must map at least one endpoint for this test to mean anything"

    report = await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id=outcome_id,
    )
    assert report.proposed_studies == ["NCT03104400"]

    answer = await comparison_service.resolve_comparison(
        db_session, network_id=report.network_id,
        treatment_a="Rinvoq", treatment_b="Humira",
    )
    assert answer["evidence_level"] == 1
    assert answer["estimate"] is not None
    assert answer["ci_lower"] < answer["estimate"] < answer["ci_upper"]
    # A randomised head-to-head result is the trial's own finding, so it does not need our
    # protocol approval to be usable.
    assert answer["is_releasable"]


async def test_verify_study_honours_commit_false(db_session):
    """The last unconditional commit in the module, and the worst place for one.

    An unconditional commit here does not just persist the verification — it persists
    **everything the caller had queued before it**, because the caller's later `rollback()`
    then rolls back an empty transaction. A maintenance script that reset rows, re-parsed a
    corpus and then verified would write all of it while printing "nothing will be written".
    That is how the first run of `scripts/reparse_dev_pilot` modified a database during what
    it reported as a dry run.
    """
    await ingest.ingest_study(
        db_session, _parsed(), raw_payload=json.dumps(_payload()), commit=False
    )
    await ingest.verify_study(
        db_session, "NCT03104400", verified_by="Dr Reviewer", commit=False
    )
    await db_session.rollback()

    survivors = (await db_session.execute(select(ClinicalStudy))).scalars().all()
    assert survivors == [], (
        "verify_study committed despite commit=False, so the rollback could not undo the "
        "ingest that preceded it"
    )
