"""Anthropic provider client — direct Anthropic API (Messages) with real source
provenance (Type B) via the native web-search server tool.

This is the OPTIONAL, citation-capable path for the monitored ``claude`` target. When
``ANTHROPIC_API_KEY`` is set, ``registry.load_targets`` repoints the ``claude`` target
from AWS Bedrock (parametric, no citations) to this client with ``grounding: true`` — so
Claude runs Anthropic's hosted ``web_search`` tool and returns the REAL pages it used,
exactly like the Gemini (Google Search) and GPT-4o (OpenAI web search) targets. With no
key, ``claude`` stays on Bedrock and this module is never dispatched.

Parsing mirrors the Gemini/OpenAI approach: we read the ``web_search_result_location``
citations attached to the answer's text blocks (the URLs + titles the model cited, and the
answer spans they support) plus the ``server_tool_use`` search queries, populating
ProviderResult.sources / grounding_supports / search_queries. Anthropic citations already
carry the final URL + title, so — like OpenAI — no redirect-resolution step is needed.

Conforms to the ProviderClient contract (NF-010): enabling/disabling is a targets.yaml +
.env change with zero orchestrator/scoring code changes, and failures normalize into the
shared error taxonomy. The SDK is imported lazily so this module loads even when
``anthropic`` is not installed and the target is on Bedrock.
"""
import asyncio
from urllib.parse import urlparse

from app.config.settings import get_settings
from app.providers.base import (
    AuthError,
    Fatal,
    GroundingSupport,
    HealthStatus,
    ModelParams,
    ProviderClient,
    ProviderResult,
    RateLimited,
    SafetyBlocked,
    Source,
    Transient,
)

# Anthropic's hosted web-search server tool. Versioned tool type per the Messages API.
_WEB_SEARCH_TOOL_TYPE = "web_search_20250305"
_WEB_SEARCH_TOOL_NAME = "web_search"
# The Anthropic production endpoint. We ALWAYS pass an explicit base_url (below) rather than
# letting the SDK default it: settings.load_yaml_config seeds os.environ with
# ANTHROPIC_BASE_URL="" for the blank setting, and the SDK reads that empty env var when
# base_url is None, building a schemeless request URL (httpx UnsupportedProtocol).
_DEFAULT_BASE_URL = "https://api.anthropic.com"
# A long web search can return stop_reason="pause_turn" with partial content; we feed the
# assistant turn back and continue, capped so a stuck search can't loop forever.
_MAX_PAUSE_CONTINUATIONS = 4


def _is_content_policy_error(msg: str) -> bool:
    """A 400 that is actually a safety block rather than a malformed request."""
    return any(k in msg.lower() for k in ("content_policy", "content_filter", "safety", "blocked"))


