"""Provider factory — picks Bedrock (default) or Fake from settings/env.

``COPILOT_PROVIDER=fake`` selects the scripted provider (tests / offline).
Otherwise a :class:`BedrockConverseProvider` is built from the app settings:
region + credentials follow the standard boto3 chain, and the model id comes
from ``copilot_model_id`` (falling back to ``orchestrator_model_id`` — the
Claude Sonnet inference profile already used elsewhere in the app).
"""
from __future__ import annotations

import os
from functools import lru_cache

from app.config.settings import get_settings
from app.copilot.providers.base import LLMProvider


@lru_cache
def get_provider() -> LLMProvider:
    settings = get_settings()
    if os.environ.get("COPILOT_PROVIDER", "").lower() == "fake":
        from app.copilot.providers.fake import FakeProvider

        return FakeProvider()

    from app.copilot.providers.bedrock_converse import BedrockConverseProvider

    model_id = settings.copilot_model_id or settings.orchestrator_model_id
    return BedrockConverseProvider(model_id=model_id, region=settings.aws_region)
