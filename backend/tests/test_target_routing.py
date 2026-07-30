"""Regression tests for persona-based target routing (registry.targets_for_persona).

Guards the safety invariant that clinical, Provider-audience targets (EvidenceMD /
OpenEvidence) never run for a non-Provider persona — including the unknown-persona
fallback. These assertions hold whether or not EvidenceMD is enabled (API key present),
so they are stable in CI.
"""
from app.providers.registry import _PROVIDER_ONLY_TARGETS, targets_for_persona


def _names(persona: str) -> set[str]:
    return {t.name for t in targets_for_persona(persona)}


def test_provider_only_targets_never_leak_to_non_provider_personas():
    # EvidenceMD / OpenEvidence are clinical, Provider-audience targets. They must never be
    # routed to Patient/Prospect, nor to an unknown persona via the conservative fallback.
    for persona in ("Patient", "Prospect", "__unknown_persona__", ""):
        leaked = _names(persona) & _PROVIDER_ONLY_TARGETS
        assert not leaked, f"{persona!r} routed to Provider-only target(s): {leaked}"


def test_public_models_still_route_to_every_persona():
    # Sanity: the always-on public target routes to every persona, including the fallback,
    # so the hardening did not empty out the fallback set.
    for persona in ("Patient", "Prospect", "Provider", "__unknown_persona__"):
        assert "claude" in _names(persona)
