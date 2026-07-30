"""EvidenceMD provider client — OpenAI-COMPATIBLE clinical-reasoning API (IN-101..105).

EvidenceMD (evidencemd.ai) exposes an OpenAI-compatible **Chat Completions** endpoint,
so this adapter reuses the `openai` SDK pointed at a custom `base_url` (Settings.
evidencemd_base_url) with a separate key (EVIDENCEMD_API_KEY). It is a DIFFERENT product
from the manual OpenEvidence capture tool (app/providers/open_evidence.py) — that stays
untouched.

Conforms to the ProviderClient contract (NF-010): enabling/disabling is a targets.yaml +
.env change with zero orchestrator/scoring changes, and failures normalize into the shared
error taxonomy. The SDK is imported lazily so this module loads even when `openai` is not
installed and the target is disabled.

EvidenceMD answers may carry peer-reviewed citations. Their exact shape is parsed
best-effort into ProviderResult.sources (message.annotations url_citation, a top-level/
message-level ``citations`` list, or none) — an unexpected shape degrades to no sources
rather than crashing.
"""
import asyncio
import re
from urllib.parse import urlparse

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
    Source,
    Transient,
)


def _is_content_policy_error(msg: str) -> bool:
    """A BadRequest that is actually a safety block rather than a malformed request."""
    return any(k in msg.lower() for k in ("content_policy", "content_filter", "safety"))


# EvidenceMD embeds peer-reviewed citations inline in the answer as markdown links,
# e.g. "...anchor drug [17](https://pmc.ncbi.nlm.nih.gov/articles/PMC8133095/)...". There is
# no structured citations field, so we parse these links out for provenance.
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\((https?://[^\s)]+)\)")


def _domain_of(url: str) -> str | None:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _as_dict(obj) -> dict:
    """Coerce an SDK model (pydantic) or dict into a plain dict, defensively."""
    if isinstance(obj, dict):
        return obj
    for attr in ("model_dump", "to_dict", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                pass
    return {}


def _extract_citations(message) -> list[Source]:
    """Best-effort parse of EvidenceMD citations into Source rows (deduped by URL).

    Handles three plausible OpenAI-compatible shapes:
      1) message.annotations[] with type "url_citation" (OpenAI Responses parity)
      2) message.citations[] — list of {url,title,...} or bare URL strings
      3) nothing → empty list
    """
    msg = _as_dict(message)
    sources: list[Source] = []
    seen: set[str] = set()

    def _add(url: str | None, title: str | None = None, snippet: str | None = None) -> None:
        if not url or url in seen:
            return
        seen.add(url)
        sources.append(Source(url=url, title=title, domain=_domain_of(url),
                              snippet=snippet, origin="GROUNDED"))

    for ann in msg.get("annotations") or []:
        a = _as_dict(ann)
        if a.get("type") == "url_citation":
            cit = _as_dict(a.get("url_citation")) or a
            _add(cit.get("url"), cit.get("title"))

    for cit in msg.get("citations") or []:
        if isinstance(cit, str):
            _add(cit)
        else:
            c = _as_dict(cit)
            _add(c.get("url") or c.get("link"), c.get("title") or c.get("name"),
                 c.get("snippet") or c.get("text"))

    return sources


def _extract_markdown_citations(text: str, existing: list[Source]) -> list[Source]:
    """Parse inline markdown links from the answer text into Source rows (deduped).

    EvidenceMD's peer-reviewed references arrive as `[label](url)` links in the content
    rather than a structured field, so this is the primary provenance path for this
    provider. URLs already captured from structured fields are not duplicated.
    """
    seen = {s.url for s in existing}
    out: list[Source] = []
    for match in _MARKDOWN_LINK_RE.finditer(text or ""):
        url = match.group(1).rstrip(").,;")
        if url in seen:
            continue
        seen.add(url)
        out.append(Source(url=url, title=None, domain=_domain_of(url), origin="GROUNDED"))
    return out


class EvidenceMDClient(ProviderClient):
    """EvidenceMD via its OpenAI-compatible Chat Completions endpoint."""

    name = "evidencemd"

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        if not self._settings.evidencemd_api_key:
            raise AuthError("EVIDENCEMD_API_KEY not configured")
        try:
            import openai
        except ImportError as e:
            raise Fatal("openai not installed — run: pip install openai") from e
        # Explicit per-request timeout + no SDK-side retries so a stalled socket can't pin the
        # asyncio.to_thread worker for the SDK default (~10 min). Set slightly ABOVE the
        # orchestrator's per-call asyncio.wait_for ceiling so the orchestrator fails first.
        self._client = openai.OpenAI(
            api_key=self._settings.evidencemd_api_key,
            base_url=self._settings.evidencemd_base_url or None,
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

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})

        kwargs: dict = {
            "model": model_id,
            "messages": messages,
            "max_tokens": params.max_tokens,
        }
        if params.temperature is not None:
            kwargs["temperature"] = params.temperature

        try:
            resp = client.chat.completions.create(**kwargs)
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

        choice = (resp.choices or [None])[0]
        if choice is None:
            raise Fatal("EvidenceMD returned no choices")

        message = getattr(choice, "message", None)
        text = (getattr(message, "content", None) or "") if message is not None else ""
        raw_finish = getattr(choice, "finish_reason", None) or "stop"
        if raw_finish == "content_filter":
            raise SafetyBlocked("EvidenceMD response blocked by content filter")
        finish = "length" if raw_finish == "length" else "stop"

        sources = _extract_citations(message) if message is not None else []
        # EvidenceMD cites inline as markdown links; harvest those too (primary path).
        sources.extend(_extract_markdown_citations(text, sources))

        usage = getattr(resp, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0

        return ProviderResult(
            text=text,
            finish_reason=finish,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model_version=getattr(resp, "model", None) or model_id,
            raw_status=200,
            block_reason=None,
            sources=sources,
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
