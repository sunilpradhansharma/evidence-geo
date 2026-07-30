"""Relevance-gate tests for ad-hoc Social Listening searches (app.social.guard).

The gate runs one LLM call to keep off-topic queries (e.g. "yogurt") from triggering an
expensive Apify capture. These tests monkeypatch the LLM helper so they never hit a provider.
"""
from unittest.mock import AsyncMock

from app.social import guard


async def test_relevant_query_is_allowed(monkeypatch):
    monkeypatch.setattr(
        guard, "chat_json", AsyncMock(return_value={"relevant": True, "reason": "medical topic"})
    )
    allowed, reason = await guard.is_pharma_relevant("Lupron endometriosis")
    assert allowed is True
    assert reason == ""


async def test_off_topic_query_is_blocked_with_reason(monkeypatch):
    monkeypatch.setattr(
        guard,
        "chat_json",
        AsyncMock(return_value={"relevant": False, "reason": "Yogurt is a food, not a medical topic."}),
    )
    allowed, reason = await guard.is_pharma_relevant("yogurt")
    assert allowed is False
    assert "food" in reason.lower()


async def test_blocked_query_gets_fallback_reason(monkeypatch):
    monkeypatch.setattr(
        guard, "chat_json", AsyncMock(return_value={"relevant": False, "reason": ""})
    )
    allowed, reason = await guard.is_pharma_relevant("bitcoin price")
    assert allowed is False
    assert reason  # a non-empty message is always shown to the analyst


async def test_empty_query_short_circuits_without_calling_the_llm(monkeypatch):
    spy = AsyncMock()
    monkeypatch.setattr(guard, "chat_json", spy)
    allowed, reason = await guard.is_pharma_relevant("   ")
    assert allowed is False
    assert reason
    spy.assert_not_awaited()


async def test_fails_open_on_llm_error(monkeypatch):
    monkeypatch.setattr(guard, "chat_json", AsyncMock(side_effect=RuntimeError("boom")))
    allowed, reason = await guard.is_pharma_relevant("anything")
    assert allowed is True
    assert reason == ""


async def test_fails_open_on_malformed_output(monkeypatch):
    monkeypatch.setattr(guard, "chat_json", AsyncMock(return_value=["not", "a", "dict"]))
    allowed, _ = await guard.is_pharma_relevant("anything")
    assert allowed is True
