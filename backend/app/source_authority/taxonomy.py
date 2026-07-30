"""Curated Source-Authority taxonomy (FR-706a.2) — the source of truth for classification.

Loads ``config/source_authority.yaml`` (third-party authority lists + explicit ownership
domains) and derives AbbVie/competitor *product* domains dynamically from ``brands.yaml``
(so ``humira.com`` maps to AbbVie without hard-coding it here — SE-007). Exposes:

  • ``authority_domain_for``  — resolve a host to its longest curated suffix (else eTLD+1)
  • ``authority_type_for``    — longest-suffix match into REGULATORY / PEER_REVIEWED / …
  • ``control_for``           — ABBVIE / COMPETITOR / None from config + brand-name tokens

Everything is ``lru_cache``d; restart the backend after editing either YAML.
"""
from __future__ import annotations

import re
from functools import lru_cache

from app.config.settings import load_yaml_config
from app.config.taxonomy import alias_index
from app.models.source_domain import (
    AUTH_GUIDELINE,
    AUTH_HEALTH_MEDIA,
    AUTH_MEDICAL_REFERENCE,
    AUTH_PEER_REVIEWED,
    AUTH_REGULATORY,
    AUTH_SOCIAL_UGC,
    CONTROL_ABBVIE,
    CONTROL_COMPETITOR,
)

# Category key in the YAML -> authority_type constant.
_CATEGORY_TO_AUTH = {
    "regulatory": AUTH_REGULATORY,
    "guideline": AUTH_GUIDELINE,
    "peer_reviewed": AUTH_PEER_REVIEWED,
    "medical_reference": AUTH_MEDICAL_REFERENCE,
    "health_media": AUTH_HEALTH_MEDIA,
    "social_ugc": AUTH_SOCIAL_UGC,
}


@lru_cache
def _config() -> dict:
    return load_yaml_config("source_authority.yaml") or {}


def rules_version() -> int:
    try:
        return int(_config().get("rules_version", 0))
    except (TypeError, ValueError):
        return 0


def _clean_list(key: str) -> tuple[str, ...]:
    vals = _config().get(key) or []
    return tuple(str(v).strip().lower() for v in vals if str(v).strip())


@lru_cache
def _authority_pairs() -> tuple[tuple[str, str], ...]:
    """(authority_domain, authority_type) for all 5 category lists, longest-first."""
    pairs: list[tuple[str, str]] = []
    for cat_key, auth in _CATEGORY_TO_AUTH.items():
        for dom in _clean_list(cat_key):
            pairs.append((dom, auth))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return tuple(pairs)


@lru_cache
def abbvie_domains() -> frozenset[str]:
    return frozenset(_clean_list("abbvie_domains"))


@lru_cache
def competitor_domains() -> frozenset[str]:
    return frozenset(_clean_list("competitor_domains"))


@lru_cache
def _all_curated_domains() -> tuple[str, ...]:
    """Every curated domain (authority lists + ownership), longest-first, for suffix resolution."""
    domains = {d for d, _ in _authority_pairs()}
    domains |= set(abbvie_domains()) | set(competitor_domains())
    return tuple(sorted(domains, key=len, reverse=True))


@lru_cache
def _brand_alias_ownership() -> tuple[tuple[str, str], ...]:
    """Single-word brand/competitor aliases -> control_type, longest-first.

    Derived from brands.yaml so product domains (humira.com, stelara.com) can be attributed
    without hard-coding. Multi-word aliases are dropped (they never match a domain label).
    """
    out: list[tuple[str, str]] = []
    for e in alias_index():
        if e["kind"] not in ("brand", "competitor"):
            continue
        alias = e["alias"]
        if len(alias) < 4 or not alias.isalnum():  # single-token, meaningful length
            continue
        out.append((alias, CONTROL_COMPETITOR if e["is_competitor"] else CONTROL_ABBVIE))
    out.sort(key=lambda p: len(p[0]), reverse=True)
    return tuple(out)


def _suffix_match(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def authority_domain_for(normalized_host: str, registrable_domain: str) -> str:
    """Longest curated domain that *host* falls under, else the registrable domain.

    Keeps meaningful subdomains distinct (``pubmed.ncbi.nlm.nih.gov`` resolves to the curated
    ``ncbi.nlm.nih.gov`` rather than collapsing to ``nih.gov``).
    """
    host = (normalized_host or "").lower()
    for dom in _all_curated_domains():  # already longest-first
        if _suffix_match(host, dom):
            return dom
    return (registrable_domain or host).lower()


def authority_type_for(normalized_host: str) -> str | None:
    """Longest-suffix authority classification, or ``None`` if no curated list matches."""
    host = (normalized_host or "").lower()
    for dom, auth in _authority_pairs():  # longest-first
        if _suffix_match(host, dom):
            return auth
    return None


def control_for(normalized_host: str, registrable_domain: str) -> str | None:
    """ABBVIE / COMPETITOR from explicit config domains first, then brand-name tokens."""
    host = (normalized_host or "").lower()
    reg = (registrable_domain or "").lower()

    for dom in abbvie_domains():
        if _suffix_match(host, dom) or reg == dom:
            return CONTROL_ABBVIE
    for dom in competitor_domains():
        if _suffix_match(host, dom) or reg == dom:
            return CONTROL_COMPETITOR

    # Brand-name token match (e.g. "stelara" in stelara.com). Exact token equality only,
    # to avoid false positives from substrings.
    sld = reg.split(".")[0] if reg else ""
    tokens = {t for t in re.split(r"[^a-z0-9]+", sld) if len(t) >= 4}
    tokens.add(sld)
    for alias, control in _brand_alias_ownership():  # longest-first
        if alias in tokens:
            return control
    return None
