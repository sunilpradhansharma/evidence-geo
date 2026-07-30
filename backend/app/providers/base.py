"""Provider-agnostic LLM contract (NF-010, design 3.2).

One ProviderClient per API provider. Each Target is pure config that names a provider.
The orchestrator is provider-blind: it only sees ProviderResult and the error taxonomy.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class ModelParams:
    max_tokens: int = 1024
    temperature: float | None = 0.3
    extra: dict = field(default_factory=dict)


@dataclass
class Source:
    """A real, provider-retrieved source used to ground a response (Type B provenance).

    Populated only when a provider runs in a grounded/search mode (e.g. Gemini with
    Google Search grounding). Pure parametric calls leave this empty.
    """

    url: str  # resolved final page URL (falls back to redirect_url if resolution fails)
    title: str | None = None  # resolved page title (falls back to domain)
    domain: str | None = None  # short site label the provider returned, e.g. "nih.gov"
    redirect_url: str | None = None  # original provider redirect (e.g. Gemini grounding link)
    snippet: str | None = None
    origin: str = "GROUNDED"  # GROUNDED (provider-retrieved) | MODEL_ASSERTED (future)


@dataclass
class GroundingSupport:
    """Maps a span of the answer text to the sources that support it (claim-level attribution)."""

    text: str
    source_indices: list[int] = field(default_factory=list)  # indices into ProviderResult.sources
    start_index: int | None = None
    end_index: int | None = None


@dataclass
class ProviderResult:
    text: str
    finish_reason: Literal["stop", "length", "blocked", "error"]
    prompt_tokens: int
    completion_tokens: int
    model_version: str
    raw_status: int | None = None
    block_reason: str | None = None
    sources: list[Source] = field(default_factory=list)  # real retrieval provenance (Type B)
    grounding_supports: list[GroundingSupport] = field(default_factory=list)  # claim -> source map
    search_queries: list[str] = field(default_factory=list)  # queries the engine issued


@dataclass
class HealthStatus:
    ok: bool
    detail: str
    model_version: str | None = None


# --- Normalized error taxonomy (every provider maps its failures into these) ---
class ProviderError(Exception):
    """Base class for normalized provider errors."""


class RateLimited(ProviderError):
    """429 / throttling — retry with backoff."""


class Transient(ProviderError):
    """5xx / timeout — retry with backoff."""


class SafetyBlocked(ProviderError):
    """Content blocked by provider safety filters (IN-204)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class AuthError(ProviderError):
    """Credentials missing/invalid — fail fast."""


class Fatal(ProviderError):
    """Non-retryable error."""


class ProviderClient(ABC):
    """Knows how to talk to one API provider: auth, request/response shape,
    error taxonomy, token accounting."""

    name: str = "base"

    @abstractmethod
    async def chat(
        self, model_id: str, system: str, user: str, params: ModelParams
    ) -> ProviderResult:
        ...

    @abstractmethod
    async def health_check(self, model_id: str) -> HealthStatus:
        ...
