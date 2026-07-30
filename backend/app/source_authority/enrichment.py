"""Source-authority domain enrichment (FR-706a) — RDAP + optional LLM, curated-first.

The curated taxonomy (config/source_authority.yaml) is the SOURCE OF TRUTH. This module only
ADDS optional, best-effort signals for domains the taxonomy does not already resolve:

  • ``registration_lookup(domain)`` — WHOIS-equivalent registration metadata (registrant org,
    registrar, registration date, and the record's *visibility* PUBLIC/REDACTED/NO_DATA). Tries
    RDAP first (the free, ICANN-mandated successor to WHOIS; no API key), then the WhoisXML
    WHOIS API only if a key is configured (for the few ccTLDs without RDAP). Redaction is NOT a
    trust signal (post-GDPR most records are redacted) so it is reported separately and never
    sets ``verification``.
  • ``classify_domain_llm(domain, …)`` — evidence-based authority classification of an
    UNCURATED domain. Gathers best-effort homepage metadata (title / description / og:site_name
    / schema.org Organization) plus the RDAP registrant org, and asks the configured scoring LLM
    to classify the SUPPLIED EVIDENCE into the authority buckets with a confidence. It NEVER
    declares legal ownership; weak/low-confidence results are flagged requires_review.
  • ``whois_lookup`` / ``categorize`` — the two legacy WhoisXML products, kept as OPTIONAL
    fallbacks (historical / reverse / higher-volume enrichment) behind their own keys.

Everything is best-effort and never raises: when unkeyed / disabled / on any failure these
return null-shaped stubs (registration) or ``None`` (LLM), so the app runs fully offline on the
curated taxonomy alone and never shows a fabricated owner. get_settings() is lru_cached →
restart after changing a key or flag.
"""
from __future__ import annotations

import json
import re

import httpx

from app.config.settings import get_settings
from app.insights.llm import chat_json
from app.models.source_domain import (
    AUTH_GUIDELINE,
    AUTH_HEALTH_MEDIA,
    AUTH_MEDICAL_REFERENCE,
    AUTH_OTHER,
    AUTH_PEER_REVIEWED,
    AUTH_REGULATORY,
    AUTH_SOCIAL_UGC,
    WHOIS_NO_DATA,
    WHOIS_PUBLIC,
    WHOIS_REDACTED,
)
from app.utils.logging import get_logger

logger = get_logger("source_authority.enrichment")

_TIMEOUT = 10.0
_META_TIMEOUT = 8.0
_MAX_HTML = 200_000  # only parse the first ~200 KB of a homepage for metadata
_UA = (
    "Mozilla/5.0 (compatible; EvidenceMonitoringBot/1.0; +https://www.abbvie.com) "
    "source-authority-classifier"
)

_VALID_AUTHORITIES = frozenset({
    AUTH_REGULATORY, AUTH_GUIDELINE, AUTH_PEER_REVIEWED, AUTH_MEDICAL_REFERENCE,
    AUTH_HEALTH_MEDIA, AUTH_SOCIAL_UGC, AUTH_OTHER,
})

# Substrings that mark a WHOIS record as privacy-redacted rather than genuinely absent.
_REDACTION_MARKERS = (
    "redacted", "privacy", "gdpr", "data protected", "whois protection",
    "not disclosed", "withheld", "identity protection", "domains by proxy",
    "perfect privacy", "contact privacy",
)


def _whois_stub() -> dict:
    return {
        "registrant_organization": None,
        "registrar_name": None,
        "whois_visibility": None,
        "created_date": None,
        "source": "stub",
    }


def _visibility(org: str | None, has_record: bool) -> str:
    if org:
        low = org.lower()
        if any(m in low for m in _REDACTION_MARKERS):
            return WHOIS_REDACTED
        return WHOIS_PUBLIC
    return WHOIS_REDACTED if has_record else WHOIS_NO_DATA


