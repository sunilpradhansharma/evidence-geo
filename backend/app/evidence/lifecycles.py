"""The three evidence lifecycles (Phase 2). They are separate on purpose.

A study is **not** universally "ratified". The same correctly-extracted, clinically
valid study can simultaneously be included in an RA ACR50 network, excluded from RA
ACR20, excluded from a TNF-IR subgroup and included in biologic-naive. One row-level
flag cannot express that, and collapsing these would let a per-analysis judgement
masquerade as a universal eligibility decision.

    1. Evidence verification   EXTRACTED -> MAPPED -> VERIFIED / REJECTED
       Row-level, universal.   "Is the source data and mapping accurate?"

    2. Network membership      PROPOSED / INCLUDED / EXCLUDED / REQUIRES_REVIEW
       Per network + protocol. "Is this study appropriate HERE?"

    3. Network ratification    DRAFT -> PENDING_MEDICAL_REVIEW
                                     -> PENDING_STATISTICAL_REVIEW
                                     -> RATIFIED / REJECTED / SUPERSEDED
       Per network.            "Is this network fit to compute on?"

Each machine is expressed as an explicit adjacency map rather than scattered ``if``
checks, so the legal transitions are readable in one place and testable without a
database. ``assert_transition`` raises; ``can_transition`` asks.
"""
from __future__ import annotations

# --- 1. Evidence verification (row-level) --------------------------------------------
EXTRACTED = "EXTRACTED"
MAPPED = "MAPPED"
VERIFIED = "VERIFIED"
REJECTED = "REJECTED"

VERIFICATION_STATES = (EXTRACTED, MAPPED, VERIFIED, REJECTED)

# Rejection is reachable from any live state — a problem can surface at any point. There
# is no edge back out of VERIFIED or REJECTED: re-opening a decided row would rewrite
# history, so a correction creates a new version instead (`version` / `superseded_by`).
_VERIFICATION_EDGES: dict[str, tuple[str, ...]] = {
    EXTRACTED: (MAPPED, REJECTED),
    MAPPED: (VERIFIED, REJECTED),
    VERIFIED: (),
    REJECTED: (),
}

# --- 2. Network membership (per network + protocol) ----------------------------------
PROPOSED = "PROPOSED"
INCLUDED = "INCLUDED"
EXCLUDED = "EXCLUDED"
REQUIRES_REVIEW = "REQUIRES_REVIEW"

MEMBERSHIP_STATES = (PROPOSED, INCLUDED, EXCLUDED, REQUIRES_REVIEW)

# Membership is revisable in both directions: a protocol amendment or a newly surfaced
# mismatch can legitimately move a study in or out, and unlike verification that is a
# re-judgement of fit rather than a rewrite of fact.
_MEMBERSHIP_EDGES: dict[str, tuple[str, ...]] = {
    PROPOSED: (INCLUDED, EXCLUDED, REQUIRES_REVIEW),
    REQUIRES_REVIEW: (INCLUDED, EXCLUDED),
    INCLUDED: (EXCLUDED, REQUIRES_REVIEW),
    EXCLUDED: (INCLUDED, REQUIRES_REVIEW),
}

# --- 3. Network ratification ---------------------------------------------------------
DRAFT = "DRAFT"
PENDING_MEDICAL_REVIEW = "PENDING_MEDICAL_REVIEW"
PENDING_STATISTICAL_REVIEW = "PENDING_STATISTICAL_REVIEW"
RATIFIED = "RATIFIED"
NETWORK_REJECTED = "REJECTED"
SUPERSEDED = "SUPERSEDED"

RATIFICATION_STATES = (
    DRAFT,
    PENDING_MEDICAL_REVIEW,
    PENDING_STATISTICAL_REVIEW,
    RATIFIED,
    NETWORK_REJECTED,
    SUPERSEDED,
)

# RATIFIED is reachable ONLY from PENDING_STATISTICAL_REVIEW, which is reachable only
# from PENDING_MEDICAL_REVIEW. The ordering is the guarantee: there is no edge that lets
# a network reach RATIFIED having seen just one of the two reviews.
_RATIFICATION_EDGES: dict[str, tuple[str, ...]] = {
    DRAFT: (PENDING_MEDICAL_REVIEW, SUPERSEDED),
    PENDING_MEDICAL_REVIEW: (PENDING_STATISTICAL_REVIEW, NETWORK_REJECTED, DRAFT, SUPERSEDED),
    PENDING_STATISTICAL_REVIEW: (RATIFIED, NETWORK_REJECTED, DRAFT, SUPERSEDED),
    RATIFIED: (SUPERSEDED, DRAFT),
    NETWORK_REJECTED: (DRAFT, SUPERSEDED),
    SUPERSEDED: (),
}

