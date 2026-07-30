"""X1 — the medical + statistical review surface.

Every test maps to a named acceptance criterion in the plan. The five that carry the
governance model, because each guards a rule that looks fine until it has already been
violated:

* ``content_hash`` is DERIVED, never accepted as input
* recording an approval does NOT change the hash
* editing a definition INVALIDATES prior approvals
* medical and statistical approvals are independently grantable and revocable
* a network cannot reach RATIFIED without both review transitions, in order
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api import evidence_review as review_api
from app.evidence import approvals, lifecycles as lc
from app.evidence import protocols, statuses
from app.models.analysis_protocol import AnalysisProtocolApproval
from app.models.audit_log import AuditLog
from app.models.database import Base, get_db
from app.models.evidence_network import EvidenceNetwork
from app.services import evidence_review_service as review

PROTOCOL = "PSA_ACR50_W16_PRIMARY"


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from app.models import (  # noqa: F401 — register the tables on Base.metadata
        analysis_protocol,
        audit_log,
        clinical_study,
        evidence_network,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.fixture
async def network(db_session):
    row = EvidenceNetwork(
        network_id="NET-PSA-ACR50",
        indication="Psoriatic Arthritis",
        canonical_outcome_id="PSA_ACR50_W16",
        treatment_phase="PRIMARY",
        protocol_id=PROTOCOL,
    )
    db_session.add(row)
    await db_session.commit()
    return row


@pytest.fixture
async def api():
    """The review router over a shared in-memory DB, for the transitions that have routes."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    from app.models import (  # noqa: F401 — register the tables on Base.metadata
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
    app.include_router(review_api.router)
    app.dependency_overrides[get_db] = _override
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, maker
    await engine.dispose()


def _row(role, decision, content_hash, *, revoked=False, minutes=0):
    """An approval row without touching the database — the pure rules are pure."""
    return AnalysisProtocolApproval(
        approval_id=f"APR-{role}-{decision}-{minutes}",
        protocol_id=PROTOCOL,
        content_hash=content_hash,
        approval_role=role,
        decision=decision,
        reviewer_id="someone",
        reviewed_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes),
        revoked_at=datetime(2026, 6, 1, tzinfo=timezone.utc) if revoked else None,
    )


# =====================================================================================
# The definition file cannot author its own approval
# =====================================================================================
def test_every_shipped_protocol_is_structurally_valid():
    assert protocols.validate() == []


def test_a_protocol_that_authors_approval_state_is_refused(monkeypatch):
    """The one mistake that would quietly dismantle the whole model."""
    for forbidden in ("content_hash", "approved", "approval_status", "reviewed_at"):
        monkeypatch.setattr(
            protocols, "_config",
            lambda f=forbidden: {"protocols": {"X": {"version": 1, f: "anything"}}},
        )
        protocols.protocols.cache_clear()
        with pytest.raises(protocols.ProtocolError, match=forbidden):
            protocols.protocols()
    protocols.protocols.cache_clear()


def test_an_approved_window_may_narrow_the_outcome_window_but_never_widen_it(monkeypatch):
    definition = dict(protocols.protocol(PROTOCOL))
    definition["approved_time_window"] = {"min_week": 2, "max_week": 99}
    monkeypatch.setattr(protocols, "_config", lambda: {"protocols": {PROTOCOL: definition}})
    protocols.protocols.cache_clear()

    errors = protocols.validate()
    assert any("wider than outcome" in e for e in errors)
    protocols.protocols.cache_clear()


# =====================================================================================
# content_hash is derived
# =====================================================================================
def test_the_hash_is_derived_from_content_and_is_stable():
    first = protocols.content_hash(PROTOCOL)
    assert first and first.startswith("sha256:")
    assert first == protocols.content_hash(PROTOCOL)


def test_an_undefined_protocol_has_no_hash():
    assert protocols.content_hash("NOT_A_PROTOCOL") is None


