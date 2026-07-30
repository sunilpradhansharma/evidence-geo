"""Deterministic adverse-event (AE) backstop (G4).

The primary AE detector is the LLM classifier (`harvest.classify`). This module is a
cheap, deterministic safety net so that a missed `adverse_event:true` — or a classifier
that errored/timed out — still trips the `QUARANTINED_AE` hold for pharmacovigilance
review. It favors RECALL over precision by design: a false positive only sends an item
to human review; a false negative could let a real safety signal slip into the bank.

This is intentionally a signal detector, NOT a reportability decision (that is the PV
team's process, out of scope here).
"""
from __future__ import annotations

import re

# Phrasings where the asker describes experiencing a harm/effect from a drug — the
# pharmacovigilance shape ("I started X and now I have Y"). General safety *questions*
# ("is X safe?") deliberately do NOT match.
_AE_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?i)\b(since|after)\s+(starting|taking|i\s+started|i\s+took|my\s+first)\b"),
    re.compile(r"(?i)\bi\s+(developed|got|experienced|noticed|have\s+been\s+having|"
               r"started\s+(?:having|getting|to))\b"),
    re.compile(r"(?i)\b(gave|made|caused)\s+me\b|\bcaused\s+my\b"),
    re.compile(r"(?i)\b(severe|bad|terrible|awful|horrible)\s+(side[\s-]*effects?|reactions?)\b"),
    re.compile(r"(?i)\b(adverse|allergic)\s+reaction\b"),
    re.compile(r"(?i)\bended\s+up\s+(?:in|at)\b[^.\n]{0,30}\b(er|hospital|emergency)\b"),
    re.compile(r"(?i)\b(landed|hospitalized|hospitalised)\b[^.\n]{0,20}\b(hospital|er)\b"),
    re.compile(r"(?i)\bever\s+since\s+(?:i|my)\b"),
    re.compile(r"(?i)\b(stopped|quit)\s+taking\b[^.\n]{0,30}\bbecause\b"),
]


def looks_like_ae(text: str) -> bool:
    """True if the text shows a deterministic adverse-event signal."""
    text = text or ""
    return any(pat.search(text) for pat in _AE_PATTERNS)
