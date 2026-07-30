"""Bedrock provider client using the Converse API (IN-101..303 via one unified path).

A single Converse client covers Claude, Nova, Llama, Mistral, etc. — provider-agnostic
by design. Synchronous boto3 calls are run in a thread executor to stay async-friendly.
"""
import asyncio

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError

from app.config.settings import get_settings
from app.providers.base import (
    AuthError,
    Fatal,
    HealthStatus,
    ModelParams,
    ProviderClient,
    ProviderResult,
    RateLimited,
    SafetyBlocked,
    Transient,
)

_TRANSIENT_CODES = {
    "ModelTimeoutException",
    "ServiceUnavailableException",
    "InternalServerException",
    "ServiceQuotaExceededException",
}
_RATE_LIMIT_CODES = {"ThrottlingException", "TooManyRequestsException"}
_AUTH_CODES = {"AccessDeniedException", "UnrecognizedClientException", "InvalidSignatureException"}


def _map_stop_reason(reason: str) -> str:
    mapping = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "tool_use": "stop",
        "max_tokens": "length",
        "content_filtered": "blocked",
        "guardrail_intervened": "blocked",
    }
    return mapping.get(reason, "stop")


class BedrockClient(ProviderClient):
    name = "bedrock"

    def __init__(self) -> None:
        settings = get_settings()
        self._client = boto3.client(
            "bedrock-runtime",
            region_name=settings.aws_region,
            aws_access_key_id=settings.aws_access_key_id or None,
            aws_secret_access_key=settings.aws_secret_access_key or None,
            config=Config(
                retries={"max_attempts": 0, "mode": "standard"},  # we handle retries ourselves
                read_timeout=120,
                connect_timeout=15,
            ),
        )

    def _converse_sync(
        self, model_id: str, system: str, user: str, params: ModelParams
    ) -> ProviderResult:
        inference_config: dict = {"maxTokens": params.max_tokens}
        if params.temperature is not None:
            inference_config["temperature"] = params.temperature

        kwargs: dict = {
            "modelId": model_id,
            "messages": [{"role": "user", "content": [{"text": user}]}],
            "inferenceConfig": inference_config,
        }
        if system:
            kwargs["system"] = [{"text": system}]

        try:
            resp = self._client.converse(**kwargs)
        except (ReadTimeoutError, EndpointConnectionError) as e:
            raise Transient(str(e)) from e
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            msg = e.response.get("Error", {}).get("Message", str(e))
            if code in _RATE_LIMIT_CODES:
                raise RateLimited(msg) from e
            if code in _TRANSIENT_CODES:
                raise Transient(msg) from e
            if code in _AUTH_CODES:
                raise AuthError(f"{code}: {msg}") from e
            raise Fatal(f"{code}: {msg}") from e

        stop_reason = resp.get("stopReason", "end_turn")
        if stop_reason in ("content_filtered", "guardrail_intervened"):
            raise SafetyBlocked(f"Bedrock stop reason: {stop_reason}")

        message = resp.get("output", {}).get("message", {})
        content_blocks = message.get("content", [])
        text = "".join(block.get("text", "") for block in content_blocks)

        usage = resp.get("usage", {})
        meta = resp.get("ResponseMetadata", {})

        return ProviderResult(
            text=text,
            finish_reason=_map_stop_reason(stop_reason),
            prompt_tokens=usage.get("inputTokens", 0),
            completion_tokens=usage.get("outputTokens", 0),
            model_version=model_id,
            raw_status=meta.get("HTTPStatusCode"),
            block_reason=None,
        )

    async def chat(
        self, model_id: str, system: str, user: str, params: ModelParams
    ) -> ProviderResult:
        return await asyncio.to_thread(self._converse_sync, model_id, system, user, params)

    async def health_check(self, model_id: str) -> HealthStatus:
        try:
            result = await self.chat(
                model_id,
                system="",
                user="ping",
                params=ModelParams(max_tokens=5, temperature=0.0),
            )
            return HealthStatus(ok=True, detail="reachable", model_version=result.model_version)
        except AuthError as e:
            return HealthStatus(ok=False, detail=f"auth error: {e}")
        except Exception as e:  # noqa: BLE001
            return HealthStatus(ok=False, detail=f"{type(e).__name__}: {e}")
