"""Protocol approval state, derived from approval rows (X1).

Approval status is **derived, never stored** on the definition. Given the protocol's
current ``content_hash`` and its approval rows, the status follows — which is what makes
"editing a protocol invalidates its prior approvals" automatic rather than a process step
someone has to remember to perform.

Two roles sign off **independently and are independently revocable**. Medical review asks
*"is this clinically sensible?"*; statistical review asks *"is this analysis valid?"*
Neither implies the other, and neither can stand in for the other, so a single
``approved`` flag could not represent the state.

Everything here is a pure function over duck-typed rows carrying ``approval_role``,
``decision``, ``content_hash`` and ``revoked_at``. No database, no imports from
``app.models`` — so the precedence rules are testable in isolation, and the model is free
to import these constants without a cycle.
"""
from __future__ import annotations

from datetime import datetime, timezone

# --- roles -----------------------------------------------------------------------------
MEDICAL = "MEDICAL"
STATISTICAL = "STATISTICAL"

# Both are REQUIRED for a protocol to reach APPROVED. The tuple is the definition of
# "fully approved" — adding a third role here automatically tightens every gate.
APPROVAL_ROLES = (MEDICAL, STATISTICAL)

# --- decisions recorded on a row -------------------------------------------------------
APPROVED = "APPROVED"
REJECTED = "REJECTED"

DECISIONS = (APPROVED, REJECTED)

# --- derived statuses ------------------------------------------------------------------
PENDING_APPROVAL = "PENDING_APPROVAL"
REVOKED = "REVOKED"
SUPERSEDED = "SUPERSEDED"

DERIVED_STATUSES = (PENDING_APPROVAL, APPROVED, REJECTED, REVOKED, SUPERSEDED)

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _reviewed_at(row) -> datetime:
    """Sort key tolerant of nulls and of naive datetimes from older rows."""
    value = getattr(row, "reviewed_at", None)
    if value is None:
        return _EPOCH
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def role_status(rows, role: str, current_hash: str | None) -> str:
    """This role's status for the protocol's *current* content hash.

    * ``APPROVED`` / ``REJECTED`` — an active decision on the current content.
    * ``REVOKED``    — the decision on the current content was withdrawn.
    * ``SUPERSEDED`` — the latest decision was made against different content, so the
      protocol has been edited since. The approval does not carry over.
    * ``PENDING_APPROVAL`` — this role has never decided.
    """
    mine = [r for r in rows if getattr(r, "approval_role", None) == role]
    if not mine:
        return PENDING_APPROVAL

    on_current = [r for r in mine if getattr(r, "content_hash", None) == current_hash]
    if not on_current:
        # Decisions exist, but all against content that has since changed.
        return SUPERSEDED

    latest = max(on_current, key=_reviewed_at)
    if getattr(latest, "revoked_at", None) is not None:
        return REVOKED
    decision = getattr(latest, "decision", None)
    return decision if decision in DECISIONS else PENDING_APPROVAL


def role_statuses(rows, current_hash: str | None) -> dict[str, str]:
    """``{role: status}`` for every required approval role."""
    return {role: role_status(rows, role, current_hash) for role in APPROVAL_ROLES}


def derived_status(rows, current_hash: str | None) -> str:
    """The protocol's overall approval status.

    Precedence is deliberate and ordered by consequence:

    1. ``REJECTED``   — one role rejecting is decisive; the other approving cannot
       overrule it. Anything else would let a rejection be voted away.
    2. ``APPROVED``   — only when *every* required role has an active approval on the
       current content.
    3. ``SUPERSEDED`` — an approval existed but the definition has since been edited.
       Reported ahead of ``REVOKED`` and ``PENDING`` because it names the cause.
    4. ``REVOKED``    — an approval was deliberately withdrawn.
    5. ``PENDING_APPROVAL`` — nothing decided yet, or only some roles have signed off.
    """
    statuses = role_statuses(rows, current_hash)
    values = tuple(statuses.values())

    if REJECTED in values:
        return REJECTED
    if all(v == APPROVED for v in values):
        return APPROVED
    if SUPERSEDED in values:
        return SUPERSEDED
    if REVOKED in values:
        return REVOKED
    return PENDING_APPROVAL


def is_approved(rows, current_hash: str | None) -> bool:
    """True only when the protocol is fully approved for its current content.

    This is the protocol half of the ``GOVERNED`` execution gate. ``EXPLORATORY``
    execution deliberately never consults it, which is what lets Phase 6 be built and
    tested before any statistician exists to approve anything.
    """
    return derived_status(rows, current_hash) == APPROVED


def missing_roles(rows, current_hash: str | None) -> tuple[str, ...]:
    """Roles still owing an active approval — what a review queue should ask for."""
    statuses = role_statuses(rows, current_hash)
    return tuple(role for role, status in statuses.items() if status != APPROVED)
