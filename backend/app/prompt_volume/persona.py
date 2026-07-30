"""Heuristic persona inference for Prompt Volume queries (FR-116, Profound "Personas" parity).

Dependency-light keyword heuristics (NO ML), consistent with the Jaccard matching used
elsewhere in this package. Maps a raw search query to one of the monitored audiences
(Patient, Provider, Prospect) or "Unclassified" when no signal is present. These are generic
English clinical / consumer cues, NOT proprietary content — brand data stays in config
(SE-007). Precedence when signals tie: Provider > Prospect > Patient (a clinical cue is more
specific and higher-value than a comparison cue, which is more specific than a generic
consumer cue).
"""
from __future__ import annotations

import re

PATIENT = "Patient"
PROVIDER = "Provider"
PROSPECT = "Prospect"
UNCLASSIFIED = "Unclassified"

# Ordered by precedence for tie-breaks (highest first).
PERSONAS: tuple[str, ...] = (PROVIDER, PROSPECT, PATIENT, UNCLASSIFIED)

# Clinical / technical cues -> a Provider (HCP) is asking.
_PROVIDER_TERMS: tuple[str, ...] = (
    "dosing", "dose", "dosage", "titration", "titrate", "mechanism of action", "moa",
    "pharmacokinetics", "pharmacodynamics", "contraindication", "contraindications",
    "indication", "indicated", "efficacy", "clinical trial", "guideline", "guidelines",
    "prescribing", "prescriber", "monotherapy", "combination therapy", "biosimilar",
    "half life", "renal", "hepatic", "adverse reactions", "monitoring", "first line",
    "second line", "onset of action", "administration", "loading dose",
)

# Comparison / evaluation / switching cues -> a Prospect is weighing options.
_PROSPECT_TERMS: tuple[str, ...] = (
    "vs", "versus", "compare", "comparison", "better than", "better", "best", "alternative",
    "alternatives", "switch", "switching", "instead of", "difference between", "worth it",
    "which is better", "reviews", "review", "success rate", "how effective", "should i take",
    "should i start",
)

# Experiential / consumer / access / self cues -> a Patient is living with the therapy.
_PATIENT_TERMS: tuple[str, ...] = (
    "side effects", "side effect", "how long", "feel", "feeling", "weight gain", "weight",
    "hair loss", "nausea", "tired", "fatigue", "pain", "hurt", "injection", "inject", "pen",
    "syringe", "cost", "price", "copay", "coupon", "insurance", "covered", "assistance",
    "help paying", "pregnancy", "pregnant", "breastfeeding", "alcohol", "diet", "missed dose",
    "miss a dose", "stop taking", "how to use", "when to take",
)

_BUCKETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (PROVIDER, _PROVIDER_TERMS),
    (PROSPECT, _PROSPECT_TERMS),
    (PATIENT, _PATIENT_TERMS),
)

_pattern_cache: dict[str, re.Pattern] = {}


def _hits(query_lower: str, term: str) -> int:
    """Word-boundary count of a (possibly multi-word) term in the query."""
    pat = _pattern_cache.get(term)
    if pat is None:
        pat = re.compile(r"(?<![a-z0-9])" + re.escape(term.strip()) + r"(?![a-z0-9])")
        _pattern_cache[term] = pat
    return len(pat.findall(query_lower))


def classify_persona(query: str) -> str:
    """Return the best-matching persona for a raw query, or ``Unclassified``.

    Counts keyword hits per bucket and picks the highest; ties break by the PERSONAS
    precedence order (Provider > Prospect > Patient). Zero signal -> ``Unclassified`` so the
    breakdown never fabricates an audience it cannot infer.
    """
    q = (query or "").lower()
    if not q:
        return UNCLASSIFIED

    scores = {name: sum(_hits(q, term) for term in terms) for name, terms in _BUCKETS}
    best_name, best_score = UNCLASSIFIED, 0
    for name in (PROVIDER, PROSPECT, PATIENT):  # precedence order
        if scores[name] > best_score:
            best_name, best_score = name, scores[name]
    return best_name
