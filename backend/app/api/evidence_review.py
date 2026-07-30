"""Medical + statistical review surface API (X1).

Three governance objects, deliberately separate endpoints:

* ``/evidence-review/protocols`` — approve, reject or revoke analysis methodology. Each
  decision is recorded against the protocol's DERIVED content hash. **No endpoint accepts
  a hash**: letting a client name the content it approves would allow sign-off on
  something other than what is on disk.
* ``/evidence-review/networks``  — the two-stage ratification transitions. Approving the
  medical stage advances to statistical review; it does not ratify.
* ``/evidence-review/studies``   — the curator's surface: which studies a network is
  waiting on, whether each still reproduces from its retained source, and the
  confirmation that a person checked it. **This is a data-accuracy step, not a clinical
  one** — its audit entries are written as ``CURATOR`` rather than ``REVIEWER`` — and it
  gates something the other two do not. Evidence gathering skips an unverified study even
  in EXPLORATORY mode, so a corpus nobody has curated yields no number at all, approved
  protocol or otherwise.
* ``/evidence-review/drug-facts`` — the same curator surface for regulatory labels. It
  gates Phase 7's approval and safety questions and Phase 8's approval, safety-warning and
  mechanism claims, all of which read **verified labels only**.
* ``/evidence-review/networks/{id}/memberships`` — Lifecycle 2. Which studies a reviewer
  has included in or excluded from one network. Separate from verification on purpose: a
  study can be correctly extracted (Lifecycle 1) and still not belong *here*.

``GET /networks/{id}/gate`` is the combined check Phase 6 consumes to decide between
``EXPLORATORY`` and ``GOVERNED`` execution. It reports the *blocking* status, so a refusal
explains itself rather than returning a bare boolean.

There is no authentication here yet — RBAC was removed from this tree (see BR-013's
findings doc, same dependency as BR-008a). ``reviewer_id`` is therefore *recorded* but not
*authenticated*, and the audit trail says who claimed to act, not who provably did. That
is a real limitation, not an oversight: the approval model and its invariants are testable
today, and enforcement attaches to these same routes once roles return.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.evidence import approvals, lifecycles, protocols
from app.models.database import get_db
from app.services import drug_fact_curation_service as fact_curation
from app.services import evidence_ingestion_service as ingestion
from app.services import evidence_review_service as svc
from app.services import study_curation_service as curation

router = APIRouter(prefix="/evidence-review", tags=["evidence-review"])


class ProtocolDecisionIn(BaseModel):
    approval_role: str = Field(description="MEDICAL or STATISTICAL")
    decision: str = Field(description="APPROVED or REJECTED")
    reviewer_id: str
    review_note: str | None = Field(
        default=None, description="Required when rejecting."
    )


class RevocationIn(BaseModel):
    approval_role: str
    revoked_by: str
    revocation_reason: str = Field(description="Required. A silent withdrawal is a mistake.")


class NetworkReviewIn(BaseModel):
    reviewer: str
    approve: bool
    note: str | None = Field(default=None, description="Required when rejecting.")


class SubmissionIn(BaseModel):
    submitted_by: str


class ReopenIn(BaseModel):
    reopened_by: str = Field(description="Recorded, not authenticated.")
    reason: str = Field(
        min_length=1,
        description=(
            "Required. Reopening withdraws a review that already happened, and an "
            "unexplained withdrawal cannot be told apart from an accident."
        ),
    )


class CuratorCheckIn(BaseModel):
    verified_by: str = Field(
        description="Who performed the check. Recorded, not authenticated."
    )
    note: str | None = Field(
        default=None, description="What was checked, in the curator's own words."
    )


class RejectionIn(BaseModel):
    rejected_by: str = Field(
        description="Who rejected it. Recorded, not authenticated."
    )
    reason: str = Field(
        min_length=1,
        description=(
            "Required. Rejection removes evidence from every network, and an unexplained "
            "removal cannot be told apart from an accident."
        ),
    )


class MembershipDecisionIn(BaseModel):
    decision: str = Field(
        description="INCLUDED, EXCLUDED or REQUIRES_REVIEW."
    )
    decided_by: str = Field(description="Recorded, not authenticated.")
    reason: str | None = Field(
        default=None, description="Required when excluding."
    )
    note: str | None = Field(default=None)


def _bad_request(e: Exception) -> HTTPException:
    """Service and lifecycle violations are client errors, not server faults.

    An illegal transition or a missing reason means the request was wrong, so it returns
    400 with the explanation the service produced rather than a 500 that hides it.
    """
    return HTTPException(status_code=400, detail=str(e))


# --- protocols -------------------------------------------------------------------------
@router.get("/protocols")
async def list_protocols(db: AsyncSession = Depends(get_db)):
    """Every defined protocol with its derived approval status.

    The review queue: anything not ``APPROVED`` names the roles still owing a decision,
    and ``SUPERSEDED`` flags a definition edited since it was signed off.
    """
    return [await svc.protocol_status(db, pid) for pid in protocols.protocol_ids()]


@router.get("/protocols/{protocol_id}")
async def get_protocol(protocol_id: str, db: AsyncSession = Depends(get_db)):
    """One protocol: its full methodology, current content hash and derived status."""
    definition = protocols.protocol(protocol_id)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Unknown protocol {protocol_id!r}")
    state = await svc.protocol_status(db, protocol_id)
    return {**state, "definition": definition}


@router.post("/protocols/{protocol_id}/decisions")
async def record_decision(
    protocol_id: str, body: ProtocolDecisionIn, db: AsyncSession = Depends(get_db)
):
    """Record one role's decision against the protocol's current content.

    The hash is derived server-side. Recording a decision does not modify the definition,
    so it cannot invalidate the approval it is granting.
    """
    try:
        row = await svc.record_protocol_decision(
            db, protocol_id=protocol_id, approval_role=body.approval_role,
            decision=body.decision, reviewer_id=body.reviewer_id,
            review_note=body.review_note,
        )
    except svc.ReviewError as e:
        raise _bad_request(e) from e
    return {
        "approval_id": row.approval_id,
        "content_hash": row.content_hash,
        **await svc.protocol_status(db, protocol_id),
    }


@router.post("/protocols/{protocol_id}/revocations")
async def revoke_decision(
    protocol_id: str, body: RevocationIn, db: AsyncSession = Depends(get_db)
):
    """Withdraw one role's active approval. The row is retained, not deleted."""
    try:
        await svc.revoke_protocol_approval(
            db, protocol_id=protocol_id, approval_role=body.approval_role,
            revoked_by=body.revoked_by, revocation_reason=body.revocation_reason,
        )
    except svc.ReviewError as e:
        raise _bad_request(e) from e
    return await svc.protocol_status(db, protocol_id)


