"""OpenAI provider client (IN-101..105) with real source provenance (Type B).

Mirrors the Gemini grounding approach: when the target is configured with
`grounding: true` (params.extra), the model runs OpenAI's hosted web-search tool and
we extract the `url_citation` annotations it returns — the REAL page URLs + titles the
model used, the answer spans they support, and the search queries it issued — populating
ProviderResult.sources / grounding_supports / search_queries. Unlike Gemini, OpenAI's
citations already carry the final URL + title, so no redirect resolution step is needed.

Uses the Responses API (client.responses.create). Auth via OPENAI_API_KEY. Conforms to
the ProviderClient contract (NF-010): enabling/disabling is a targets.yaml + .env change
with zero orchestrator/scoring code changes, and failures are normalized into the shared
error taxonomy. The SDK is imported lazily so this module loads even when `openai` is not
installed and the target is disabled.
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

# Hosted web-search tool. The GA tool type is "web_search"; older models/snapshots expect
# the preview alias "web_search_preview". We try the GA name first and transparently fall
# back so grounding "just works" across model versions. Pin one per target via the
# `web_search_tool` param to skip the fallback probe.
_WEB_SEARCH_TOOLS = ("web_search", "web_search_preview")


def _is_content_policy_error(msg: str) -> bool:
    """A BadRequest that is actually a safety block rather than a malformed request."""
    return any(k in msg.lower() for k in ("content_policy", "content_filter", "safety"))


def _domain_of(url: str) -> str | None:
    """Short site label for a URL, e.g. 'nih.gov' — mirrors Gemini's domain field."""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _finish_reason(resp) -> str:
    """Map a Responses API result to the shared finish-reason taxonomy."""
    status = getattr(resp, "status", None)
    if status == "incomplete":
        details = getattr(resp, "incomplete_details", None)
        reason = getattr(details, "reason", None) if details is not None else None
        if reason == "content_filter":
            return "blocked"
        # max_output_tokens (or any other truncation) -> length
        return "length"
    return "stop"


def _query_from_action(action) -> str | None:
    """Pull the issued query out of a web_search_call action (object or dict shape)."""
    if action is None:
        return None
    query = getattr(action, "query", None)
    if query is None and isinstance(action, dict):
        query = action.get("query")
    return query


def _extract_output(resp) -> tuple[str, list[Source], list[GroundingSupport], list[str]]:
    """Parse Responses API output into (text, sources, claim->source supports, queries).

    Defensive: every field is read via getattr so an unexpected SDK shape degrades to empty
    rather than crashing. Sources are de-duplicated by URL; annotation spans that share the
    same (start, end) are merged so one claim can point at several sources (Gemini parity).
    Annotation indices are relative to their content part, so they're offset by the running
    length of the concatenated answer text to stay aligned with the combined string.
    """
    text_parts: list[str] = []
    sources: list[Source] = []
    url_to_index: dict[str, int] = {}
    queries: list[str] = []
    span_supports: dict[tuple[int | None, int | None], GroundingSupport] = {}
    ordered_spans: list[tuple[int | None, int | None]] = []

    for item in getattr(resp, "output", None) or []:
        itype = getattr(item, "type", None)
        if itype == "web_search_call":
            query = _query_from_action(getattr(item, "action", None))
            if query and query not in queries:
                queries.append(query)
        elif itype == "message":
            for part in getattr(item, "content", None) or []:
                ptype = getattr(part, "type", None)
                if ptype == "output_text":
                    ptext = getattr(part, "text", None) or ""
                    base = sum(len(t) for t in text_parts)  # offset into combined text
                    text_parts.append(ptext)
                    for ann in getattr(part, "annotations", None) or []:
                        if getattr(ann, "type", None) != "url_citation":
                            continue
                        url = getattr(ann, "url", None)
                        if not url:
                            continue
                        if url not in url_to_index:
                            url_to_index[url] = len(sources)
                            sources.append(Source(
                                url=url,
                                title=getattr(ann, "title", None),
                                domain=_domain_of(url),
                                origin="GROUNDED",
                            ))
                        idx = url_to_index[url]
                        start = getattr(ann, "start_index", None)
                        end = getattr(ann, "end_index", None)
                        gstart = base + start if start is not None else None
                        gend = base + end if end is not None else None
                        key = (gstart, gend)
                        existing = span_supports.get(key)
                        if existing is None:
                            snippet = ptext[start:end] if (start is not None and end is not None) else ""
                            span_supports[key] = GroundingSupport(
                                text=snippet,
                                source_indices=[idx],
                                start_index=gstart,
                                end_index=gend,
                            )
                            ordered_spans.append(key)
                        elif idx not in existing.source_indices:
                            existing.source_indices.append(idx)
                elif ptype == "refusal":
                    text_parts.append(getattr(part, "refusal", None) or "")

    text = "".join(text_parts)
    if not text:
        text = getattr(resp, "output_text", None) or ""
    supports = [span_supports[k] for k in ordered_spans]
    return text, sources, supports, queries


