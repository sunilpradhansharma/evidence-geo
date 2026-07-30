"""HarvestSource contract — one implementation per discovery backend (Tavily, etc.).

Mirrors the provider-agnostic ProviderClient pattern: each source normalizes its
backend's results into a common RawItem so the harvest pipeline stays source-blind.
Adding a new backend = a new HarvestSource subclass; the pipeline does not change.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass
class RawItem:
    """A normalized search result from any source."""
    source: str
    url: str | None
    title: str | None
    domain: str | None
    content: str
    score: float | None = None
    query: str | None = None

    # Social-listening enrichment (Apify channels). Optional/defaulted so existing
    # sources (e.g. Tavily) are unaffected. ``engagement_score`` is a RAW per-channel
    # metric (upvotes/likes/views/etc.) whose meaning differs by platform, so it is
    # never summed or ranked across channels.
    channel: str | None = None
    engagement_score: int | None = None
    comment_count: int | None = None
    posted_at: datetime | None = None


@dataclass
class RawComment:
    """A normalized comment/reply scraped for a parent social post.

    The parent linkage is implicit: comments are fetched one parent post at a time, so the
    caller already knows which post they belong to (no fragile parent-id mapping). No author
    identity is carried — the pipeline scrubs anything that slips through. ``engagement_score``
    is a RAW per-channel metric (comment upvotes/likes), never summed across channels.
    """
    channel: str | None
    text: str
    engagement_score: int | None = None
    posted_at: datetime | None = None
    url: str | None = None


class HarvestSource(ABC):
    name: str = "base"

    @abstractmethod
    def is_configured(self) -> bool:
        """True if credentials/config are present so the source can run."""

    @abstractmethod
    async def search(self, query: str, *, max_results: int) -> list[RawItem]:
        """Run one query and return normalized results. Must never raise — return [] on error."""
