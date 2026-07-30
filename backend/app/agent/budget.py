"""Token budget guard and cost estimation (NF-014, NF-015)."""
from functools import lru_cache

from app.config.settings import load_yaml_config


@lru_cache
def _pricing() -> dict:
    return load_yaml_config("pricing.yaml")


def estimate_cost(model_id: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Estimate USD cost for a single call based on config/pricing.yaml (NF-014)."""
    cfg = _pricing()
    entry = None
    for row in cfg.get("pricing", []):
        if row["match"] in model_id:
            entry = row
            break
    if entry is None:
        entry = cfg.get("default", {"input_per_million": 1.0, "output_per_million": 3.0})
    cost = (
        prompt_tokens / 1_000_000 * entry["input_per_million"]
        + completion_tokens / 1_000_000 * entry["output_per_million"]
    )
    return round(cost, 6)


class BudgetGuard:
    """Tracks cumulative tokens against MAX_TOKENS_PER_RUN (NF-015)."""

    def __init__(self, max_tokens: int):
        self.max_tokens = max_tokens
        self.used = 0

    def add(self, tokens: int) -> None:
        self.used += tokens

    def exceeded(self) -> bool:
        return self.max_tokens > 0 and self.used >= self.max_tokens
