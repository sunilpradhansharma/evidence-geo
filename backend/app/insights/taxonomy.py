"""Theme taxonomy discovery (map-reduce over the response corpus).

Map: sample distilled signals (key claims + short snippets) in batches and ask the model to
list candidate themes. Reduce: consolidate all candidates into a compact, deduplicated
taxonomy, each theme carrying a rich keyword list used later for transparent tagging.
"""
import asyncio
import json
from dataclasses import dataclass

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights.llm import chat_json
from app.models.response import Response
from app.models.scoring import ScoringRecord
from app.utils.logging import get_logger

logger = get_logger("insights.taxonomy")

_SCOREABLE = ("SUCCESS", "TRUNCATED")
_MAP_BATCH = 25
_MAP_CONCURRENCY = 5  # bounded fan-out for map calls — fast without bursting Bedrock throttling

_VALID_CATEGORIES = {"Efficacy", "Safety", "Access", "Comparative", "Experience", "Other"}

_MAP_SYSTEM = (
    "You are a research analyst clustering what AI assistants say about pharmaceutical brands. "
    "Given a batch of response excerpts, identify the distinct THEMES present — recurring topics, "
    "concerns, claims, or talking points. Return ONLY JSON of the form "
    '{"themes":[{"label":"<=6 words","description":"one sentence"}]}. '
    "Prefer specific, content-bearing themes (e.g. 'Injection site reactions', "
    "'Cost and insurance access', 'Comparison vs competitor X') over generic ones like "
    "'General information'."
)

def _reduce_system(lo: int, hi: int) -> str:
    # NOTE: built by concatenation (not str.format / f-string over the whole text) because the
    # JSON example below contains literal { } braces that str.format would mis-parse as fields.
    return (
        "You are consolidating candidate themes into a clean taxonomy for an analytics dashboard. "
        "Merge duplicates and near-duplicates, drop vague themes, and produce between "
        f"{lo} and {hi} high-signal themes that together cover the material. "
        "For each theme provide: a short label, a one-sentence description, a category (one of: "
        "Efficacy, Safety, Access, Comparative, Experience, Other), and 6-15 lowercase "
        "keywords/phrases that would literally appear in text expressing this theme (include brand "
        "names, synonyms, and common phrasings). "
        'Return ONLY JSON of the form {"themes":[{"label":...,"description":...,"category":...,'
        '"keywords":["...","..."]}]}.'
    )


@dataclass
class CorpusItem:
    response_id: str
    text: str  # compact signal string (claims + snippet)


async def _latest_claims_map(db: AsyncSession, ids: list[str]) -> dict[str, list[str]]:
    if not ids:
        return {}
    subq = (
        select(ScoringRecord.response_id, func.max(ScoringRecord.score_version).label("maxv"))
        .where(ScoringRecord.response_id.in_(ids))
        .group_by(ScoringRecord.response_id)
        .subquery()
    )
    stmt = select(ScoringRecord).join(
        subq,
        and_(
            ScoringRecord.response_id == subq.c.response_id,
            ScoringRecord.score_version == subq.c.maxv,
        ),
    )
    out: dict[str, list[str]] = {}
    for s in (await db.execute(stmt)).scalars().all():
        try:
            out[s.response_id] = json.loads(s.key_claims) if s.key_claims else []
        except Exception:  # noqa: BLE001
            out[s.response_id] = []
    return out


async def gather_corpus(db: AsyncSession, *, cap: int = 300) -> list[CorpusItem]:
    """Build a representative sample of distilled response signals for taxonomy discovery."""
    rstmt = (
        select(Response)
        .where(Response.status.in_(_SCOREABLE))
        .order_by(Response.timestamp_utc.desc())
        .limit(cap)
    )
    responses = list((await db.execute(rstmt)).scalars().all())
    claims_map = await _latest_claims_map(db, [r.response_id for r in responses])

    items: list[CorpusItem] = []
    for r in responses:
        claims = claims_map.get(r.response_id, [])
        snippet = " ".join((r.response_text or "").split())[:400]
        parts: list[str] = []
        if claims:
            parts.append("Claims: " + " | ".join(str(c) for c in claims[:5]))
        if snippet:
            parts.append(f"[{r.therapeutic_area}/{r.brand_focus}/{r.persona}] {snippet}")
        text = "\n".join(parts).strip()
        if text:
            items.append(CorpusItem(response_id=r.response_id, text=text))
    return items


