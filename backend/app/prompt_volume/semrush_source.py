"""In-app SEMrush keyword/question discovery for Prompt Volume (FR-116).

Unlike ``remediation/semrush.py`` (single-keyword ENRICHMENT with an offline stub), this
module DISCOVERS demand in bulk: it expands seed terms from ``brands.yaml`` for a chosen
therapeutic area / brand, then pulls the SEMrush Analytics API's multi-row reports:

  * ``phrase_questions`` — real natural-language questions people search (mapped to a
    ``prompt_text`` so gaps monitor the exact wording, not an auto-generated question).
  * ``phrase_related``   — related keyword terms (mapped to ``query_text`` only).

Both carry an absolute monthly ``Search Volume`` (``Nq``). Results are merged **volume-safe**:
the SAME normalized query collapses to one row at the MAX volume (never summed — SEMrush
reports one absolute volume per keyword; summing would double-count), preferring a questions
member so the real question is kept. DISTINCT queries stay separate so their volumes sum
downstream exactly like a CSV upload.

There is NO offline stub here: fabricating demand would be misleading, so a missing key
raises :class:`NotConfigured` and individual failed seeds are skipped (best-effort). Requires
SEMrush *Analytics API* access (billed in API units); ``get_settings()`` is lru_cached, so
restart the backend after changing the key.
"""
from __future__ import annotations

import asyncio

import httpx

from app.config import taxonomy
from app.config.settings import get_settings
from app.config.taxonomy import keys_for_area
from app.prompt_volume import gap, parser
from app.utils.logging import get_logger

logger = get_logger("prompt_volume.semrush_source")

_TIMEOUT = 20.0
_MAX_CONCURRENCY = 5
_EXPORT_COLUMNS = "Ph,Nq,Cp,Co,Nr"


class NotConfigured(RuntimeError):
    """Raised when a live SEMrush Analytics API key is not configured."""


def is_configured() -> bool:
    return bool(get_settings().semrush_api_key)


def _report_types(reports: str | None) -> list[str]:
    r = (reports or "both").strip().lower()
    if r == "questions":
        return ["phrase_questions"]
    if r == "related":
        return ["phrase_related"]
    return ["phrase_questions", "phrase_related"]


# --------------------------------------------------------------------------------
#  Seed expansion (from the taxonomy — the SE-007 single source of truth)
# --------------------------------------------------------------------------------
def _area_blocks() -> dict:
    return taxonomy.config().get("therapeutic_areas", {}) or {}


def _resolve_ta_keys(therapeutic_area: str | None) -> list[str]:
    """Resolve an area display name (-> all its stored keys) OR a single stored TA key."""
    ta = (therapeutic_area or "").strip()
    if not ta:
        return []
    keys = list(keys_for_area(ta))
    if keys:
        return keys
    return [ta] if ta in _area_blocks() else []


def expand_seeds(
    therapeutic_area: str,
    *,
    brand: str | None = None,
    include_generics: bool = True,
    include_indications: bool = True,
    include_competitors: bool = True,
) -> list[dict]:
    """Seed terms for a scope, deduped case-insensitively and capped at ``max_seeds``.

    Accepts an area display name (e.g. "Women's Health" -> every indication under it) or a
    single stored TA key (e.g. "Endometriosis"). ``brand`` narrows the FOCUS brands to one
    (its generic + its indications follow); competitors remain area-level and are governed
    only by ``include_competitors``. Each seed is ``{term, kind, ta_key}``.
    """
    ta_keys = _resolve_ta_keys(therapeutic_area)
    blocks = _area_blocks()
    brand_l = (brand or "").strip().lower()

    seeds: list[dict] = []
    seen: set[str] = set()

    def _add(term: str | None, kind: str, ta_key: str) -> None:
        t = (term or "").strip()
        if len(t) < 2:
            return
        k = t.lower()
        if k in seen:
            return
        seen.add(k)
        seeds.append({"term": t, "kind": kind, "ta_key": ta_key})

    for ta_key in ta_keys:
        block = blocks.get(ta_key) or {}
        for b in block.get("focus_brands", []) or []:
            name = b.get("name")
            if brand_l and (name or "").strip().lower() != brand_l:
                continue
            _add(name, "brand", ta_key)
            if include_generics:
                _add(b.get("generic"), "generic", ta_key)
            if include_indications:
                for ind in b.get("indications", []) or []:
                    _add(ind, "indication", ta_key)
        if include_competitors:
            for c in block.get("competitors", []) or []:
                _add(c.get("name"), "competitor", ta_key)
                if include_generics:
                    _add(c.get("generic"), "competitor_generic", ta_key)

    max_seeds = get_settings().prompt_volume_semrush_max_seeds
    return seeds[:max_seeds]


def estimate_units(seed_count: int, per_seed_limit: int, reports: str | None) -> int:
    """Rough BILLED-line estimate (upper bound) for the UI cost note."""
    return max(0, seed_count) * max(1, int(per_seed_limit)) * len(_report_types(reports))