class OpenAIClient(ProviderClient):
    """OpenAI via the Responses API. Web-search grounding yields real source URLs."""

    name = "openai"

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        if not self._settings.openai_api_key:
            raise AuthError("OPENAI_API_KEY not configured")
        try:
            import openai
        except ImportError as e:
            raise Fatal("openai not installed — run: pip install openai") from e
        # Explicit per-request timeout + no SDK-side retries so a stalled socket can't pin the
        # asyncio.to_thread worker for the SDK default (~10 min). Set slightly ABOVE the
        # orchestrator's per-call asyncio.wait_for ceiling so the orchestrator fails first
        # (fail-fast), then the abandoned thread dies shortly after instead of leaking a
        # pool slot. The orchestrator handles retries itself (matches Bedrock's max_attempts=0).
        self._client = openai.OpenAI(
            api_key=self._settings.openai_api_key,
            timeout=self._settings.target_call_timeout_seconds + 30,
            max_retries=0,
        )
        return self._client

    def _generate_sync(
        self, model_id: str, system: str, user: str, params: ModelParams
    ) -> ProviderResult:
        try:
            import openai
        except ImportError as e:
            raise Fatal("openai not installed — run: pip install openai") from e

        client = self._get_client()  # may raise AuthError/Fatal — propagate as-is

        kwargs: dict = {
            "model": model_id,
            "input": user,
            "max_output_tokens": params.max_tokens,
        }
        if params.temperature is not None:
            kwargs["temperature"] = params.temperature
        if system:
            kwargs["instructions"] = system
        # Real source provenance (Type B): OpenAI runs a hosted web search and returns the
        # pages it used as url_citation annotations (parsed by _extract_output). The tool name
        # varies by model/API era, so we probe candidates and keep the first that's accepted.
        if params.extra.get("grounding") or params.extra.get("web_search"):
            override = params.extra.get("web_search_tool")
            tool_candidates: list[str | None] = [override] if override else list(_WEB_SEARCH_TOOLS)
            # force_search → require the model to actually run web search on every call, so the
            # response always carries sources (Gemini-grounding parity) instead of letting the
            # model answer from parametric memory with no citations.
            if params.extra.get("force_search"):
                kwargs["tool_choice"] = "required"
        else:
            tool_candidates = [None]

        try:
            resp = None
            for i, tool_type in enumerate(tool_candidates):
                call_kwargs = dict(kwargs)
                if tool_type is not None:
                    call_kwargs["tools"] = [{"type": tool_type}]
                try:
                    resp = client.responses.create(**call_kwargs)
                    break
                except openai.BadRequestError as e:
                    # A grounded request can 400 when this model/API era doesn't accept the
                    # tool name — fall back to the next candidate. Content-policy 400s are real
                    # blocks and must not be retried.
                    if (
                        tool_type is not None
                        and i < len(tool_candidates) - 1
                        and not _is_content_policy_error(str(e))
                    ):
                        continue
                    raise
        except openai.RateLimitError as e:
            raise RateLimited(str(e)) from e
        except (openai.APITimeoutError, openai.APIConnectionError) as e:
            raise Transient(str(e)) from e
        except openai.AuthenticationError as e:
            raise AuthError(str(e)) from e
        except openai.PermissionDeniedError as e:
            raise AuthError(str(e)) from e
        except openai.BadRequestError as e:
            msg = str(e)
            if _is_content_policy_error(msg):
                raise SafetyBlocked(msg) from e
            raise Fatal(msg) from e
        except openai.InternalServerError as e:
            raise Transient(str(e)) from e
        except openai.APIStatusError as e:
            code = getattr(e, "status_code", None)
            if code == 429:
                raise RateLimited(str(e)) from e
            if code in (500, 502, 503, 504):
                raise Transient(str(e)) from e
            if code in (401, 403):
                raise AuthError(str(e)) from e
            raise Fatal(str(e)) from e
        except (RateLimited, Transient, SafetyBlocked, AuthError, Fatal):
            raise
        except Exception as e:  # noqa: BLE001 — connection/unknown
            raise Transient(str(e)) from e

        finish = _finish_reason(resp)
        if finish == "blocked":
            raise SafetyBlocked("OpenAI response blocked by content filter")

        text, sources, supports, queries = _extract_output(resp)

        usage = getattr(resp, "usage", None)
        prompt_tokens = getattr(usage, "input_tokens", 0) or 0
        completion_tokens = getattr(usage, "output_tokens", 0) or 0

        return ProviderResult(
            text=text,
            finish_reason=finish,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model_version=getattr(resp, "model", None) or model_id,
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
