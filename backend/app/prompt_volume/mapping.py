"""Map a search-demand query to the brand taxonomy (FR-116.1).

Reads the alias index built from brands.yaml via ``config.taxonomy`` (the SE-007 single
source of truth) — it does NOT maintain a separate prompt-volume taxonomy. A specific drug
name (focus brand or competitor) wins over an indication, which wins over a bare area name;
among the same kind the longest alias wins. Queries that match nothing are "Unmapped" with
confidence 0.0 rather than forced into a wrong category.

**Disease is resolved on a SECOND, independent pass.** The single-winner ranking above
discards the indication whenever a drug is also present, so in *"for atopic dermatitis, is
Rinvoq or Dupixent more effective?"* the Atopic Dermatitis hit was simply thrown away — and
that is the signal that decides which competitive field the question belongs to. The two
passes never compete, so the four pre-existing keys are byte-identical to before; ``disease``
is purely additive. ``test_multi_ta.py`` pins that with a snapshot over a fixed corpus,
because Prompt Volume (FR-116) consumes these keys directly.
"""
from __future__ import annotations

import re

from app.config.taxonomy import alias_index, disease_index, drug_index

# A specific drug beats a disease/indication, which beats a bare therapeutic-area name.
_KIND_PRIORITY = {"competitor": 3, "brand": 3, "indication": 2, "area": 1}

_alias_patterns: dict[str, re.Pattern] = {}


def _matches(query_lower: str, alias: str) -> bool:
    """Word-boundary containment so "ra" doesn't match inside "arthritis"."""
    pat = _alias_patterns.get(alias)
    if pat is None:
        pat = re.compile(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])")
        _alias_patterns[alias] = pat
    return pat.search(query_lower) is not None


def aliases_for_drug(name: str | None) -> tuple[str, ...]:
    """Every lowercased alias a drug is known by (brand, generic, curated aliases).

    Resolved through ``drug_index()`` so one curated record answers for all its spellings.
    Falls back to the bare name for an uncurated agent rather than returning nothing.
    """
    key = (name or "").strip().lower()
    if not key:
        return ()
    index = drug_index()
    record = index.get(key)
    if record is None:
        return (key,)
    canonical = (record.get("canonical") or "").strip().lower()
    return tuple(
        sorted(
            {alias for alias, entry in index.items()
             if (entry.get("canonical") or "").strip().lower() == canonical} | {key},
            key=len,
            reverse=True,
        )
    )


def mentions(text: str, name: str | None) -> bool:
    """True when *text* names this drug under any of its aliases.

    ``map_query`` deliberately returns a SINGLE winner, so it cannot answer "are both
    of these named here?" — the question a head-to-head comparison turns on. This shares
    the same word-boundary matcher rather than forking a second one, so "RA" still does
    not match inside "arthritis".
    """
    haystack = (text or "").lower()
    if not haystack:
        return False
    return any(_matches(haystack, alias) for alias in aliases_for_drug(name))


def resolve_disease(query: str) -> str | None:
    """Canonical disease named in *query*, or ``None``.

    Longest matching alias wins, so "plaque psoriasis" beats "psoriasis" and
    "non-radiographic axial spondyloarthritis" beats "axspa". ``disease_index()`` is
    already sorted longest-first, so the first hit is the answer.
    """
    q = (query or "").lower()
    if not q:
        return None
    for entry in disease_index():
        if _matches(q, entry["alias"]):
            return entry["disease"]
    return None


def map_query(query: str) -> dict:
    """Return ``{therapeutic_area, competitor, brand, confidence, disease}`` for a query."""
    q = (query or "").lower()
    disease = resolve_disease(q)

    best = None
    best_rank: tuple[int, int] | None = None
    for entry in alias_index():
        if not _matches(q, entry["alias"]):
            continue
        rank = (_KIND_PRIORITY.get(entry["kind"], 0), len(entry["alias"]))
        if best is None or rank > best_rank:
            best, best_rank = entry, rank

    if best is None:
        return {"therapeutic_area": "Unmapped", "competitor": None, "brand": None,
                "confidence": 0.0, "disease": disease}

    is_drug = best["kind"] in ("brand", "competitor")
    return {
        "therapeutic_area": best["ta_key"],
        "competitor": best["canonical"] if best["is_competitor"] else None,
        "brand": best["canonical"] if best["kind"] == "brand" else None,
        # A drug hit is a confident map; an indication/area hit is softer.
        "confidence": 1.0 if is_drug else 0.6,
        # Additive: the indication the query names, independent of the winner above.
        # None when the query names no disease we track.
        "disease": disease,
    }
