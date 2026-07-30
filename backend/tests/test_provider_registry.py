"""Registry claude-swap tests.

Setting ANTHROPIC_API_KEY flips the monitored `claude` target from AWS Bedrock (parametric,
no citations) to the direct Anthropic API with web-search grounding; a blank key keeps it on
Bedrock. Uses monkeypatched settings + clears the load_targets lru_cache, so no real key or
network is needed. Orchestrator/scoring config is untouched by this swap.
"""
from types import SimpleNamespace

from app.providers import registry


def _reload_targets_with(monkeypatch, **overrides):
    base = {
        "anthropic_api_key": "",
        "target_claude_anthropic_model_id": "claude-sonnet-4-5-20250929",
        "evidencemd_api_key": "",
    }
    base.update(overrides)
    monkeypatch.setattr(registry, "get_settings", lambda: SimpleNamespace(**base))
    registry.load_targets.cache_clear()
    try:
        return {t.name: t for t in registry.load_targets()}
    finally:
        registry.load_targets.cache_clear()  # don't leak the fake into other tests


def test_no_key_keeps_claude_on_bedrock(monkeypatch):
    targets = _reload_targets_with(monkeypatch, anthropic_api_key="")
    claude = targets["claude"]
    assert claude.provider == "bedrock"
    assert not claude.params.extra.get("grounding")


def test_key_switches_claude_to_anthropic(monkeypatch):
    targets = _reload_targets_with(monkeypatch, anthropic_api_key="sk-ant-xxx")
    claude = targets["claude"]
    assert claude.provider == "anthropic"
    assert claude.model_id == "claude-sonnet-4-5-20250929"
    assert claude.params.extra.get("grounding") is True
    # force_search → every Claude answer runs web search (citation parity with Gemini/GPT-4o).
    assert claude.params.extra.get("force_search") is True


def test_key_switch_leaves_other_targets_on_bedrock(monkeypatch):
    targets = _reload_targets_with(monkeypatch, anthropic_api_key="sk-ant-xxx")
    # Only claude moves; nova-pro/llama stay Bedrock, gemini/gpt-4o stay on their providers.
    assert targets["nova-pro"].provider == "bedrock"
    assert targets["llama"].provider == "bedrock"
    assert targets["gemini"].provider == "google"
    assert targets["gpt-4o"].provider == "openai"


def test_anthropic_client_registered():
    # The provider client is wired so a swapped target can be dispatched.
    assert registry.get_provider_client("anthropic").name == "anthropic"
