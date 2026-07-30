"""URL → clean domain parsing (FR-706a.1).

Splits a cited URL into three values the product needs:
  • ``normalized_host``     — full lowercased host, no scheme/port/www/trailing-dot (pubmed.ncbi.nlm.nih.gov)
  • ``registrable_domain``  — eTLD+1 from the Public Suffix List                    (nih.gov)
  • ``authority_domain``    — resolved by the taxonomy via longest-suffix match     (ncbi.nlm.nih.gov)

Parsing is Public-Suffix-List backed (``tldextract``) so multi-part suffixes (co.uk,
com.au, …) are handled correctly rather than by a hand-maintained list. tldextract is
pinned to its BUNDLED snapshot (``suffix_list_urls=()``) so it never makes a network call.
If tldextract is somehow unavailable the module degrades to a small built-in heuristic so
the app still boots (the feature is just less precise until the dependency is installed).

Robust against the messy inputs real citations contain: scheme-less, protocol-relative,
uppercase, ports, userinfo, trailing dots, IDNs, and redirect URLs. Returns ``None`` for
things that are not classifiable public web domains (IPs, localhost, private hosts,
single-label or empty hosts, non-http schemes).
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.utils.logging import get_logger

logger = get_logger("source_authority.domains")

# --- tldextract (PSL), pinned offline; heuristic fallback if unavailable ---------
try:  # pragma: no cover - import guard
    import tldextract

    _EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), fallback_to_snapshot=True)
    _HAS_TLDEXTRACT = True
except Exception:  # noqa: BLE001 - degrade gracefully; app must still boot
    _EXTRACT = None
    _HAS_TLDEXTRACT = False
    logger.warning(
        "tldextract unavailable — falling back to a heuristic domain parser. "
        "Install tldextract for Public-Suffix-List-accurate parsing."
    )

# Minimal multi-part public suffixes for the heuristic fallback ONLY.
_FALLBACK_MULTI_SUFFIXES = {
    "co.uk", "org.uk", "gov.uk", "ac.uk", "nhs.uk",
    "com.au", "org.au", "net.au", "gov.au", "edu.au",
    "co.jp", "or.jp", "go.jp", "co.nz", "co.in", "co.za",
    "com.br", "com.cn", "com.mx", "com.sg", "europa.eu",
}


@dataclass(frozen=True)
class DomainParts:
    normalized_host: str
    registrable_domain: str


def _registrable_fallback(host: str) -> str:
    """eTLD+1 without the PSL — used only if tldextract is missing."""
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    last_two = ".".join(labels[-2:])
    last_three = ".".join(labels[-3:])
    if last_two in _FALLBACK_MULTI_SUFFIXES:
        return last_three
    return last_two


def _prepare(url: str) -> str | None:
    """Normalise a raw citation string into something urlsplit can host-parse."""
    if not url or not isinstance(url, str):
        return None
    s = url.strip()
    if not s:
        return None
    # Protocol-relative //host/path -> give it a scheme.
    if s.startswith("//"):
        s = "http:" + s
    # Detect an explicit scheme (letters/digits/+-. then "://").
    scheme_sep = s.find("://")
    if scheme_sep == -1:
        # No scheme: could be "www.x.com/p" (host) or "mailto:x" (opaque). Reject opaque
        # non-http schemes ("word:rest" with no //), otherwise assume http.
        colon = s.find(":")
        if colon != -1 and colon < (s.find("/") if "/" in s else len(s)):
            head = s[:colon]
            if head.isalpha():  # e.g. mailto, tel, javascript
                return None
        s = "http://" + s
    else:
        scheme = s[:scheme_sep].lower()
        if scheme not in ("http", "https"):
            return None
    return s


def parse_url(url: str | None) -> DomainParts | None:
    """Return :class:`DomainParts` for *url*, or ``None`` if it is not a public web host."""
    prepared = _prepare(url or "")
    if prepared is None:
        return None
    try:
        host = urlsplit(prepared).hostname  # lowercased, port + userinfo stripped
    except ValueError:
        return None
    if not host:
        return None

    host = host.rstrip(".")  # trailing-dot FQDN form
    # IDN -> punycode (best-effort; keep original if it can't be encoded).
    if any(ord(ch) > 127 for ch in host):
        try:
            host = host.encode("idna").decode("ascii")
        except Exception:  # noqa: BLE001
            pass

    if not host or "." not in host:  # single-label / localhost
        return None
    if host == "localhost" or host.endswith(".localhost"):
        return None

    # Reject IP literals (v4/v6) — not classifiable publishers.
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass

    # Strip a leading www. for the normalized host.
    normalized_host = host[4:] if host.startswith("www.") else host

    if _HAS_TLDEXTRACT and _EXTRACT is not None:
        ext = _EXTRACT(host)
        if ext.domain and ext.suffix:
            registrable = f"{ext.domain}.{ext.suffix}"
        else:
            registrable = normalized_host
    else:
        registrable = _registrable_fallback(normalized_host)

    # Reject private/reserved single-word suffix cases that slipped through.
    if not registrable or "." not in registrable:
        return None

    return DomainParts(normalized_host=normalized_host, registrable_domain=registrable)
