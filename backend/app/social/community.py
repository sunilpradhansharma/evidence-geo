"""Community-crawl enrichment for patient-community channels (myRAteam / Bezzy RA).

The generic classifier (:mod:`app.social.classify`) tags each captured post with ONE
``brand_focus`` + one ``topic`` + a sentiment — fine for platform brand-chatter, but it
discards most of the signal in a patient-community page, which typically names MANY
treatments and carries rich patient-experience context (concerns, treatment journey,
switching drivers, quality-of-life impact, access barriers) plus the real questions
patients are asking.

This module adds a SECOND, community-tuned LLM pass that runs ONLY for the community-crawl
channels (those carrying ``only_areas`` in social_sources.yaml — myRAteam / Bezzy RA). Per
page it extracts:

  - ``brand_mentions``: EVERY monitored drug/brand named on the page (vocabulary-constrained
    to brands.yaml so the model cannot invent drug names), each with the patient sentiment
    toward it on THIS page plus a short context phrase.
  - ``patient_signals``: ``{concerns, journey_stage, switching_drivers, qol_impacts,
    access_barriers, questions}`` — the qualitative patient-experience read.

Runs on the already-redacted English text, so no raw identifier can leak. Best-effort: any
failure degrades to leaving a post un-enriched rather than failing the ingest. Internal
demo — Legal/Privacy/PV sign-off required before any production use.
"""
import json
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import taxonomy
from app.insights.llm import chat_json
from app.models.social_post import SocialPost
from app.utils.logging import get_logger

logger = get_logger("social.community")

# Dominant patient journey stage (clamped enum; None when not evident on the page).
VALID_STAGES = {
    "newly_diagnosed", "starting_treatment", "switching", "long_term", "tapering",
}

# Per-page caps for the multi-valued lists (keep the payload demo-sized + scannable).
_MAX_LIST = 6
_MAX_QUESTIONS = 5
_MAX_MENTIONS = 20


@lru_cache
def _brand_lookup() -> dict:
    """Lowercased brand/generic alias -> canonical ``{name, generic, company, owner}``.

    Built from brands.yaml so the enrichment stays content-agnostic (SE-007). ``owner`` is
    AbbVie vs Competitor keyed off ``company`` (some entries under ``competitors`` are
    actually AbbVie assets). Brand NAMES are canonical and never collide, so they win;
    ambiguous generics only fill gaps via ``setdefault``. This is the anti-hallucination
    gate: an extracted drug name not present here is DROPPED.
    """
    out: dict[str, dict] = {}
    # Cleared by ``taxonomy.reload()`` — see ``taxonomy._DEPENDENT_CACHES``.
    cfg = taxonomy.config()
    for _ta, block in (cfg.get("therapeutic_areas") or {}).items():
        block = block or {}
        for kind in ("focus_brands", "competitors"):
            for b in block.get(kind, []) or []:
                name = (b.get("name") or "").strip()
                generic = (b.get("generic") or "").strip()
                company = (b.get("company") or "").strip() or None
                owner = "AbbVie" if company and "abbvie" in company.lower() else "Competitor"
                rec = {"name": name, "generic": generic or None,
                       "company": company, "owner": owner}
                if name:
                    out.setdefault(name.lower(), rec)
                if generic:
                    out.setdefault(generic.lower(), rec)
    return out


_SYSTEM = (
    "You are a patient-community insights analyst for a pharmaceutical monitoring system. "
    "You receive REAL text crawled from PUBLIC rheumatoid-arthritis PATIENT COMMUNITY pages "
    "(articles and member posts on myRAteam / Bezzy RA). For EACH page, extract structured "
    "patient-experience signals. Return STRICT JSON only — no prose.\n\n"
    "Field rules (per page):\n"
    "- brand_mentions: list EVERY drug/treatment from the MONITORED LIST below that is named "
    "or clearly discussed on the page. Use the monitored brand name EXACTLY as listed. Do NOT "
    "invent drugs and do NOT include drugs that are not on the monitored list. Each item: "
    "{name, sentiment (a number -1..1 for the patient sentiment toward that drug on THIS "
    "page, or null if mentioned only factually), context (<=120 chars, quoted or paraphrased "
    "from the page)}.\n"
    "- concerns: short phrases for what patients are worried about or asking about.\n"
    "- journey_stage: the dominant patient stage — one of newly_diagnosed, starting_treatment, "
    "switching, long_term, tapering — or null if not evident.\n"
    "- switching_drivers: reasons patients switch or stop a treatment (e.g. efficacy loss, "
    "side effects, cost, access); [] if none.\n"
    "- qol_impacts: quality-of-life / functional impacts mentioned (e.g. pain, fatigue, work, "
    "sleep, mobility, mental health); [] if none.\n"
    "- access_barriers: cost / insurance / affordability / access struggles; [] if none.\n"
    "- questions: the actual questions patients are asking, rewritten as short standalone "
    "questions; [] if none.\n"
    "Keep every list concise (a few items). Use [] for empty lists and null for unknown."
)