def test_changing_methodology_changes_the_hash(monkeypatch):
    before = protocols.content_hash(PROTOCOL)
    edited = dict(protocols.protocol(PROTOCOL))
    edited["heterogeneity_rule"] = "FIXED_EFFECTS_ALWAYS"
    monkeypatch.setattr(protocols, "_config", lambda: {"protocols": {PROTOCOL: edited}})
    protocols.protocols.cache_clear()

    assert protocols.content_hash(PROTOCOL) != before
    protocols.protocols.cache_clear()


def test_rewrapping_prose_does_not_change_the_hash(monkeypatch):
    """YAML folded scalars discard the author's line breaks before we ever see them.

    Re-wrapping a long ``estimand`` is therefore indistinguishable from the original after
    parsing, so it must not retire an approval. Changing the *words* still does.
    """
    before = protocols.content_hash(PROTOCOL)
    rewrapped = dict(protocols.protocol(PROTOCOL))
    rewrapped["estimand"] = "  " + rewrapped["estimand"].replace(" ", "  ") + "\n"
    monkeypatch.setattr(protocols, "_config", lambda: {"protocols": {PROTOCOL: rewrapped}})
    protocols.protocols.cache_clear()

    assert protocols.content_hash(PROTOCOL) == before
    protocols.protocols.cache_clear()


# =====================================================================================
# Derived status — the pure precedence rules
# =====================================================================================
def test_a_protocol_with_no_decisions_is_pending():
    assert approvals.derived_status([], "sha256:abc") == approvals.PENDING_APPROVAL


def test_one_role_approving_is_not_enough():
    rows = [_row(approvals.MEDICAL, approvals.APPROVED, "sha256:abc")]
    assert approvals.derived_status(rows, "sha256:abc") == approvals.PENDING_APPROVAL
    assert approvals.missing_roles(rows, "sha256:abc") == (approvals.STATISTICAL,)


def test_both_roles_approving_the_current_content_is_approved():
    rows = [
        _row(approvals.MEDICAL, approvals.APPROVED, "sha256:abc"),
        _row(approvals.STATISTICAL, approvals.APPROVED, "sha256:abc"),
    ]
    assert approvals.derived_status(rows, "sha256:abc") == approvals.APPROVED
    assert approvals.is_approved(rows, "sha256:abc")
    assert approvals.missing_roles(rows, "sha256:abc") == ()


def test_a_rejection_cannot_be_outvoted_by_an_approval():
    """One role rejecting is decisive. Anything else would let a rejection be voted away."""
    rows = [
        _row(approvals.MEDICAL, approvals.APPROVED, "sha256:abc"),
        _row(approvals.STATISTICAL, approvals.REJECTED, "sha256:abc"),
    ]
    assert approvals.derived_status(rows, "sha256:abc") == approvals.REJECTED
    assert not approvals.is_approved(rows, "sha256:abc")


def test_editing_the_definition_invalidates_prior_approvals():
    """THE governance criterion: approval attests to specific methodology.

    Both roles signed off on ``sha256:abc``. The definition then changed. Nothing was
    revoked and no reviewer acted, yet the protocol is no longer approved — the hash
    mismatch alone retires it, so invalidation cannot be forgotten.
    """
    rows = [
        _row(approvals.MEDICAL, approvals.APPROVED, "sha256:abc"),
        _row(approvals.STATISTICAL, approvals.APPROVED, "sha256:abc"),
    ]
    assert approvals.is_approved(rows, "sha256:abc")

    assert approvals.derived_status(rows, "sha256:EDITED") == approvals.SUPERSEDED
    assert not approvals.is_approved(rows, "sha256:EDITED")


def test_a_revoked_approval_stops_counting():
    rows = [
        _row(approvals.MEDICAL, approvals.APPROVED, "sha256:abc"),
        _row(approvals.STATISTICAL, approvals.APPROVED, "sha256:abc", revoked=True),
    ]
    assert approvals.derived_status(rows, "sha256:abc") == approvals.REVOKED
    assert approvals.role_statuses(rows, "sha256:abc") == {
        approvals.MEDICAL: approvals.APPROVED,
        approvals.STATISTICAL: approvals.REVOKED,
    }


