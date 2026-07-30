"""Hybrid intent classifier — The Triage Gate (Layer 1 + Layer 2).

Layer 1: deterministic (persona, domain) → intent from intent_rules.yaml.
Layer 2: lightweight Claude Haiku call for UNCERTAIN cases.
"""
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from app.config.settings import load_yaml_config
from app.providers.base import ModelParams
from app.providers.registry import get_orchestrator_config, get_provider_client
from app.utils.logging import get_logger

logger = get_logger("intent_classifier")

IntentType = Literal["CLINICAL", "EXPERIENTIAL", "SHORTHAND", "SCREENING"]
VALID_INTENTS: set[str] = {"CLINICAL", "EXPERIENTIAL", "SHORTHAND", "SCREENING"}


@dataclass
class IntentResult:
    intent: IntentType
    source: Literal["RULE", "LLM"]
    confidence: float


@lru_cache
def _load_rules() -> dict:
    return load_yaml_config("intent_rules.yaml")


def _check_shorthand(question_text: str) -> bool:
    """Check if the question matches any shorthand keyword pattern."""
    rules = _load_rules()
    max_words = rules.get("shorthand_max_words", 6)
    if max_words and len(question_text.split()) > max_words:
        return False
    patterns = rules.get("shorthand_patterns", [])
    for pattern in patterns:
        try:
            if re.search(pattern, question_text, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def classify_by_rules(persona: str, domain: str, question_text: str) -> IntentResult | None:
    """Layer 1: deterministic classification from (persona, domain) mapping.

    Returns IntentResult if a rule matches, None if UNCERTAIN.
    """
    # Check shorthand patterns first — they override persona/domain rules
    if _check_shorthand(question_text):
        return IntentResult(intent="SHORTHAND", source="RULE", confidence=0.9)

    rules = _load_rules()
    persona_rules = rules.get("rules", {}).get(persona, {})
    intent = persona_rules.get(domain)

    if intent and intent in VALID_INTENTS:
        return IntentResult(intent=intent, source="RULE", confidence=0.95)

    return None  # UNCERTAIN — delegate to Layer 2


async def classify_by_llm(question_text: str, persona: str, domain: str) -> IntentResult:
    """Layer 2: classify using Claude Haiku when rules are insufficient."""
    cfg = get_orchestrator_config()
    client = get_provider_client(cfg.provider)

    system = (
        "You are a medical query intent classifier. Classify the user's question "
        "into exactly ONE of these categories:\n"
        "- CLINICAL: Technical, trial-specific, guideline-driven, dosing, safety data\n"
        "- EXPERIENTIAL: Practical, lifestyle, emotional, patient-experience\n"
        "- SCREENING: Comparative, exploratory, 'what are my options'\n"
        "- SHORTHAND: Abbreviated, jargon-heavy, single-drug-name queries\n\n"
        "Respond with ONLY the single category word, nothing else."
    )
    user = f"Persona: {persona}\nDomain: {domain}\nQuestion: {question_text}\n\nCategory:"

    try:
        result = await client.chat(
            cfg.model_id, system, user,
            ModelParams(max_tokens=10, temperature=0.0),
        )
        raw = (result.text or "").strip().upper()
        for valid in VALID_INTENTS:
            if valid in raw:
                return IntentResult(intent=valid, source="LLM", confidence=0.8)
        # Fallback if LLM returns something unexpected
        return IntentResult(intent="SCREENING", source="LLM", confidence=0.5)
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM intent classification failed: %s — defaulting to SCREENING", e)
        return IntentResult(intent="SCREENING", source="RULE", confidence=0.3)


async def classify_intent(
    question_text: str, persona: str, domain: str
) -> IntentResult:
    """Main entry point: Layer 1 (rules) → Layer 2 (LLM) if uncertain."""
    # Layer 1: deterministic
    result = classify_by_rules(persona, domain, question_text)
    if result is not None:
        return result

    # Layer 2: LLM fallback
    return await classify_by_llm(question_text, persona, domain)
