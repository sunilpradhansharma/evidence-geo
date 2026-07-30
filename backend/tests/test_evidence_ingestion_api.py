"""The evidence ingestion API. No network in any test.

The three routines behind this router already existed as CLI scripts, so what is new — and
what these tests pin — is everything the *surface* adds:

* **the form's vocabulary comes from config**, so adding an indication to YAML widens the UI
  rather than diverging from it
* **a bad form fails at submit** (422) rather than as a job that dies quietly in the
  background, and the message names the known set exactly as the script's exit-2 does
* **one job at a time**, across all three kinds — a re-parse racing an ingest fights over the
  same rows, and the claim happens in the REQUEST because a ``BackgroundTasks`` callable runs
  after the response
* **a preview writes nothing, including no audit row** — a run that promises to write nothing
  must not write a row to say so
* **nothing here can verify** — the request models forbid unknown fields, so ``verified_by``
  is a 422 rather than an ignored key, and no route reaches ``verify_study``. The CLI's
  ``--verify-as`` is deliberately not ported
* **a refusal is prose, not a 500** — rebuilding a RATIFIED network is a governance refusal
* **progress agrees with the report**, because a counter that disagrees with the total is
  worse than no counter

``ctg.search`` / ``ctg.fetch`` / ``fda.fetch_label`` are monkeypatched throughout: the 3B rule
is that no test touches the wire.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api import evidence_ingestion as api
from app.evidence import lifecycles
from app.evidence.sources import clinicaltrials as ctg
from app.evidence.sources import openfda_facts as fda
from app.evidence.sources.base import FetchResult
from app.geo.sources.openfda import LabelSeed
from app.models.audit_log import AuditLog
from app.models.clinical_study import ClinicalStudy
from app.models.database import Base
from app.models.drug_fact import DrugFact
from app.models.evidence_network import EvidenceNetwork, NetworkMembership

FIXTURES = Path(__file__).parent / "fixtures"
INDICATION = "Psoriatic Arthritis"
# The committed fixture yields PSA_ACR20_W16, not ACR50 — see the fixture's own note in
# tests/test_evidence_ingestion.py. Both are canonical for PsA, so both are valid input here.
OUTCOME = "PSA_ACR20_W16"
PROTOCOL = "PSA_ACR50_W16_PRIMARY"


def _payload() -> dict:
    return json.loads((FIXTURES / "ctg_select_psa_1.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def reset_job():
    """The job slot is a module global, so a leaked ``running`` would fail the next test."""
    before = dict(api._JOB)
    api._JOB.update(
        running=False, kind=None, mode=None, scope=None, started_at=None,
        finished_at=None, progress=None, report=None, error=None,
    )
    yield
    api._JOB.clear()
    api._JOB.update(before)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Serve the committed registry fixture and a synthetic label for both wire boundaries."""
    payload = _payload()

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

    async def _label(brand, _generic=None):
        return LabelSeed(
            set_id=f"set-{brand.lower()}",
            manufacturer="AbbVie Inc.",
            effective_time="20250101",
            prescribing_information=f"https://example.test/{brand.lower()}.pdf",
        )

    monkeypatch.setattr(ctg, "search", _search)
    monkeypatch.setattr(ctg, "fetch", _fetch)
    monkeypatch.setattr(fda, "fetch_label", _label)