def test_the_latest_decision_for_a_role_wins():
    """A role may reconsider; the record keeps both rows and reads the newer one."""
    rows = [
        _row(approvals.MEDICAL, approvals.REJECTED, "sha256:abc", minutes=0),
        _row(approvals.MEDICAL, approvals.APPROVED, "sha256:abc", minutes=60),
    ]
    assert approvals.role_status(rows, approvals.MEDICAL, "sha256:abc") == approvals.APPROVED


# =====================================================================================
# Recording decisions through the service
# =====================================================================================
async def test_recording_an_approval_does_not_change_the_hash(db_session):
    """Approval state lives outside the hashed content, so signing off is inert on it."""
    before = protocols.content_hash(PROTOCOL)
    await review.record_protocol_decision(
        db_session, protocol_id=PROTOCOL, approval_role=approvals.MEDICAL,
        decision=approvals.APPROVED, reviewer_id="dr.medical",
    )
    assert protocols.content_hash(PROTOCOL) == before


async def test_the_caller_cannot_choose_which_content_it_approves(db_session):
    """No parameter accepts a hash; the service always derives it from the definition."""
    row = await review.record_protocol_decision(
        db_session, protocol_id=PROTOCOL, approval_role=approvals.MEDICAL,
        decision=approvals.APPROVED, reviewer_id="dr.medical",
    )
    assert row.content_hash == protocols.content_hash(PROTOCOL)


async def test_the_two_roles_are_independently_grantable(db_session):
    await review.record_protocol_decision(
        db_session, protocol_id=PROTOCOL, approval_role=approvals.MEDICAL,
        decision=approvals.APPROVED, reviewer_id="dr.medical",
    )
    state = await review.protocol_status(db_session, PROTOCOL)
    assert state["status"] == approvals.PENDING_APPROVAL
    assert state["missing_roles"] == [approvals.STATISTICAL]

    await review.record_protocol_decision(
        db_session, protocol_id=PROTOCOL, approval_role=approvals.STATISTICAL,
        decision=approvals.APPROVED, reviewer_id="dr.stats",
    )
    state = await review.protocol_status(db_session, PROTOCOL)
    assert state["status"] == approvals.APPROVED
    assert state["is_approved"]


async def test_the_two_roles_are_independently_revocable(db_session):
    for role, who in ((approvals.MEDICAL, "dr.medical"), (approvals.STATISTICAL, "dr.stats")):
        await review.record_protocol_decision(
            db_session, protocol_id=PROTOCOL, approval_role=role,
            decision=approvals.APPROVED, reviewer_id=who,
        )
    assert (await review.protocol_status(db_session, PROTOCOL))["is_approved"]

    await review.revoke_protocol_approval(
        db_session, protocol_id=PROTOCOL, approval_role=approvals.STATISTICAL,
        revoked_by="dr.stats", revocation_reason="tau-squared assumption no longer defensible",
    )
    state = await review.protocol_status(db_session, PROTOCOL)
    assert state["status"] == approvals.REVOKED
    assert state["role_statuses"][approvals.MEDICAL] == approvals.APPROVED
    assert state["role_statuses"][approvals.STATISTICAL] == approvals.REVOKED


async def test_revocation_withdraws_without_deleting(db_session):
    """"Approved, then withdrawn, because X" must stay answerable."""
    await review.record_protocol_decision(
        db_session, protocol_id=PROTOCOL, approval_role=approvals.MEDICAL,
        decision=approvals.APPROVED, reviewer_id="dr.medical",
    )
    await review.revoke_protocol_approval(
        db_session, protocol_id=PROTOCOL, approval_role=approvals.MEDICAL,
        revoked_by="dr.medical", revocation_reason="dose pooling was wrong",
    )
    rows = list((await db_session.execute(select(AnalysisProtocolApproval))).scalars().all())
    assert len(rows) == 1
    assert rows[0].decision == approvals.APPROVED
    assert rows[0].revocation_reason == "dose pooling was wrong"
    assert not rows[0].is_active


