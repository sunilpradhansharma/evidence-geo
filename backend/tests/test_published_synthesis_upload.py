"""Phase 4 — the governed manual-upload path and licence-tier retention.

The load-bearing guarantee: **a restricted source cannot end up holding a document**, no
matter what the uploader sends. Retention is decided inside ``SourcePayload.record``, so
this service has no code path that bypasses it.

Also pinned here: a fresh upload is NOT marked ``PUBLISHED_RESULT_AVAILABLE``, because that
status asserts a suitability check that has not happened.
"""
from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.evidence import licensing, statuses
from app.models.audit_log import AuditLog
from app.models.database import Base
from app.models.nma_result import PUBLISHED, NMAResult
from app.models.source_payload import SourcePayload
from app.services import published_synthesis_service as svc

PROTOCOL = "PSA_ACR50_W16_PRIMARY"
PAYWALLED = "<html>the entire paywalled Cochrane review</html>"


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from app.models import (  # noqa: F401 — register the tables on Base.metadata
        audit_log,
        clinical_study,
        nma_result,
        source_payload,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _extraction(**overrides) -> dict:
    base = {
        "source_type": "COCHRANE",
        "source_identifier": "10.1002/14651858.CD013967",
        "citation": "Cochrane Database Syst Rev. 2024;3:CD013967.",
        "publication_date": "2024-03-01",
        "indication": "Psoriatic Arthritis",
        "endpoint": "ACR50",
        "timepoint_week": 16,
        "effect_measure": "RR",
        "model_type": "random-effects",
        "interval_type": "95% CI",
        "grade_certainty": "MODERATE",
        "league_table": {"Upadacitinib 15 mg": {"Adalimumab 40 mg": "1.40 (1.10 to 1.80)"}},
        "included_studies": ["NCT03104400", "NCT03104374"],
        "tau_squared": 0.04,
        "inconsistency": {"design_by_treatment_p": 0.42},
    }
    base.update(overrides)
    return base


# =====================================================================================
# Licence-tier retention
# =====================================================================================
async def test_a_restricted_upload_keeps_the_values_and_drops_the_document(db_session):
    """THE guarantee of this path. Uploading a paywalled PDF grants no right to store it."""
    result, parsed, payload = await svc.record_synthesis(
        db_session, _extraction(), uploaded_by="curator.a",
        raw_document=PAYWALLED,
        retained_fragment="ACR50 at week 16: RR 1.40 (1.10 to 1.80)",
        page_provenance="Table 3, p. 14",
    )

    assert payload.license_class == licensing.RESTRICTED
    assert payload.retention_policy == licensing.FRAGMENT_ONLY
    assert payload.raw_payload is None
    assert json.loads(payload.dropped_fields) == ["raw_payload"]

    # What survives is what makes the evidence auditable.
    assert payload.retained_fragment == "ACR50 at week 16: RR 1.40 (1.10 to 1.80)"
    assert payload.page_provenance == "Table 3, p. 14"
    assert payload.checksum and payload.checksum.startswith("sha256:")
    assert result.citation == "Cochrane Database Syst Rev. 2024;3:CD013967."
    assert parsed.contrasts[0].estimate == 1.40


async def test_the_checksum_is_of_the_document_even_when_it_is_not_retained(db_session):
    """A checksum is a fact ABOUT a document, not a copy of it, so it survives every tier."""
    from app.models.source_payload import checksum_of

    _, _, payload = await svc.record_synthesis(
        db_session, _extraction(), uploaded_by="curator.a", raw_document=PAYWALLED,
    )
    assert payload.raw_payload is None
    assert payload.checksum == checksum_of(PAYWALLED)


async def test_a_public_domain_upload_may_retain_the_document(db_session):
    _, _, payload = await svc.record_synthesis(
        db_session, _extraction(source_type="JOURNAL"), uploaded_by="curator.a",
        raw_document="full text", license_class=licensing.PUBLIC_DOMAIN,
    )
    assert payload.license_class == licensing.PUBLIC_DOMAIN
    assert payload.raw_payload == "full text"
    assert payload.dropped_fields is None
    assert payload.expires_at is None


async def test_an_open_access_upload_carries_an_expiry(db_session):
    """OA grants are revocable in practice, so full text gets a re-check date."""
    _, _, payload = await svc.record_synthesis(
        db_session, _extraction(source_type="JOURNAL"), uploaded_by="curator.a",
        raw_document="full text", license_class=licensing.OPEN_ACCESS,
    )
    assert payload.raw_payload == "full text"
    assert payload.expires_at is not None


async def test_an_unrecognised_licence_override_does_not_become_permissive(db_session):
    _, _, payload = await svc.record_synthesis(
        db_session, _extraction(), uploaded_by="curator.a",
        raw_document=PAYWALLED, license_class="TOTALLY_FINE_HONESTLY",
    )
    assert payload.license_class == licensing.RESTRICTED
    assert payload.raw_payload is None


# =====================================================================================
# What the stored row asserts
# =====================================================================================
async def test_a_fresh_upload_does_not_claim_to_have_passed_suitability(db_session):
    """PUBLISHED_RESULT_AVAILABLE means a paper passed a check nobody has run yet."""
    result, _, _ = await svc.record_synthesis(
        db_session, _extraction(), uploaded_by="curator.a",
    )
    assert result.status == statuses.MEDICAL_REVIEW_REQUIRED
    assert result.status != statuses.PUBLISHED_RESULT_AVAILABLE
    assert not statuses.is_releasable(result.status)


async def test_the_source_is_citable_but_our_extraction_is_not_approved(db_session):
    result, _, _ = await svc.record_synthesis(
        db_session, _extraction(), uploaded_by="curator.a",
    )
    assert result.source == PUBLISHED
    assert result.source_is_citable
    assert not result.claim_is_approved_for_external_use


async def test_the_upload_is_audited_with_what_retention_removed(db_session):
    """A reviewer seeing an absent payload must be able to tell licence from failure."""
    await svc.record_synthesis(
        db_session, _extraction(), uploaded_by="curator.a", raw_document=PAYWALLED,
    )
    entries = list((await db_session.execute(select(AuditLog))).scalars().all())
    assert len(entries) == 1
    context = json.loads(entries[0].context)
    assert entries[0].event == "PUBLISHED_SYNTHESIS_UPLOADED"
    assert context["license_class"] == licensing.RESTRICTED
    assert context["full_document_retained"] is False
    assert context["uploaded_by"] == "curator.a"


# =====================================================================================
# Rejection and deduplication
# =====================================================================================
async def test_an_extraction_with_no_estimates_is_rejected(db_session):
    with pytest.raises(svc.UploadRejected, match="league_table"):
        await svc.record_synthesis(
            db_session, _extraction(league_table=None), uploaded_by="curator.a",
        )
    assert (await db_session.execute(select(NMAResult))).scalars().first() is None


async def test_an_anonymous_upload_is_rejected(db_session):
    with pytest.raises(svc.UploadRejected, match="uploaded_by"):
        await svc.record_synthesis(db_session, _extraction(), uploaded_by="  ")


async def test_a_flagged_but_coherent_extraction_is_still_stored(db_session):
    """A missing study list is the Level-2 gate's problem, not a reason to lose the paper."""
    result, parsed, _ = await svc.record_synthesis(
        db_session, _extraction(included_studies=[]), uploaded_by="curator.a",
    )
    assert result.result_id
    assert not result.included_studies_recoverable
    assert "INCLUDED_STUDIES_NOT_RECOVERABLE" in parsed.flags


async def test_re_uploading_the_same_document_does_not_duplicate_it(db_session):
    """Manual upload has a submit button, so double submission is a real failure mode."""
    first, _, _ = await svc.record_synthesis(
        db_session, _extraction(), uploaded_by="curator.a", raw_document=PAYWALLED,
    )
    second, _, _ = await svc.record_synthesis(
        db_session, _extraction(), uploaded_by="curator.a", raw_document=PAYWALLED,
    )
    assert second.result_id == first.result_id
    rows = list((await db_session.execute(select(NMAResult))).scalars().all())
    assert len(rows) == 1
    payloads = list((await db_session.execute(select(SourcePayload))).scalars().all())
    assert len(payloads) == 1


# =====================================================================================
# The Level-2 answer, end to end
# =====================================================================================
async def test_a_stored_synthesis_answers_a_matching_question(db_session):
    await svc.record_synthesis(db_session, _extraction(), uploaded_by="curator.a")
    answer = await svc.assess_for_question(
        db_session, indication="Psoriatic Arthritis", treatment_a="Rinvoq",
        treatment_b="Humira", canonical_outcome_id="PSA_ACR50_W16",
        protocol_id=PROTOCOL, as_of=date(2026, 1, 1),
    )
    assert answer["suitable"]
    assert answer["status"] == statuses.PUBLISHED_RESULT_AVAILABLE
    assert answer["estimate"]["estimate"] == 1.40
    assert answer["citation"].startswith("Cochrane")


async def test_the_reasons_survive_the_round_trip_through_the_database(db_session):
    """A stored row must be judged by exactly the rules a fresh upload would be."""
    await svc.record_synthesis(
        db_session, _extraction(timepoint_week=24), uploaded_by="curator.a",
    )
    answer = await svc.assess_for_question(
        db_session, indication="Psoriatic Arthritis", treatment_a="Rinvoq",
        treatment_b="Humira", canonical_outcome_id="PSA_ACR50_W16",
        protocol_id=PROTOCOL, as_of=date(2026, 1, 1),
    )
    assert not answer["suitable"]
    assert answer["status"] == statuses.TIMEPOINT_MISMATCH
    assert "timepoint" in answer["failed_dimensions"]
    # The citation is still surfaced, so the paper stays findable.
    assert answer["citation"].startswith("Cochrane")


async def test_no_stored_synthesis_is_a_structured_answer_not_an_error(db_session):
    answer = await svc.assess_for_question(
        db_session, indication="Psoriatic Arthritis", treatment_a="Rinvoq",
        treatment_b="Humira", canonical_outcome_id="PSA_ACR50_W16",
    )
    assert not answer["suitable"]
    assert answer["candidates_considered"] == 0
    assert answer["status"] == statuses.PUBLISHED_SYNTHESIS_UNSUITABLE


async def test_a_synthesis_for_another_indication_is_not_considered(db_session):
    await svc.record_synthesis(
        db_session,
        _extraction(indication="Plaque Psoriasis", endpoint="PASI90",
                    canonical_outcome_id="PSO_PASI90_W16"),
        uploaded_by="curator.a",
    )
    answer = await svc.assess_for_question(
        db_session, indication="Psoriatic Arthritis", treatment_a="Rinvoq",
        treatment_b="Humira", canonical_outcome_id="PSA_ACR50_W16",
    )
    assert answer["candidates_considered"] == 0