# --------------------------------------------------------------------------------
#  SEMrush Analytics API (multi-row reports)
# --------------------------------------------------------------------------------
def _parse_rows(text: str) -> list[dict]:
    """Parse SEMrush's ';'-separated CSV (header + N value rows) into a list of dicts."""
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        return []
    header = [h.strip() for h in lines[0].split(";")]
    out: list[dict] = []
    for ln in lines[1:]:
        vals = [v.strip() for v in ln.split(";")]
        if len(vals) < len(header):
            continue
        out.append({h: v for h, v in zip(header, vals)})
    return out


def _cpc(raw: str | None) -> float | None:
    if raw in (None, ""):
        return None
    try:
        return float(str(raw).replace(",", "").replace("$", ""))
    except ValueError:
        return None


async def _fetch_report(
    client: httpx.AsyncClient, seed: str, report_type: str, *, limit: int,
    key: str, database: str, base_url: str,
) -> list[dict]:
    """Best-effort single report for one seed. Returns [] on any failure (seed skipped)."""
    params = {
        "type": report_type,
        "key": key,
        "phrase": seed,
        "database": database or "us",
        "export_columns": _EXPORT_COLUMNS,
        "display_limit": max(1, int(limit)),
    }
    try:
        resp = await client.get(base_url.rstrip("/") + "/", params=params)
    except Exception as e:  # noqa: BLE001 — best-effort per seed
        logger.info("SEMrush %s failed for %r: %s", report_type, seed, e)
        return []
    if resp.status_code != 200 or not resp.text or resp.text.startswith("ERROR"):
        logger.info("SEMrush %s unavailable (%s) for %r: %s",
                    report_type, resp.status_code, seed, (resp.text or "")[:120])
        return []

    is_question = report_type == "phrase_questions"
    rows: list[dict] = []
    for r in _parse_rows(resp.text):
        ph = (r.get("Keyword") or r.get("Ph") or "").strip()
        if not ph:
            continue
        rows.append({
            "query_text": ph,
            "prompt_text": ph if is_question else None,
            "search_volume": parser.parse_volume(r.get("Search Volume") or r.get("Nq")),
            "keyword_difficulty": None,
            "cpc": _cpc(r.get("CPC") or r.get("Cp")),
            "report": "questions" if is_question else "related",
        })
    return rows


def _merge_dedupe(rows: list[dict]) -> list[dict]:
    """Volume-safe merge: same normalized_query -> one row at MAX volume, questions preferred.

    Distinct normalized queries stay separate so their volumes sum downstream. Sorted by
    volume desc so previews/samples lead with the strongest demand.
    """
    by_norm: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        norm = gap.normalize(r["query_text"])
        key = norm or (r["query_text"] or "").strip().lower()
        if not key:
            continue
        cur = by_norm.get(key)
        if cur is None:
            by_norm[key] = {**r, "normalized_query": norm}
            order.append(key)
            continue
        # Same keyword from another seed/report: keep MAX volume, never sum.
        cur["search_volume"] = max(cur.get("search_volume") or 0, r.get("search_volume") or 0)
        # Prefer a questions member so a real prompt is retained as the representative.
        if r["report"] == "questions" and cur.get("report") != "questions":
            cur["query_text"] = r["query_text"]
            cur["prompt_text"] = r["prompt_text"]
            cur["report"] = "questions"
        if cur.get("cpc") is None and r.get("cpc") is not None:
            cur["cpc"] = r["cpc"]
    merged = [by_norm[k] for k in order]
    merged.sort(key=lambda r: r.get("search_volume") or 0, reverse=True)
    return merged


async def fetch(
    therapeutic_area: str,
    *,
    brand: str | None = None,
    include_generics: bool = True,
    include_indications: bool = True,
    include_competitors: bool = True,
    per_seed_limit: int | None = None,
    reports: str | None = None,
) -> dict:
    """Fetch + merge questions/related for a scope. Raises :class:`NotConfigured` w/o a key."""
    settings = get_settings()
    if not settings.semrush_api_key:
        raise NotConfigured("SEMrush Analytics API key not configured")

    seeds = expand_seeds(
        therapeutic_area, brand=brand, include_generics=include_generics,
        include_indications=include_indications, include_competitors=include_competitors,
    )
    report_types = _report_types(reports or settings.prompt_volume_semrush_reports)
    if not seeds:
        return {"rows": [], "seeds": [], "seeds_queried": 0, "lines_returned": 0, "reports": report_types}

    limit = per_seed_limit or settings.prompt_volume_semrush_per_seed_limit
    limit = max(1, min(int(limit), 100))
    key = settings.semrush_api_key
    database = settings.semrush_database
    base_url = settings.semrush_base_url
    sem = asyncio.Semaphore(_MAX_CONCURRENCY)

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async def _one(term: str, report_type: str) -> list[dict]:
            async with sem:
                return await _fetch_report(
                    client, term, report_type, limit=limit,
                    key=key, database=database, base_url=base_url,
                )
        results = await asyncio.gather(
            *[_one(s["term"], rt) for s in seeds for rt in report_types]
        )

    collected: list[dict] = []
    lines_returned = 0
    for chunk in results:
        lines_returned += len(chunk)
        collected.extend(chunk)

    return {
        "rows": _merge_dedupe(collected),
        "seeds": seeds,
        "seeds_queried": len(seeds),
        "lines_returned": lines_returned,
        "reports": report_types,
    }
