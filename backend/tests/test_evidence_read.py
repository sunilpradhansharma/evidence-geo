"""The evidence read surface (X2). No network in any test.

Until this router existed, ``/comparisons/resolve`` and ``/evidence-review/networks/{id}``
both required a ``network_id`` that **nothing exposed**. The first test here is therefore the
whole point of the phase: a network can be discovered.

The rest pin the properties that make a read surface safe to put in front of a reviewer:

* **a GET writes nothing** — not a network, not a membership, not an audit row
* **both topologies are disclosed** — the stored endpoint-level graph AND what the governing
  protocol's window leaves answerable, so the page cannot promise a node the resolver refuses
* **no protocol means no scope**, rather than an empty graph claiming nothing is answerable
* **mismatch flags survive the projection** — a surface that shows a clean number for a row
  flagged ``EVENTS_DERIVED_FROM_PERCENTAGE`` launders a caveat the extraction recorded
* **citability and external approval stay independent**
* **a malformed JSON column degrades, never 500s**
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api import evidence as evidence_api
from app.evidence import lifecycles
from app.models.audit_log import AuditLog
from app.models.clinical_study import BINARY, ClinicalStudy, OutcomeResult, StudyArm
from app.models.database import Base, get_db
from app.models.drug_fact import DrugFact
from app.models.evidence_network import EvidenceNetwork, NetworkMembership
from app.services import evidence_read_service as svc
from app.services import network_builder_service as builder

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
        drug_fact,
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


@pytest.fixture
async def api():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    from app.models import (  # noqa: F401
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

    async def _override():
        async with maker() as s:
            yield s

    app = FastAPI()
    app.include_router(evidence_api.router)
    app.dependency_overrides[get_db] = _override
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, maker
    await engine.dispose()


async def _study(
    db,
    study_id: str,
    arms: list[tuple[str, int, int]],
    *,
    outcome_id: str | None = "PSA_ACR50_W16",
    week: float = 16,
    flags: str | None = None,
    verification: str = lifecycles.VERIFIED,
) -> None:
    db.add(ClinicalStudy(
        study_id=study_id, indication=INDICATION, registry_id=f"NCT{study_id}",
        title=f"{study_id} trial", verification_status=verification,
    ))
    for index, (treatment, events, n) in enumerate(arms):
        arm_id = f"{study_id}:A{index}"
        db.add(StudyArm(
            arm_id=arm_id, study_id=study_id, treatment=treatment,
            is_placebo=treatment == "Placebo", sample_size=n,
            administration_route="ORAL" if treatment == "Rinvoq" else "SC",
        ))
        db.add(OutcomeResult(
            result_id=f"{study_id}:R{index}", study_id=study_id, arm_id=arm_id,
            canonical_outcome_id=outcome_id, endpoint="ACR50", timepoint_week=week,
            outcome_type=BINARY, events=events, sample_size=n, mismatch_flags=flags,
        ))
    await db.commit()


async def _psa_network_with_one_early_reader(db) -> None:
    """A star network where Rinvoq reports at week 12, outside PROTOCOL's [14, 18]."""
    await _study(db, "EARLY", [("Rinvoq", 45, 100), ("Placebo", 20, 100)], week=12)
    await _study(db, "S2", [("Humira", 35, 100), ("Placebo", 18, 100)], week=16)


# =====================================================================================
# Discovery — the gap this phase closes
# =====================================================================================
async def test_a_network_can_be_discovered_without_already_knowing_its_id(api):
    """The whole reason X2 exists.

    ``/comparisons/resolve`` and ``/evidence-review/networks/{id}`` both take a network_id,
    and before this router nothing returned one — networks are created by a CLI script, so
    the resolver was reachable only by someone who had read the script's output.
    """
    client, maker = api
    async with maker() as db:
        await _psa_network_with_one_early_reader(db)
        await builder.build_network(
            db, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
            protocol_id=PROTOCOL,
        )

    listed = (await client.get("/evidence/networks")).json()
    assert listed["total"] == 1
    row = listed["networks"][0]
    assert row["network_id"] == "NET-PSORIATIC-ARTHRITIS-PSA_ACR50_W16"
    assert row["indication"] == INDICATION
    assert row["protocol_id"] == PROTOCOL
    # Enough to choose a network: how big it is, and whether anyone has ratified it.
    assert row["node_count"] == 3
    assert row["ratification_status"] == lifecycles.DRAFT
    # Memberships are PROPOSED — the builder proposes, a human ratifies.
    assert row["membership_counts"] == {lifecycles.PROPOSED: 2}

    # And the id it returns is the one the resolver accepts.
    detail = await client.get(f"/evidence/networks/{row['network_id']}")
    assert detail.status_code == 200