def _domain_of(url: str) -> str | None:
    """Short site label for a URL, e.g. 'nih.gov' — mirrors Gemini/OpenAI's domain field."""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _get(obj, key, default=None):
    """Read a field from an SDK model (attribute) or a plain dict, defensively."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _map_stop_reason(reason: str | None) -> str:
    """Map an Anthropic stop_reason to the shared finish-reason taxonomy."""
    if reason == "max_tokens":
        return "length"
    if reason == "refusal":
        return "blocked"
    # end_turn | stop_sequence | tool_use | pause_turn(resolved) -> stop
    return "stop"


def _extract_output(content_blocks) -> tuple[str, list[Source], list[GroundingSupport], list[str]]:
    """Parse Messages content blocks into (text, sources, claim->source supports, queries).

    Defensive: every field is read via ``_get`` so an unexpected SDK shape degrades to empty
    rather than crashing. Only CITED sources are emitted (the ``web_search_result_location``
    citations on the answer's text blocks) — matching the OpenAI target's "what the model
    cited" semantics so Source Authority never over-counts uncited search hits. Sources are
    de-duplicated by URL; each cited text block becomes one GroundingSupport whose span offset
    tracks the running length of the concatenated answer text.
    """
    text_parts: list[str] = []
    sources: list[Source] = []
    url_to_index: dict[str, int] = {}
    supports: list[GroundingSupport] = []
    queries: list[str] = []

    for block in content_blocks or []:
        btype = _get(block, "type")
        if btype == "server_tool_use":
            if _get(block, "name") == _WEB_SEARCH_TOOL_NAME:
                query = _get(_get(block, "input"), "query")
                if query and query not in queries:
                    queries.append(query)
        elif btype == "text":
            ptext = _get(block, "text") or ""
            base = sum(len(t) for t in text_parts)  # offset into the combined answer text
            text_parts.append(ptext)
            cited_idxs: list[int] = []
            for cit in _get(block, "citations") or []:
                if _get(cit, "type") != "web_search_result_location":
                    continue
                url = _get(cit, "url")
                if not url:
                    continue
                if url not in url_to_index:
                    url_to_index[url] = len(sources)
                    sources.append(Source(
                        url=url,
                        title=_get(cit, "title"),
                        domain=_domain_of(url),
                        snippet=_get(cit, "cited_text"),
                        origin="GROUNDED",
                    ))
                idx = url_to_index[url]
                if idx not in cited_idxs:
                    cited_idxs.append(idx)
            if cited_idxs:
                supports.append(GroundingSupport(
                    text=ptext,
                    source_indices=cited_idxs,
                    start_index=base,
                    end_index=base + len(ptext),
                ))

    return "".join(text_parts), sources, supports, queries


class AnthropicClient(ProviderClient):
    """Anthropic Messages API. Web-search grounding yields real source URLs + cited claims."""

    name = "anthropic"

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        if not self._settings.anthropic_api_key:
            raise AuthError("ANTHROPIC_API_KEY not configured")
        try:
            import anthropic
        except ImportError as e:
            raise Fatal("anthropic not installed — run: pip install anthropic") from e
        # Explicit per-request timeout + no SDK-side retries so a stalled socket can't pin the
        # asyncio.to_thread worker for the SDK default (~10 min). Set slightly ABOVE the
        # orchestrator's per-call asyncio.wait_for ceiling so the orchestrator fails first
        # (fail-fast); the orchestrator handles retries itself (matches Bedrock's max_attempts=0).
        self._client = anthropic.Anthropic(
            api_key=self._settings.anthropic_api_key,
            base_url=self._settings.anthropic_base_url or _DEFAULT_BASE_URL,
            timeout=self._settings.target_call_timeout_seconds + 30,
            max_retries=0,
        )
        return self._client

    def _generate_sync(
        self, model_id: str, system: str, user: str, params: ModelParams
    ) -> ProviderResult:
        try:
            import anthropic
        except ImportError as e:
            raise Fatal("anthropic not installed — run: pip install anthropic") from e

        client = self._get_client()  # may raise AuthError/Fatal — propagate as-is

        kwargs: dict = {"model": model_id, "max_tokens": params.max_tokens}
        if params.temperature is not None:
            kwargs["temperature"] = params.temperature
        if system:
            kwargs["system"] = system
        # Real source provenance (Type B): enable Anthropic's hosted web search so the answer
        # carries web_search_result_location citations (parsed by _extract_output). By default
        # search is model-decided (tool_choice auto); with force_search we require it (below),
        # for Gemini-grounding / OpenAI parity. A parametric answer is never hard-failed — it
        # simply carries no sources (like Gemini/GPT-4o when they choose not to search).
        if params.extra.get("grounding") or params.extra.get("web_search"):
            max_uses = params.extra.get("web_search_max_uses") or self._settings.anthropic_web_search_max_uses
            tool: dict = {"type": _WEB_SEARCH_TOOL_TYPE, "name": _WEB_SEARCH_TOOL_NAME}
            if max_uses:
                tool["max_uses"] = int(max_uses)
            kwargs["tools"] = [tool]
            # force_search → make Claude ALWAYS run web search instead of answering from
            # parametric memory with no citations (mirrors OpenAI's tool_choice="required").
            # Anthropic forces a specific tool via tool_choice={"type":"tool","name":...}. Set
            # only for the FIRST turn; a pause_turn continuation drops it (below) so the resumed
            # turn can finalize the answer instead of being forced to search again.
            if params.extra.get("force_search"):
                kwargs["tool_choice"] = {"type": "tool", "name": _WEB_SEARCH_TOOL_NAME}

        messages: list[dict] = [{"role": "user", "content": user}]
        all_content: list = []
        total_in = total_out = 0
        stop_reason = None
        model_version = model_id

        for _ in range(1 + _MAX_PAUSE_CONTINUATIONS):
            try:
                resp = client.messages.create(messages=messages, **kwargs)
            except anthropic.RateLimitError as e:
                raise RateLimited(str(e)) from e
            except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
                raise Transient(str(e)) from e
            except anthropic.AuthenticationError as e:
                raise AuthError(str(e)) from e
            except anthropic.PermissionDeniedError as e:
                raise AuthError(str(e)) from e
            except anthropic.BadRequestError as e:
                msg = str(e)
                if _is_content_policy_error(msg):
                    raise SafetyBlocked(msg) from e
                raise Fatal(msg) from e
            except anthropic.InternalServerError as e:
                raise Transient(str(e)) from e
            except anthropic.APIStatusError as e:
                code = getattr(e, "status_code", None)
                if code == 429:
                    raise RateLimited(str(e)) from e
                if code in (500, 502, 503, 504, 529):
                    raise Transient(str(e)) from e
                if code in (401, 403):
                    raise AuthError(str(e)) from e
                raise Fatal(str(e)) from e
            except (RateLimited, Transient, SafetyBlocked, AuthError, Fatal):
                raise
            except Exception as e:  # noqa: BLE001 — connection/unknown
                raise Transient(str(e)) from e

            blocks = getattr(resp, "content", None) or []
            all_content.extend(blocks)
            usage = getattr(resp, "usage", None)
            total_in += getattr(usage, "input_tokens", 0) or 0
            total_out += getattr(usage, "output_tokens", 0) or 0
            stop_reason = getattr(resp, "stop_reason", None)
            model_version = getattr(resp, "model", None) or model_id

            # A paused web search returns partial content — feed the assistant turn back and
            # continue so the answer (and its citations) completes.
            if stop_reason == "pause_turn":
                messages = messages + [{"role": "assistant", "content": blocks}]
                kwargs.pop("tool_choice", None)  # resumed turn finalizes (no forced re-search loop)
                continue
            break

        if stop_reason == "refusal":
            raise SafetyBlocked("Anthropic refused to answer (stop_reason=refusal)")

        text, sources, supports, queries = _extract_output(all_content)

        return ProviderResult(
            text=text,
            finish_reason=_map_stop_reason(stop_reason),
            prompt_tokens=total_in,
            completion_tokens=total_out,
            model_version=model_version,
            raw_status=200,
            block_reason=None,
            sources=sources,
            grounding_supports=supports,
            search_queries=queries,
        )

    async def chat(
        self, model_id: str, system: str, user: str, params: ModelParams
    ) -> ProviderResult:
        return await asyncio.to_thread(self._generate_sync, model_id, system, user, params)

    async def health_check(self, model_id: str) -> HealthStatus:
        try:
            result = await self.chat(
                model_id,
                system="",
                user="ping",
                params=ModelParams(max_tokens=16, temperature=0.0),
            )
            return HealthStatus(ok=True, detail="reachable", model_version=result.model_version)
        except AuthError as e:
            return HealthStatus(ok=False, detail=f"auth error: {e}")
        except Exception as e:  # noqa: BLE001
            return HealthStatus(ok=False, detail=f"{type(e).__name__}: {e}")