async def whois_lookup(domain: str) -> dict:
    """WHOIS registrant/registrar/visibility for *domain*. Nulls (source='stub') if unkeyed."""
    settings = get_settings()
    if not domain or not settings.whoisxml_api_key:
        return _whois_stub()

    params = {
        "apiKey": settings.whoisxml_api_key,
        "domainName": domain,
        "outputFormat": "JSON",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(settings.whoisxml_base_url, params=params)
        if resp.status_code != 200:
            logger.info("WhoisXML WHOIS unavailable (%s) for %s", resp.status_code, domain)
            return _whois_stub()
        record = (resp.json() or {}).get("WhoisRecord") or {}
        registry = record.get("registryData") or {}
        registrant = record.get("registrant") or registry.get("registrant") or {}
        org = registrant.get("organization") or registrant.get("name")
        registrar = record.get("registrarName") or registry.get("registrarName")
        created = record.get("createdDate") or registry.get("createdDate")
        has_record = bool(record) and record.get("dataError") != "MISSING_WHOIS_DATA"
        # A redaction marker in the org means we DID get a record but it's masked.
        org_clean = org if (org and not any(m in org.lower() for m in _REDACTION_MARKERS)) else None
        return {
            "registrant_organization": org_clean,
            "registrar_name": registrar,
            "whois_visibility": _visibility(org, has_record),
            "created_date": created,
            "source": "live",
        }
    except Exception as e:  # noqa: BLE001 — best-effort; never raise
        logger.info("WhoisXML WHOIS lookup failed for %s: %s", domain, e)
        return _whois_stub()


def _categorize_stub() -> dict:
    return {"categories": [], "confidence": None, "source": "stub"}


async def categorize(domain: str) -> dict:
    """Website content categories + confidence for *domain*. Empty (source='stub') if unkeyed."""
    settings = get_settings()
    if not domain or not settings.whoisxml_categorization_api_key:
        return _categorize_stub()

    payload = {"apiKey": settings.whoisxml_categorization_api_key, "url": domain}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(settings.whoisxml_categorization_base_url, json=payload)
        if resp.status_code != 200:
            logger.info("WhoisXML categorization unavailable (%s) for %s", resp.status_code, domain)
            return _categorize_stub()
        data = resp.json() or {}
        raw = data.get("categories") or data.get("websiteResponse", {}).get("categories") or []
        cats: list[str] = []
        conf: float | None = None
        for c in raw:
            name = (c.get("name") or c.get("tier1", {}).get("name")) if isinstance(c, dict) else None
            if name:
                cats.append(str(name))
            cval = c.get("confidence") if isinstance(c, dict) else None
            if cval is not None:
                try:
                    conf = max(conf or 0.0, float(cval))
                except (TypeError, ValueError):
                    pass
        return {"categories": cats, "confidence": conf, "source": "live" if cats else "stub"}
    except Exception as e:  # noqa: BLE001 — best-effort; never raise
        logger.info("WhoisXML categorization failed for %s: %s", domain, e)
        return _categorize_stub()


# ---------------------------------------------------------------------------
# RDAP registration metadata (free, no key) + WhoisXML fallback
# ---------------------------------------------------------------------------
def _registration_stub() -> dict:
    """The null registration shape, used when neither RDAP nor WhoisXML answers."""
    return _whois_stub()


def _vcard_field(vcard_array, field: str) -> str | None:
    """Pull a field value (e.g. 'org', 'fn') out of an RDAP jCard/vcardArray."""
    if not isinstance(vcard_array, list) or len(vcard_array) < 2:
        return None
    entries = vcard_array[1]
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, list) and len(entry) >= 4 and entry[0] == field:
            val = entry[3]
            if isinstance(val, list):
                val = " ".join(str(v) for v in val if v)
            val = str(val).strip()
            if val:
                return val
    return None


def _rdap_is_redacted(data: dict) -> bool:
    """Detect an RFC 9537 ``redacted`` array or a privacy remark/notice in an RDAP record."""
    if data.get("redacted"):
        return True
    blobs: list[str] = []
    for block in (data.get("remarks") or []) + (data.get("notices") or []):
        if not isinstance(block, dict):
            continue
        desc = block.get("description")
        desc_txt = " ".join(str(d) for d in desc) if isinstance(desc, list) else str(desc or "")
        blobs.append(f"{block.get('title') or ''} {desc_txt}".lower())
    joined = " ".join(blobs)
    return any(m in joined for m in _REDACTION_MARKERS)


