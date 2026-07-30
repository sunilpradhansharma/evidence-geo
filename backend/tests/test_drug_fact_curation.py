"""Drug facts, end to end: ingestion, label-date versioning, curation, and what unblocks.

The defect these tests exist to keep closed is not a wrong answer, it is an **absent** one.
``openfda_facts.parse_label`` had no caller and ``DrugFact.verification_status`` had no way
out of ``EXTRACTED``, so three consumers that filter on ``VERIFIED`` returned nothing —
which reads as "no findings" rather than "not wired", and is therefore invisible.

The last section is the important one. It asserts that a verified fact actually reaches
Phase 7's question generator and Phase 8's claim grader, because rebuilding the ingestion
half without checking the consumers would leave exactly the same silence in place.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.evidence import lifecycles
from app.evidence.sources import openfda_facts as fda
from app.geo.sources.openfda import LabelSeed
from app.models.database import Base
from app.models.drug_fact import DrugFact
from app.models.source_payload import SourcePayload
from app.services import drug_fact_curation_service as curation
from app.services import evidence_ingestion_service as ingest

BRAND = "Rinvoq"


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from app.models import (  # noqa: F401 — register tables on Base.metadata
        analysis_protocol,
        audit_log,
        clinical_study,
        competitor_candidate,
        drug_fact,
        evidence_network,
        nma_result,
        question_evidence,
        source_payload,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _seed(**overrides) -> LabelSeed:
    """A realistic openFDA seed for a curated brand."""
    base = dict(
        brand_name="RINVOQ",
        generic_name="upadacitinib",
        manufacturer="AbbVie Inc.",
        administration_route="ORAL",
        drug_class="Janus Kinase Inhibitor",
        active_ingredient="upadacitinib",
        has_boxed_warning=True,
        boxed_warning_text=(
            "SERIOUS INFECTIONS\nMORTALITY\nMALIGNANCIES\n"
            "MAJOR ADVERSE CARDIOVASCULAR EVENTS\nTHROMBOSIS"
        ),
        indications_text="RINVOQ is indicated for the treatment of adults with ...",
        effective_time="2024-03-11",
        set_id="abc-123",
        prescribing_information="https://dailymed.nlm.nih.gov/x",
    )
    base.update(overrides)
    return LabelSeed(**base)


async def _ingest(
    db, seed: LabelSeed | None = None, brand: str = BRAND, generic: str | None = "upadacitinib"
):
    return await ingest.ingest_drug_fact(
        db, seed or _seed(), brand=brand, generic=generic, commit=False
    )


# =====================================================================================
# Ingestion
# =====================================================================================
async def test_a_label_is_persisted_with_its_retained_payload(db_session):
    outcome = await _ingest(db_session)

    assert outcome.action == "INGESTED"
    assert outcome.fact_id == "DF-RINVOQ-2024-03-11"

    fact = (await db_session.execute(
        select(DrugFact).where(DrugFact.fact_id == outcome.fact_id)
    )).scalar_one()
    assert fact.brand == BRAND
    assert fact.has_boxed_warning is True
    assert fact.source_payload_id

    payload = (await db_session.execute(
        select(SourcePayload).where(SourcePayload.payload_id == fact.source_payload_id)
    )).scalar_one()
    # openFDA is PUBLIC_DOMAIN, so the parser's input is retained in full.
    assert payload.license_class == "PUBLIC_DOMAIN"
    assert payload.raw_payload is not None


async def test_ingestion_never_verifies_its_own_output(db_session):
    """The rule that makes the verification lifecycle mean anything."""
    outcome = await _ingest(db_session)
    assert outcome.verification_status in (lifecycles.EXTRACTED, lifecycles.MAPPED)
    assert outcome.verification_status != lifecycles.VERIFIED


async def test_a_curated_brand_reaches_mapped_and_an_uncurated_one_does_not(db_session):
    """MAPPED is a statement of fact — the brand resolved onto the catalog — not a judgement."""
    curated = await _ingest(db_session)
    assert curated.verification_status == lifecycles.MAPPED

    # No generic either: the catalog is searched on both, so passing a curated generic
    # would resolve the drug despite the unknown brand.
    unknown = await _ingest(
        db_session,
        _seed(brand_name="NOTADRUG", generic_name=None, drug_class=None,
              administration_route=None, set_id="zzz-999"),
        brand="Notadrug",
        generic=None,
    )
    assert fda.FLAG_NO_CURATED_ENTRY in unknown.flags
    assert unknown.verification_status == lifecycles.EXTRACTED


async def test_re_ingesting_the_same_label_updates_rather_than_duplicating(db_session):
    first = await _ingest(db_session)
    second = await _ingest(db_session)

    assert first.fact_id == second.fact_id
    assert second.action == "UPDATED"
    total = (await db_session.execute(select(DrugFact))).scalars().all()
    assert len(total) == 1


async def test_a_new_label_date_supersedes_rather_than_overwrites(db_session):
    """A drug fact versions by LABEL DATE, not by extraction correction.

    The old row stays because it remains a true statement about what the label said then,
    and a claim graded against it last quarter has to stay explicable.
    """
    old = await _ingest(db_session)
    new = await _ingest(db_session, _seed(effective_time="2025-06-01"))

    assert new.action == "SUPERSEDED"
    assert new.supersedes == old.fact_id

    rows = {f.fact_id: f for f in (await db_session.execute(select(DrugFact))).scalars()}
    assert len(rows) == 2
    assert rows[old.fact_id].superseded_by == new.fact_id
    assert rows[new.fact_id].superseded_by is None
    assert rows[new.fact_id].version == 2


async def test_an_older_label_never_moves_the_brand_backwards(db_session):
    """An out-of-order retrieval is reported and dropped, not applied."""
    await _ingest(db_session, _seed(effective_time="2025-06-01"))
    stale = await _ingest(db_session, _seed(effective_time="2024-03-11"))

    assert stale.action == "SKIPPED"
    assert "would move the brand backwards" in stale.reason


async def test_a_decided_fact_is_never_overwritten(db_session):
    outcome = await _ingest(db_session)
    await ingest.verify_drug_fact(
        db_session, outcome.fact_id, verified_by="A Curator", commit=False
    )

    again = await _ingest(db_session)
    assert again.action == "SKIPPED"
    assert "already VERIFIED" in again.reason


async def test_a_brand_openfda_has_nothing_for_does_not_fail_the_run(db_session, monkeypatch):
    """One unavailable label must not cost the other three."""
    async def _fetch(brand, generic=None):
        return None if brand == "Ghostdrug" else _seed()

    monkeypatch.setattr(fda, "fetch_label", _fetch)
    report = await ingest.ingest_drug_facts(
        db_session, ["Ghostdrug", BRAND], commit=False
    )

    data = report.as_dict()
    assert data["not_found"] == 1
    assert data["ingested"] == 1


# =====================================================================================
# Curation
# =====================================================================================
async def test_a_freshly_ingested_fact_reproduces_from_its_retained_seed(db_session):
    outcome = await _ingest(db_session)

    diff = await curation.rederivation_diff(db_session, outcome.fact_id)

    assert diff["checkable"] is True
    assert diff["reproducible"] is True
    assert diff["difference_count"] == 0
    # The narrower claim is stated in the payload, not left to the reader.
    assert "mapping" in diff["checks"]
    assert diff["prescribing_information"] == "https://dailymed.nlm.nih.gov/x"


async def test_a_drifted_fact_does_not_reproduce_and_cannot_be_verified(db_session):
    outcome = await _ingest(db_session)
    fact = (await db_session.execute(
        select(DrugFact).where(DrugFact.fact_id == outcome.fact_id)
    )).scalar_one()
    fact.manufacturer = "Someone Else Ltd"
    await db_session.flush()

    diff = await curation.rederivation_diff(db_session, outcome.fact_id)
    assert diff["reproducible"] is False
    assert any(d["field"] == "manufacturer" for d in diff["differences"])

    with pytest.raises(curation.CurationError, match="does not reproduce"):
        await curation.record_curator_check(
            db_session, fact_id=outcome.fact_id, verified_by="A Curator", commit=False
        )


async def test_a_clean_fact_can_be_verified_by_a_named_curator(db_session):
    outcome = await _ingest(db_session)

    result = await curation.record_curator_check(
        db_session, fact_id=outcome.fact_id, verified_by="A Curator", commit=False
    )

    assert result["verification_status"] == lifecycles.VERIFIED
    assert result["verified_by"] == "A Curator"


async def test_an_anonymous_check_is_refused(db_session):
    outcome = await _ingest(db_session)
    with pytest.raises(curation.CurationError, match="verified_by is required"):
        await curation.record_curator_check(
            db_session, fact_id=outcome.fact_id, verified_by="  ", commit=False
        )


async def test_a_superseded_version_cannot_be_verified(db_session):
    """Certifying a stale label version would put an outdated claim behind a verified flag."""
    old = await _ingest(db_session)
    await _ingest(db_session, _seed(effective_time="2025-06-01"))

    with pytest.raises(ingest.IngestionError, match="superseded"):
        await ingest.verify_drug_fact(
            db_session, old.fact_id, verified_by="A Curator", commit=False
        )


async def test_rejecting_a_fact_requires_a_reason(db_session):
    outcome = await _ingest(db_session)
    with pytest.raises(ingest.IngestionError, match="reason is required"):
        await ingest.reject_drug_fact(
            db_session, outcome.fact_id, rejected_by="A Curator", reason="", commit=False
        )

    rejected = await ingest.reject_drug_fact(
        db_session, outcome.fact_id, rejected_by="A Curator",
        reason="the boxed warning text belongs to a different product", commit=False,
    )
    assert rejected.verification_status == lifecycles.REJECTED
    assert "different product" in rejected.rejection_reason


# =====================================================================================
# The queue ranks by whether the work changes an answer
# =====================================================================================
async def test_the_queue_reports_approval_as_blocked_by_something_curation_cannot_fix(
    db_session,
):
    """The finding worth surfacing: verifying does not make an approval claim answerable.

    ``openfda_facts`` deliberately leaves the indications prose unstructured, so
    ``drug_fact_question`` refuses and an approval claim has no list to grade against —
    however carefully a curator checks the row.
    """
    await _ingest(db_session)

    queue = await curation.curation_queue(db_session)

    assert queue["total"] == 1
    entry = queue["facts"][0]
    assert entry["answers_safety_claim"] is True
    assert entry["answers_mechanism_claim"] is True
    assert entry["answers_approval_claim"] is False
    assert queue["approval_blocked"] == [entry["fact_id"]]
    assert "not a curator" in queue["note"]


async def test_a_self_contradictory_row_cannot_answer_a_safety_claim(db_session):
    """Boxed-warning text with the flag unset is a contradiction, not a low confidence."""
    await _ingest(db_session, _seed(has_boxed_warning=False))

    queue = await curation.curation_queue(db_session)
    entry = queue["facts"][0]
    assert entry["answers_safety_claim"] is False
    assert any("contradicts itself" in b for b in entry["blockers"])


async def test_superseded_versions_are_out_of_the_queue_by_default(db_session):
    await _ingest(db_session)
    await _ingest(db_session, _seed(effective_time="2025-06-01"))

    assert (await curation.curation_queue(db_session))["total"] == 1
    assert (await curation.curation_queue(
        db_session, include_superseded=True
    ))["total"] == 2


# =====================================================================================
# What verifying a fact actually unblocks — the point of the whole workstream
# =====================================================================================
async def test_a_verified_fact_reaches_phase_7_question_generation(db_session):
    """Before this workstream, ``_from_drug_facts`` could never return a question."""
    from app.services import evidence_question_service as questions

    outcome = await _ingest(db_session)
    refused: list[dict] = []
    assert await questions._from_drug_facts(
        db_session, "Psoriatic Arthritis", "Immunology", refused
    ) == []

    await curation.record_curator_check(
        db_session, fact_id=outcome.fact_id, verified_by="A Curator", commit=False
    )
    generated = await questions._from_drug_facts(
        db_session, "Psoriatic Arthritis", "Immunology", refused
    )

    assert [q.question_text for q in generated] == ["Does Rinvoq carry a boxed warning?"]
    # And the approval question is refused for a stated reason rather than silently absent.
    assert any("approved-indication list" in r["reason"] for r in refused)


async def test_a_verified_fact_becomes_the_authority_for_a_safety_claim(db_session):
    """Phase 8's ``SAFETY_WARNING_CLAIM`` had no authority to resolve against."""
    from app.services import claim_evaluation_service as claims

    outcome = await _ingest(db_session)
    assert await claims._verified_drug_fact(db_session, BRAND) is None

    await curation.record_curator_check(
        db_session, fact_id=outcome.fact_id, verified_by="A Curator", commit=False
    )

    fact = await claims._verified_drug_fact(db_session, BRAND)
    assert fact is not None
    assert fact.fact_id == outcome.fact_id
    assert fact.has_boxed_warning is True


