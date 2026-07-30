"""Prompt-injection / jailbreak detection for untrusted text (G3).

Harvested questions are scraped verbatim from public forums and later fed to target
LLMs. Untrusted web text can carry prompt-injection payloads ("ignore previous
instructions...", "you are now DAN", embedded system prompts, etc.). This module is a
fast, deterministic screen applied:
  - in the harvest pipeline (flag suspicious staged rows),
  - at promotion (hard block — defense in depth),
  - at orchestrator dispatch (final gate before any text reaches a target model).

Deterministic by design: cheap, explainable, and never sends text anywhere. It favors
recall on the well-known injection shapes; a flagged item is quarantined for human
review rather than silently dropped.
"""
from __future__ import annotations

import re

# Each rule: (label, compiled pattern). Patterns are intentionally broad but anchored
# to phrasing that is highly unusual in a genuine patient/provider health question.
_RULES: list[tuple[str, re.Pattern]] = [
    ("ignore_instructions", re.compile(
        r"(?i)\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b"
        r"(previous|prior|above|earlier|all)\b[^.\n]{0,20}\b"
        r"(instruction|instructions|prompt|prompts|rule|rules|context)\b"
    )),
    ("system_prompt_injection", re.compile(
        r"(?i)\b(system\s*prompt|developer\s*message|<\s*/?\s*system\s*>|"
        r"\[\s*system\s*\]|begin\s+system)\b"
    )),
    ("role_override", re.compile(
        r"(?i)\byou\s+are\s+now\b|\bact\s+as\b[^.\n]{0,30}\b(?:dan|jailbreak|unrestricted|"
        r"developer\s*mode|no\s+restrictions?)\b|\bpretend\s+(?:to\s+be|you\s+are)\b"
    )),
    ("jailbreak_terms", re.compile(
        r"(?i)\b(jailbreak|dan\s+mode|do\s+anything\s+now|bypass\b[^.\n]{0,20}\b"
        r"(?:filter|guardrail|safety|restriction)|disable\b[^.\n]{0,20}\b"
        r"(?:filter|guardrail|safety))\b"
    )),
    ("instruction_reveal", re.compile(
        r"(?i)\b(reveal|print|repeat|show|output)\b[^.\n]{0,30}\b"
        r"(your\s+(?:system\s+)?(?:prompt|instructions?|rules?)|the\s+above)\b"
    )),
    ("prompt_delimiters", re.compile(
        r"(?i)(```+\s*system|<\|im_start\|>|<\|im_end\|>|\[/?INST\]|\\n\\nHuman:|\\n\\nAssistant:)"
    )),
    ("encoded_payload", re.compile(
        # long base64-looking blobs are a common smuggling vector in scraped text
        r"\b[A-Za-z0-9+/]{120,}={0,2}\b"
    )),
]


def scan_injection(text: str) -> list[str]:
    """Return the sorted unique injection-rule labels matched. Empty list = clean."""
    text = text or ""
    found = {label for label, pat in _RULES if pat.search(text)}
    return sorted(found)


def is_injection(text: str) -> bool:
    """True if the text trips any prompt-injection rule."""
    return bool(scan_injection(text))
