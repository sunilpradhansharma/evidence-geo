"""Alert rule evaluation (FR-405)."""
import uuid

from app.models.alert import Alert

LOW_SENTIMENT_THRESHOLD = -0.3
COMPETITOR_ADVANTAGE_DELTA = 0.4


def evaluate_alerts(
    *,
    response_id: str,
    score_id: str,
    sentiment_score: float | None,
    competitive_position: str | None,
    brand_mentions: list[dict] | None,
    focus_brand: str,
) -> list[Alert]:
    """Return Alert rows for any triggered rules (FR-405)."""
    alerts: list[Alert] = []

    if sentiment_score is not None and sentiment_score < LOW_SENTIMENT_THRESHOLD:
        alerts.append(_mk(response_id, score_id, "LOW_SENTIMENT",
                          f"sentiment {sentiment_score} < {LOW_SENTIMENT_THRESHOLD}"))

    if competitive_position == "NOT_RECOMMENDED":
        alerts.append(_mk(response_id, score_id, "NOT_RECOMMENDED",
                          "focus brand classified NOT_RECOMMENDED"))

    # Competitor advantage: a competitor mentioned with materially higher sentiment.
    if brand_mentions and sentiment_score is not None:
        for m in brand_mentions:
            name = (m.get("brand") or m.get("name") or "").strip()
            comp_sent = m.get("sentiment")
            is_competitor = m.get("is_competitor", False)
            if not name or comp_sent is None or name.lower() == focus_brand.lower():
                continue
            if is_competitor and (comp_sent - sentiment_score) >= COMPETITOR_ADVANTAGE_DELTA:
                alerts.append(_mk(
                    response_id, score_id, "COMPETITOR_ADVANTAGE",
                    f"{name} sentiment {comp_sent} exceeds {focus_brand} {sentiment_score} "
                    f"by >= {COMPETITOR_ADVANTAGE_DELTA}",
                ))
    return alerts


def _mk(response_id: str, score_id: str, rule: str, detail: str) -> Alert:
    return Alert(
        alert_id=str(uuid.uuid4()),
        score_id=score_id,
        response_id=response_id,
        rule_triggered=rule,
        detail=detail,
    )
