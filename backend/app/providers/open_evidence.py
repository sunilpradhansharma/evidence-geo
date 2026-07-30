"""Open Evidence provider client — placeholder (API access not yet confirmed).

Conforms to the ProviderClient contract so it can be enabled in targets.yaml
with zero code changes once the API is available.
"""
from app.providers.base import (
    Fatal,
    HealthStatus,
    ModelParams,
    ProviderClient,
    ProviderResult,
)


class OpenEvidenceClient(ProviderClient):
    """Placeholder for Open Evidence API integration."""

    name = "open-evidence"

    async def chat(
        self, model_id: str, system: str, user: str, params: ModelParams
    ) -> ProviderResult:
        raise Fatal(
            "Open Evidence API is not yet integrated. "
            "Disable the open-evidence target in targets.yaml or wait for API access."
        )

    async def health_check(self, model_id: str) -> HealthStatus:
        return HealthStatus(
            ok=False,
            detail="dormant — Open Evidence API access pending confirmation",
        )
