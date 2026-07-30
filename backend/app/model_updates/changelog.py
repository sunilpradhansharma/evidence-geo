"""Fetch + extract vendor changelog entries (FR-707a).

Best-effort, never-raises capture of a vendor's public changelog / release-notes page
(HTML) or What's-New feed (RSS). The raw text is handed to the configured scoring LLM,
which extracts structured `(version, effective_date, summary, event_type, confidence)`
entries relevant to the platforms a source covers. Reuses the fetch + JSON-LLM patterns
already used by source_authority.enrichment / insights.llm.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

import httpx

from app.config.settings import get_settings
from app.insights.llm import chat_json
from app.model_updates.sources import VendorSource
from app.utils.logging import get_logger

logger = get_logger("model_updates.changelog")

_TIMEOUT = 15.0
_MAX_CHARS = 24_000  # cap the text handed to the LLM (most-recent entries are at the top)
_UA = (
    "Mozilla/5.0 (compatible; EvidenceMonitoringBot/1.0; +https://www.abbvie.com) "
    "model-update-capture"
)

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"[ \t\f\v]+")
_MULTINL_RE = re.compile(r"\n\s*\n\s*\n+")
_ITEM_RE = re.compile(r"<item\b[^>]*>(.*?)</item>", re.IGNORECASE | re.DOTALL)
_RSS_FIELD_RE = {
    "title": re.compile(r"<title\b[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL),
    "link": re.compile(r"<link\b[^>]*>(.*?)</link>", re.IGNORECASE | re.DOTALL),
    "pubDate": re.compile(r"<pubDate\b[^>]*>(.*?)</pubDate>", re.IGNORECASE | re.DOTALL),
    "description": re.compile(r"<description\b[^>]*>(.*?)</description>", re.IGNORECASE | re.DOTALL),
}
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)


@dataclass
class ChangelogEntry:
    """One captured vendor update, normalized for the model_release_log."""

    platform: str
    version: str | None
    effective_date: date | None
    summary: str
    event_type: str
    confidence: float
    url: str | None
    vendor: str


def _strip_html(html: str) -> str:
    html = _SCRIPT_STYLE_RE.sub(" ", html or "")
    text = _TAG_RE.sub(" ", html)
    for ent, ch in (("&amp;", "&"), ("&#39;", "'"), ("&quot;", '"'), ("&nbsp;", " "),
                    ("&lt;", "<"), ("&gt;", ">"), ("&mdash;", "-"), ("&ndash;", "-")):
        text = text.replace(ent, ch)
    text = _WS_RE.sub(" ", text)
    text = _MULTINL_RE.sub("\n\n", text)
    return text.strip()


def _rss_to_text(xml: str, *, max_items: int) -> str:
    """Flatten the most-recent RSS <item>s into a compact 'title | date | description' text."""
    out: list[str] = []
    for block in _ITEM_RE.findall(xml or "")[: max_items * 3]:  # over-fetch; LLM filters
        parts: dict[str, str] = {}
        for key, rx in _RSS_FIELD_RE.items():
            m = rx.search(block)
            if not m:
                continue
            val = m.group(1)
            cd = _CDATA_RE.search(val)
            val = cd.group(1) if cd else val
            parts[key] = _strip_html(val)[:600]
        title = parts.get("title", "")
        if not title:
            continue
        line = title
        if parts.get("pubDate"):
            line += f"  [{parts['pubDate']}]"
        if parts.get("description"):
            line += f"\n{parts['description']}"
        if parts.get("link"):
            line += f"\n{parts['link']}"
        out.append(line)
    return "\n\n".join(out)


async def fetch_source_text(source: VendorSource) -> str:
    """Fetch + flatten a vendor source to plaintext. Empty string on any failure."""
    url = source.url()
    if not url:
        return ""
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, follow_redirects=True, headers={"User-Agent": _UA}
        ) as client:
            resp = await client.get(url)
        if resp.status_code >= 400:
            logger.info("changelog fetch %s -> HTTP %s", source.vendor, resp.status_code)
            return ""
        raw = resp.text or ""
    except Exception as e:  # noqa: BLE001 — best-effort; never raise
        logger.info("changelog fetch failed for %s: %s", source.vendor, e)
        return ""

    max_items = get_settings().model_update_max_entries_per_vendor
    text = _rss_to_text(raw, max_items=max_items) if source.fmt == "rss" else _strip_html(raw)
    return text[:_MAX_CHARS]


_SYSTEM = (
    "You extract MODEL-VERSION RELEASE entries from a vendor's changelog / release-notes / "
    "product-update text. Return ONLY entries that announce a NEW or UPDATED AI MODEL VERSION "
    "(a release, silent retrain, capability change, or deprecation) for the platforms in FOCUS. "
    "Ignore SDK/library/pricing/console/docs-only changes.\n"
    "For each qualifying entry return:\n"
    "- platform: one of the FOCUS platform names, lowercase (or null if unclear)\n"
    "- version: the exact model/version string if stated (e.g. 'gpt-4o-2024-08-06', "
    "'claude-3-5-sonnet-20241022', 'gemini-2.0-flash'), else null\n"
    "- effective_date: the entry's date as YYYY-MM-DD, else null\n"
    "- event_type: one of release|retrain|capability|deprecation\n"
    "- summary: <= 240 chars, plain description of WHAT CHANGED\n"
    "- confidence: 0..1 that this is a real model-version event for a FOCUS platform\n"
    "Respond with STRICT JSON only: "
    '{"entries": [{"platform": "...", "version": "...", "effective_date": "YYYY-MM-DD", '
    '"event_type": "release", "summary": "...", "confidence": 0.0}]}'
)


def _parse_date(val) -> date | None:
    if not val or not isinstance(val, str):
        return None
    val = val.strip()[:10]
    try:
        return datetime.strptime(val, "%Y-%m-%d").date()
    except ValueError:
        return None


def _valid_platform(platform, source: VendorSource) -> str | None:
    if not platform or not isinstance(platform, str):
        # Single-platform sources can safely default; multi-platform can't guess.
        return source.platforms[0] if len(source.platforms) == 1 else None
    p = platform.strip().lower()
    return p if p in source.platforms else (source.platforms[0] if len(source.platforms) == 1 else None)


async def extract_entries(source: VendorSource, text: str) -> list[ChangelogEntry]:
    """LLM-extract structured changelog entries from *text*. Empty on failure/empty text."""
    if not text.strip():
        return []
    settings = get_settings()
    user = (
        f"FOCUS platforms: {', '.join(source.platforms)}\n"
        f"FOCUS description: {source.focus}\n"
        f"Vendor: {source.vendor}\n\n"
        f"CHANGELOG TEXT:\n{text}\n\nExtract the qualifying model-version entries now."
    )
    try:
        data = await chat_json(_SYSTEM, user, max_tokens=2000)
    except Exception as e:  # noqa: BLE001 — degrade to no entries
        logger.info("changelog extraction failed for %s: %s", source.vendor, e)
        return []

    raw_entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(raw_entries, list):
        return []

    min_conf = settings.model_update_extract_min_confidence
    limit = settings.model_update_max_entries_per_vendor
    out: list[ChangelogEntry] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            continue
        try:
            conf = float(item.get("confidence"))
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))
        if conf < min_conf:
            continue
        platform = _valid_platform(item.get("platform"), source)
        if not platform:
            continue
        summary = str(item.get("summary") or "").strip()[:240]
        if not summary:
            continue
        event_type = str(item.get("event_type") or "release").strip().lower()
        if event_type not in ("release", "retrain", "capability", "deprecation"):
            event_type = "release"
        version = item.get("version")
        version = str(version).strip()[:128] if version else None
        out.append(ChangelogEntry(
            platform=platform,
            version=version,
            effective_date=_parse_date(item.get("effective_date")),
            summary=summary,
            event_type=event_type,
            confidence=conf,
            url=source.url() or None,
            vendor=source.vendor,
        ))
        if len(out) >= limit:
            break
    return out


async def capture_vendor(source: VendorSource) -> list[ChangelogEntry]:
    """Fetch + extract one vendor source end-to-end. Never raises."""
    text = await fetch_source_text(source)
    return await extract_entries(source, text)