@pytest.fixture
async def client(monkeypatch):
    """The router plus a shared in-memory DB.

    The background tasks open their OWN ``AsyncSessionLocal`` — a request-scoped session is
    closed by the time a ``BackgroundTasks`` callable runs — so the factory is monkeypatched
    rather than ``get_db`` overridden. ``StaticPool`` keeps the task's session and the
    assertions' session on one connection.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    from app.models import (  # noqa: F401 — register tables on Base.metadata
        analysis_protocol,
        audit_log,
        clinical_study,
        drug_fact,
        evidence_network,
        nma_result,
        source_payload,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(api, "AsyncSessionLocal", maker)

    app = FastAPI()
    app.include_router(api.router)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http, maker
    await engine.dispose()


async def _counts(maker) -> dict[str, int]:
    async with maker() as db:
        return {
            "studies": (await db.execute(
                select(func.count()).select_from(ClinicalStudy)
            )).scalar_one(),
            "networks": (await db.execute(
                select(func.count()).select_from(EvidenceNetwork)
            )).scalar_one(),
            "memberships": (await db.execute(
                select(func.count()).select_from(NetworkMembership)
            )).scalar_one(),
            "drug_facts": (await db.execute(
                select(func.count()).select_from(DrugFact)
            )).scalar_one(),
            "audit": (await db.execute(
                select(func.count()).select_from(AuditLog)
            )).scalar_one(),
        }


# =====================================================================================
# Options — the form's vocabulary
# =====================================================================================
async def test_options_are_derived_from_config_not_hardcoded(client):
    """A form that hardcodes its own list drifts from the validator that rejects it.

    Both come from the taxonomy here, so adding an indication to brands.yaml widens the UI
    and the validator together.
    """
    http, _ = client
    from app.config import outcomes, taxonomy
    from app.evidence import protocols

    body = (await http.get("/evidence-ingestion/options")).json()
    assert body["indications"] == list(taxonomy.diseases())
    assert INDICATION in body["indications"]
    assert body["outcomes_by_indication"][INDICATION] == list(
        taxonomy.canonical_outcomes_for_disease(INDICATION)
    )
    assert body["protocols"] == list(protocols.protocol_ids())
    assert body["treatment_phases"] == list(outcomes.TREATMENT_PHASES)
    assert body["full_depth_drugs"] == list(taxonomy.full_depth_drugs())
    # Stated in the payload, because it is the property most likely to be assumed away.
    assert "cannot verify" in body["verification"]


# =====================================================================================
# Validation happens at submit, not in a job that dies quietly
# =====================================================================================
async def test_an_unknown_indication_is_rejected_before_any_job_starts(client):
    http, maker = client
    response = await http.post(
        "/evidence-ingestion/trials", json={"indication": "Space Madness"}
    )
    assert response.status_code == 422
    # And it names the known set, so the message is actionable rather than a refusal.
    assert INDICATION in response.json()["detail"]
    assert api._JOB["running"] is False
    assert (await _counts(maker))["studies"] == 0


async def test_an_outcome_from_another_indication_is_rejected_naming_the_known_set(client):
    """Exactly what the script's exit-2 does. A silent default would build the wrong network."""
    http, _ = client
    response = await http.post(
        "/evidence-ingestion/trials",
        json={"indication": INDICATION, "outcome": "UC_REMISSION_INDUCTION_W8"},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "UC_REMISSION_INDUCTION_W8" in detail
    assert OUTCOME in detail


async def test_an_undefined_protocol_is_rejected_at_submit(client):
    """``build_network`` raises for this too, but only after minutes of harvesting."""
    http, _ = client
    response = await http.post(
        "/evidence-ingestion/trials",
        json={"indication": INDICATION, "outcome": OUTCOME, "protocol": "NOPE"},
    )
    assert response.status_code == 422
    assert "not defined in analysis_protocols.yaml" in response.json()["detail"]


async def test_an_unrecognised_treatment_phase_is_rejected(client):
    """Induction and maintenance populations are never poolable, so this cannot default."""
    http, _ = client
    response = await http.post(
        "/evidence-ingestion/trials",
        json={"indication": INDICATION, "outcome": OUTCOME, "phase": "WHENEVER"},
    )
    assert response.status_code == 422
    assert "INDUCTION" in response.json()["detail"]


# =====================================================================================
# No route can verify. This is the guardrail, not a nice-to-have.
# =====================================================================================
@pytest.mark.parametrize(
    "path,body",
    [
        ("/evidence-ingestion/trials", {"indication": INDICATION, "verified_by": "Dr X"}),
        ("/evidence-ingestion/drug-facts", {"brands": ["Rinvoq"], "verify_as": "Dr X"}),
        ("/evidence-ingestion/reparse", {"verified_by": "Dr X"}),
    ],
)
async def test_no_request_can_smuggle_a_verifier(client, path, body):
    """``extra="forbid"`` is what makes verification *unreachable* rather than merely unused.

    Bulk-stamping one name across studies nobody opened manufactures an audit trail that
    looks real, which is why the CLI's ``--verify-as`` is not ported. Silently ignoring the
    field would leave a caller believing it had been honoured.
    """
    http, _ = client
    assert (await http.post(path, json=body)).status_code == 422


async def test_a_committed_ingest_never_reaches_verified(client):
    """Ingestion lands EXTRACTED or MAPPED. The resolver's refusal depends on it."""
    http, maker = client
    await http.post(
        "/evidence-ingestion/trials",
        json={"indication": INDICATION, "outcome": OUTCOME, "commit": True},
    )
    async with maker() as db:
        statuses = set((await db.execute(
            select(ClinicalStudy.verification_status)
        )).scalars().all())
    assert statuses
    assert lifecycles.VERIFIED not in statuses
    assert statuses <= {lifecycles.EXTRACTED, lifecycles.MAPPED}


# =====================================================================================
# Preview writes nothing — including the audit table
# =====================================================================================
async def test_a_preview_writes_no_rows_and_no_audit_entry(client):
    """A run that promises to write nothing must not write a row to say so.

    An audit entry for a preview would also make the log unable to answer "what changed the
    corpus?" without reading a mode field on every row.
    """
    http, maker = client
    before = await _counts(maker)

    response = await http.post(
        "/evidence-ingestion/trials",
        json={"indication": INDICATION, "outcome": OUTCOME, "limit": 1},
    )
    assert response.status_code == 202
    assert response.json()["mode"] == "PREVIEW"

    # The report is fully populated: a preview whose value is a spinner is not a preview.
    status = (await http.get("/evidence-ingestion/status")).json()
    assert status["running"] is False
    assert status["error"] is None
    assert status["report"]["committed"] is False
    assert status["report"]["ingestion"]["ingested"] == 1
    assert status["report"]["network"]["network_id"]

    assert await _counts(maker) == before


async def test_a_preview_reparse_writes_nothing(client):
    http, maker = client
    await http.post(
        "/evidence-ingestion/trials", json={"indication": INDICATION, "commit": True}
    )
    async with maker() as db:
        study = (await db.execute(select(ClinicalStudy))).scalars().first()
        original_payload_id = study.source_payload_id
    before = await _counts(maker)

    response = await http.post(
        "/evidence-ingestion/reparse", json={"indication": INDICATION}
    )
    assert response.status_code == 202
    report = (await http.get("/evidence-ingestion/status")).json()["report"]
    assert report["committed"] is False
    assert report["reparse"]["studies"] == 1

    after = await _counts(maker)
    assert after == before
    async with maker() as db:
        study = (await db.execute(select(ClinicalStudy))).scalars().first()
        assert study.source_payload_id == original_payload_id


async def test_a_preview_drug_facts_run_writes_nothing(client):
    http, maker = client
    response = await http.post(
        "/evidence-ingestion/drug-facts", json={"brands": ["Rinvoq", "Skyrizi"]}
    )
    assert response.status_code == 202
    report = (await http.get("/evidence-ingestion/status")).json()["report"]
    assert report["committed"] is False
    assert report["drug_facts"]["requested"] == 2

    counts = await _counts(maker)
    assert counts["drug_facts"] == 0
    assert counts["audit"] == 0


# =====================================================================================
# Commit writes — PROPOSED and DRAFT, never decided
# =====================================================================================
async def test_a_commit_writes_studies_proposed_memberships_and_a_draft_network(client):
    """The builder proposes; a human ratifies. Both halves are asserted.

    A surface that could write INCLUDED or RATIFIED would be inventing the review it exists
    to prepare for.
    """
    http, maker = client
    response = await http.post(
        "/evidence-ingestion/trials",
        json={
            "indication": INDICATION, "outcome": OUTCOME, "protocol": PROTOCOL,
            "commit": True,
        },
    )
    assert response.status_code == 202
    assert response.json()["mode"] == "COMMIT"

    status = (await http.get("/evidence-ingestion/status")).json()
    assert status["error"] is None
    assert status["report"]["committed"] is True

    counts = await _counts(maker)
    assert counts["studies"] == 1
    assert counts["networks"] == 1
    assert counts["memberships"] >= 1
    # A commit DOES audit: one OPERATOR row for the request plus the service's own entries.
    assert counts["audit"] >= 1

    async with maker() as db:
        network = (await db.execute(select(EvidenceNetwork))).scalars().one()
        assert network.ratification_status == lifecycles.DRAFT
        statuses = set((await db.execute(
            select(NetworkMembership.membership_status)
        )).scalars().all())
    assert statuses == {lifecycles.PROPOSED}

    async with maker() as db:
        events = set((await db.execute(select(AuditLog.event))).scalars().all())
    assert "EVIDENCE_INGESTION_REQUESTED" in events


async def test_a_commit_writes_drug_facts_unverified(client):
    http, maker = client
    await http.post(
        "/evidence-ingestion/drug-facts", json={"brands": ["Rinvoq"], "commit": True}
    )
    async with maker() as db:
        fact = (await db.execute(select(DrugFact))).scalars().one()
    assert fact.brand == "Rinvoq"
    assert fact.verification_status in (lifecycles.EXTRACTED, lifecycles.MAPPED)


async def test_ingesting_without_an_outcome_builds_no_network(client):
    """"Ingest only" is a real mode: the corpus can grow before anyone scopes a question."""
    http, maker = client
    await http.post(
        "/evidence-ingestion/trials", json={"indication": INDICATION, "commit": True}
    )
    counts = await _counts(maker)
    assert counts["studies"] == 1
    assert counts["networks"] == 0
    assert (await http.get("/evidence-ingestion/status")).json()["report"]["network"] is None


# =====================================================================================
# One job at a time
# =====================================================================================
async def test_a_second_job_is_refused_while_one_runs(client):
    """Across all three kinds, not per kind: a re-parse racing an ingest shares the rows."""
    http, _ = client
    api._JOB.update(running=True, kind="trials", mode="COMMIT")

    for path, body in (
        ("/evidence-ingestion/trials", {"indication": INDICATION}),
        ("/evidence-ingestion/drug-facts", {"brands": ["Rinvoq"]}),
        ("/evidence-ingestion/reparse", {}),
    ):
        response = await http.post(path, json=body)
        assert response.status_code == 409
        assert "already running" in response.json()["detail"]


async def test_the_slot_is_claimed_in_the_request_not_in_the_task(client):
    """A ``BackgroundTasks`` callable runs after the response is sent.

    A guard that only flipped the flag inside the task would let two submissions moments
    apart both pass, which is the race the guard exists to prevent. Asserted through a
    rejected form: it must NOT leave the slot occupied either.
    """
    http, _ = client
    assert (await http.post(
        "/evidence-ingestion/trials", json={"indication": "Space Madness"}
    )).status_code == 422
    assert api._JOB["running"] is False
    # ... and a valid one now succeeds, so the failed submit released nothing it never took.
    assert (await http.post(
        "/evidence-ingestion/trials", json={"indication": INDICATION, "limit": 1}
    )).status_code == 202


# =====================================================================================
# A refusal is prose, not a 500
# =====================================================================================
async def test_a_ratified_network_surfaces_the_refusal_as_error_prose(client):
    """Rebuilding a RATIFIED network would change the evidence set a reviewer approved.

    That is a governance refusal with a reason attached, so it belongs in ``error`` where the
    UI can render it — a 500 would present a correct decision as a fault.
    """
    http, maker = client
    await http.post(
        "/evidence-ingestion/trials",
        json={"indication": INDICATION, "outcome": OUTCOME, "commit": True},
    )
    async with maker() as db:
        network = (await db.execute(select(EvidenceNetwork))).scalars().one()
        network.ratification_status = lifecycles.RATIFIED
        await db.commit()

    response = await http.post(
        "/evidence-ingestion/trials",
        json={"indication": INDICATION, "outcome": OUTCOME, "commit": True},
    )
    assert response.status_code == 202  # accepted; the refusal is discovered in the job
    status = (await http.get("/evidence-ingestion/status")).json()
    assert status["running"] is False
    assert "RATIFIED" in status["error"]
    # The remedy the prose names has to be one that exists. This pinned "Supersede it" while
    # no supersede route, service function or button did — the refusal was rendered to the
    # operator pointing at nothing. It now points at POST /evidence-review/networks/{id}/reopen.
    assert "Reopen it to DRAFT" in status["error"]
    assert status["report"] is None


# =====================================================================================
# Progress
# =====================================================================================
async def test_progress_reaches_done_and_agrees_with_the_report(client):
    """A counter that disagrees with the total it is counting toward is worse than none."""
    http, _ = client
    await http.post(
        "/evidence-ingestion/trials",
        json={"indication": INDICATION, "outcome": OUTCOME, "limit": 1},
    )
    status = (await http.get("/evidence-ingestion/status")).json()
    progress, report = status["progress"], status["report"]

    assert progress["phase"] == "done"
    assert progress["studies_done"] == progress["studies_total"] == 1
    assert progress["ingested"] == report["ingestion"]["ingested"]
    assert progress["screened_out"] == report["ingestion"]["screened_out"]
    assert progress["node_count"] == report["network"]["endpoint_topology"]["node_count"]


async def test_status_echoes_the_scope_that_was_asked_for(client):
    """"What did this run actually cover?" must be answerable from the status alone."""
    http, _ = client
    await http.post(
        "/evidence-ingestion/trials",
        json={"indication": INDICATION, "drugs": ["Rinvoq"], "limit": 1},
    )
    status = (await http.get("/evidence-ingestion/status")).json()
    assert status["kind"] == "trials"
    assert status["mode"] == "PREVIEW"
    assert status["scope"]["indication"] == INDICATION
    assert status["scope"]["drugs"] == ["Rinvoq"]
    assert status["scope"]["limit"] == 1


async def test_reparse_progress_counts_the_studies_it_re_read(client):
    http, _ = client
    await http.post(
        "/evidence-ingestion/trials", json={"indication": INDICATION, "commit": True}
    )
    await http.post("/evidence-ingestion/reparse", json={"commit": True})

    status = (await http.get("/evidence-ingestion/status")).json()
    assert status["progress"]["phase"] == "done"
    assert status["progress"]["studies_done"] == status["progress"]["studies_total"] == 1
    assert status["report"]["reparse"]["by_action"] == {"UPDATED": 1}


# =====================================================================================
# The settings flag is the only gate, so it has to actually gate
# =====================================================================================
async def test_every_route_is_403_when_the_surface_is_disabled(client, monkeypatch):
    """There is no RBAC in this tree, so this flag is the whole access control."""
    http, _ = client
    settings = api.get_settings()
    monkeypatch.setattr(
        api, "get_settings",
        lambda: type(settings)(**{
            **settings.model_dump(), "evidence_ingestion_api_enabled": False
        }),
    )

    assert (await http.get("/evidence-ingestion/options")).status_code == 403
    assert (await http.get("/evidence-ingestion/status")).status_code == 403
    assert (await http.post(
        "/evidence-ingestion/trials", json={"indication": INDICATION}
    )).status_code == 403
    assert (await http.post(
        "/evidence-ingestion/drug-facts", json={"brands": ["Rinvoq"]}
    )).status_code == 403
    assert (await http.post("/evidence-ingestion/reparse", json={})).status_code == 403