async def test_revoking_requires_a_reason(db_session):
    await review.record_protocol_decision(
        db_session, protocol_id=PROTOCOL, approval_role=approvals.MEDICAL,
        decision=approvals.APPROVED, reviewer_id="dr.medical",
    )
    with pytest.raises(review.ReviewError, match="reason"):
        await review.revoke_protocol_approval(
            db_session, protocol_id=PROTOCOL, approval_role=approvals.MEDICAL,
            revoked_by="dr.medical", revocation_reason="   ",
        )


async def test_rejecting_a_protocol_requires_an_explanation(db_session):
    with pytest.raises(review.ReviewError, match="review_note"):
        await review.record_protocol_decision(
            db_session, protocol_id=PROTOCOL, approval_role=approvals.STATISTICAL,
            decision=approvals.REJECTED, reviewer_id="dr.stats",
        )


async def test_an_unknown_protocol_cannot_be_approved(db_session):
    with pytest.raises(review.ReviewError, match="unknown protocol"):
        await review.record_protocol_decision(
            db_session, protocol_id="INVENTED", approval_role=approvals.MEDICAL,
            decision=approvals.APPROVED, reviewer_id="dr.medical",
        )


async def test_an_anonymous_approval_is_refused(db_session):
    with pytest.raises(review.ReviewError, match="reviewer_id"):
        await review.record_protocol_decision(
            db_session, protocol_id=PROTOCOL, approval_role=approvals.MEDICAL,
            decision=approvals.APPROVED, reviewer_id="  ",
        )


# =====================================================================================
# Network ratification through the service
# =====================================================================================
async def test_the_full_ratification_path_records_both_reviewers(db_session, network):
    await review.submit_for_medical_review(
        db_session, network_id=network.network_id, submitted_by="analyst"
    )
    await review.record_medical_review(
        db_session, network_id=network.network_id, reviewer="dr.medical",
        approve=True, note="population and endpoint appropriate",
    )
    result = await review.record_statistical_review(
        db_session, network_id=network.network_id, reviewer="dr.stats",
        approve=True, note="topology supports netmeta",
    )
    assert result.ratification_status == lc.RATIFIED
    assert lc.is_computable(result.ratification_status)
    assert result.medical_reviewer == "dr.medical"
    assert result.statistical_reviewer == "dr.stats"
    assert result.medical_reviewed_at and result.statistical_reviewed_at


async def test_statistical_review_cannot_be_reached_without_medical_review(db_session, network):
    """The ordering is the guarantee, enforced by the machine not by the caller."""
    with pytest.raises(lc.LifecycleError):
        await review.record_statistical_review(
            db_session, network_id=network.network_id, reviewer="dr.stats", approve=True,
        )
    assert network.ratification_status == lc.DRAFT


async def test_medical_approval_alone_does_not_ratify(db_session, network):
    await review.submit_for_medical_review(
        db_session, network_id=network.network_id, submitted_by="analyst"
    )
    result = await review.record_medical_review(
        db_session, network_id=network.network_id, reviewer="dr.medical", approve=True,
    )
    assert result.ratification_status == lc.PENDING_STATISTICAL_REVIEW
    assert not lc.is_computable(result.ratification_status)


async def test_rejecting_a_network_requires_a_note(db_session, network):
    await review.submit_for_medical_review(
        db_session, network_id=network.network_id, submitted_by="analyst"
    )
    with pytest.raises(review.ReviewError, match="note"):
        await review.record_medical_review(
            db_session, network_id=network.network_id, reviewer="dr.medical", approve=False,
        )


# =====================================================================================
# Reopening — the exit the refusal messages named and nothing implemented
# =====================================================================================
# Every refusal on a frozen network told the operator to "supersede it and build a new
# version". No route, service function or button existed for either half of that, so a
# ratified network was a dead end: membership could not be decided, the graph could not be
# rebuilt and the re-parse script refused too. Worse, `_network_out` published
# `allowed_transitions` straight off the state machine, so the API advertised moves it
# could not perform.
async def _audit_events(db_session, event: str) -> list[dict]:
    rows = (await db_session.execute(
        select(AuditLog).where(AuditLog.event == event)
    )).scalars().all()
    return [json.loads(r.context or "{}") for r in rows]


