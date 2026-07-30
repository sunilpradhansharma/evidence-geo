"""Source-authority alert rules (FR-706a.4).

Flags a response when the publishers an AI model leaned on are risky:
  • ``ONLY_COMPETITOR_SOURCES``            — every cited domain is competitor-controlled
  • ``COMPETITOR_CONTROLLED_TOP_SOURCE``   — the most-cited domain is competitor-controlled
  • ``UNVERIFIED_TOP_SOURCE``              — the most-cited domain can't be vouched for

"Top source" uses actual citation frequency (``citation_count``), tie-broken by the earliest
citation position then domain name, so ranking is deterministic. Alerts reuse the existing
``Alert`` table (entity_type=SOURCE_AUTHORITY, no score_id) so they surface in the standard
per-response alert panel and the ``alert_only`` filter.
"""
from __future__ import annotations

import uuid

from app.models.alert import ENTITY_SOURCE_AUTHORITY, Alert
from app.models.source_domain import CONTROL_COMPETITOR, UNVERIFIED

RULE_ONLY_COMPETITOR = "ONLY_COMPETITOR_SOURCES"
RULE_COMPETITOR_TOP = "COMPETITOR_CONTROLLED_TOP_SOURCE"
RULE_UNVERIFIED_TOP = "UNVERIFIED_TOP_SOURCE"


def _top_citation(citations: list[dict]) -> dict:
    """Most-cited domain: max citation_count, then earliest position, then domain name."""
    return sorted(
        citations,
        key=lambda c: (
            -int(c.get("citation_count") or 0),
            int(c.get("first_citation_position") or 0),
            str(c.get("authority_domain") or ""),
        ),
    )[0]


def evaluate_source_alerts(*, response_id: str, citations: list[dict]) -> list[Alert]:
    """Return Alert rows for any source-authority rule the response's citations trigger.

    ``citations`` items need: ``authority_domain``, ``control_type``, ``verification``,
    ``citation_count``, ``first_citation_position``.
    """
    if not citations:
        return []

    alerts: list[Alert] = []
    top = _top_citation(citations)

    if all(c.get("control_type") == CONTROL_COMPETITOR for c in citations):
        alerts.append(_mk(
            response_id, RULE_ONLY_COMPETITOR,
            f"All {len(citations)} cited domain(s) are competitor-controlled",
        ))
    if top.get("control_type") == CONTROL_COMPETITOR:
        alerts.append(_mk(
            response_id, RULE_COMPETITOR_TOP,
            f"Top-cited source {top.get('authority_domain')} is competitor-controlled "
            f"({top.get('citation_count')} citation(s))",
        ))
    if top.get("verification") == UNVERIFIED:
        alerts.append(_mk(
            response_id, RULE_UNVERIFIED_TOP,
            f"Top-cited source {top.get('authority_domain')} is unverified "
            f"({top.get('citation_count')} citation(s))",
        ))
    return alerts


def _mk(response_id: str, rule: str, detail: str) -> Alert:
    return Alert(
        alert_id=str(uuid.uuid4()),
        score_id=None,
        response_id=response_id,
        entity_type=ENTITY_SOURCE_AUTHORITY,
        entity_id=response_id,
        rule_triggered=rule,
        detail=detail,
    )
