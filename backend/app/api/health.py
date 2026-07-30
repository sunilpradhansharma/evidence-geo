"""Health-check endpoints (NF-009, IN-502) — per-target connectivity report."""
import asyncio

from fastapi import APIRouter

from app.providers.registry import enabled_targets, get_provider_client

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    """Basic liveness check."""
    return {"status": "ok"}


@router.get("/health/targets")
async def health_targets():
    """Verify connectivity to every enabled LLM target (NF-009)."""
    targets = enabled_targets()

    async def check(t):
        client = get_provider_client(t.provider)
        status = await client.health_check(t.model_id)
        return {
            "name": t.name,
            "provider": t.provider,
            "model_id": t.model_id,
            "ok": status.ok,
            "detail": status.detail,
        }

    results = await asyncio.gather(*(check(t) for t in targets))
    all_ok = all(r["ok"] for r in results)
    return {"all_ok": all_ok, "targets": results}