def _parse_rdap(data: dict) -> dict:
    """Map an RDAP domain object into the registration-metadata shape (source='live')."""
    registrant_org = None
    registrar_name = None

    def _walk(entities) -> None:
        nonlocal registrant_org, registrar_name
        if not isinstance(entities, list):
            return
        for ent in entities:
            if not isinstance(ent, dict):
                continue
            roles = [str(r).lower() for r in (ent.get("roles") or [])]
            name = _vcard_field(ent.get("vcardArray"), "org") or _vcard_field(ent.get("vcardArray"), "fn")
            if "registrant" in roles and registrant_org is None:
                registrant_org = name
            if "registrar" in roles and registrar_name is None:
                registrar_name = name
            _walk(ent.get("entities"))

    _walk(data.get("entities"))

    created = None
    for ev in data.get("events") or []:
        if isinstance(ev, dict) and str(ev.get("eventAction") or "").lower() == "registration":
            created = ev.get("eventDate")
            break

    has_record = bool(
        data.get("ldhName") or data.get("handle") or data.get("entities") or data.get("events")
    )
    org_clean = (
        registrant_org
        if registrant_org and not any(m in registrant_org.lower() for m in _REDACTION_MARKERS)
        else None
    )
    if org_clean:
        visibility = WHOIS_PUBLIC
    elif has_record or _rdap_is_redacted(data):
        visibility = WHOIS_REDACTED
    else:
        visibility = WHOIS_NO_DATA
    return {
        "registrant_organization": org_clean,
        "registrar_name": registrar_name,
        "whois_visibility": visibility,
        "created_date": created,
        "source": "live",
    }


async def rdap_lookup(domain: str) -> dict:
    """RDAP registration record for *domain*. Null stub (source='stub') if RDAP has no data."""
    if not domain:
        return _registration_stub()
    base = (get_settings().rdap_base_url or "https://rdap.org/domain").rstrip("/")
    url = f"{base}/{domain}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers={"Accept": "application/rdap+json"})
        if resp.status_code != 200:
            logger.info("RDAP unavailable (%s) for %s", resp.status_code, domain)
            return _registration_stub()
        return _parse_rdap(resp.json() or {})
    except Exception as e:  # noqa: BLE001 — best-effort; never raise
        logger.info("RDAP lookup failed for %s: %s", domain, e)
        return _registration_stub()


async def registration_lookup(domain: str) -> dict:
    """RDAP-first registration metadata; WhoisXML WHOIS API as an OPTIONAL fallback.

    RDAP needs no key and covers all gTLDs. Only if RDAP returns no data AND a WhoisXML key is
    configured do we fall back (useful for a handful of ccTLDs). Otherwise returns the stub.
    """
    result = await rdap_lookup(domain)
    if result.get("source") == "live":
        return result
    if get_settings().whoisxml_api_key:
        wx = await whois_lookup(domain)
        if wx.get("source") == "live":
            return wx
    return result


