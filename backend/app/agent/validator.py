"""Response validation (FR-202, FR-211) — Claude orchestrator judgment + heuristics.

The orchestrator (role=ORCHESTRATOR) classifies suspicious responses. We use a cheap
deterministic heuristic first; the LLM orchestrator is consulted only for ambiguous cases
to keep cost and latency low while preserving the FR-202 "Claude coordinates" requirement.
"""
import re

from app.providers.base import ModelParams, ProviderResult
from app.providers.registry import get_orchestrator_config, get_provider_client

_TRUNCATION_HINTS = re.compile(r"[A-Za-z0-9,]\s*$")  # ends mid-word/clause, no terminal punctuation


def looks_truncated(result: ProviderResult) -> bool:
    """Heuristic truncation detection (FR-211)."""
    if result.finish_reason == "length":
        return True
    text = (result.text or "").rstrip()
    if not text:
        return False
    # Ends without sentence-terminating punctuation and not a list/colon
    if text[-1] not in ".!?:)]\"'" and _TRUNCATION_HINTS.search(text):
        return True
    return False


async def classify_with_orchestrator(question: str, response_text: str) -> dict:
    """Use the orchestrator model to classify a suspicious response (FR-202).

    Returns {"verdict": "OK|TRUNCATED|REFUSAL|UNCLEAR", "reason": str}.
    """
    cfg = get_orchestrator_config()
    client = get_provider_client(cfg.provider)
    system = (
        "You are an orchestration validator. Classify the assistant response to a user "
        "question into exactly one verdict: OK (complete & on-topic), TRUNCATED (cut off "
        "mid-thought), or REFUSAL (declined to answer). Respond with only the single word."
    )
    user = f"QUESTION:\n{question}\n\nRESPONSE:\n{response_text[:3000]}\n\nVerdict:"
    result = await client.chat(cfg.model_id, system, user, ModelParams(max_tokens=10, temperature=0.0))
    verdict = (result.text or "").strip().upper()
    for v in ("TRUNCATED", "REFUSAL", "OK"):
        if v in verdict:
            return {"verdict": v, "reason": verdict}
    return {"verdict": "UNCLEAR", "reason": verdict}