async def test_networks_filter_by_indication_and_ratification_status(api):
    client, maker = api
    async with maker() as db:
        db.add(EvidenceNetwork(
            network_id="N-RA", indication="Rheumatoid Arthritis",
            canonical_outcome_id="RA_ACR50_W12", ratification_status=lifecycles.DRAFT,
        ))
        db.add(EvidenceNetwork(
            network_id="N-PSA", indication=INDICATION,
            canonical_outcome_id="PSA_ACR50_W16",
            ratification_status=lifecycles.RATIFIED,
        ))
        await db.commit()

    assert (await client.get(
        "/evidence/networks", params={"indication": INDICATION}
    )).json()["total"] == 1
    ratified = (await client.get(
        "/evidence/networks", params={"ratification_status": lifecycles.RATIFIED}
    )).json()
    assert [n["network_id"] for n in ratified["networks"]] == ["N-PSA"]


# =====================================================================================
# Both topologies, and the refusal to invent a scope
# =====================================================================================
async def test_network_detail_discloses_what_the_protocol_window_narrows(api):
    """The stored graph is endpoint-level; a protocol-scoped resolve can see fewer nodes.

    Showing only the stored graph is how a surface promises a comparison the resolver then
    refuses — the exact defect the ProtocolScope disclosure was added to reveal.
    """
    client, maker = api
    async with maker() as db:
        await _psa_network_with_one_early_reader(db)
        await builder.build_network(
            db, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
            protocol_id=PROTOCOL,
        )

    body = (await client.get(
        "/evidence/networks/NET-PSORIATIC-ARTHRITIS-PSA_ACR50_W16"
    )).json()

    # Stored, endpoint-level: Rinvoq is a node and the graph is connected.
    assert "Rinvoq" in body["endpoint_topology"]["nodes"]
    assert body["is_connected"] is True
    # Scoped: the week-12 reader falls outside [14, 18], so Rinvoq is not answerable.
    scope = body["protocol_scope"]
    assert scope["protocol_id"] == PROTOCOL
    assert scope["approved_time_window"] == [14.0, 18.0]
    assert scope["nodes_lost_to_window"] == ["Rinvoq"]
    assert scope["studies_out_of_window"] == ["EARLY"]
    assert scope["narrows_the_network"] is True
    assert scope["topology"]["node_count"] == 2
    assert body["overstates_answerable"] is True
    # Route is carried per node so a route-mixed network is visible at a glance.
    assert body["endpoint_topology"]["administration_routes"]["Rinvoq"] == "ORAL"


async def test_a_network_with_no_protocol_reports_no_scope_rather_than_an_empty_one(api):
    """With no approved window there is nothing to narrow.

    An empty scoped graph would say nothing is answerable, which is a different and worse
    claim than "no protocol governs this network yet".
    """
    client, maker = api
    async with maker() as db:
        await _psa_network_with_one_early_reader(db)
        await builder.build_network(
            db, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
        )

    body = (await client.get(
        "/evidence/networks/NET-PSORIATIC-ARTHRITIS-PSA_ACR50_W16"
    )).json()
    assert body["protocol_id"] is None
    assert body["protocol_scope"] is None
    assert body["overstates_answerable"] is False
    assert "Rinvoq" in body["endpoint_topology"]["nodes"]


async def test_reading_a_network_writes_nothing(db_session):
    """A read surface must not be able to change the evidence set or the audit trail.

    ``build_network`` mutates the session, refuses a RATIFIED network and writes an audit
    row, so routing a GET through it would make browsing a governance event.
    """
    await _psa_network_with_one_early_reader(db_session)
    await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
        protocol_id=PROTOCOL,
    )

    async def _counts() -> tuple[int, int, int]:
        return (
            (await db_session.execute(select(func.count()).select_from(AuditLog))).scalar_one(),
            (await db_session.execute(
                select(func.count()).select_from(EvidenceNetwork)
            )).scalar_one(),
            (await db_session.execute(
                select(func.count()).select_from(NetworkMembership)
            )).scalar_one(),
        )

    before = await _counts()
    for _ in range(3):
        body = await svc.get_network(
            db_session, "NET-PSORIATIC-ARTHRITIS-PSA_ACR50_W16"
        )
        assert body["overstates_answerable"] is True
    assert await _counts() == before


