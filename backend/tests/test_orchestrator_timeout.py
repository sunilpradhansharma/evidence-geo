"""Regression: a hung provider call must not freeze the whole run.

A target whose socket stalls used to block `_call_target_with_retry` forever (there was no
per-call timeout), so the question never committed, its concurrency slot never freed, and the
run stayed RUNNING indefinitely (observed live: frozen at 2/100 questions for ~1.5h). The fix
wraps `client.chat(...)` in `asyncio.wait_for(get_settings().target_call_timeout_seconds)` and
fails fast on timeout. These tests pin that behavior and confirm the healthy path is untouched.

Uses asyncio_mode=auto (pytest.ini), monkeypatched provider client + settings, and a tiny
timeout so the suite stays fast. No network and no real provider are involved.
"""
import asyncio
import time
from types import SimpleNamespace

from app.agent import orchestrator
from app.agent.rate_limiter import RateLimiterRegistry
from app.providers.base import ModelParams, ProviderResult
from app.providers.registry import Target


class _HangingClient:
    """Simulates a provider whose socket stalls: chat() never returns on its own."""

    async def chat(self, model_id, system, user, params):
        await asyncio.Event().wait()  # blocks until cancelled by the timeout


class _FastClient:
    """A healthy provider that returns promptly with a complete answer."""

    async def chat(self, model_id, system, user, params):
        return ProviderResult(
            text="This is a complete answer.",  # ends in '.' so looks_truncated() is False
            finish_reason="stop",
            prompt_tokens=1,
            completion_tokens=2,
            model_version=model_id,
            raw_status=200,
        )


def _target() -> Target:
    return Target(
        name="gemini",
        provider="google",
        model_id="gemini-test",
        params=ModelParams(max_tokens=64, temperature=0.0),
        enabled=True,
        role="TARGET",
        rate_limit={"rpm": 600},  # high so the token bucket never blocks the test
    )


def _patch(monkeypatch, client, timeout_seconds):
    monkeypatch.setattr(orchestrator, "get_provider_client", lambda provider: client)
    monkeypatch.setattr(
        orchestrator,
        "get_settings",
        lambda: SimpleNamespace(target_call_timeout_seconds=timeout_seconds),
    )


async def test_hung_target_times_out_as_failed(monkeypatch):
    """A stalled call becomes a FAILED response instead of hanging the run."""
    _patch(monkeypatch, _HangingClient(), timeout_seconds=0.3)

    started = time.monotonic()
    # Outer guard: if the fix regressed (no per-call timeout) the inner call would hang
    # forever; this bounds the test rather than freezing the whole suite.
    result, status, error = await asyncio.wait_for(
        orchestrator._call_target_with_retry(
            _target(), "sys", "user", RateLimiterRegistry(), "run-timeout-test"
        ),
        timeout=10,
    )
    elapsed = time.monotonic() - started

    assert result is None
    assert status == "FAILED"
    assert "Timed out" in (error or "")
    # Fail-fast: a single timeout period (~0.3s), NOT retried 4x with backoff (~15s).
    assert elapsed < 3.0


async def test_fast_target_still_succeeds_through_wait_for(monkeypatch):
    """The wait_for wrapper must not disturb a normal, prompt response."""
    _patch(monkeypatch, _FastClient(), timeout_seconds=30)

    result, status, error = await orchestrator._call_target_with_retry(
        _target(), "sys", "user", RateLimiterRegistry(), "run-ok-test"
    )

    assert status == "SUCCESS"
    assert error is None
    assert result is not None
    assert result.text == "This is a complete answer."
