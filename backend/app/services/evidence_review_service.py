"""The medical + statistical review surface (X1).

Three things the plan assumes exist and did not: protocol approval keyed to a derived
content hash, the two-stage network ratification transitions, and a single gate that
decides whether a computation may run in ``GOVERNED`` mode.

**Approval and ratification are independent gates, not a sequence.** A protocol is
approved methodology; a network is an assembled evidence set judged fit to compute on. A
ratified network under an unapproved protocol is still not governable, and an approved
protocol pointed at a draft network is not either. ``governance_gate`` is the only place
that combines them, so no caller has to remember both.

**The caller never supplies a content hash.** Every function here derives it from the
protocol definition. Accepting one as input would let a reviewer approve content that is
not what is actually on disk — the exact failure the derived-hash design exists to
prevent.

Every state change writes to the append-only ``AuditLog``. The queue's *state* is mutable
by necessity; the record of who changed it and why is not.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.evidence import approvals, lifecycles, protocols, statuses
from app.models.analysis_protocol import AnalysisProtocolApproval
from app.models.evidence_network import EvidenceNetwork, NetworkMembership
from app.utils.audit import write_audit

RATIFICATION = "ratification"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReviewError(ValueError):
    """A review action that would leave the governance record incoherent."""


# =====================================================================================
# Protocol approval
# =====================================================================================
async def _approval_rows(db: AsyncSession, protocol_id: str) -> list[AnalysisProtocolApproval]:
    result = await db.execute(
        select(AnalysisProtocolApproval).where(
            AnalysisProtocolApproval.protocol_id == protocol_id
        )
    )
    return list(result.scalars().all())


async def protocol_status(db: AsyncSession, protocol_id: str) -> dict:
    """The full derived approval picture for one protocol.

    Returns the current hash alongside the status so a UI can show *"approved, but the
    definition has changed since"* rather than a bare label. That distinction is the
    whole reason the hash is stored on the approval row.
    """
    definition = protocols.protocol(protocol_id)
    if definition is None:
        raise ReviewError(f"unknown protocol {protocol_id!r}")

    current_hash = protocols.content_hash(protocol_id)
    rows = await _approval_rows(db, protocol_id)
    return {
        "protocol_id": protocol_id,
        "version": definition.get("version"),
        "indication": definition.get("indication"),
        "canonical_outcome_id": definition.get("canonical_outcome_id"),
        "content_hash": current_hash,
        "status": approvals.derived_status(rows, current_hash),
        "role_statuses": approvals.role_statuses(rows, current_hash),
        "missing_roles": list(approvals.missing_roles(rows, current_hash)),
        "is_approved": approvals.is_approved(rows, current_hash),
    }


async def record_protocol_decision(
    db: AsyncSession,
    *,
    protocol_id: str,
    approval_role: str,
    decision: str,
    reviewer_id: str,
    review_note: str | None = None,
) -> AnalysisProtocolApproval:
    """Record one role's decision against the protocol's CURRENT content.

    The hash is derived here, never passed in. A rejection requires a note: an
    unexplained rejection blocks every downstream computation and leaves nobody able to
    tell a considered objection from a misclick.
    """
    if approval_role not in approvals.APPROVAL_ROLES:
        raise ReviewError(
            f"approval_role {approval_role!r} is not one of "
            f"{', '.join(approvals.APPROVAL_ROLES)}"
        )
    if decision not in approvals.DECISIONS:
        raise ReviewError(
            f"decision {decision!r} is not one of {', '.join(approvals.DECISIONS)}"
        )
    if not (reviewer_id or "").strip():
        raise ReviewError("reviewer_id is required — an anonymous approval is not an approval")

    current_hash = protocols.content_hash(protocol_id)
    if current_hash is None:
        raise ReviewError(
            f"unknown protocol {protocol_id!r}; it must exist in analysis_protocols.yaml "
            "before it can be reviewed"
        )
    if decision == approvals.REJECTED and not (review_note or "").strip():
        raise ReviewError("rejecting a protocol requires a review_note explaining why")

    row = AnalysisProtocolApproval(
        approval_id=f"APR-{uuid4().hex}",
        protocol_id=protocol_id,
        content_hash=current_hash,
        approval_role=approval_role,
        decision=decision,
        reviewer_id=reviewer_id.strip(),
        review_note=review_note,
        reviewed_at=utcnow(),
    )
    db.add(row)

    await write_audit(
        db, role="REVIEWER", event="PROTOCOL_APPROVAL_RECORDED",
        context={
            "protocol_id": protocol_id, "content_hash": current_hash,
            "approval_role": approval_role, "decision": decision,
            "reviewer_id": row.reviewer_id,
        },
        commit=False,
    )
    await db.commit()
    return row


async def revoke_protocol_approval(
    db: AsyncSession,
    *,
    protocol_id: str,
    approval_role: str,
    revoked_by: str,
    revocation_reason: str,
) -> AnalysisProtocolApproval:
    """Withdraw the active decision for one role, leaving the row in place.

    Revocation is a withdrawal, not a deletion — "approved, then withdrawn, because X"
    must stay answerable. A reason is mandatory for the same reason excluding a study
    from a network requires one.
    """
    if not (revocation_reason or "").strip():
        raise ReviewError("revoking an approval requires a reason")
    if not (revoked_by or "").strip():
        raise ReviewError("revoked_by is required")

    current_hash = protocols.content_hash(protocol_id)
    if current_hash is None:
        raise ReviewError(f"unknown protocol {protocol_id!r}")

    rows = await _approval_rows(db, protocol_id)
    active = [
        r for r in rows
        if r.approval_role == approval_role
        and r.content_hash == current_hash
        and r.revoked_at is None
    ]
    if not active:
        raise ReviewError(
            f"no active {approval_role} decision on the current content of {protocol_id!r} "
            "to revoke"
        )

    row = max(active, key=lambda r: r.reviewed_at or utcnow())
    row.revoked_at = utcnow()
    row.revoked_by = revoked_by.strip()
    row.revocation_reason = revocation_reason

    await write_audit(
        db, role="REVIEWER", event="PROTOCOL_APPROVAL_REVOKED",
        context={
            "protocol_id": protocol_id, "content_hash": current_hash,
            "approval_role": approval_role, "revoked_by": row.revoked_by,
            "reason": revocation_reason,
        },
        commit=False,
    )
    await db.commit()
    return row


# =====================================================================================
# Network ratification — two stages, in order
# =====================================================================================
async def _network(db: AsyncSession, network_id: str) -> EvidenceNetwork:
    network = (await db.execute(
        select(EvidenceNetwork).where(EvidenceNetwork.network_id == network_id)
    )).scalar_one_or_none()
    if network is None:
        raise ReviewError(f"unknown network {network_id!r}")
    return network


async def _transition(
    db: AsyncSession, network: EvidenceNetwork, target: str, *, event: str, context: dict
) -> EvidenceNetwork:
    """Apply a ratification transition through the state machine, then audit it."""
    lifecycles.assert_transition(RATIFICATION, network.ratification_status, target)
    before = network.ratification_status
    network.ratification_status = target
    network.updated_at = utcnow()

    await write_audit(
        db, role="REVIEWER", event=event,
        context={"network_id": network.network_id, "from": before, "to": target, **context},
        commit=False,
    )
    await db.commit()
    return network


async def submit_for_medical_review(
    db: AsyncSession, *, network_id: str, submitted_by: str
) -> EvidenceNetwork:
    """DRAFT -> PENDING_MEDICAL_REVIEW.

    Refuses a network with no governing protocol: membership decisions are only
    meaningful relative to one, so there would be nothing for a reviewer to judge the
    evidence set against.
    """
    network = await _network(db, network_id)
    if not network.protocol_id:
        raise ReviewError(
            f"network {network_id!r} has no protocol_id — membership decisions are only "
            "meaningful relative to a protocol, so there is nothing to review it against"
        )
    if not protocols.is_defined(network.protocol_id):
        raise ReviewError(
            f"network {network_id!r} references protocol {network.protocol_id!r}, which is "
            "not defined in analysis_protocols.yaml"
        )
    return await _transition(
        db, network, lifecycles.PENDING_MEDICAL_REVIEW,
        event="NETWORK_SUBMITTED_FOR_MEDICAL_REVIEW",
        context={"submitted_by": submitted_by, "protocol_id": network.protocol_id},
    )


async def record_medical_review(
    db: AsyncSession, *, network_id: str, reviewer: str, approve: bool,
    note: str | None = None,
) -> EvidenceNetwork:
    """Stage 1. Approving advances to statistical review; it does NOT ratify.

    The ordering is the guarantee that a network cannot reach ``RATIFIED`` having seen
    only one of the two reviews — enforced by the state machine, not by this function
    remembering to check.
    """
    if not (reviewer or "").strip():
        raise ReviewError("reviewer is required")
    network = await _network(db, network_id)

    if not approve and not (note or "").strip():
        raise ReviewError("rejecting a network requires a note explaining why")

    network.medical_reviewer = reviewer.strip()
    network.medical_reviewed_at = utcnow()
    network.medical_review_note = note
    if not approve:
        network.rejection_reason = note

    target = lifecycles.PENDING_STATISTICAL_REVIEW if approve else lifecycles.NETWORK_REJECTED
    return await _transition(
        db, network, target,
        event="NETWORK_MEDICAL_REVIEW_RECORDED",
        context={"reviewer": network.medical_reviewer, "approved": approve},
    )


async def record_statistical_review(
    db: AsyncSession, *, network_id: str, reviewer: str, approve: bool,
    note: str | None = None,
) -> EvidenceNetwork:
    """Stage 2. Approving here is what ratifies the network."""
    if not (reviewer or "").strip():
        raise ReviewError("reviewer is required")
    network = await _network(db, network_id)

    if not approve and not (note or "").strip():
        raise ReviewError("rejecting a network requires a note explaining why")

    network.statistical_reviewer = reviewer.strip()
    network.statistical_reviewed_at = utcnow()
    network.statistical_review_note = note
    if not approve:
        network.rejection_reason = note

    target = lifecycles.RATIFIED if approve else lifecycles.NETWORK_REJECTED
    return await _transition(
        db, network, target,
        event="NETWORK_STATISTICAL_REVIEW_RECORDED",
        context={"reviewer": network.statistical_reviewer, "approved": approve},
    )


async def reopen_network(
    db: AsyncSession, *, network_id: str, reopened_by: str, reason: str
) -> EvidenceNetwork:
    """Take a network out of review or ratification and back to ``DRAFT``.

    **This is the exit every refusal message already named and nothing implemented.**
    ``build_network``, ``decide_membership`` and ``reparse_dev_pilot`` all told the operator
    to supersede the network and build a new version; there was no route, no service
    function and no button for either half of that sentence. Worse, ``_network_out``
    advertised ``allowed_transitions`` straight off the state machine, so the API published
    a move it could not perform. A ratified network was a dead end.

    **Reopening is not superseding, and the difference matters.** Superseding retains the
    approved evidence set as its own row and points it at a replacement — that is what
    ``version`` and ``superseded_by`` are for, and nothing writes them for networks yet
    because the deterministic ``network_id_for`` id makes the two rows collide. Reopening
    keeps one row and **keeps no snapshot of what was approved**. So it is the right action
    for an approval that should not have happened — premature, mistaken, or a test click —
    and the wrong one for a genuine change of mind about a set you may need to show later.

    The review stamps are cleared, because a ``DRAFT`` still displaying a
    ``statistical_reviewer`` reads as approved to anyone scanning the page. Nothing is lost:
    the audit entry carries the names and dates it cleared, which is where a superseded
    fact belongs — the row states what is true now, the log states what was true.
    """
    if not (reopened_by or "").strip():
        raise ReviewError(
            "reopened_by is required — an anonymous withdrawal of an approval is not auditable"
        )
    if not (reason or "").strip():
        raise ReviewError(
            "reopening requires a reason. It withdraws a review that has already happened, "
            "and an unexplained withdrawal cannot be told apart from an accident"
        )

    network = await _network(db, network_id)
    before = network.ratification_status
    if not lifecycles.can_transition(RATIFICATION, before, lifecycles.DRAFT):
        raise ReviewError(
            f"network {network_id!r} is {before} and cannot be reopened. "
            + (
                "It is already a draft, so there is nothing to reopen"
                if before == lifecycles.DRAFT
                else "A superseded network is retired for good — build its replacement instead"
            )
        )

    # Captured before clearing so the log keeps what the row is about to stop claiming.
    cleared = {
        "medical_reviewer": network.medical_reviewer,
        "medical_reviewed_at": (
            network.medical_reviewed_at.isoformat() if network.medical_reviewed_at else None
        ),
        "statistical_reviewer": network.statistical_reviewer,
        "statistical_reviewed_at": (
            network.statistical_reviewed_at.isoformat()
            if network.statistical_reviewed_at else None
        ),
        "rejection_reason": network.rejection_reason,
    }
    network.medical_reviewer = None
    network.medical_reviewed_at = None
    network.medical_review_note = None
    network.statistical_reviewer = None
    network.statistical_reviewed_at = None
    network.statistical_review_note = None
    network.rejection_reason = None

    return await _transition(
        db, network, lifecycles.DRAFT,
        event="NETWORK_REOPENED",
        context={
            "reopened_by": reopened_by.strip(),
            "reason": reason.strip(),
            "cleared_review_record": cleared,
            "authentication": "RECORDED_NOT_AUTHENTICATED",
        },
    )


# =====================================================================================
# Network membership — Lifecycle 2, per network AND protocol
# =====================================================================================
# The builder proposes every membership as PROPOSED and, until this existed, nothing could
# promote one. The state machine, the exclusion-reason rule and the `decided_by` column had
# all been in place since Phase 2 with no way to reach them, so a network's own membership
# decisions were simply not expressible: a study a human screened out could not be recorded
# as screened out.
#
# **The cliff this opens, and why the preview is not optional.** `membership_filter` returns
# None when nothing is INCLUDED, meaning "membership narrows nothing, consult the whole
# indication". The instant ONE study is INCLUDED the filter binds and every other study in
# the network stops contributing. A curator including a single study they were confident
# about would silently shrink the corpus to that one study, and nothing in the resolve would
# say so. Every decision here therefore returns what the evidence set looks like before and
# after, and `membership_preview` exposes the same figures without writing anything.
#
# Whether the filter *should* bind before a network is declared membership-reviewed is a
# reviewer's question, not an engineering default, so it is disclosed rather than decided.
async def _membership(
    db: AsyncSession, network_id: str, study_id: str
) -> NetworkMembership:
    row = (await db.execute(
        select(NetworkMembership).where(
            NetworkMembership.network_id == network_id,
            NetworkMembership.study_id == study_id,
        )
    )).scalar_one_or_none()
    if row is None:
        raise ReviewError(
            f"study {study_id!r} is not a member of network {network_id!r}. Membership is "
            "proposed by the network builder; a study it never proposed cannot be decided "
            "here"
        )
    return row


async def _membership_counts(db: AsyncSession, network_id: str) -> dict[str, int]:
    rows = (await db.execute(
        select(NetworkMembership.membership_status).where(
            NetworkMembership.network_id == network_id
        )
    )).scalars().all()
    counts: dict[str, int] = {}
    for status in rows:
        counts[status] = counts.get(status, 0) + 1
    return counts


def _narrowing_note(included: int, total: int) -> str:
    """What the INCLUDED set currently does to evidence gathering, in plain words."""
    if included == 0:
        return (
            f"No study is INCLUDED, so membership narrows nothing and a resolve consults "
            f"all {total} proposed studies. Including even one study changes that: the "
            "filter binds and the other studies stop contributing."
        )
    return (
        f"{included} of {total} studies are INCLUDED, so a resolve consults those "
        f"{included} and no others."
    )


async def membership_preview(db: AsyncSession, *, network_id: str) -> dict:
    """What the membership set does to a resolve right now. Reads only.

    Exists so a UI can show the consequence *before* the first inclusion rather than after
    it, which is the only point at which the warning is useful.
    """
    await _network(db, network_id)
    counts = await _membership_counts(db, network_id)
    total = sum(counts.values())
    included = counts.get(lifecycles.INCLUDED, 0)
    return {
        "network_id": network_id,
        "counts": counts,
        "total": total,
        "included": included,
        "filter_binds": included > 0,
        "studies_consulted": included if included else total,
        "note": _narrowing_note(included, total),
    }


async def decide_membership(
    db: AsyncSession,
    *,
    network_id: str,
    study_id: str,
    decision: str,
    decided_by: str,
    reason: str | None = None,
    note: str | None = None,
) -> dict:
    """Record one study's membership decision for one network.

    ``decision`` is a membership state: INCLUDED, EXCLUDED or REQUIRES_REVIEW. The
    transition goes through ``lifecycles.assert_transition``, which is also what enforces
    that an exclusion carries a reason — restating that rule here would give it two owners.

    Returns the decision alongside a **before/after view of what a resolve will consult**,
    because the first inclusion on a network silently narrows the evidence set from every
    proposed study to that one.
    """
    if not (decided_by or "").strip():
        raise ReviewError(
            "decided_by is required — an anonymous membership decision is not auditable"
        )
    decision = (decision or "").strip().upper()
    if decision not in lifecycles.MEMBERSHIP_STATES:
        raise ReviewError(
            f"{decision!r} is not a membership state; expected one of "
            f"{', '.join(lifecycles.MEMBERSHIP_STATES)}"
        )
    if decision == lifecycles.PROPOSED:
        raise ReviewError(
            "PROPOSED is what the builder writes, not a decision a reviewer can make. "
            "Use REQUIRES_REVIEW to send a study back for another look"
        )

    network = await _network(db, network_id)
    if lifecycles.is_frozen_for_edit(network.ratification_status):
        status = network.ratification_status
        raise ReviewError(
            f"network {network_id!r} is {status}; changing its membership would alter the "
            f"evidence set — {lifecycles.frozen_explanation(status)}. Reopen it to DRAFT, "
            "recording why, before changing what it contains"
        )

    row = await _membership(db, network_id, study_id)
    before = row.membership_status
    before_view = await membership_preview(db, network_id=network_id)

    # Raises when the edge is illegal, and when EXCLUDED arrives with no reason.
    lifecycles.assert_transition("membership", before, decision, reason=reason)
    row.membership_status = decision
    row.exclusion_reason = reason.strip() if reason and reason.strip() else None
    row.review_note = note
    row.decided_by = decided_by.strip()
    row.decided_at = utcnow()

    await db.flush()
    after_view = await membership_preview(db, network_id=network_id)

    await write_audit(
        db, role="REVIEWER", event="NETWORK_MEMBERSHIP_DECIDED",
        context={
            "network_id": network_id,
            "study_id": study_id,
            "protocol_id": row.protocol_id,
            "from": before,
            "to": decision,
            "reason": row.exclusion_reason,
            "note": note,
            "decided_by": row.decided_by,
            "studies_consulted_before": before_view["studies_consulted"],
            "studies_consulted_after": after_view["studies_consulted"],
            "authentication": "RECORDED_NOT_AUTHENTICATED",
        },
        commit=False,
    )
    await db.commit()

    narrowed = (
        after_view["studies_consulted"] < before_view["studies_consulted"]
    )
    return {
        "network_id": network_id,
        "study_id": study_id,
        "membership_status": row.membership_status,
        "exclusion_reason": row.exclusion_reason,
        "review_note": row.review_note,
        "decided_by": row.decided_by,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "before": before,
        "membership": after_view,
        # The headline a UI must show. A decision that quietly removed 36 studies from
        # every future resolve is exactly the kind of change that should not be discovered
        # later from a study count.
        "narrowed_the_evidence_set": narrowed,
        "narrowing_warning": (
            f"This decision changed what a resolve consults from "
            f"{before_view['studies_consulted']} studies to "
            f"{after_view['studies_consulted']}."
            if narrowed else None
        ),
    }


# =====================================================================================
# The combined gate
# =====================================================================================
async def governance_gate(db: AsyncSession, *, network_id: str) -> dict:
    """May this network be computed on in ``GOVERNED`` mode, and if not, why?

    Returns the *blocking* status from ``evidence.statuses`` rather than a bare boolean, so
    the refusal is reportable: *"the network is ratified but the protocol's statistical
    approval was revoked"* is a usable answer, ``False`` is not.

    ``blocking_status`` is ``None`` when the gate is open. It deliberately does NOT return
    ``GOVERNED_SYNTHESIS_COMPLETED`` in that case — that status asserts a computation was
    performed under an approved protocol, and this function has computed nothing. Handing
    a result status back from a permission check is how a gate ends up mistaken for an
    answer.

    ``EXPLORATORY`` execution deliberately never calls this — which is what lets Phase 6
    be built and tested before any approver exists.
    """
    network = await _network(db, network_id)
    protocol_id = network.protocol_id
    ratified = lifecycles.is_computable(network.ratification_status)

    protocol_state: str | None = None
    if protocol_id and protocols.is_defined(protocol_id):
        rows = await _approval_rows(db, protocol_id)
        protocol_state = approvals.derived_status(rows, protocols.content_hash(protocol_id))

    # Protocol first: an unapproved protocol means the analysis has no agreed method, which
    # is a more fundamental objection than the evidence set not yet being signed off.
    blocking: str | None
    if protocol_state == approvals.REJECTED:
        blocking = statuses.PROTOCOL_REJECTED
    elif protocol_state != approvals.APPROVED:
        blocking = statuses.PROTOCOL_PENDING_APPROVAL
    elif not ratified:
        blocking = statuses.NETWORK_NOT_RATIFIED
    else:
        blocking = None

    return {
        "network_id": network_id,
        "protocol_id": protocol_id,
        "ratification_status": network.ratification_status,
        "protocol_status": protocol_state,
        "may_compute_governed": blocking is None,
        "blocking_status": blocking,
        "reason": (
            "Protocol approved for its current content and network ratified; GOVERNED "
            "execution is permitted."
            if blocking is None else statuses.describe(blocking)
        ),
        # Answered here because this call is already the review UI's source for
        # `ratification_status`, and because the alternative is the client keeping its own
        # copy of the ratification edges. `allowed_transitions` is deliberately NOT
        # returned: it lists `SUPERSEDED`, which no route implements, and a UI that
        # rendered a button per transition would offer a move that 404s.
        "can_reopen": lifecycles.can_transition(
            RATIFICATION, network.ratification_status, lifecycles.DRAFT
        ),
    }