async def test_a_ratified_network_is_readable(db_session):
    """``build_network`` raises on a RATIFIED network. Reading one must still work.

    A ratified network is precisely the one people most need to look at, so the read path
    deliberately does not go through the builder's rebuild guard.
    """
    await _psa_network_with_one_early_reader(db_session)
    await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
        protocol_id=PROTOCOL,
    )
    network = (await db_session.execute(
        select(EvidenceNetwork).where(
            EvidenceNetwork.network_id == "NET-PSORIATIC-ARTHRITIS-PSA_ACR50_W16"
        )
    )).scalar_one()
    network.ratification_status = lifecycles.RATIFIED
    await db_session.commit()

    body = await svc.get_network(db_session, network.network_id)
    assert body["ratification_status"] == lifecycles.RATIFIED
    assert body["protocol_scope"]["nodes_lost_to_window"] == ["Rinvoq"]


async def test_membership_rows_carry_their_study_identity_and_exclusion_reason(db_session):
    """"Why was my trial not used?" must be answerable without a second request."""
    await _psa_network_with_one_early_reader(db_session)
    await builder.build_network(
        db_session, indication=INDICATION, canonical_outcome_id="PSA_ACR50_W16",
        protocol_id=PROTOCOL,
    )
    membership = (await db_session.execute(
        select(NetworkMembership).where(NetworkMembership.study_id == "EARLY")
    )).scalar_one()
    membership.membership_status = lifecycles.EXCLUDED
    membership.exclusion_reason = "reports the endpoint outside the approved window"
    await db_session.commit()

    body = await svc.get_network(db_session, "NET-PSORIATIC-ARTHRITIS-PSA_ACR50_W16")
    early = next(m for m in body["memberships"] if m["study_id"] == "EARLY")
    assert early["membership_status"] == lifecycles.EXCLUDED
    assert "outside the approved window" in early["exclusion_reason"]
    assert early["registry_id"] == "NCTEARLY"
    assert early["verification_status"] == lifecycles.VERIFIED
    assert body["membership_counts"] == {
        lifecycles.EXCLUDED: 1, lifecycles.PROPOSED: 1
    }


async def test_an_unknown_network_is_a_404(api):
    client, _ = api
    assert (await client.get("/evidence/networks/NOPE")).status_code == 404


# =====================================================================================
# Studies
# =====================================================================================
async def test_studies_are_filterable_by_the_treatment_on_an_arm(api):
    """"Which trials have a Rinvoq arm?" is the first question a curator asks."""
    client, maker = api
    async with maker() as db:
        await _psa_network_with_one_early_reader(db)

    body = (await client.get(
        "/evidence/studies", params={"treatment": "Rinvoq"}
    )).json()
    assert body["total"] == 1
    assert body["studies"][0]["study_id"] == "EARLY"
    assert body["studies"][0]["treatments"] == ["Placebo", "Rinvoq"]


async def test_a_study_row_reports_how_many_rows_a_network_could_use(api):
    """Canonical endpoint coverage per study, because an unmapped row is invisible.

    A study can post dozens of results and contribute nothing: without a
    ``canonical_outcome_id`` no network can see the row.
    """
    client, maker = api
    async with maker() as db:
        await _study(db, "MAPPED", [("Rinvoq", 40, 100), ("Placebo", 20, 100)])
        await _study(
            db, "UNMAPPED", [("Humira", 30, 100), ("Placebo", 15, 100)], outcome_id=None
        )

    rows = {s["study_id"]: s for s in (await client.get("/evidence/studies")).json()["studies"]}
    assert rows["MAPPED"]["outcome_count"] == 2
    assert rows["MAPPED"]["canonical_outcome_count"] == 2
    assert rows["MAPPED"]["canonical_outcome_ids"] == ["PSA_ACR50_W16"]
    # Ingested, and unusable by any network. The count says so rather than implying depth.
    assert rows["UNMAPPED"]["outcome_count"] == 2
    assert rows["UNMAPPED"]["canonical_outcome_count"] == 0
    assert rows["UNMAPPED"]["canonical_outcome_ids"] == []


async def test_study_detail_keeps_the_mismatch_flags_on_every_row(api):
    """A clean-looking number for a flagged row launders a caveat extraction recorded."""
    client, maker = api
    async with maker() as db:
        await _study(
            db, "DERIVED", [("Rinvoq", 45, 100), ("Placebo", 20, 100)],
            flags='["EVENTS_DERIVED_FROM_PERCENTAGE"]',
        )

    body = (await client.get("/evidence/studies/DERIVED")).json()
    assert len(body["outcomes"]) == 2
    assert all(
        row["mismatch_flags"] == ["EVENTS_DERIVED_FROM_PERCENTAGE"]
        for row in body["outcomes"]
    )
    # The arm each row belongs to is named, so a reader is not left joining ids by hand.
    assert {row["arm_treatment"] for row in body["outcomes"]} == {"Rinvoq", "Placebo"}
    assert {a["treatment"] for a in body["arms"]} == {"Rinvoq", "Placebo"}


