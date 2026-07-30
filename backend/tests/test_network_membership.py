"""Network membership decisions — Lifecycle 2, and the cliff the first inclusion opens.

The builder has always written every membership ``PROPOSED`` and nothing could promote one,
so a network's own membership decisions were not expressible: a study a human screened out
could not be recorded as screened out, and ``EXCLUDED`` required a reason nothing collected.

The tests that earn their place here are the ones about **narrowing**.
``comparison_service.membership_filter`` returns ``None`` when nothing is ``INCLUDED``,
meaning "membership narrows nothing, consult the whole indication". The instant one study is
included the filter binds and every other study stops contributing — so a curator including
a single study they were sure about would silently shrink the corpus to one. That is not a
bug to fix by weakening the filter; it is a consequence to disclose before the click.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.evidence import lifecycles
from app.models.database import Base
from app.models.evidence_network import EvidenceNetwork, NetworkMembership
from app.services import comparison_service as comparisons
from app.services import evidence_review_service as svc

NETWORK = "NET-PSORIATIC-ARTHRITIS-PSA_ACR50_W16"
STUDIES = ("NCT00000001", "NCT00000002", "NCT00000003")


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


async def _network(db, *, ratification_status: str = lifecycles.DRAFT) -> EvidenceNetwork:
    network = EvidenceNetwork(
        network_id=NETWORK,
        indication="Psoriatic Arthritis",
        canonical_outcome_id="PSA_ACR50_W16",
        treatment_phase="PRIMARY",
        protocol_id="PSA_ACR50_W16_PRIMARY",
        ratification_status=ratification_status,
    )
    db.add(network)
    for study_id in STUDIES:
        db.add(NetworkMembership(
            membership_id=f"NM-{NETWORK}-{study_id}",
            network_id=NETWORK,
            study_id=study_id,
            protocol_id="PSA_ACR50_W16_PRIMARY",
            membership_status=lifecycles.PROPOSED,
        ))
    await db.flush()
    return network


async def _row(db, study_id: str) -> NetworkMembership:
    return (await db.execute(
        select(NetworkMembership).where(
            NetworkMembership.network_id == NETWORK,
            NetworkMembership.study_id == study_id,
        )
    )).scalar_one()


# =====================================================================================
# The decision itself
# =====================================================================================
async def test_a_proposed_study_can_be_included(db_session):
    await _network(db_session)

    result = await svc.decide_membership(
        db_session, network_id=NETWORK, study_id=STUDIES[0],
        decision="INCLUDED", decided_by="A Reviewer",
    )

    assert result["membership_status"] == lifecycles.INCLUDED
    assert result["before"] == lifecycles.PROPOSED
    assert result["decided_by"] == "A Reviewer"
    assert (await _row(db_session, STUDIES[0])).decided_at is not None


async def test_excluding_without_a_reason_is_refused(db_session):
    """The rule lives in ``lifecycles.assert_transition`` and is not restated here."""
    await _network(db_session)

    with pytest.raises(lifecycles.LifecycleError, match="requires a reason"):
        await svc.decide_membership(
            db_session, network_id=NETWORK, study_id=STUDIES[0],
            decision="EXCLUDED", decided_by="A Reviewer",
        )


async def test_excluding_with_a_reason_records_it(db_session):
    await _network(db_session)

    result = await svc.decide_membership(
        db_session, network_id=NETWORK, study_id=STUDIES[0],
        decision="EXCLUDED", decided_by="A Reviewer",
        reason="paediatric population, not comparable with the adult network",
    )

    assert result["membership_status"] == lifecycles.EXCLUDED
    assert "paediatric" in result["exclusion_reason"]


async def test_an_anonymous_decision_is_refused(db_session):
    await _network(db_session)
    with pytest.raises(svc.ReviewError, match="decided_by is required"):
        await svc.decide_membership(
            db_session, network_id=NETWORK, study_id=STUDIES[0],
            decision="INCLUDED", decided_by="   ",
        )


async def test_proposed_is_not_a_decision_a_reviewer_can_make(db_session):
    """Re-proposing would un-decide a study and lose who decided and why."""
    await _network(db_session)
    with pytest.raises(svc.ReviewError, match="not a decision a reviewer can make"):
        await svc.decide_membership(
            db_session, network_id=NETWORK, study_id=STUDIES[0],
            decision="PROPOSED", decided_by="A Reviewer",
        )


async def test_an_unknown_state_is_refused(db_session):
    await _network(db_session)
    with pytest.raises(svc.ReviewError, match="not a membership state"):
        await svc.decide_membership(
            db_session, network_id=NETWORK, study_id=STUDIES[0],
            decision="MAYBE", decided_by="A Reviewer",
        )


async def test_a_study_the_builder_never_proposed_cannot_be_decided(db_session):
    await _network(db_session)
    with pytest.raises(svc.ReviewError, match="not a member of network"):
        await svc.decide_membership(
            db_session, network_id=NETWORK, study_id="NCT99999999",
            decision="INCLUDED", decided_by="A Reviewer",
        )


async def test_membership_cannot_be_changed_under_a_ratified_network(db_session):
    """Altering the evidence set a reviewer approved, while it still looks approved."""
    await _network(db_session, ratification_status=lifecycles.RATIFIED)

    with pytest.raises(svc.ReviewError, match="RATIFIED") as excinfo:
        await svc.decide_membership(
            db_session, network_id=NETWORK, study_id=STUDIES[0],
            decision="INCLUDED", decided_by="A Reviewer",
        )
    # The refusal used to end "Supersede it and build a new version instead", naming a
    # remedy with no route, no service function and no button behind it. A refusal that
    # points at nothing is a dead end, so it now points at something that exists.
    assert "Reopen it to DRAFT" in str(excinfo.value)


@pytest.mark.parametrize(
    "status", (lifecycles.PENDING_MEDICAL_REVIEW, lifecycles.PENDING_STATISTICAL_REVIEW)
)
async def test_membership_cannot_be_changed_while_a_review_is_in_progress(db_session, status):
    """A network mid-review is as untouchable as a ratified one.

    This guard refused only on ``RATIFIED``, so a study could be included or excluded under
    a reviewer who was part-way through reading the evidence set — changing what they were
    looking at, mid-look, with nothing in the record to say the set had moved.
    ``scripts/reparse_dev_pilot.py`` had the broader rule right and kept it privately;
    ``lifecycles.FROZEN_FOR_EDIT`` is now its one owner.
    """
    await _network(db_session, ratification_status=status)

    with pytest.raises(svc.ReviewError, match=status) as excinfo:
        await svc.decide_membership(
            db_session, network_id=NETWORK, study_id=STUDIES[0],
            decision="INCLUDED", decided_by="A Reviewer",
        )
    # And it must not tell a mid-review reviewer they already approved something.
    assert "part-way through" in str(excinfo.value)


async def test_reopening_a_ratified_network_unfreezes_its_membership(db_session):
    """The round trip the refusal promises: reopen, then decide."""
    await _network(db_session, ratification_status=lifecycles.RATIFIED)
    with pytest.raises(svc.ReviewError):
        await svc.decide_membership(
            db_session, network_id=NETWORK, study_id=STUDIES[0],
            decision="INCLUDED", decided_by="A Reviewer",
        )

    await svc.reopen_network(
        db_session, network_id=NETWORK, reopened_by="A Reviewer",
        reason="ratified before anyone had decided membership",
    )

    result = await svc.decide_membership(
        db_session, network_id=NETWORK, study_id=STUDIES[0],
        decision="INCLUDED", decided_by="A Reviewer",
    )
    assert result["membership_status"] == lifecycles.INCLUDED
    # Reopening does not soften the cliff — the first inclusion still binds the filter.
    assert result["narrowed_the_evidence_set"] is True
    assert result["membership"]["studies_consulted"] == 1


async def test_a_decision_is_revisable_in_both_directions(db_session):
    """Unlike verification, membership is a re-judgement of fit rather than a rewrite."""
    await _network(db_session)
    await svc.decide_membership(
        db_session, network_id=NETWORK, study_id=STUDIES[0],
        decision="INCLUDED", decided_by="A Reviewer",
    )
    result = await svc.decide_membership(
        db_session, network_id=NETWORK, study_id=STUDIES[0],
        decision="EXCLUDED", decided_by="A Reviewer",
        reason="a protocol amendment moved the window",
    )
    assert result["before"] == lifecycles.INCLUDED
    assert result["membership_status"] == lifecycles.EXCLUDED


# =====================================================================================
# The cliff — disclosed, not smoothed over
# =====================================================================================
async def test_with_nothing_included_membership_narrows_nothing(db_session):
    network = await _network(db_session)

    preview = await svc.membership_preview(db_session, network_id=NETWORK)
    assert preview["included"] == 0
    assert preview["filter_binds"] is False
    assert preview["studies_consulted"] == 3
    assert "narrows nothing" in preview["note"]

    # And the resolver reads the same rule the same way.
    assert await comparisons.membership_filter(db_session, network) is None


async def test_the_first_inclusion_narrows_the_evidence_set_and_says_so(db_session):
    """The whole reason the decision route returns a before/after view."""
    network = await _network(db_session)

    result = await svc.decide_membership(
        db_session, network_id=NETWORK, study_id=STUDIES[0],
        decision="INCLUDED", decided_by="A Reviewer",
    )

    assert result["narrowed_the_evidence_set"] is True
    assert "from 3 studies to 1" in result["narrowing_warning"]
    assert result["membership"]["filter_binds"] is True

    # Not a UI nicety: the resolver really does drop the other two now.
    assert await comparisons.membership_filter(db_session, network) == {STUDIES[0]}


async def test_excluding_a_study_does_not_narrow_anything_while_nothing_is_included(
    db_session,
):
    """An exclusion with no inclusions leaves the filter unbound, so nothing changes."""
    await _network(db_session)

    result = await svc.decide_membership(
        db_session, network_id=NETWORK, study_id=STUDIES[0],
        decision="EXCLUDED", decided_by="A Reviewer", reason="healthy-volunteer PK study",
    )

    assert result["narrowed_the_evidence_set"] is False
    assert result["narrowing_warning"] is None
    assert result["membership"]["studies_consulted"] == 3


async def test_the_preview_writes_nothing(db_session):
    await _network(db_session)
    before = {m.membership_status for m in (await db_session.execute(
        select(NetworkMembership)
    )).scalars()}

    await svc.membership_preview(db_session, network_id=NETWORK)

    after = {m.membership_status for m in (await db_session.execute(
        select(NetworkMembership)
    )).scalars()}
    assert before == after == {lifecycles.PROPOSED}


async def test_an_unknown_network_is_not_an_empty_preview(db_session):
    with pytest.raises(svc.ReviewError):
        await svc.membership_preview(db_session, network_id="NET-NOPE")
