"""LLM tagging of social-listening posts — brand/TA/domain + sentiment + topic + AE.

Reuses the configured scoring model via insights.llm.chat_json (same provider/creds that
power response scoring) and the brand/TA vocabulary from harvest.classify.build_vocab so
this module stays content-agnostic (SE-007). Batched to bound cost.

Sentiment is a float in [-1, 1]; topic is a short free-text theme phrase (clustered later
by the analytics service). adverse_event is a fail-closed pharmacovigilance signal. The
model also detects the source language and returns an English translation (``text_en``) so
non-English posts/comments are searchable and scorable in English; because the pipeline
redacts BEFORE calling this module, translation only ever sees already-scrubbed text.

The same batched classifier is reused for comments (they are social text too) — only the
fields the comment pipeline needs (sentiment/label, language, text_en, adverse_event) are
consumed there.
"""
from app.harvest.classify import build_vocab  # re-exported for the social pipeline
from app.insights.llm import chat_json
from app.utils.logging import get_logger

logger = get_logger("social.classify")

VALID_DOMAINS = {"Efficacy", "Safety", "Access", "Comparative", "General"}

__all__ = ["build_vocab", "classify_posts", "normalize_tags", "VALID_DOMAINS"]


_SYSTEM = (
    "You are a pharmaceutical social-listening analyst. You receive REAL public social "
    "media posts (Reddit/TikTok/Instagram/Facebook/X) about pharmaceutical brands, "
    "therapies, and the health conditions they treat. For EACH post, decide whether it "
    "concerns one of the monitored therapeutic areas/brands below and tag it. Return "
    "STRICT JSON only — no prose.\n\n"
    "Field rules:\n"
    "- relevant: false if the post is NOT about any monitored brand or area.\n"
    "- brand_focus: the monitored brand the post most concerns (exact name from the list), "
    "or null if none/ambiguous.\n"
    "- therapeutic_area: exactly one of the listed area names, or null.\n"
    "- domain: one of Efficacy, Safety, Access, Comparative, General.\n"
    "- topic: a SHORT (2-5 word) lowercase theme phrase capturing what the post is about "
    "(e.g. 'injection site soreness', 'insurance coverage', 'weight loss progress').\n"
    "- sentiment: a number from -1.0 (very negative) to 1.0 (very positive) reflecting the "
    "author's stance toward the brand/therapy.\n"
    "- adverse_event: true ONLY if the author describes experiencing a specific harm or "
    "side effect from a named drug (a pharmacovigilance signal). General safety discussion "
    "is NOT an adverse event.\n"
    "- language: the source language as a short English name (e.g. 'English', 'Spanish', "
    "'Portuguese', 'French'). Use 'English' when it is already English.\n"
    "- text_en: a faithful English translation of the text. If it is already English, return "
    "it unchanged. Preserve any [redacted] placeholders verbatim and do not add commentary."
)


async def classify_posts(texts: list[str], vocab: str) -> list[dict]:
    """Tag a batch of post texts. Returns a list aligned by index (best-effort)."""
    if not texts:
        return []
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(texts))
    user = (
        f"Monitored therapeutic areas and brands:\n{vocab}\n\n"
        f"Posts:\n{numbered}\n\n"
        "Return a JSON array with one object per post, each having keys: index (int, "
        "matching the number above), relevant (bool), brand_focus, therapeutic_area, "
        "domain, topic, sentiment (number -1..1), adverse_event (bool), language (string), "
        "text_en (string English translation). Use null for unknown string fields."
    )
    try:
        data = await chat_json(_SYSTEM, user, max_tokens=2000)
    except Exception as e:  # noqa: BLE001 — degrade to untagged rather than fail the run
        logger.warning("classify_posts failed: %s", e)
        return [{} for _ in texts]

    if isinstance(data, dict):
        data = data.get("results") or data.get("posts") or data.get("items") or []
    if not isinstance(data, list):
        return [{} for _ in texts]

    by_index: dict[int, dict] = {}
    for obj in data:
        if isinstance(obj, dict) and isinstance(obj.get("index"), int):
            by_index[obj["index"]] = obj
    if not by_index:  # model omitted indices — fall back to positional
        for i, obj in enumerate(data):
            if isinstance(obj, dict):
                by_index[i] = obj
    return [by_index.get(i, {}) for i in range(len(texts))]


def _sentiment_label(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= 0.15:
        return "positive"
    if score <= -0.15:
        return "negative"
    return "neutral"


def normalize_tags(obj: dict, valid_tas: set[str]) -> dict:
    """Clamp raw LLM output to valid enums/ranges for persistence."""
    ta = obj.get("therapeutic_area")
    ta = ta if ta in valid_tas else None
    domain = obj.get("domain")
    domain = domain if domain in VALID_DOMAINS else None
    brand = obj.get("brand_focus") or None
    topic = obj.get("topic")
    topic = topic.strip().lower()[:160] if isinstance(topic, str) and topic.strip() else None
    relevant = bool(obj.get("relevant", False))
    ae = bool(obj.get("adverse_event", False))

    sentiment = obj.get("sentiment")
    try:
        sentiment = max(-1.0, min(1.0, float(sentiment)))
    except (TypeError, ValueError):
        sentiment = None

    lang = obj.get("language")
    lang = lang.strip()[:32] if isinstance(lang, str) and lang.strip() else None
    text_en = obj.get("text_en")
    text_en = text_en.strip() if isinstance(text_en, str) and text_en.strip() else None
    # Translated only when a non-English source language was detected AND we got a rendering.
    is_translated = bool(
        lang and lang.lower() not in {"english", "en", "en-us", "unknown", "und"} and text_en
    )

    return {
        "brand_focus": brand,
        "therapeutic_area": ta,
        "domain": domain,
        "topic": topic,
        "sentiment": sentiment,
        "sentiment_label": _sentiment_label(sentiment),
        "ae_flag": ae,
        "relevant": relevant,
        "language": lang,
        "text_en": text_en,
        "is_translated": is_translated,
    }