async def test_a_malformed_flags_column_degrades_instead_of_failing_the_page(api):
    """One row written by an older parser must not take out the whole study view."""
    client, maker = api
    async with maker() as db:
        await _study(
            db, "BADJSON", [("Rinvoq", 45, 100), ("Placebo", 20, 100)],
            flags="not json at all",
        )

    response = await client.get("/evidence/studies/BADJSON")
    assert response.status_code == 200
    assert all(row["mismatch_flags"] == [] for row in response.json()["outcomes"])


async def test_an_unknown_study_is_a_404(api):
    client, _ = api
    assert (await client.get("/evidence/studies/NOPE")).status_code == 404


# =====================================================================================
# Drug facts
# =====================================================================================
async def _fact(db, fact_id: str, *, version: int, superseded_by: str | None) -> None:
    db.add(DrugFact(
        fact_id=fact_id, brand="Rinvoq", generic="upadacitinib",
        drug_class="JAK inhibitor", administration_route="ORAL",
        approved_indications='["Psoriatic Arthritis"]',
        boxed_warnings='["Serious infections"]', has_boxed_warning=True,
        verification_status=lifecycles.EXTRACTED,
        version=version, superseded_by=superseded_by,
    ))
    await db.commit()


async def test_a_drug_fact_reports_citability_and_external_approval_separately(api):
    """Two independent properties. Collapsing them is how unreviewed wording ships.

    A published label is citable the moment it exists; our extraction of it is not approved
    for external use until MLR says so.
    """
    client, maker = api
    async with maker() as db:
        await _fact(db, "DF-1", version=1, superseded_by=None)

    row = (await client.get("/evidence/drug-facts")).json()["drug_facts"][0]
    assert row["source_is_citable"] is True
    assert row["claim_is_approved_for_external_use"] is False
    # And a structured field arrives structured, not as a JSON string.
    assert row["boxed_warnings"] == ["Serious infections"]
    assert row["approved_indications"] == ["Psoriatic Arthritis"]
    assert row["verification_status"] == lifecycles.EXTRACTED


async def test_superseded_label_versions_are_history_not_deletions(api):
    """"What did the label say when we scored that response?" has an auditable answer."""
    client, maker = api
    async with maker() as db:
        await _fact(db, "DF-OLD", version=1, superseded_by="DF-NEW")
        await _fact(db, "DF-NEW", version=2, superseded_by=None)

    # The list defaults to current versions only.
    current = (await client.get("/evidence/drug-facts")).json()
    assert [f["fact_id"] for f in current["drug_facts"]] == ["DF-NEW"]
    assert (await client.get(
        "/evidence/drug-facts", params={"current_only": "false"}
    )).json()["total"] == 2

    detail = (await client.get("/evidence/drug-facts/rinvoq")).json()
    assert detail["current"]["fact_id"] == "DF-NEW"
    assert [f["fact_id"] for f in detail["history"]] == ["DF-OLD"]


async def test_an_unknown_brand_is_a_404(api):
    client, _ = api
    assert (await client.get("/evidence/drug-facts/Nonesuch")).status_code == 404


# =====================================================================================
# Overview
# =====================================================================================
async def test_the_overview_leads_with_canonical_endpoint_coverage(api):
    """A landing page reporting only rows ingested would be true and deeply misleading.

    An outcome row with no canonical id is invisible to every network, so the ratio is the
    difference between a store that looks full and one that can answer something.
    """
    client, maker = api
    async with maker() as db:
        await _study(db, "MAPPED", [("Rinvoq", 40, 100), ("Placebo", 20, 100)])
        await _study(
            db, "UNMAPPED", [("Humira", 30, 100), ("Placebo", 15, 100)], outcome_id=None
        )
        await _fact(db, "DF-1", version=1, superseded_by=None)

    body = (await client.get("/evidence/overview")).json()
    assert body["studies"]["total"] == 2
    assert body["studies"]["by_verification_status"] == {lifecycles.VERIFIED: 2}
    assert body["studies"]["by_indication"] == {INDICATION: 2}
    assert body["outcome_results"]["total"] == 4
    assert body["outcome_results"]["with_canonical_outcome"] == 2
    assert body["outcome_results"]["canonical_coverage_pct"] == 50.0
    assert body["drug_facts"]["total"] == 1


async def test_an_empty_store_reports_zero_coverage_without_dividing_by_zero(api):
    client, _ = api
    body = (await client.get("/evidence/overview")).json()
    assert body["outcome_results"]["total"] == 0
    assert body["outcome_results"]["canonical_coverage_pct"] == 0.0
    assert body["networks"]["total"] == 0
