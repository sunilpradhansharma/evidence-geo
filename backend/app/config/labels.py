"""Shared, mandated output labels (SE-007-safe — no brand/indication content)."""

# FR-108a.6/7: the EXACT string that must appear on every disease-state / pre-launch
# dashboard, report, and export. Do not alter the wording.
PRELAUNCH_LABEL = "Pre-Launch / Pipeline Intelligence - No AbbVie Brand Asset"

# Targets intentionally hidden from all user-facing analytics, response lists,
# comparison views, and filters. Their rows are still stored and still flow through
# the pipeline/scoring/consensus — they are simply not surfaced in the UI.
# `open-evidence` is the legacy manual-capture bridge; its automated replacement
# (`evidencemd`) remains visible. Edit this set to change what is hidden.
HIDDEN_LLM_NAMES: frozenset[str] = frozenset({"open-evidence"})

# Marketer-facing display names for the AI platforms (a.k.a. targets / LLMs). Keep in
# sync with the frontend MODEL_META in SourceAuthority.tsx. Lookup is case-insensitive;
# unknown ids fall back to the raw name so we never surface an ugly internal token.
AI_PLATFORM_LABELS: dict[str, str] = {
    "claude": "Claude",
    "evidencemd": "EvidenceMD",
    "open-evidence": "Open Evidence",
    "gemini": "Gemini",
    "gpt-4o": "GPT-4o",
    "nova-pro": "Nova Pro",
    "llama": "Llama",
}


def platform_label(name: str) -> str:
    """Friendly, marketer-facing label for an AI platform id (e.g. 'gpt-4o' -> 'GPT-4o')."""
    if not name:
        return name
    return AI_PLATFORM_LABELS.get(name.lower(), name)
