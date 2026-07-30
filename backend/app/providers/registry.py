"""Target registry — loads targets.yaml and wires logical targets to provider clients."""
from dataclasses import dataclass, field
from functools import lru_cache

from app.config.settings import get_settings, load_yaml_config
from app.providers.anthropic_client import AnthropicClient
from app.providers.base import ModelParams, ProviderClient
from app.providers.bedrock import BedrockClient
from app.providers.evidencemd_client import EvidenceMDClient
from app.providers.google_client import GoogleClient
from app.providers.open_evidence import OpenEvidenceClient
from app.providers.openai_client import OpenAIClient


@dataclass
class Target:
    name: str
    provider: str
    model_id: str
    params: ModelParams
    enabled: bool
    role: str = "TARGET"
    rate_limit: dict = field(default_factory=dict)


def _build_params(raw: dict) -> ModelParams:
    raw = raw or {}
    known = {"max_tokens", "temperature"}
    return ModelParams(
        max_tokens=raw.get("max_tokens", 1024),
        temperature=raw.get("temperature", 0.3),
        extra={k: v for k, v in raw.items() if k not in known},
    )


@lru_cache
def _provider_clients() -> dict[str, ProviderClient]:
    """Instantiate one client per provider (lazy, cached)."""
    return {
        "bedrock": BedrockClient(),
        "anthropic": AnthropicClient(),
        "openai": OpenAIClient(),
        "evidencemd": EvidenceMDClient(),
        "google": GoogleClient(),
        "open-evidence": OpenEvidenceClient(),
    }


def get_provider_client(provider: str) -> ProviderClient:
    clients = _provider_clients()
    if provider not in clients:
        raise ValueError(f"Unknown provider: {provider}")
    return clients[provider]


@lru_cache
def load_targets() -> list[Target]:
    cfg = load_yaml_config("targets.yaml")
    settings = get_settings()
    targets = []
    for raw in cfg.get("targets", []):
        enabled = raw.get("enabled", False)
        provider = raw["provider"]
        model_id = raw["model_id"]
        params = _build_params(raw.get("params", {}))
        # EvidenceMD is opt-in: only enabled when its API key is configured, so a run
        # never fails on a missing credential when no key is present.
        if provider == "evidencemd" and not settings.evidencemd_api_key:
            enabled = False
        # The monitored `claude` target auto-switches from AWS Bedrock (parametric, no
        # citations) to the direct Anthropic API with the native web_search tool the moment
        # ANTHROPIC_API_KEY is set — no YAML edit needed. Injecting `grounding` HERE (not in
        # targets.yaml) keeps Bedrock-mode claude OUT of the citation-capable set, so the
        # fallback path never drags Source Authority coverage. Orchestrator + scoring are
        # configured separately and stay on Bedrock.
        if raw["name"] == "claude" and settings.anthropic_api_key:
            provider = "anthropic"
            model_id = settings.target_claude_anthropic_model_id
            params.extra["grounding"] = True
            # force_search → always run web search so EVERY Claude answer carries citations,
            # matching Gemini/GPT-4o (which the analyst compares side by side). Bedrock ignores it.
            params.extra["force_search"] = True
        targets.append(
            Target(
                name=raw["name"],
                provider=provider,
                model_id=model_id,
                params=params,
                enabled=enabled,
                role=raw.get("role", "TARGET"),
                rate_limit=raw.get("rate_limit", {}),
            )
        )
    return targets


def enabled_targets() -> list[Target]:
    return [t for t in load_targets() if t.enabled]


# Provider-only clinical targets — excluded from the unknown-persona fallback below so they
# can never run for Patient/Prospect (defense-in-depth; persona is validated at creation).
_PROVIDER_ONLY_TARGETS = {"evidencemd", "open-evidence"}


def targets_for_persona(persona: str) -> list[Target]:
    """Filter enabled targets by persona-based routing rules."""
    routing = load_yaml_config("target_routing.yaml")
    persona_cfg = routing.get("routing", {}).get(persona)
    all_enabled = enabled_targets()
    if persona_cfg is None:
        # Unknown persona: all enabled EXCEPT Provider-only clinical targets.
        return [t for t in all_enabled if t.name not in _PROVIDER_ONLY_TARGETS]
    allowed_names = set(persona_cfg.get("targets", []))
    return [t for t in all_enabled if t.name in allowed_names]


def get_orchestrator_config() -> Target:
    cfg = load_yaml_config("targets.yaml")
    raw = cfg["orchestrator"]
    return Target(
        name="orchestrator",
        provider=raw["provider"],
        model_id=raw["model_id"],
        params=_build_params(raw.get("params", {})),
        enabled=True,
        role="ORCHESTRATOR",
    )


def get_scoring_config() -> Target:
    cfg = load_yaml_config("targets.yaml")
    raw = cfg["scoring"]
    return Target(
        name="scoring",
        provider=raw["provider"],
        model_id=raw["model_id"],
        params=_build_params(raw.get("params", {})),
        enabled=True,
        role="ORCHESTRATOR",
    )
