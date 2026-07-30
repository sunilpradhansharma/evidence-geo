"""SEMrush SEO enrichment (BR-012 step 2) with a deterministic offline stub.

Live path (when ``SEMRUSH_API_KEY`` is set) queries the SEMrush Analytics API for:
  - keyword **search volume** — Keyword Overview report (``type=phrase_this``, column ``Nq``)
  - domain **Authority Score** — Backlinks Overview report (``type=backlinks_overview``,
    column ``ascore``)

Both are best-effort: any missing key, timeout, non-2xx, or parse failure falls back to a
**deterministic stub** (stable pseudo-metrics derived from the domain/keyword) so the
recommendation engine always runs — offline, in CI, and before a key is provisioned. Every
result is labelled with its ``source`` ("live" | "stub") so the UI can badge stubbed data.
This module never raises.

NOTE: SEMrush export column codes / report endpoints vary slightly by plan. The parsing
here is defensive; if the live shape differs for your account, tune ``_fetch_*`` only —
the engine consumes the normalised {search_volume, domain_authority, source} dict.
"""
from __future__ import annotations

import hashlib

import httpx

from app.config.settings import get_settings
from app.utils.logging import get_logger

logger = get_logger("remediation.semrush")

_TIMEOUT = 10.0


def _stub_metrics(seed: str) -> tuple[int, int]:
    """Deterministic, stable pseudo-metrics for offline/test use.

    Same seed -> same numbers, so rankings are reproducible without a live key.
    search_volume in [200, 50000]; domain_authority in [20, 90].
    """
    digest = hashlib.md5((seed or "seed").encode("utf-8")).hexdigest()
    vol = 200 + (int(digest[:8], 16) % 49800)
    authority = 20 + (int(digest[8:16], 16) % 71)
    return vol, authority


def _parse_semrush_csv(text: str) -> dict[str, str]:
    """Parse SEMrush's ';'-separated CSV (header row + one value row) into a dict."""
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        return {}
    header = [h.strip() for h in lines[0].split(";")]
    values = [v.strip() for v in lines[1].split(";")]
    return {h: v for h, v in zip(header, values)}


async def _fetch_search_volume(keyword: str) -> int | None:
    """Keyword Overview -> search volume (column 'Nq' / 'Search Volume'). None on failure."""
    settings = get_settings()
    if not keyword:
        return None
    params = {
        "type": "phrase_this",
        "key": settings.semrush_api_key,
        "phrase": keyword,
        "database": settings.semrush_database or "us",
        "export_columns": "Ph,Nq,Cp,Co,Nr",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(settings.semrush_base_url.rstrip("/") + "/", params=params)
        if resp.status_code != 200 or not resp.text or resp.text.startswith("ERROR"):
            logger.info("SEMrush keyword lookup unavailable (%s): %s",
                        resp.status_code, resp.text[:120])
            return None
        row = _parse_semrush_csv(resp.text)
        raw = row.get("Search Volume") or row.get("Nq")
        return int(float(raw)) if raw not in (None, "") else None
    except Exception as e:  # noqa: BLE001 — best-effort; fall back to stub
        logger.info("SEMrush keyword lookup failed: %s", e)
        return None


async def _fetch_domain_authority(domain: str) -> int | None:
    """Backlinks Overview -> Authority Score (column 'ascore'). None on failure."""
    settings = get_settings()
    if not domain:
        return None
    params = {
        "type": "backlinks_overview",
        "key": settings.semrush_api_key,
        "target": domain,
        "target_type": "root_domain",
        "export_columns": "ascore,total,domains_num",
    }
    # The backlinks report lives under /analytics/v1/ on the same host.
    url = settings.semrush_base_url.rstrip("/") + "/analytics/v1/"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, params=params)
        if resp.status_code != 200 or not resp.text or resp.text.startswith("ERROR"):
            logger.info("SEMrush authority lookup unavailable (%s): %s",
                        resp.status_code, resp.text[:120])
            return None
        row = _parse_semrush_csv(resp.text)
        raw = row.get("Authority Score") or row.get("ascore")
        return int(float(raw)) if raw not in (None, "") else None
    except Exception as e:  # noqa: BLE001 — best-effort; fall back to stub
        logger.info("SEMrush authority lookup failed: %s", e)
        return None


async def enrich(domain: str | None, *, keyword: str) -> dict:
    """Return {search_volume, domain_authority, source} for a competitor.

    ``keyword`` (the competitor / brand name or topic) drives search volume; ``domain``
    (when resolvable from the response citations) drives Authority Score. Falls back to a
    deterministic stub for any metric the live API can't provide.
    """
    stub_vol, stub_auth = _stub_metrics((domain or "") + "|" + (keyword or ""))
    settings = get_settings()

    if not settings.semrush_api_key:
        return {"search_volume": stub_vol, "domain_authority": stub_auth, "source": "stub"}

    live_vol = await _fetch_search_volume(keyword)
    live_auth = await _fetch_domain_authority(domain) if domain else None

    source = "live" if (live_vol is not None or live_auth is not None) else "stub"
    return {
        "search_volume": live_vol if live_vol is not None else stub_vol,
        "domain_authority": live_auth if live_auth is not None else stub_auth,
        "source": source,
    }


def is_configured() -> bool:
    """True when a live SEMrush key is present (else the engine uses stub metrics)."""
    return bool(get_settings().semrush_api_key)