async def test_only_the_current_version_answers_a_claim(db_session):
    """A superseded label must not keep grading claims after a newer one lands."""
    from app.services import claim_evaluation_service as claims

    old = await _ingest(db_session)
    await curation.record_curator_check(
        db_session, fact_id=old.fact_id, verified_by="A Curator", commit=False
    )
    assert (await claims._verified_drug_fact(db_session, BRAND)).fact_id == old.fact_id

    await _ingest(db_session, _seed(effective_time="2025-06-01"))

    # The new version is unverified, and the old one is no longer current, so there is no
    # authority at all — which is correct and must not silently fall back to the old row.
    assert await claims._verified_drug_fact(db_session, BRAND) is None


# =====================================================================================
# Payload round-trip
# =====================================================================================
def test_a_retained_seed_round_trips_through_the_payload():
    seed = _seed()
    raw = ingest.seed_payload(seed, brand=BRAND, generic="upadacitinib")
    back, brand, generic = ingest.seed_from_payload(raw)

    assert (brand, generic) == (BRAND, "upadacitinib")
    assert back == seed


def test_an_unknown_field_in_a_stored_seed_does_not_break_re_derivation():
    """A payload retained before a field was added must still re-derive.

    Passing it straight to the constructor would raise a TypeError deep inside a curation
    read, presenting a schema change as a corrupt document.
    """
    raw = json.dumps({
        "brand": BRAND, "generic": None,
        "seed": {"brand_name": "RINVOQ", "a_field_from_the_future": 1},
    })
    seed, brand, _generic = ingest.seed_from_payload(raw)
    assert seed.brand_name == "RINVOQ"
    assert brand == BRAND