async def test_a_ratified_network_can_be_reopened_to_draft(db_session, network):
    await _ratify(db_session, network)

    result = await review.reopen_network(
        db_session, network_id=network.network_id, reopened_by="dr.stats",
        reason="ratified before anyone had decided membership",
    )

    assert result.ratification_status == lc.DRAFT
    assert not lc.is_computable(result.ratification_status)


async def test_reopening_clears_the_review_record_from_the_row(db_session, network):
    """A DRAFT still showing a statistical reviewer reads as approved to anyone scanning."""
    await _ratify(db_session, network)

    result = await review.reopen_network(
        db_session, network_id=network.network_id, reopened_by="an.analyst",
        reason="the approved window is still disputed",
    )

    assert result.medical_reviewer is None
    assert result.statistical_reviewer is None
    assert result.medical_reviewed_at is None
    assert result.statistical_reviewed_at is None


async def test_reopening_keeps_the_cleared_review_record_in_the_audit_log(db_session, network):
    """The row states what is true now; the log states what was true. Nothing is lost."""
    await _ratify(db_session, network)
    await review.reopen_network(
        db_session, network_id=network.network_id, reopened_by="an.analyst",
        reason="ratified by mistake",
    )

    entries = await _audit_events(db_session, "NETWORK_REOPENED")
    assert len(entries) == 1
    context = entries[0]
    assert context["reopened_by"] == "an.analyst"
    assert context["reason"] == "ratified by mistake"
    assert context["from"] == lc.RATIFIED
    assert context["to"] == lc.DRAFT
    # The names the row stopped claiming are still recoverable.
    assert context["cleared_review_record"]["medical_reviewer"] == "dr.medical"
    assert context["cleared_review_record"]["statistical_reviewer"] == "dr.stats"


async def test_reopening_requires_a_reason(db_session, network):
    """It withdraws a review that happened; an unexplained withdrawal reads as an accident."""
    await _ratify(db_session, network)
    with pytest.raises(review.ReviewError, match="reason"):
        await review.reopen_network(
            db_session, network_id=network.network_id, reopened_by="an.analyst", reason="  ",
        )
    assert network.ratification_status == lc.RATIFIED


async def test_reopening_requires_a_named_person(db_session, network):
    await _ratify(db_session, network)
    with pytest.raises(review.ReviewError, match="reopened_by"):
        await review.reopen_network(
            db_session, network_id=network.network_id, reopened_by="   ",
            reason="ratified by mistake",
        )


async def test_a_draft_network_has_nothing_to_reopen(db_session, network):
    with pytest.raises(review.ReviewError, match="already a draft"):
        await review.reopen_network(
            db_session, network_id=network.network_id, reopened_by="an.analyst",
            reason="belt and braces",
        )


async def test_a_superseded_network_cannot_be_revived(db_session, network):
    """SUPERSEDED is the one genuinely terminal state, and reopen must not undo it."""
    network.ratification_status = lc.SUPERSEDED
    await db_session.commit()
    with pytest.raises(review.ReviewError, match="retired for good"):
        await review.reopen_network(
            db_session, network_id=network.network_id, reopened_by="an.analyst",
            reason="changed my mind",
        )


async def test_a_network_mid_review_can_be_pulled_back(db_session, network):
    """Not only RATIFIED: a submission made in error should not have to be reviewed out."""
    await review.submit_for_medical_review(
        db_session, network_id=network.network_id, submitted_by="analyst"
    )
    result = await review.reopen_network(
        db_session, network_id=network.network_id, reopened_by="analyst",
        reason="submitted the wrong network",
    )
    assert result.ratification_status == lc.DRAFT