# --- networks --------------------------------------------------------------------------
@router.post("/networks/{network_id}/submit")
async def submit_network(
    network_id: str, body: SubmissionIn, db: AsyncSession = Depends(get_db)
):
    """DRAFT -> PENDING_MEDICAL_REVIEW."""
    try:
        network = await svc.submit_for_medical_review(
            db, network_id=network_id, submitted_by=body.submitted_by
        )
    except (svc.ReviewError, lifecycles.LifecycleError) as e:
        raise _bad_request(e) from e
    return _network_out(network)


@router.post("/networks/{network_id}/medical-review")
async def medical_review(
    network_id: str, body: NetworkReviewIn, db: AsyncSession = Depends(get_db)
):
    """Stage 1. Approving advances to statistical review — it does not ratify."""
    try:
        network = await svc.record_medical_review(
            db, network_id=network_id, reviewer=body.reviewer,
            approve=body.approve, note=body.note,
        )
    except (svc.ReviewError, lifecycles.LifecycleError) as e:
        raise _bad_request(e) from e
    return _network_out(network)


@router.post("/networks/{network_id}/statistical-review")
async def statistical_review(
    network_id: str, body: NetworkReviewIn, db: AsyncSession = Depends(get_db)
):
    """Stage 2. Approving here is what ratifies the network."""
    try:
        network = await svc.record_statistical_review(
            db, network_id=network_id, reviewer=body.reviewer,
            approve=body.approve, note=body.note,
        )
    except (svc.ReviewError, lifecycles.LifecycleError) as e:
        raise _bad_request(e) from e
    return _network_out(network)


@router.post("/networks/{network_id}/reopen")
async def reopen_network(
    network_id: str, body: ReopenIn, db: AsyncSession = Depends(get_db)
):
    """Any frozen state -> DRAFT, so the evidence set can be changed again.

    The exit the other refusals kept naming. ``build_network`` and ``decide_membership``
    both refuse on a network that is ratified or mid-review and tell the operator to
    supersede it; until this route existed there was nothing behind that instruction, and
    ``_network_out`` was publishing ``allowed_transitions`` the API could not perform.

    **It is not supersede.** No snapshot of the approved evidence set is kept — see
    ``reopen_network`` in the service for why the two-row version of this is a larger job
    than it looks. Reopening suits an approval that should not have happened; retaining an
    approved set while moving on does not exist yet.
    """
    try:
        network = await svc.reopen_network(
            db, network_id=network_id, reopened_by=body.reopened_by, reason=body.reason,
        )
    except (svc.ReviewError, lifecycles.LifecycleError) as e:
        raise _bad_request(e) from e
    return _network_out(network)