# ---------------------------------------------------------------------------
# Best-effort website metadata (evidence for the LLM classifier)
# ---------------------------------------------------------------------------
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_META_NAME_RE = re.compile(r"""(?:name|property)\s*=\s*[\"']([^\"']+)[\"']""", re.IGNORECASE)
_META_CONTENT_RE = re.compile(r"""content\s*=\s*[\"']([^\"']*)[\"']""", re.IGNORECASE)
_LDJSON_RE = re.compile(
    r"""<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>""",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_ORG_TYPES = {
    "organization", "corporation", "newsmediaorganization", "governmentorganization",
    "medicalorganization", "ngo", "educationalorganization",
}


def _clean_text(text: str | None, *, limit: int = 300) -> str | None:
    if not text:
        return None
    cleaned = _WS_RE.sub(" ", _TAG_RE.sub(" ", str(text)))
    for ent, ch in (("&amp;", "&"), ("&#39;", "'"), ("&quot;", '"'), ("&nbsp;", " "),
                    ("&lt;", "<"), ("&gt;", ">"), ("&mdash;", "-"), ("&ndash;", "-")):
        cleaned = cleaned.replace(ent, ch)
    cleaned = cleaned.strip()
    return cleaned[:limit] or None


def _iter_ld_nodes(data):
    if isinstance(data, dict):
        if isinstance(data.get("@graph"), list):
            for node in data["@graph"]:
                yield from _iter_ld_nodes(node)
        yield data
    elif isinstance(data, list):
        for node in data:
            yield from _iter_ld_nodes(node)


def _ld_json_org(html: str) -> str | None:
    for block in _LDJSON_RE.findall(html):
        try:
            data = json.loads(block.strip())
        except Exception:  # noqa: BLE001 — skip malformed ld+json
            continue
        for node in _iter_ld_nodes(data):
            if not isinstance(node, dict):
                continue
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if any(str(t).lower() in _ORG_TYPES for t in types) and node.get("name"):
                return _clean_text(str(node["name"]))
            pub = node.get("publisher")
            if isinstance(pub, dict) and pub.get("name"):
                return _clean_text(str(pub["name"]))
    return None


def _parse_site_metadata(html: str) -> dict:
    """Extract title / description / og:site_name / schema.org Organization from HTML."""
    html = (html or "")[:_MAX_HTML]
    out = {"title": None, "description": None, "site_name": None, "organization": None}
    m = _TITLE_RE.search(html)
    if m:
        out["title"] = _clean_text(m.group(1))
    for tag in _META_TAG_RE.findall(html):
        name_m = _META_NAME_RE.search(tag)
        content_m = _META_CONTENT_RE.search(tag)
        if not name_m or not content_m:
            continue
        key = name_m.group(1).strip().lower()
        val = _clean_text(content_m.group(1))
        if not val:
            continue
        if key in ("description", "og:description") and not out["description"]:
            out["description"] = val
        elif key in ("og:site_name", "application-name") and not out["site_name"]:
            out["site_name"] = val
    org = _ld_json_org(html)
    if org:
        out["organization"] = org
    return out


async def fetch_site_metadata(domain: str) -> dict:
    """Best-effort homepage metadata for *domain*. Empty dict on any failure (never raises)."""
    if not domain:
        return {}
    try:
        async with httpx.AsyncClient(
            timeout=_META_TIMEOUT, follow_redirects=True, headers={"User-Agent": _UA}
        ) as client:
            resp = await client.get(f"https://{domain}/")
        if resp.status_code >= 400:
            return {}
        return _parse_site_metadata(resp.text or "")
    except Exception as e:  # noqa: BLE001 — best-effort; never raise
        logger.info("site metadata fetch failed for %s: %s", domain, e)
        return {}


# ---------------------------------------------------------------------------
# Evidence-based LLM authority classifier (for UNCURATED domains only)
# ---------------------------------------------------------------------------
_LLM_SYSTEM = (
    "You classify a health or medical web DOMAIN into exactly one authority category, based "
    "ONLY on the evidence supplied. Categories:\n"
    "- REGULATORY: government or regulatory agencies and official clinical-trial registries "
    "(e.g. FDA, EMA, NIH, clinicaltrials.gov).\n"
    "- GUIDELINE: clinical-practice guideline or health-technology-assessment bodies "
    "(e.g. NCCN, NICE, ESMO, CDA-AMC).\n"
    "- PEER_REVIEWED: peer-reviewed journals or scholarly literature indexes/publishers "
    "(e.g. PubMed, NEJM, The Lancet).\n"
    "- MEDICAL_REFERENCE: professional medical reference or health information from medical "
    "organizations (e.g. Mayo Clinic, Medscape, drugs.com).\n"
    "- HEALTH_MEDIA: health news or media outlets.\n"
    "- SOCIAL_UGC: social networks, forums, or other user-generated-content communities.\n"
    "- OTHER: anything else, or when the evidence is insufficient to decide.\n\n"
    "Rules: classify the SUPPLIED EVIDENCE only. Do NOT assert who legally owns the domain. "
    "If the evidence is weak, missing, or conflicting, return OTHER with a low confidence and "
    "requires_review=true. Respond with STRICT JSON only, no prose:\n"
    '{"authority_type": "<one category>", "publisher": "<publisher name or null>", '
    '"confidence": <number 0..1>, "evidence": ["<short reason>"], '
    '"requires_review": <true|false>}'
)


def _normalize_llm_result(data, meta: dict) -> dict | None:
    if not isinstance(data, dict):
        return None
    auth = str(data.get("authority_type") or "").strip().upper()
    if auth not in _VALID_AUTHORITIES:
        auth = AUTH_OTHER
    conf = data.get("confidence")
    try:
        conf = max(0.0, min(1.0, float(conf)))
    except (TypeError, ValueError):
        conf = None
    ev = data.get("evidence")
    if isinstance(ev, str):
        ev = [ev]
    if not isinstance(ev, list):
        ev = []
    evidence = [str(x).strip() for x in ev if str(x).strip()][:6]
    publisher = data.get("publisher") or meta.get("organization") or meta.get("site_name")
    publisher = _clean_text(str(publisher), limit=255) if publisher else None
    return {
        "authority_type": auth,
        "publisher": publisher,
        "confidence": conf,
        "evidence": evidence,
        "requires_review": bool(data.get("requires_review")),
        "source": "live",
    }


async def classify_domain_llm(
    domain: str, normalized_host: str | None = None, *, registrant_org: str | None = None
) -> dict | None:
    """Classify an UNCURATED *domain* from evidence. Returns a result dict, or None if disabled/failed.

    Result: {authority_type, publisher, confidence(0..1|None), evidence[list], requires_review}.
    Offline-safe: when the classifier is disabled or the LLM call fails this returns None and the
    caller keeps the domain unclassified (never fabricates a classification).
    """
    settings = get_settings()
    if not settings.source_authority_llm_enabled or not domain:
        return None

    meta: dict = {}
    if settings.source_authority_fetch_metadata:
        meta = await fetch_site_metadata(domain)

    lines = [f"Domain: {domain}"]
    if normalized_host and normalized_host != domain:
        lines.append(f"Host: {normalized_host}")
    if meta.get("title"):
        lines.append(f"Homepage title: {meta['title']}")
    if meta.get("description"):
        lines.append(f"Meta description: {meta['description']}")
    if meta.get("site_name"):
        lines.append(f"og:site_name: {meta['site_name']}")
    if meta.get("organization"):
        lines.append(f"schema.org organization: {meta['organization']}")
    if registrant_org:
        lines.append(f"Registrant organization (RDAP/WHOIS): {registrant_org}")
    if len(lines) == 1:
        lines.append("(No homepage metadata or registration organization was available.)")

    user = "EVIDENCE:\n" + "\n".join(lines) + "\n\nClassify the domain now."
    try:
        data = await chat_json(_LLM_SYSTEM, user, max_tokens=500)
    except Exception as e:  # noqa: BLE001 — best-effort; degrade to no classification
        logger.info("LLM domain classification failed for %s: %s", domain, e)
        return None
    return _normalize_llm_result(data, meta)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def is_configured() -> bool:
    """True when enrichment beyond the curated taxonomy is available.

    RDAP needs no key and is always on, so registration enrichment is effectively always
    available; the LLM fallback adds authority inference for uncurated domains. Kept for the
    /status endpoint's back-compat flag.
    """
    return True


def enrichment_status() -> dict:
    """What optional enrichment is active (surfaced on the Source Authority page)."""
    s = get_settings()
    whoisxml = bool(s.whoisxml_api_key or s.whoisxml_categorization_api_key)
    return {
        "rdap_enabled": True,
        "llm_classifier_enabled": bool(s.source_authority_llm_enabled),
        "website_metadata_enabled": bool(s.source_authority_fetch_metadata),
        "whoisxml_fallback_configured": whoisxml,
        # Legacy alias kept for the existing frontend badge.
        "whoisxml_configured": whoisxml,
    }