async def extract_batch(texts: list[str], vocab: str, *, max_tokens: int = 2600) -> list[dict]:
    """Run the community extraction for a batch of page texts. Aligned by index (best-effort)."""
    if not texts:
        return []
    numbered = "\n\n".join(f"[PAGE {i}]\n{t}" for i, t in enumerate(texts))
    user = (
        f"MONITORED LIST (therapeutic areas, brands, competitors):\n{vocab}\n\n"
        f"Pages:\n{numbered}\n\n"
        "Return a JSON array with one object per page, each having keys: index (int, matching "
        "the PAGE number above), brand_mentions (array), concerns (array of strings), "
        "journey_stage (string or null), switching_drivers (array), qol_impacts (array), "
        "access_barriers (array), questions (array of strings)."
    )
    try:
        data = await chat_json(_SYSTEM, user, max_tokens=max_tokens)
    except Exception as e:  # noqa: BLE001 — degrade to un-enriched rather than fail the run
        logger.warning("community extract_batch failed: %s", e)
        return [{} for _ in texts]

    if isinstance(data, dict):
        data = data.get("results") or data.get("pages") or data.get("items") or []
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


def _clamp_sentiment(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return max(-1.0, min(1.0, round(f, 3)))


def _str_list(value, *, cap: int = _MAX_LIST, item_max: int = 90) -> list[str]:
    """Coerce a model field to a clean, deduped list of short strings."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        s = str(item or "").strip().strip(".,;")
        if len(s) < 2:
            continue
        s = s[:item_max]
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= cap:
            break
    return out


def _norm_mentions(raw) -> list[dict]:
    """Map extracted drug names to the monitored vocabulary (dropping anything unknown)."""
    if not isinstance(raw, list):
        return []
    lookup = _brand_lookup()
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        name = None
        sentiment = None
        context = None
        if isinstance(item, dict):
            name = item.get("name") or item.get("brand") or item.get("drug")
            sentiment = _clamp_sentiment(item.get("sentiment"))
            context = item.get("context") or item.get("quote")
        elif isinstance(item, str):
            name = item
        rec = lookup.get(str(name or "").strip().lower())
        if not rec:  # anti-hallucination: keep only monitored brands/generics
            continue
        canonical = rec["name"] or str(name)
        key = canonical.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "name": canonical,
            "generic": rec["generic"],
            "company": rec["company"],
            "owner": rec["owner"],
            "sentiment": sentiment,
            "context": (str(context).strip()[:160] or None) if context else None,
        })
        if len(out) >= _MAX_MENTIONS:
            break
    return out


def _norm_signals(obj: dict) -> dict:
    """Clamp the qualitative patient-experience fields to safe, capped shapes."""
    stage = obj.get("journey_stage")
    stage = stage if stage in VALID_STAGES else None
    return {
        "concerns": _str_list(obj.get("concerns")),
        "journey_stage": stage,
        "switching_drivers": _str_list(obj.get("switching_drivers")),
        "qol_impacts": _str_list(obj.get("qol_impacts")),
        "access_barriers": _str_list(obj.get("access_barriers")),
        "questions": _str_list(obj.get("questions"), cap=_MAX_QUESTIONS, item_max=160),
    }


def normalize(obj: dict) -> tuple[list[dict], dict]:
    """Return ``(brand_mentions, patient_signals)`` for one page's raw LLM object."""
    if not isinstance(obj, dict):
        return [], _norm_signals({})
    return _norm_mentions(obj.get("brand_mentions")), _norm_signals(obj)


def _has_signal(mentions: list[dict], signals: dict) -> bool:
    """True when the page produced any usable enrichment (skip storing empties)."""
    if mentions:
        return True
    return any(signals.get(k) for k in
               ("concerns", "journey_stage", "switching_drivers",
                "qol_impacts", "access_barriers", "questions"))


async def extract_and_apply(db: AsyncSession, posts: list[SocialPost], *, vocab: str,
                            batch_size: int = 5, max_chars: int = 4000) -> dict:
    """Enrich community-crawl posts in place: set ``brand_mentions`` / ``patient_signals``.

    Batched to bound cost; each page text is truncated to ``max_chars`` for the prompt.
    Commits once at the end. Returns ``{enriched, brand_mentions, questions}`` counts.
    """
    posts = [p for p in posts if p is not None and (p.text or "").strip()]
    if not posts:
        return {"enriched": 0, "brand_mentions": 0, "questions": 0}

    enriched = mention_total = question_total = 0
    for start in range(0, len(posts), max(1, batch_size)):
        chunk = posts[start:start + max(1, batch_size)]
        texts = [(p.text or "")[:max_chars] for p in chunk]
        results = await extract_batch(texts, vocab)
        for post, raw in zip(chunk, results):
            mentions, signals = normalize(raw or {})
            if not _has_signal(mentions, signals):
                continue
            post.brand_mentions = json.dumps(mentions) if mentions else None
            post.patient_signals = json.dumps(signals)
            enriched += 1
            mention_total += len(mentions)
            question_total += len(signals.get("questions") or [])

    await db.commit()
    result = {"enriched": enriched, "brand_mentions": mention_total,
              "questions": question_total}
    logger.info("community enrichment: %s", result)
    return result
