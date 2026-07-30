"""Relevance gate for ad-hoc social-listening searches.

The Social Listening surface lets an analyst type a free-text query that triggers a live
Apify capture. Because captured posts are force-stamped with the typed query as their scope
label (scope isolation), an off-topic query (e.g. "yogurt") would otherwise scrape, tag, and
render unrelated content — wasting Apify + LLM credits and polluting the dashboard.

This module runs ONE cheap LLM classification BEFORE the expensive ingest to decide whether a
query is in scope for a pharmaceutical/medical social-listening tool. It is only invoked for
ad-hoc free-text searches; the configured therapeutic-area dropdown never hits it.

Fails OPEN: if the LLM call errors, we allow the search (consistent with the classifier's
degrade-gracefully behaviour) rather than block legitimate use on a transient outage.
"""
from app.insights.llm import chat_json
from app.utils.logging import get_logger

logger = get_logger("social.guard")

__all__ = ["is_pharma_relevant"]


_SYSTEM = (
    "You are a relevance filter for a PHARMACEUTICAL social-listening tool. Decide whether a "
    "user's search query is in scope for monitoring public health conversations. IN SCOPE: "
    "drugs/medicines (brand or generic), medical conditions and diseases, symptoms, "
    "treatments and therapies, clinical or pharmacovigilance topics, medical devices, and "
    "patient or provider experiences with any of the above. OUT OF SCOPE: unrelated consumer "
    "topics such as food, sports, celebrities, politics, finance, travel, general retail "
    "products, and other non-medical subjects. When a query is ambiguous but plausibly "
    "medical, treat it as in scope. Return STRICT JSON only — no prose."
)


async def is_pharma_relevant(query: str) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for an ad-hoc search query.

    ``allowed`` is True when the query is in scope for a pharma/medical social-listening
    search. ``reason`` is a short human-readable explanation shown to the analyst on a block.
    Fails open (returns ``(True, "")``) if the LLM call fails.
    """
    q = (query or "").strip()
    if not q:
        return False, "Enter a search term."

    user = (
        f'Query: "{q[:200]}"\n\n'
        'Is this query in scope for a pharmaceutical/medical social-listening search? '
        'Return JSON: {"relevant": boolean, "reason": "<one short sentence>"}.'
    )
    try:
        data = await chat_json(_SYSTEM, user, max_tokens=120)
    except Exception as e:  # noqa: BLE001 — never block a search on a transient LLM outage
        logger.warning("relevance gate failed for %r; allowing: %s", q, e)
        return True, ""

    if not isinstance(data, dict):
        return True, ""
    allowed = bool(data.get("relevant"))
    reason = str(data.get("reason") or "").strip()[:200]
    if allowed:
        return True, ""
    return False, reason or "This query doesn't look related to pharma or a therapeutic area."