async def _map_batch(items: list[CorpusItem]) -> list[dict]:
    body = "\n\n".join(f"- {it.text}" for it in items)
    try:
        data = await chat_json(
            _MAP_SYSTEM,
            f"RESPONSE EXCERPTS:\n{body}\n\nList the themes now.",
            max_tokens=1200,
        )
    except Exception as e:  # noqa: BLE001 — one bad batch shouldn't sink the rebuild
        logger.warning("taxonomy map batch failed: %s", e)
        return []
    raw = data.get("themes") if isinstance(data, dict) else data
    out: list[dict] = []
    for t in raw or []:
        if not isinstance(t, dict):
            continue
        label = (t.get("label") or "").strip()
        if label:
            out.append({"label": label, "description": (t.get("description") or "").strip()})
    return out


def _normalize_theme(t: dict) -> dict | None:
    if not isinstance(t, dict):
        return None
    label = (t.get("label") or "").strip()
    if not label:
        return None
    keywords = []
    for kw in t.get("keywords") or []:
        kw = str(kw).strip().lower()
        if kw and kw not in keywords:
            keywords.append(kw)
    # ensure the label words are usable as keywords too
    label_kw = label.strip().lower()
    if len(label_kw) >= 3 and label_kw not in keywords:
        keywords.append(label_kw)
    category = (t.get("category") or "Other").strip().title()
    if category not in _VALID_CATEGORIES:
        category = "Other"
    return {
        "label": label[:160],
        "description": (t.get("description") or "").strip(),
        "category": category,
        "keywords": keywords[:18],
    }


async def _reduce(candidates: list[dict], target: int) -> list[dict]:
    listing = "\n".join(f"- {c['label']}: {c.get('description', '')}" for c in candidates)
    system = _reduce_system(max(4, target - 4), target + 3)
    data = await chat_json(
        system,
        f"CANDIDATE THEMES ({len(candidates)}):\n{listing}\n\nProduce the consolidated taxonomy now.",
        max_tokens=3500,
    )
    raw = data.get("themes") if isinstance(data, dict) else data
    themes: list[dict] = []
    seen: set[str] = set()
    for t in raw or []:
        norm = _normalize_theme(t)
        if norm and norm["label"].lower() not in seen:
            seen.add(norm["label"].lower())
            themes.append(norm)
    return themes


async def build_taxonomy(items: list[CorpusItem], *, target: int = 12) -> list[dict]:
    """Discover a consolidated theme taxonomy from a corpus sample. Returns theme dicts."""
    if not items:
        return []

    batches = [items[i : i + _MAP_BATCH] for i in range(0, len(items), _MAP_BATCH)]
    sem = asyncio.Semaphore(_MAP_CONCURRENCY)

    async def _guarded(batch: list[CorpusItem]) -> list[dict]:
        async with sem:
            return await _map_batch(batch)

    # Run map batches concurrently (bounded). _map_batch never raises, so gather is safe.
    batch_results = await asyncio.gather(*(_guarded(b) for b in batches))
    candidates: list[dict] = [c for result in batch_results for c in result]

    if not candidates:
        return []

    # Deduplicate identical labels before the reduce step to keep the prompt compact.
    deduped: dict[str, dict] = {}
    for c in candidates:
        key = c["label"].lower()
        if key not in deduped:
            deduped[key] = c
    unique = list(deduped.values())

    logger.info("taxonomy: %d candidate themes from %d items", len(unique), len(items))
    return await _reduce(unique, target)