_MACHINES = {
    "verification": (_VERIFICATION_EDGES, VERIFICATION_STATES),
    "membership": (_MEMBERSHIP_EDGES, MEMBERSHIP_STATES),
    "ratification": (_RATIFICATION_EDGES, RATIFICATION_STATES),
}


class LifecycleError(ValueError):
    """An illegal lifecycle transition, or a transition missing required justification."""


def _machine(name: str) -> tuple[dict[str, tuple[str, ...]], tuple[str, ...]]:
    try:
        return _MACHINES[name]
    except KeyError:
        raise LifecycleError(
            f"unknown lifecycle {name!r}; expected one of {', '.join(_MACHINES)}"
        ) from None


def can_transition(lifecycle: str, current: str | None, target: str | None) -> bool:
    """True when *target* is a legal next state for *current* in *lifecycle*."""
    edges, _states = _machine(lifecycle)
    return (target or "") in edges.get(current or "", ())


def allowed_transitions(lifecycle: str, current: str | None) -> tuple[str, ...]:
    """Legal next states from *current* — what a UI should offer as actions."""
    edges, _states = _machine(lifecycle)
    return edges.get(current or "", ())


def assert_transition(
    lifecycle: str,
    current: str | None,
    target: str | None,
    *,
    reason: str | None = None,
) -> str:
    """Validate a transition and return *target*, or raise ``LifecycleError``.

    ``reason`` is **required to exclude a study from a network**. An exclusion without a
    recorded justification is the single most damaging silent decision in the whole
    pipeline: it removes evidence from an analysis and leaves no way for a reviewer to
    tell a considered judgement apart from an accident.
    """
    edges, states = _machine(lifecycle)

    if target not in states:
        raise LifecycleError(
            f"{lifecycle}: {target!r} is not a valid state; expected one of {', '.join(states)}"
        )
    if current is not None and current not in states:
        raise LifecycleError(
            f"{lifecycle}: current state {current!r} is not valid; "
            f"expected one of {', '.join(states)}"
        )
    if target not in edges.get(current or "", ()):
        legal = ", ".join(edges.get(current or "", ())) or "(none — terminal state)"
        raise LifecycleError(
            f"{lifecycle}: cannot move {current!r} -> {target!r}. Legal transitions: {legal}"
        )
    if lifecycle == "membership" and target == EXCLUDED and not (reason or "").strip():
        raise LifecycleError(
            "membership: excluding a study from a network requires a reason — an "
            "unexplained exclusion is indistinguishable from a mistake in review"
        )
    return target


def is_terminal(lifecycle: str, state: str | None) -> bool:
    """True when no transition leads out of *state*."""
    return not allowed_transitions(lifecycle, state)


def is_computable(ratification_status: str | None) -> bool:
    """True only for a RATIFIED network — the gate for ``GOVERNED`` execution.

    ``EXPLORATORY`` execution deliberately does not consult this, which is what lets
    Phase 6 be built and tested before any statistician exists to ratify anything.
    """
    return ratification_status == RATIFIED


# A network part-way through review is as untouchable as a ratified one. Rewriting the
# evidence set under a reviewer changes what they are reading, mid-read — which is the same
# harm as changing it after they signed, discovered a step earlier.
#
# This tuple exists because three callers had independently decided what "do not touch"
# means and two of them were wrong: `build_network` and `decide_membership` refused only on
# RATIFIED, while `scripts/reparse_dev_pilot.py` kept its own broader copy. One opinion per
# rule — the callers ask, they do not each remember.
FROZEN_FOR_EDIT = (PENDING_MEDICAL_REVIEW, PENDING_STATISTICAL_REVIEW, RATIFIED)


def is_frozen_for_edit(ratification_status: str | None) -> bool:
    """True when a network's evidence set must not be changed.

    Covers both review stages as well as ``RATIFIED``. Deliberately NOT the inverse of
    :func:`is_computable`: ``DRAFT`` is neither computable nor frozen, and ``SUPERSEDED``
    is not frozen because a retired network is no longer anybody's live evidence set.
    """
    return ratification_status in FROZEN_FOR_EDIT


def frozen_explanation(ratification_status: str | None) -> str:
    """Why this state refuses the edit, in words a refusal message can embed.

    The two cases are genuinely different and a caller that collapsed them would tell a
    reviewer mid-review that they had already approved something.
    """
    if ratification_status == RATIFIED:
        return (
            "a reviewer approved that evidence set, and changing it now would leave the "
            "approval standing over something else"
        )
    return (
        "a reviewer is part-way through reading that evidence set, and changing it now "
        "would move what they are reviewing under them"
    )
