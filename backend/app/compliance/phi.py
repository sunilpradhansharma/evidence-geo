"""Central PHI/PII detection & redaction (G2).

Single source of truth for identifying and stripping patient-identifying content from
untrusted text (harvested forum posts, CSV imports, operator-entered questions). Used
on every inbound free-text path AND before any free-text is logged/exported.

Layers (each stacks on the previous):
  1. A fast, dependency-free regex layer (always on) covering the direct
     identifiers: email, phone, SSN/MRN-style ids, dates, ages, @handles, IPs.
  2. A dependency-free heuristic layer (always on) covering self-disclosed names
     and US locations the strict patterns miss. Conservative by design so it
     preserves clinical phrasing (it does not touch drug or brand names).
  3. An optional NLP layer (AWS Comprehend Medical) for the harder HIPAA
     Safe-Harbor identifiers (names, geography, contextual). Off by default;
     enable with PHI_DETECTION_BACKEND=comprehend_medical.

The regex layer is intentionally a *superset* of the legacy ``utils.pii_lint`` patterns
so existing callers keep working after they are rewired here.
"""
from __future__ import annotations

import asyncio
import re

from app.utils.logging import get_logger

logger = get_logger("compliance.phi")

# --- Regex layer (placeholder token, compiled pattern) ---------------------------
# Order matters: more specific patterns run first so e.g. an SSN isn't partly eaten
# by the generic date/number patterns.
_REGEX_RULES: list[tuple[str, str, re.Pattern]] = [
    ("Email", "[email]", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("SSN", "[id]", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("MRN", "[id]", re.compile(r"(?i)\bMRN[:#]?\s*\d+\b")),
    ("Phone", "[phone]", re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b")),
    ("IP", "[ip]", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("FullDate", "[date]", re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")),
    # Ages — HIPAA treats age (esp. >89) as an identifier; in forum text age is a
    # strong quasi-identifier, so we redact explicit ages regardless of value.
    ("Age", "[age]", re.compile(
        r"(?i)\b\d{1,3}\s*(?:yo|y/o|y\.o\.|years?[ -]old|year[ -]old)\b"
    )),
    ("Age", "[age]", re.compile(r"(?i)\b(?:i\s*am|i'm|im|aged)\s+\d{1,3}\b")),
    # Social handles / usernames that re-identify a poster.
    ("Handle", "", re.compile(r"(?:(?<=\s)|^)(?:@|u/)\w{2,}", re.IGNORECASE)),
]

# DOB mention (no value captured) — flagged but the surrounding date pattern handles
# any actual date value.
_DOB_HINT = re.compile(r"(?i)\b(DOB|date of birth)\b")


# --- Heuristic layer (always on, dependency-free) --------------------------------
# Catches the self-disclosed names / US locations the strict direct-identifier rules
# above miss. Deliberately conservative: each rule is anchored to a disclosure verb, a
# relationship word, or a title, and the value it redacts must be a Capitalized token, so
# it does not mangle clinical text (drug and brand names, symptoms). Each rule keeps its
# leading context (captured as group 1) and redacts only the value, and is idempotent (a
# value already replaced by a placeholder cannot re-match). The optional Comprehend Medical
# layer below is the catch-all for anything these heuristics do not cover.
_NAME = r"[A-Z][A-Za-z'\-]+"
_US_STATES = (
    r"AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|"
    r"MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY"
)
_HEURISTIC_RULES: list[tuple[str, str, re.Pattern]] = [
    # "my name is Sarah", "my name's Sarah", "call me Sarah", "I am/I'm called Sarah"
    ("Name", r"\1 [name]", re.compile(
        r"((?i:\bmy name(?:'s| is)\b|\bi(?:'m| am) called\b|\bcall me\b))\s+" + _NAME
    )),
    # relationship + name: "my son Jake", "my wife Maria", "my partner Alex"
    ("Name", r"\1 [name]", re.compile(
        r"((?i:\bmy (?:son|daughter|husband|wife|mom|mother|dad|father|partner|"
        r"boyfriend|girlfriend|brother|sister|fiance|fiancee)\b))\s+" + _NAME
    )),
    # clinician name: "Dr. Smith", "Doctor Jones", "Prof Adams"
    ("Name", r"\1[name]", re.compile(
        r"((?i:\b(?:dr|doctor|prof|professor)\b)\.?\s+)" + _NAME
    )),
    # US "City, ST" after a location verb: "I live in Austin, TX", "from Fort Worth, TX"
    ("Location", r"\1[location]", re.compile(
        r"((?i:\b(?:in|from|near|based in|live in|moved to)\b)\s+)"
        + _NAME + r"(?:\s" + _NAME + r")?,\s*(?:" + _US_STATES + r")\b"
    )),
    # ZIP only after a state code or an explicit label (never a bare 5-digit number)
    ("Zip", r"\1[zip]", re.compile(r"(\b(?:" + _US_STATES + r")\s+)\d{5}(?:-\d{4})?\b")),
    ("Zip", r"\1[zip]", re.compile(r"((?i:\bzip(?:\s*code)?\b)[:#]?\s*)\d{5}(?:-\d{4})?")),
]

# Direct identifiers run first, then the softer heuristics.
_RULES: list[tuple[str, str, re.Pattern]] = [*_REGEX_RULES, *_HEURISTIC_RULES]


def scan(text: str) -> list[str]:
    """Return the sorted unique PHI/PII categories found via the regex layer.

    Synchronous and network-free — safe to call from any context. This is the
    drop-in replacement for the legacy ``pii_lint.scan_for_pii``.
    """
    text = text or ""
    found: set[str] = set()
    for name, _placeholder, pat in _RULES:
        if pat.search(text):
            found.add(name)
    if _DOB_HINT.search(text):
        found.add("DOB")
    return sorted(found)


_WS = re.compile(r"\s+")


def redact(text: str) -> tuple[str, list[str]]:
    """Regex-layer redaction. Returns ``(clean_text, flags)``.

    Replaces direct identifiers with neutral placeholders while preserving the
    surrounding phrasing (we want questions to read as people actually ask them).
    """
    text = text or ""
    flags: set[str] = set()
    out = text
    for name, placeholder, pat in _RULES:
        if pat.search(out):
            flags.add(name)
            out = pat.sub(placeholder, out)
    if _DOB_HINT.search(out):
        flags.add("DOB")
    out = _WS.sub(" ", out).strip()
    return out, sorted(flags)


# --- Optional NLP layer (AWS Comprehend Medical) ---------------------------------
def _comprehend_detect(text: str) -> list[dict]:
    """Call AWS Comprehend Medical DetectPHI. Sync (boto3); run via to_thread.

    Returns the raw entity list. Any failure raises — the async wrapper degrades.
    """
    import boto3  # local import: only needed when the NLP backend is enabled

    from app.config.settings import get_settings

    settings = get_settings()
    region = settings.phi_comprehend_region or settings.aws_region
    client = boto3.client(
        "comprehendmedical",
        region_name=region,
        aws_access_key_id=settings.aws_access_key_id or None,
        aws_secret_access_key=settings.aws_secret_access_key or None,
    )
    # Comprehend Medical caps a single DetectPHI call at 20k UTF-8 bytes.
    resp = client.detect_phi(Text=text[:20000])
    return resp.get("Entities", [])


def _apply_nlp_spans(text: str, entities: list[dict]) -> tuple[str, list[str]]:
    """Redact Comprehend Medical entity spans (highest offset first) and collect flags."""
    flags: set[str] = set()
    spans = sorted(
        (e for e in entities if "BeginOffset" in e and "EndOffset" in e),
        key=lambda e: e["BeginOffset"],
        reverse=True,
    )
    out = text
    for e in spans:
        category = (e.get("Type") or e.get("Category") or "PHI").title()
        flags.add(category)
        begin, end = e["BeginOffset"], e["EndOffset"]
        out = out[:begin] + f"[{category.lower()}]" + out[end:]
    return out, sorted(flags)


def _nlp_enabled() -> bool:
    from app.config.settings import get_settings

    return get_settings().phi_detection_backend.lower() == "comprehend_medical"


async def redact_async(text: str) -> tuple[str, list[str]]:
    """Full redaction: regex layer + (optional) Comprehend Medical NLP layer.

    Use this from async paths (e.g. the harvest pipeline) where the NLP backend's
    network call can run off the event loop. Falls back to the regex-only result if
    the NLP backend is disabled or errors — never raises (fail-open on detection,
    but callers treat any remaining flags as fail-closed; see harvest pipeline)."""
    clean, flags = redact(text)
    if not _nlp_enabled() or not clean:
        return clean, flags
    try:
        entities = await asyncio.to_thread(_comprehend_detect, clean)
        if entities:
            clean, nlp_flags = _apply_nlp_spans(clean, entities)
            flags = sorted(set(flags) | set(nlp_flags))
    except Exception as e:  # noqa: BLE001 — degrade to regex layer, but record it
        logger.warning("Comprehend Medical PHI detection failed; using regex layer only: %s", e)
        flags = sorted(set(flags) | {"NLP_UNAVAILABLE"})
    return clean, flags


async def scan_async(text: str) -> list[str]:
    """Detect-only async variant (regex + optional NLP)."""
    _clean, flags = await redact_async(text)
    return flags
