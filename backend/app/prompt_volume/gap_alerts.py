"""Coverage-gap alert lifecycle planning (FR-116.3 enhancement).

Turns high-volume Prompt Volume coverage gaps into TRACKABLE, de-duplicated alerts with an
OPEN -> RESOLVED lifecycle. Anti-fatigue by design:

  * a NEW alert is raised only the FIRST time a topic appears,
  * a recurring gap UPDATES in place (never a duplicate alert),
  * an alert AUTO-RESOLVES once the Approved Question Bank covers the topic,
  * a manually DISMISSED gap stays quiet even if it recurs.

This module is PURE (no DB / no I/O) so the lifecycle is unit-testable; the service layer loads
state, calls :func:`plan_sync`, and applies the returned plan. Mirrors the dependency-light,
plain-dict style of ``gap.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.prompt_volume.gap import normalize, similarity, tokens

STATUS_OPEN = "OPEN"
STATUS_RESOLVED = "RESOLVED"
STATUS_DISMISSED = "DISMISSED"

REASON_COVERED = "COVERED"


def topic_key(label: str) -> str:
    """Stable dedupe key for a gap topic across uploads (normalized phrasing)."""
    return normalize(label)


def is_covered(label: str, question_token_sets: list[set[str]], threshold: float) -> bool:
    """True if the topic now matches an approved question (i.e. the bank covers it)."""
    lt = tokens(label)
    if not lt:
        return False
    return any(similarity(lt, qt) >= threshold for qt in question_token_sets)


@dataclass
class GapAlertPlan:
    """Actions the service should apply. ``create`` items are full field dicts; ``resolve`` is
    a list of topic_keys; the rest carry the refreshed fields keyed by topic_key."""
    create: list[dict] = field(default_factory=list)
    update: list[dict] = field(default_factory=list)   # OPEN -> refresh
    reopen: list[dict] = field(default_factory=list)    # RESOLVED -> OPEN (gap returned)
    touch: list[dict] = field(default_factory=list)     # DISMISSED -> refresh last_seen only
    resolve: list[str] = field(default_factory=list)    # OPEN -> RESOLVED (now covered)


def _fields(gap: dict, key: str, batch_id: str) -> dict:
    return {
        "topic_key": key,
        "label": gap.get("label") or "",
        "question": gap.get("question"),
        "therapeutic_area": gap.get("therapeutic_area"),
        "competitor": gap.get("competitor"),
        "combined_volume": gap.get("combined_volume") or 0,
        "opportunity_score": gap.get("opportunity_score") or 0.0,
        "query_count": gap.get("query_count") or 0,
        "batch_id": batch_id,
    }


def plan_sync(
    alertable: list[dict],
    existing_by_key: dict[str, dict],
    covered_keys: set[str],
    *,
    batch_id: str,
) -> GapAlertPlan:
    """Reconcile the current alertable gaps against existing alerts.

    ``alertable``      : current high-volume gaps eligible to be alerts (already capped/sorted).
    ``existing_by_key``: topic_key -> {"status": ..., "label": ...} for every existing alert.
    ``covered_keys``   : topic_keys of existing OPEN alerts the bank now covers (-> auto-resolve).
    """
    plan = GapAlertPlan()
    current_keys: set[str] = set()

    for gap in alertable:
        key = topic_key(gap.get("label") or "")
        if not key or key in current_keys:
            continue  # skip empty keys and within-batch duplicates
        current_keys.add(key)
        fields = _fields(gap, key, batch_id)
        prior = existing_by_key.get(key)
        if prior is None:
            plan.create.append(fields)
        elif prior["status"] == STATUS_OPEN:
            plan.update.append(fields)
        elif prior["status"] == STATUS_RESOLVED:
            plan.reopen.append(fields)          # a previously-closed gap has returned
        elif prior["status"] == STATUS_DISMISSED:
            plan.touch.append(fields)           # analyst muted it — refresh but stay quiet

    # Auto-resolve OPEN alerts the bank now covers (independent of this upload's contents).
    for key in covered_keys:
        prior = existing_by_key.get(key)
        if prior and prior["status"] == STATUS_OPEN and key not in current_keys:
            plan.resolve.append(key)

    return plan