@router.get("/networks/{network_id}/gate")
async def network_gate(network_id: str, db: AsyncSession = Depends(get_db)):
    """May this network be computed on in GOVERNED mode, and if not, exactly why?"""
    try:
        return await svc.governance_gate(db, network_id=network_id)
    except svc.ReviewError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


# --- studies ---------------------------------------------------------------------------
@router.get("/studies")
async def curation_queue(
    network_id: str | None = None,
    indication: str | None = None,
    verification_status: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Studies awaiting a curator. Pass ``network_id`` to see only what blocks that network.

    Scoping delegates to the resolver's own rule, so the queue is exactly the corpus a
    resolve would consult — including the case where no membership is INCLUDED, which means
    membership narrows nothing rather than that nothing qualifies.
    """
    try:
        return await curation.curation_queue(
            db, network_id=network_id, indication=indication,
            verification_status=verification_status, limit=limit,
        )
    except curation.CurationError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/studies/{study_id}/source-check")
async def study_source_check(study_id: str, db: AsyncSession = Depends(get_db)):
    """Re-derive this study from its retained payload and diff it against the stored rows.

    Read-only, and no network call: every byte compared is already on disk, so a
    difference is always attributable to our parser rather than to the registry having
    changed underneath us.

    A clean result means the extraction is **reproducible**, not that it is **correct**.
    The response carries the source URL and the parser's own mismatch flags for that
    reason — they are what send a curator to the registry record itself.
    """
    try:
        return await curation.rederivation_diff(db, study_id)
    except curation.CurationError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/studies/{study_id}/curator-check")
async def record_curator_check(
    study_id: str, body: CuratorCheckIn, db: AsyncSession = Depends(get_db)
):
    """Confirm a person checked this extraction against its source, advancing it to VERIFIED.

    Refused while the study does not reproduce from its retained payload. A VERIFIED row
    is skipped by ``ingest_study``, so certifying a stale extraction would put it beyond
    the reach of the ordinary re-parse.
    """
    try:
        return await curation.record_curator_check(
            db, study_id=study_id, verified_by=body.verified_by, note=body.note,
        )
    except curation.CurationError as e:
        raise _bad_request(e) from e


@router.post("/studies/{study_id}/reject")
async def reject_study(
    study_id: str, body: RejectionIn, db: AsyncSession = Depends(get_db)
):
    """Record that an extraction is wrong and must not be used. A reason is required.

    The other half of the verification lifecycle. Without it a curator who found a bad
    extraction could only leave it unverified — indistinguishable from one nobody has
    opened yet, which is how a known-bad row sits in a queue looking like backlog.
    """
    try:
        study = await ingestion.reject_study(
            db, study_id, rejected_by=body.rejected_by, reason=body.reason,
        )
    except ingestion.IngestionError as e:
        raise _bad_request(e) from e
    return {
        "study_id": study.study_id,
        "verification_status": study.verification_status,
        "rejection_reason": study.rejection_reason,
        "rejected_by": study.verified_by,
    }


# --- network membership (Lifecycle 2) ----------------------------------------------------
@router.get("/networks/{network_id}/memberships")
async def membership_preview(network_id: str, db: AsyncSession = Depends(get_db)):
    """What this network's membership set currently does to a resolve. Reads only.

    **Read this before the first inclusion.** With nothing INCLUDED, membership narrows
    nothing and a resolve consults every proposed study. Including one study binds the
    filter and the rest stop contributing, so the consequence is worth seeing beforehand
    rather than inferring it later from a study count.
    """
    try:
        return await svc.membership_preview(db, network_id=network_id)
    except svc.ReviewError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/networks/{network_id}/memberships/{study_id}/decision")
async def decide_membership(
    network_id: str,
    study_id: str,
    body: MembershipDecisionIn,
    db: AsyncSession = Depends(get_db),
):
    """Include, exclude or flag one study for one network. Excluding requires a reason.

    Distinct from study verification: *"is this extraction accurate?"* is universal and
    *"does this study belong in THIS network?"* is per analysis, so the same verified study
    can be included in an ACR50 network and excluded from ACR20 at once.

    The response reports how many studies a resolve consulted before and after, because the
    first inclusion on a network narrows the evidence set to that one study.
    """
    try:
        return await svc.decide_membership(
            db,
            network_id=network_id,
            study_id=study_id,
            decision=body.decision,
            decided_by=body.decided_by,
            reason=body.reason,
            note=body.note,
        )
    except svc.ReviewError as e:
        raise _bad_request(e) from e
    except lifecycles.LifecycleError as e:
        raise _bad_request(e) from e


# --- drug facts --------------------------------------------------------------------------
@router.get("/drug-facts")
async def drug_fact_queue(
    brand: str | None = None,
    verification_status: str | None = None,
    include_superseded: bool = False,
    limit: int = Query(default=200, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Regulatory labels awaiting a curator, ranked by whether verifying one changes an answer.

    ``approval_blocked`` is the figure to read: a label whose indication list was never
    structured cannot answer an approval claim **however carefully it is verified**, so
    those rows are not curator backlog.
    """
    return await fact_curation.curation_queue(
        db, brand=brand, verification_status=verification_status,
        include_superseded=include_superseded, limit=limit,
    )


@router.get("/drug-facts/{fact_id}/source-check")
async def drug_fact_source_check(fact_id: str, db: AsyncSession = Depends(get_db)):
    """Re-derive this label from its retained seed and diff it against the stored fact.

    Narrower than the study equivalent, and the response says so: the retained payload is
    the normalised label seed rather than the SPL document, so a clean result proves our
    **mapping** reproduces, not that the label was read correctly. The prescribing
    information URL is returned for exactly that reason.
    """
    try:
        return await fact_curation.rederivation_diff(db, fact_id)
    except curation.CurationError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/drug-facts/{fact_id}/curator-check")
async def record_drug_fact_check(
    fact_id: str, body: CuratorCheckIn, db: AsyncSession = Depends(get_db)
):
    """Confirm a person checked this label extraction, advancing it to VERIFIED.

    This is the gate three phases wait on: question generation, approval/safety/mechanism
    claim grading and the misinformation-risk implication all read verified labels only.
    """
    try:
        return await fact_curation.record_curator_check(
            db, fact_id=fact_id, verified_by=body.verified_by, note=body.note,
        )
    except curation.CurationError as e:
        raise _bad_request(e) from e
    except ingestion.IngestionError as e:
        raise _bad_request(e) from e


@router.post("/drug-facts/{fact_id}/reject")
async def reject_drug_fact(
    fact_id: str, body: RejectionIn, db: AsyncSession = Depends(get_db)
):
    """Record that a label extraction is wrong and must not be used. Requires a reason."""
    try:
        fact = await ingestion.reject_drug_fact(
            db, fact_id, rejected_by=body.rejected_by, reason=body.reason,
        )
    except ingestion.IngestionError as e:
        raise _bad_request(e) from e
    return {
        "fact_id": fact.fact_id,
        "brand": fact.brand,
        "verification_status": fact.verification_status,
        "rejection_reason": fact.rejection_reason,
        "rejected_by": fact.verified_by,
    }


@router.get("/roles")
async def review_roles():
    """The vocabulary a review UI needs, served from one place.

    Roles and decisions are returned rather than hardcoded client-side so adding a third
    required approval role tightens the UI and the gate together.
    """
    return {
        "approval_roles": list(approvals.APPROVAL_ROLES),
        "decisions": list(approvals.DECISIONS),
        "derived_statuses": list(approvals.DERIVED_STATUSES),
        "ratification_states": list(lifecycles.RATIFICATION_STATES),
        "verification_states": list(lifecycles.VERIFICATION_STATES),
        # PROPOSED is excluded deliberately: it is what the builder writes, not a decision
        # a reviewer can make. Offering it would invite un-deciding a study by re-proposing
        # it, which loses who decided and why.
        "membership_decisions": [
            s for s in lifecycles.MEMBERSHIP_STATES if s != lifecycles.PROPOSED
        ],
    }


def _network_out(network) -> dict:
    return {
        "network_id": network.network_id,
        "protocol_id": network.protocol_id,
        "ratification_status": network.ratification_status,
        "allowed_transitions": list(
            lifecycles.allowed_transitions("ratification", network.ratification_status)
        ),
        "is_computable": lifecycles.is_computable(network.ratification_status),
        "medical_reviewer": network.medical_reviewer,
        "medical_reviewed_at": network.medical_reviewed_at,
        "statistical_reviewer": network.statistical_reviewer,
        "statistical_reviewed_at": network.statistical_reviewed_at,
        "rejection_reason": network.rejection_reason,
    }
