"""Google Gemini provider client (google-genai SDK), IN-201..204.

Supports two auth modes (tried in order):
  1. API key — set GOOGLE_API_KEY (simplest, no GCP IAM required).
  2. Vertex AI — set GOOGLE_CLOUD_PROJECT + GOOGLE_APPLICATION_CREDENTIALS (or ADC).

Conforms to the ProviderClient contract (NF-010): enabling/disabling Gemini is a
targets.yaml + .env change with zero orchestrator/scoring code changes. Provider
failures are normalized into the shared error taxonomy; safety blocks map to
SafetyBlocked so the BLOCKED status path (IN-204) works unchanged.
"""
import asyncio
import html as html_lib
import re

import httpx

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

_BLOCK_REASONS = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}


def _map_finish_reason(reason) -> str:
    name = (getattr(reason, "name", None) or str(reason or "")).upper()
    if name == "MAX_TOKENS":
        return "length"
    if name in _BLOCK_REASONS:
        return "blocked"
    return "stop"


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _extract_grounding(cand) -> tuple[list[Source], list[GroundingSupport], list[str]]:
    """Parse Gemini grounding metadata into (sources, claim->source supports, queries).

    Defensive: every field is read via getattr so an unexpected SDK shape degrades to
    empty rather than crashing. Source order mirrors grounding_chunks and support indices
    are remapped onto the emitted sources list so the two stay aligned. The URLs captured
    here are opaque Gemini redirect links; _resolve_sources turns them into real pages.
    """
    meta = getattr(cand, "grounding_metadata", None)
    if meta is None:
        return [], [], []

    sources: list[Source] = []
    index_map: dict[int, int] = {}  # original chunk index -> emitted sources index
    for i, chunk in enumerate(getattr(meta, "grounding_chunks", None) or []):
        web = getattr(chunk, "web", None)
        uri = getattr(web, "uri", None) if web is not None else None
        if not uri:
            continue
        domain = getattr(web, "title", None) if web is not None else None
        index_map[i] = len(sources)
        sources.append(Source(url=uri, domain=domain, redirect_url=uri, origin="GROUNDED"))

    supports: list[GroundingSupport] = []
    for sup in getattr(meta, "grounding_supports", None) or []:
        mapped = [index_map[j] for j in (getattr(sup, "grounding_chunk_indices", None) or []) if j in index_map]
        if not mapped:
            continue
        seg = getattr(sup, "segment", None)
        supports.append(GroundingSupport(
            text=(getattr(seg, "text", None) if seg is not None else None) or "",
            source_indices=mapped,
            start_index=getattr(seg, "start_index", None) if seg is not None else None,
            end_index=getattr(seg, "end_index", None) if seg is not None else None,
        ))

    queries = list(getattr(meta, "web_search_queries", None) or [])
    return sources, supports, queries


def _extract_title(html_text: str) -> str | None:
    match = _TITLE_RE.search(html_text or "")
    if not match:
        return None
    title = html_lib.unescape(re.sub(r"\s+", " ", match.group(1)).strip())
    return title[:300] or None


async def _resolve_one(client: httpx.AsyncClient, s: Source) -> None:
    """Follow one grounding redirect to its real page; fill final URL + title. Best-effort."""
    if not s.redirect_url:
        return
    try:
        async with client.stream("GET", s.redirect_url) as resp:
            s.url = str(resp.url)  # final URL after following redirects
            if resp.status_code != 200:
                return
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                if len(buf) >= 65536 or b"</title>" in bytes(buf).lower():
                    break
            title = _extract_title(bytes(buf).decode("utf-8", "ignore"))
            if title:
                s.title = title
    except Exception:
        pass  # keep redirect URL + domain as fallback


async def _resolve_sources(sources: list[Source]) -> None:
    """Resolve all grounding redirects concurrently. Gemini redirect links expire (~30d),
    so this runs at capture time. Never raises — provenance is best-effort."""
    targets = [s for s in sources if s.redirect_url]
    if not targets:
        return
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(10.0),
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            await asyncio.gather(*(_resolve_one(client, s) for s in targets))
    except Exception:
        pass


