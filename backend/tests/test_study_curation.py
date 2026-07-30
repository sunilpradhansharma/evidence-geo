"""Curator-facing study verification.

The gate these tests protect is the one that blocks everything else: evidence gathering
skips an unverified study **even in EXPLORATORY mode**, so a corpus nobody has curated
yields no number at all. Verification is therefore the cheapest thing standing between a
built network and a result — and the easiest to fake, which is what the reproducibility
refusal below exists to prevent.

Uses the committed SELECT-PsA 1 registry record rather than a hand-written payload, so a
re-derivation is exercised against the real shape of the thing: multi-period arms, class
axes, back-derived events and all.
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
from app.models.clinical_study import ClinicalStudy
from app.models.database import Base
from app.models.evidence_network import EvidenceNetwork, NetworkMembership
from app.models.source_payload import SourcePayload
from app.services import evidence_ingestion_service as ingest
from app.services import study_curation_service as curation

FIXTURES = Path(__file__).parent / "fixtures"
INDICATION = "Psoriatic Arthritis"
STUDY = "NCT03104400"


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


def _parsed(payload: dict) -> ctg.ParsedStudy:
    parsed = ctg.parse(
        FetchResult(
            ok=True, source_type=ctg.SOURCE_TYPE, source_identifier=STUDY,
            payload=payload, raw_text=json.dumps(payload),
        ),
        indication=INDICATION,
    )
    assert parsed is not None
    return parsed


async def _ingested(db, payload: dict | None = None) -> None:
    body = payload or _payload()
    await ingest.ingest_study(db, _parsed(body), raw_payload=json.dumps(body))


async def _study_row(db) -> ClinicalStudy:
    return (await db.execute(
        select(ClinicalStudy).where(ClinicalStudy.study_id == STUDY)
    )).scalar_one()


# =====================================================================================
# The re-derivation check
# =====================================================================================
async def test_a_freshly_ingested_study_reproduces_from_its_retained_payload(db_session):
    """The baseline. Same bytes, same parser, so nothing may differ."""
    await _ingested(db_session)

    diff = await curation.rederivation_diff(db_session, STUDY)

    assert diff["checkable"] is True
    assert diff["blocked_reason"] is None
    assert diff["reproducible"] is True
    assert diff["difference_count"] == 0
    assert diff["counts"]["arms"]["stored"] == diff["counts"]["arms"]["source"]
    assert diff["counts"]["outcomes"]["stored"] == diff["counts"]["outcomes"]["source"]


async def test_the_check_reports_the_source_and_the_parsers_own_doubts(db_session):
    """A green diff is not evidence of correctness, so the response cannot stop there.

    Reproducibility only rules out the stored rows being *separately* stale. What sends a
    curator to the registry record is the provenance and the flags the parser raised about
    its own reading — most of all `EVENTS_DERIVED_FROM_PERCENTAGE`, where the number on
    screen was back-derived from a rounded percentage rather than posted.
    """
    await _ingested(db_session)

    diff = await curation.rederivation_diff(db_session, STUDY)

    assert diff["source"]["source_identifier"] == STUDY
    assert diff["source"]["checksum"]
    # The parse of a real registry record is never silent about what it had to infer.
    assert diff["flag_counts"], "a real extraction carries mismatch flags to disclose"


async def test_a_drifted_stored_row_is_named_field_by_field(db_session):
    """What the curator is actually being asked to trust.

    A stored row that no longer matches its own source means the parser has moved on and
    nobody re-parsed. The diff has to say which field, not merely that something differs.
    """
    await _ingested(db_session)
    study = await _study_row(db_session)
    study.enrollment = 99999
    arm = sorted(study.arms, key=lambda a: a.arm_id)[0]
    arm.sample_size = 12345
    await db_session.commit()

    diff = await curation.rederivation_diff(db_session, STUDY)

    assert diff["reproducible"] is False
    fields = {(d["kind"], d["field"]) for d in diff["differences"]}
    assert ("study", "enrollment") in fields
    assert ("arm", "sample_size") in fields
    enrollment = next(d for d in diff["differences"] if d["field"] == "enrollment")
    assert enrollment["stored"] == 99999
    assert enrollment["source"] != 99999


async def test_the_check_never_writes(db_session):
    """The reason this is not ``reparse_study``.

    ``reparse_study`` delegates to ``ingest_study``, which deletes the study and rewrites
    it from the re-derived rows. A curator has to be able to look before anything moves,
    so a drifted row must still be drifted after the check.
    """
    await _ingested(db_session)
    study = await _study_row(db_session)
    study.enrollment = 99999
    await db_session.commit()

    await curation.rederivation_diff(db_session, STUDY)

    db_session.expire_all()
    assert (await _study_row(db_session)).enrollment == 99999


async def test_a_study_whose_licence_forbids_retention_cannot_be_machine_checked(db_session):
    """"Nothing to re-read" and "nothing changed" are different answers.

    Reporting a FRAGMENT_ONLY study as reproducible would claim a check that is physically
    impossible — there is no document to check against.
    """
    await _ingested(db_session)
    study = await _study_row(db_session)
    payload = (await db_session.execute(
        select(SourcePayload).where(SourcePayload.payload_id == study.source_payload_id)
    )).scalar_one()
    payload.raw_payload = None
    await db_session.commit()

    diff = await curation.rederivation_diff(db_session, STUDY)

    assert diff["checkable"] is False
    assert diff["reproducible"] is False
    assert "no document retained" in diff["blocked_reason"]


# =====================================================================================
# The curator's confirmation
# =====================================================================================
async def test_confirming_a_reproducible_study_advances_it_to_verified(db_session):
    await _ingested(db_session)

    result = await curation.record_curator_check(
        db_session, study_id=STUDY, verified_by="S Bandgar",
        note="arms and ACR50 rows checked against the registry record",
    )

    assert result["verification_status"] == lifecycles.VERIFIED
    assert result["verified_by"] == "S Bandgar"
    assert (await _study_row(db_session)).verification_status == lifecycles.VERIFIED


async def test_a_stale_study_cannot_be_verified(db_session):
    """The trap this refusal exists to prevent.

    ``ingest_study`` SKIPS a VERIFIED row, so certifying a stale extraction puts it beyond
    the reach of the ordinary re-parse — the correction path is gone and only a deliberate
    out-of-band reset can recover it. Being asked to re-parse first is the cheaper failure.
    """
    await _ingested(db_session)
    study = await _study_row(db_session)
    study.enrollment = 99999
    await db_session.commit()

    with pytest.raises(curation.CurationError) as excinfo:
        await curation.record_curator_check(
            db_session, study_id=STUDY, verified_by="S Bandgar"
        )

    assert "does not reproduce" in str(excinfo.value)
    assert "Re-parse this study first" in str(excinfo.value)
    db_session.expire_all()
    assert (await _study_row(db_session)).verification_status != lifecycles.VERIFIED


async def test_an_anonymous_check_is_refused(db_session):
    await _ingested(db_session)
    with pytest.raises(curation.CurationError):
        await curation.record_curator_check(db_session, study_id=STUDY, verified_by="  ")


async def test_the_audit_entry_says_what_the_check_was_and_was_not(db_session):
    """A CURATOR entry, and one that refuses to be misread as a clinical sign-off.

    The same string field records both kinds of name, so the record has to carry the
    distinction itself rather than leaving it to whoever reads the trail later.
    """
    from app.models.audit_log import AuditLog

    await _ingested(db_session)
    await curation.record_curator_check(
        db_session, study_id=STUDY, verified_by="S Bandgar", note="spot-checked ACR50",
    )

    entry = (await db_session.execute(
        select(AuditLog).where(AuditLog.event == "STUDY_CURATION_CHECK_RECORDED")
    )).scalars().one()
    context = json.loads(entry.context)
    assert entry.role == "CURATOR"
    assert context["payload_checksum"]
    assert "clinical" in context["check_is_not"]
    assert context["difference_count"] == 0


# =====================================================================================
# The queue
# =====================================================================================
async def test_the_queue_scopes_to_what_a_network_is_actually_waiting_on(db_session):
    """An open-ended backlog and a finite task are different propositions.

    Scoped to a network, the queue names exactly the studies a resolve would consult, so a
    curator can see the end of the work rather than the size of the corpus.
    """
    await _ingested(db_session)
    db_session.add(ClinicalStudy(
        study_id="NCT99999999", indication=INDICATION, is_randomised=True,
    ))
    db_session.add(EvidenceNetwork(
        network_id="NET-X", indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
        protocol_id="PSA_ACR50_W16_PRIMARY",
    ))
    db_session.add(NetworkMembership(
        membership_id="NM-1", network_id="NET-X", study_id=STUDY,
        membership_status=lifecycles.INCLUDED,
    ))
    await db_session.commit()

    scoped = await curation.curation_queue(db_session, network_id="NET-X")
    everything = await curation.curation_queue(db_session)

    assert [s["study_id"] for s in scoped["studies"]] == [STUDY]
    assert scoped["blocking"] == 1
    # The unrelated study is real and unverified, but no resolve of NET-X would consult it.
    assert len(everything["studies"]) == 2


async def test_an_empty_included_set_means_membership_narrows_nothing(db_session):
    """The live case, and the one this queue originally got backwards.

    The builder proposes every membership as PROPOSED and nothing promotes them, so every
    real network has an empty INCLUDED set. ``gather_evidence`` reads that as "membership
    is not narrowing anything" — `if included is not None` — and consults the whole
    indication. A queue that instead read it as an empty corpus reported that verifying
    these studies would change nothing, which is the exact opposite of the truth.
    """
    await _ingested(db_session)
    db_session.add(EvidenceNetwork(
        network_id="NET-PROPOSED", indication=INDICATION,
        canonical_outcome_id="PSA_ACR50_W16",
    ))
    db_session.add(NetworkMembership(
        membership_id="NM-P", network_id="NET-PROPOSED", study_id=STUDY,
        membership_status=lifecycles.PROPOSED,
    ))
    await db_session.commit()

    queue = await curation.curation_queue(db_session, network_id="NET-PROPOSED")

    assert [s["study_id"] for s in queue["studies"]] == [STUDY]
    assert queue["blocking"] == 1
    assert "consults every" in queue["note"]


async def test_the_queue_and_the_resolver_cannot_disagree_about_scope(db_session):
    """Both read the same rule from the same function, so they move together."""
    from app.services import comparison_service as comparisons

    network = EvidenceNetwork(
        network_id="NET-SHARED", indication=INDICATION,
        canonical_outcome_id="PSA_ACR50_W16",
    )
    db_session.add(network)
    db_session.add(NetworkMembership(
        membership_id="NM-S", network_id="NET-SHARED", study_id=STUDY,
        membership_status=lifecycles.PROPOSED,
    ))
    await db_session.commit()

    assert await comparisons.membership_filter(db_session, network) is None


async def test_the_queue_separates_work_that_matters_from_work_that_cannot(db_session):
    """37 unverified studies and 11 that could change the answer are different tasks.

    Two thirds of the live PsA corpus carry no canonical outcome at all — healthy-subject
    PK studies, genetic association studies, a plaque psoriasis trial. Verifying them is
    real work that cannot move the network, so the queue must not present them as though
    it could.
    """
    await _ingested(db_session)
    db_session.add(ClinicalStudy(
        study_id="NCT-BYSTANDER", indication=INDICATION, is_randomised=True,
    ))
    # The committed SELECT-PsA 1 record reports ACR20, not ACR50 — the same mismatch that
    # makes issue 1 real. Pointing this network at ACR50 would make every study in it a
    # non-contributor and the test would pass without exercising anything.
    db_session.add(EvidenceNetwork(
        network_id="NET-C", indication=INDICATION,
        canonical_outcome_id="PSA_ACR20_W16",
    ))
    await db_session.commit()

    queue = await curation.curation_queue(db_session, network_id="NET-C")
    by_id = {s["study_id"]: s for s in queue["studies"]}

    assert queue["blocking"] == 2
    assert queue["worth_verifying"] == 1
    assert by_id[STUDY]["could_contribute"] is True
    assert by_id[STUDY]["in_scope_arm_count"] >= 2
    # No outcome rows at all, so nothing this network measures.
    assert by_id["NCT-BYSTANDER"]["could_contribute"] is False
    # And the one that matters is listed first.
    assert queue["studies"][0]["study_id"] == STUDY
    # The contributing study reports plenty of other endpoints. None of them is a reason:
    # "measures HAQ-DI, not PSA_ACR20_W16" is true of most rows in most trials and drowns
    # out the refusal that actually needs a human.
    assert by_id[STUDY]["withheld_reasons"] == []
    assert queue["protocol_blocked"] == []


async def test_a_study_refused_by_the_window_is_a_protocol_decision_not_curation_work(
    db_session, monkeypatch,
):
    """Issue 1's fingerprint, per study.

    A trial that reports exactly the network's outcome and is still refused cannot be
    fixed by checking it more carefully. Filing it as curation backlog sends a curator to
    re-read a paper that was never going to count, and hides a decision only a reviewer
    can make.
    """
    await _ingested(db_session)
    db_session.add(EvidenceNetwork(
        network_id="NET-W", indication=INDICATION,
        canonical_outcome_id="PSA_ACR20_W16", protocol_id="P-NARROW",
    ))
    await db_session.commit()

    # The committed record reports ACR20 at week 16; a window that excludes it turns every
    # in-scope row into a refusal without touching the data.
    monkeypatch.setattr(
        curation.protocols, "approved_time_window", lambda pid: (30.0, 40.0)
    )

    queue = await curation.curation_queue(db_session, network_id="NET-W")
    row = next(s for s in queue["studies"] if s["study_id"] == STUDY)

    assert row["could_contribute"] is False
    assert row["withheld_row_count"] > 0
    assert any("outside the approved window" in r for r in row["withheld_reasons"])
    assert queue["protocol_blocked"] == [STUDY]
    assert "protocol decision, not curation work" in queue["note"]


async def test_an_unknown_network_is_an_error_not_an_empty_queue(db_session):
    """A typo and "nothing to do here" must not look the same.

    Returning an empty list for a network that does not exist asserts something about a
    network nobody has, and sends the reader looking for missing memberships.
    """
    with pytest.raises(curation.CurationError) as excinfo:
        await curation.curation_queue(db_session, network_id="NET-TYPO")

    assert "unknown network" in str(excinfo.value)


async def test_the_queue_flags_a_study_that_cannot_be_machine_checked(db_session):
    """Sending a curator to a screen with nothing on it is its own defect."""
    db_session.add(ClinicalStudy(
        study_id="NCT77777777", indication=INDICATION, is_randomised=True,
    ))
    await db_session.commit()

    queue = await curation.curation_queue(db_session)

    row = next(s for s in queue["studies"] if s["study_id"] == "NCT77777777")
    assert row["has_retained_document"] is False