async def test_a_rejected_network_can_be_reopened(db_session, network):
    """REJECTED reads as terminal but the machine has always allowed DRAFT out of it."""
    await review.submit_for_medical_review(
        db_session, network_id=network.network_id, submitted_by="analyst"
    )
    await review.record_medical_review(
        db_session, network_id=network.network_id, reviewer="dr.medical",
        approve=False, note="population is wrong",
    )
    result = await review.reopen_network(
        db_session, network_id=network.network_id, reopened_by="analyst",
        reason="population corrected upstream",
    )
    assert result.ratification_status == lc.DRAFT
    assert result.rejection_reason is None


async def test_reopening_loses_both_reviews_rather_than_resuming(db_session, network):
    """The ordering guarantee survives a reopen: RATIFIED still needs both stages again."""
    await _ratify(db_session, network)
    await review.reopen_network(
        db_session, network_id=network.network_id, reopened_by="an.analyst",
        reason="ratified by mistake",
    )
    with pytest.raises(lc.LifecycleError):
        await review.record_statistical_review(
            db_session, network_id=network.network_id, reviewer="dr.stats", approve=True,
        )


async def test_the_gate_reports_whether_a_reopen_is_legal(db_session, network):
    """Asked of the state machine's owner, so the UI keeps no second copy of the edges."""
    assert (await review.governance_gate(
        db_session, network_id=network.network_id
    ))["can_reopen"] is False  # DRAFT has no edge back to itself

    await _ratify(db_session, network)
    assert (await review.governance_gate(
        db_session, network_id=network.network_id
    ))["can_reopen"] is True

    network.ratification_status = lc.SUPERSEDED
    await db_session.commit()
    assert (await review.governance_gate(
        db_session, network_id=network.network_id
    ))["can_reopen"] is False


