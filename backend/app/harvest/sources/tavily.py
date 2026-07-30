"""Tavily AI-search source — REST over httpx (no extra dependency).

Tavily searches the web constrained to an allowlist of domains and returns clean,
extracted page content. We then mine real user questions out of that content. The
API key lives in settings (TAVILY_API_KEY); when absent the source reports
is_configured() == False and the pipeline degrades gracefully.
"""
from urllib.parse import urlparse

import httpx

from app.config.settings import get_settings
from app.harvest.sources.base import HarvestSource, RawItem
from app.utils.logging import get_logger

logger = get_logger("harvest.tavily")


class TavilySource(HarvestSource):
    name = "tavily"

    def __init__(self, *, search_depth: str = "advanced", include_domains=None,
                 exclude_domains=None, include_answer: bool = False,
                 include_raw_content: bool = False):
        s = get_settings()
        self._api_key = s.tavily_api_key
        self._base_url = s.tavily_base_url.rstrip("/")
        self.search_depth = search_depth
        self.include_domains = include_domains or []
        self.exclude_domains = exclude_domains or []
        self.include_answer = include_answer
        self.include_raw_content = include_raw_content

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def search(self, query: str, *, max_results: int = 8,
                     include_domains: list[str] | None = None) -> list[RawItem]:
        if not self.is_configured():
            return []
        payload = {
            "api_key": self._api_key,
            "query": query,
            "search_depth": self.search_depth,
            "max_results": max_results,
            "include_answer": self.include_answer,
            "include_raw_content": self.include_raw_content,
        }
        domains = include_domains if include_domains is not None else self.include_domains
        if domains:
            payload["include_domains"] = domains
        if self.exclude_domains:
            payload["exclude_domains"] = self.exclude_domains

        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                resp = await client.post(f"{self._base_url}/search", json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:  # noqa: BLE001 — never break the harvest on one query
            logger.warning("Tavily search failed for %r: %s", query, e)
            return []

        items: list[RawItem] = []
        for r in data.get("results", []) or []:
            url = r.get("url")
            content = r.get("raw_content") or r.get("content") or ""
            items.append(RawItem(
                source=self.name,
                url=url,
                title=r.get("title"),
                domain=_domain(url),
                content=content,
                score=r.get("score"),
                query=query,
            ))
        return items


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return (urlparse(url).netloc or "").replace("www.", "") or None
    except Exception:  # noqa: BLE001
        return None
