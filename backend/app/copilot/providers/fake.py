"""Fake async LLM provider — scripted responses for tests / offline dev.

Seed it with an ordered list of :class:`LLMResponse` (consumed one per
``invoke``) or a callable that maps the request to a response. Falls back to
a benign text response so a smoke test never raises.
"""
from __future__ import annotations

from typing import Any, Callable

from app.copilot.providers.base import LLMMessage, LLMResponse


class FakeProvider:
    def __init__(
        self,
        responses: list[LLMResponse] | None = None,
        *,
        handler: Callable[[list[LLMMessage], str], LLMResponse] | None = None,
        model_id: str = "fake-copilot",
    ) -> None:
        self._model_id = model_id
        self._responses = list(responses or [])
        self._handler = handler
        self.call_count = 0
        self.last_request: tuple[str, list[LLMMessage]] | None = None

    @property
    def model_id(self) -> str:
        return self._model_id

    async def invoke(
        self,
        messages: list[LLMMessage],
        *,
        system: str = "",
        temperature: float = 0.0,
        max_tokens: int = 1024,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
    ) -> LLMResponse:
        self.call_count += 1
        self.last_request = (system, list(messages))
        if self._handler is not None:
            return self._handler(messages, system)
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(
            text="(fake provider) No scripted response available.",
            model_id=self._model_id,
        )