async def test_the_reopen_route_returns_a_draft_network(api):
    client, maker = api
    async with maker() as db:
        db.add(EvidenceNetwork(
            network_id="NET-PSA-ACR50", indication="Psoriatic Arthritis",
            canonical_outcome_id="PSA_ACR50_W16", protocol_id=PROTOCOL,
            ratification_status=lc.RATIFIED,
            medical_reviewer="dr.medical", statistical_reviewer="dr.stats",
        ))
        await db.commit()

    response = await client.post(
        "/evidence-review/networks/NET-PSA-ACR50/reopen",
        json={"reopened_by": "an.analyst", "reason": "ratified before membership was decided"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ratification_status"] == lc.DRAFT
    assert body["is_computable"] is False
    assert body["medical_reviewer"] is None
    assert body["statistical_reviewer"] is None


async def test_the_reopen_route_refuses_a_blank_reason(api):
    """`min_length=1` passes whitespace, so the service's strip check is the real guard."""
    client, maker = api
    async with maker() as db:
        db.add(EvidenceNetwork(
            network_id="NET-PSA-ACR50", indication="Psoriatic Arthritis",
            canonical_outcome_id="PSA_ACR50_W16", protocol_id=PROTOCOL,
            ratification_status=lc.RATIFIED,
        ))
        await db.commit()

    response = await client.post(
        "/evidence-review/networks/NET-PSA-ACR50/reopen",
        json={"reopened_by": "an.analyst", "reason": "   "},
    )
    assert response.status_code == 400
    assert "reason" in response.json()["detail"]

    # And the network is untouched by the refusal.
    async with maker() as db:
        row = (await db.execute(select(EvidenceNetwork))).scalar_one()
        assert row.ratification_status == lc.RATIFIED


async def test_the_reopen_route_requires_a_reason_at_all(api):
    client, maker = api
    async with maker() as db:
        db.add(EvidenceNetwork(
            network_id="NET-PSA-ACR50", indication="Psoriatic Arthritis",
            canonical_outcome_id="PSA_ACR50_W16", protocol_id=PROTOCOL,
            ratification_status=lc.RATIFIED,
        ))
        await db.commit()

    response = await client.post(
        "/evidence-review/networks/NET-PSA-ACR50/reopen",
        json={"reopened_by": "an.analyst"},
    )
    assert response.status_code == 422


async def test_reopening_an_unknown_network_is_a_client_error(api):
    client, _maker = api
    response = await client.post(
        "/evidence-review/networks/NET-NOPE/reopen",
        json={"reopened_by": "an.analyst", "reason": "typo"},
    )
    assert response.status_code == 400
    assert "unknown network" in response.json()["detail"]


async def test_a_network_without_a_protocol_cannot_enter_review(db_session):
    orphan = EvidenceNetwork(
        network_id="NET-ORPHAN", indication="Psoriatic Arthritis",
        canonical_outcome_id="PSA_ACR50_W16", protocol_id=None,
    )
    db_session.add(orphan)
    await db_session.commit()
    with pytest.raises(review.ReviewError, match="protocol_id"):
        await review.submit_for_medical_review(
            db_session, network_id="NET-ORPHAN", submitted_by="analyst"
        )


# =====================================================================================
# The combined gate
# =====================================================================================
async def _ratify(db_session, network):
    await review.submit_for_medical_review(
        db_session, network_id=network.network_id, submitted_by="analyst"
    )
    await review.record_medical_review(
        db_session, network_id=network.network_id, reviewer="dr.medical", approve=True
    )
    await review.record_statistical_review(
        db_session, network_id=network.network_id, reviewer="dr.stats", approve=True
    )


async def _approve_protocol(db_session):
    for role, who in ((approvals.MEDICAL, "dr.medical"), (approvals.STATISTICAL, "dr.stats")):
        await review.record_protocol_decision(
            db_session, protocol_id=PROTOCOL, approval_role=role,
            decision=approvals.APPROVED, reviewer_id=who,
        )


async def test_a_ratified_network_under_an_unapproved_protocol_is_still_blocked(
    db_session, network
):
    await _ratify(db_session, network)
    gate = await review.governance_gate(db_session, network_id=network.network_id)
    assert not gate["may_compute_governed"]
    assert gate["blocking_status"] == statuses.PROTOCOL_PENDING_APPROVAL


async def test_an_approved_protocol_over_a_draft_network_is_still_blocked(db_session, network):
    await _approve_protocol(db_session)
    gate = await review.governance_gate(db_session, network_id=network.network_id)
    assert not gate["may_compute_governed"]
    assert gate["blocking_status"] == statuses.NETWORK_NOT_RATIFIED


async def test_both_gates_passing_permits_governed_execution(db_session, network):
    await _approve_protocol(db_session)
    await _ratify(db_session, network)
    gate = await review.governance_gate(db_session, network_id=network.network_id)
    assert gate["may_compute_governed"]
    assert gate["blocking_status"] is None
    assert gate["protocol_status"] == approvals.APPROVED
    assert gate["ratification_status"] == lc.RATIFIED


async def test_the_open_gate_never_claims_a_computation_happened(db_session, network):
    """A permission check must not hand back a result status."""
    await _approve_protocol(db_session)
    await _ratify(db_session, network)
    gate = await review.governance_gate(db_session, network_id=network.network_id)
    assert gate["blocking_status"] != statuses.GOVERNED_SYNTHESIS_COMPLETED
    assert statuses.GOVERNED_SYNTHESIS_COMPLETED not in gate.values()


async def test_revoking_an_approval_recloses_the_gate(db_session, network):
    """The reason revocation exists: a governed analysis can be withdrawn after the fact."""
    await _approve_protocol(db_session)
    await _ratify(db_session, network)
    assert (await review.governance_gate(
        db_session, network_id=network.network_id
    ))["may_compute_governed"]

    await review.revoke_protocol_approval(
        db_session, protocol_id=PROTOCOL, approval_role=approvals.STATISTICAL,
        revoked_by="dr.stats", revocation_reason="heterogeneity rule needs revisiting",
    )
    gate = await review.governance_gate(db_session, network_id=network.network_id)
    assert not gate["may_compute_governed"]
    assert gate["blocking_status"] == statuses.PROTOCOL_PENDING_APPROVAL
