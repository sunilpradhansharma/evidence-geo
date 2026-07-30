"""Shared LLM helper for the insights pipeline.

Reuses the configured scoring model (targets.yaml -> `scoring`) so theme discovery runs on
the same provider/credentials that already power response scoring. Returns parsed JSON with a
single retry, mirroring app.scoring.scorer's robustness.
"""
import json
import re

from app.providers.base import ModelParams
from app.providers.registry import get_provider_client, get_scoring_config
from app.utils.logging import get_logger

logger = get_logger("insights.llm")

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str):
    """Best-effort parse of a JSON object/array from model output."""
    if not text or not text.strip():
        raise ValueError("empty model output")

    candidates: list[str] = []
    fence = _FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(text.strip())
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end != -1 and end > start:
            candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:  # noqa: BLE001 — try the next candidate
            continue
    raise ValueError("no JSON found in model output")


async def chat_json(system: str, user: str, *, max_tokens: int = 2000):
    """Call the scoring model and return parsed JSON. Retries once on any failure."""
    cfg = get_scoring_config()
    client = get_provider_client(cfg.provider)
    params = ModelParams(max_tokens=max_tokens, temperature=0.0)

    last_err: Exception | None = None
    for attempt in range(2):
        try:
            result = await client.chat(cfg.model_id, system, user, params)
            return extract_json(result.text)
        except Exception as e:  # noqa: BLE001 — transport/parse failure, retry once
            last_err = e
            logger.warning("insights chat_json attempt %d failed: %s", attempt + 1, e)
    raise RuntimeError(f"insights LLM call failed: {last_err}")