class GoogleClient(ProviderClient):
    """Gemini via API key or Vertex AI. The SDK is imported lazily so this module
    loads even when google-genai is not installed and the target is disabled."""

    name = "google"

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        s = self._settings
        try:
            from google import genai
            from google.genai import types
        except ImportError as e:
            raise Fatal("google-genai not installed — run: pip install google-genai") from e

        # Client-level HTTP timeout (milliseconds) so a stalled Google socket aborts the
        # request instead of blocking the asyncio.to_thread worker forever. Set slightly
        # ABOVE the orchestrator's per-call asyncio.wait_for ceiling so the orchestrator
        # fails the call first (fail-fast); this then lets the abandoned worker thread die
        # rather than leaking a thread-pool slot on every hang.
        http_options = types.HttpOptions(
            timeout=(s.target_call_timeout_seconds + 30) * 1000
        )

        # Path 1: API key auth (preferred — simplest setup)
        if s.google_api_key:
            self._client = genai.Client(api_key=s.google_api_key, http_options=http_options)
            return self._client

        # Path 2: Vertex AI (service-account or ADC)
        if not s.google_cloud_project:
            raise AuthError(
                "Neither GOOGLE_API_KEY nor GOOGLE_CLOUD_PROJECT is configured. "
                "Set GOOGLE_API_KEY for API key auth, or GOOGLE_CLOUD_PROJECT "
                "+ GOOGLE_APPLICATION_CREDENTIALS for Vertex AI."
            )

        credentials = None
        if s.google_application_credentials:
            try:
                from google.oauth2 import service_account

                credentials = service_account.Credentials.from_service_account_file(
                    s.google_application_credentials,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )
            except Exception as e:  # noqa: BLE001
                raise AuthError(f"Failed to load service-account credentials: {e}") from e

        self._client = genai.Client(
            vertexai=True,
            project=s.google_cloud_project,
            location=s.google_cloud_location,
            credentials=credentials,
            http_options=http_options,
        )
        return self._client

    def _generate_sync(
        self, model_id: str, system: str, user: str, params: ModelParams
    ) -> ProviderResult:
        try:
            from google.genai import errors, types
        except ImportError as e:
            raise Fatal("google-genai not installed — run: pip install google-genai") from e

        client = self._get_client()  # may raise AuthError/Fatal — propagate as-is

        config_kwargs: dict = {"max_output_tokens": params.max_tokens}
        if params.temperature is not None:
            config_kwargs["temperature"] = params.temperature
        if system:
            config_kwargs["system_instruction"] = system
        if params.extra.get("grounding"):
            # Real source provenance (Type B): Gemini performs Google Search and returns
            # the pages it used in candidate.grounding_metadata (parsed by _extract_sources).
            config_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]

        try:
            resp = client.models.generate_content(
                model=model_id,
                contents=user,
                config=types.GenerateContentConfig(**config_kwargs),
            )
        except errors.APIError as e:
            code = getattr(e, "code", None) or getattr(e, "status_code", None)
            msg = getattr(e, "message", None) or str(e)
            if code == 429:
                raise RateLimited(msg) from e
            if code in (500, 502, 503, 504):
                raise Transient(msg) from e
            if code in (401, 403):
                raise AuthError(f"{code}: {msg}") from e
            raise Fatal(f"{code}: {msg}") from e
        except (RateLimited, Transient, SafetyBlocked, AuthError, Fatal):
            raise
        except Exception as e:  # noqa: BLE001 — connection/timeout
            raise Transient(str(e)) from e

        feedback = getattr(resp, "prompt_feedback", None)
        if feedback is not None and getattr(feedback, "block_reason", None):
            raise SafetyBlocked(f"Gemini prompt blocked: {feedback.block_reason}")

        candidates = getattr(resp, "candidates", None) or []
        if not candidates:
            raise SafetyBlocked("Gemini returned no candidates (likely a safety block)")

        cand = candidates[0]
        raw_finish = getattr(cand, "finish_reason", None)
        finish = _map_finish_reason(raw_finish)
        if finish == "blocked":
            raise SafetyBlocked(f"Gemini finish reason: {getattr(raw_finish, 'name', raw_finish)}")

        text = ""
        content = getattr(cand, "content", None)
        if content is not None and getattr(content, "parts", None):
            text = "".join(getattr(p, "text", "") or "" for p in content.parts)

        usage = getattr(resp, "usage_metadata", None)
        prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
        completion_tokens = getattr(usage, "candidates_token_count", 0) or 0

        sources, supports, queries = _extract_grounding(cand)

        return ProviderResult(
            text=text,
            finish_reason=finish,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model_version=model_id,
            raw_status=200,
            block_reason=None,
            sources=sources,
            grounding_supports=supports,
            search_queries=queries,
        )

    async def chat(
        self, model_id: str, system: str, user: str, params: ModelParams
    ) -> ProviderResult:
        result = await asyncio.to_thread(self._generate_sync, model_id, system, user, params)
        # Resolve grounding redirects into real article URLs + titles (concurrent, best-effort).
        if result.sources:
            await _resolve_sources(result.sources)
        return result

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
