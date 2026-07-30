"""Apify-backed social-listening source (Social Listening surface).

ApifySource runs a configured Apify Actor (one per channel: Reddit/TikTok/Instagram/
Facebook/X) via Apify's REST API and normalizes the scraped public posts into RawItem
so the social pipeline stays source-blind (mirrors the Tavily/HarvestSource pattern).

Gating:
  - ``settings.apify_enabled``  — MASTER on/off (env APIFY_ENABLED, default ON).
  - ``settings.apify_api_token`` — required for any live fetch (env APIFY_API_TOKEN).
When either is missing, ``is_configured()`` is False and ``search`` returns ``[]``.

IMPORTANT (methodology): ``engagement_score`` is a raw per-channel metric whose meaning
differs by platform (a Reddit upvote != a TikTok view != an Instagram like). Downstream it
is weighted per channel and compared only directionally — never summed across channels.
Author handles are intentionally NOT carried for persistence; the pipeline scrubs anything
that slips through. Coverage is best-effort scraped public posts, not a licensed firehose.
"""
import asyncio
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from app.config.settings import get_settings
from app.harvest.sources.base import HarvestSource, RawComment, RawItem
from app.utils.logging import get_logger

logger = get_logger("harvest.apify")

# Apify actor runs (cold start + crawl) routinely take minutes. Rather than the blocking
# run-sync endpoint (which only returns once the WHOLE crawl finishes), we start the run,
# poll its dataset, and early-return as soon as we have enough items OR this budget elapses
# — then abort the run to cap cost. Scrapers push items incrementally, so partial results
# are usually available within seconds.
_MAX_WAIT_SECONDS = 110.0
_POLL_SECONDS = 5.0
_TERMINAL = ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT", "TIMING-OUT")

# Actors differ in their output field names, so we probe a candidate list per concept.
_TEXT_KEYS = ("text", "caption", "content", "title", "body", "fullText",
              "postText", "message", "description", "tweetText")
_LIKE_KEYS = ("likes", "likesCount", "diggCount", "favoriteCount", "favouritesCount",
              "reactionsCount", "reactions", "upVotes", "upvotes", "score",
              "numLikes", "likeCount", "playCount", "viewCount", "views")
_COMMENT_KEYS = ("comments", "commentsCount", "commentCount", "replyCount",
                 "numComments", "repliesCount", "numberOfComments")
_URL_KEYS = ("url", "postUrl", "webVideoUrl", "link", "permalink", "tweetUrl", "postLink")
# Platform-scraper time fields FIRST, then the website-content-crawler / structured-data
# publish dates (openGraph article:* + JSON-LD datePublished) surfaced by ``_flatten`` so
# crawl-mode community pages (myRAteam / Bezzy RA) carry a real posted_at, not just null.
_TIME_KEYS = ("timestamp", "createTime", "createTimeISO", "publishedAt", "postedAt",
              "created_at", "createdAt", "date", "time", "dateTime",
              "article:published_time", "article:modified_time", "og:updated_time",
              "publishedTime", "modifiedTime", "datePublished", "dateModified")
# Comment/reply text variants (Reddit uses "body"; others "text"/"comment"/etc.).
_COMMENT_TEXT_KEYS = ("text", "comment", "commentText", "content", "message", "body",
                      "replyText", "comment_text")
# Comments come back fast and we run one actor per parent post, so use a shorter budget.
_COMMENT_MAX_WAIT_SECONDS = 90.0


class ApifySource(HarvestSource):
    """One Apify Actor, wrapped as a HarvestSource for a single social channel."""

    def __init__(self, channel: str, actor_id: str, input_template: dict,
                 results_cap: int = 30, enabled: bool = True,
                 mode: str = "search", max_posts: int | None = None,
                 comments_actor_id: str = "", comments_input_template: dict | None = None,
                 comments_enabled: bool = False, max_comments_per_post: int = 20):
        s = get_settings()
        self.channel = channel
        self.name = f"apify:{channel}"
        self.actor_id = actor_id or ""
        self.input_template = input_template or {}
        self._enabled = bool(s.apify_enabled and enabled)
        self._token = s.apify_api_token
        self._base_url = (s.apify_base_url or "https://api.apify.com").rstrip("/")
        self.results_cap = int(results_cap or 30)
        # "search" = term-seeded platform scraper (loops seed terms). "crawl" = single-shot
        # site crawl with fixed startUrls (ignores the seed term); the pipeline runs it once.
        self.mode = (mode or "search").strip().lower()
        # Optional per-channel override of the pipeline's global max_posts_per_channel cap.
        self.max_posts = int(max_posts) if max_posts else None
        # Comments are scraped by a SEPARATE actor per channel (it takes a post URL as input).
        self.comments_actor_id = comments_actor_id or ""
        self.comments_input_template = comments_input_template or {}
        self.comments_enabled = bool(comments_enabled)
        self.max_comments_per_post = int(max_comments_per_post or 20)

    def is_configured(self) -> bool:
        """True only when the master switch is ON, a token is set, and an actor is mapped."""
        return bool(self._enabled and self._token and self.actor_id)

    def comments_configured(self) -> bool:
        """True when comment scraping is enabled and a comments actor is mapped for this channel."""
        return bool(self._enabled and self._token and self.comments_enabled and self.comments_actor_id)

    async def search(self, query: str, *, max_results: int | None = None) -> list[RawItem]:
        if not self.is_configured():
            return []
        cap = min(max_results or self.results_cap, self.results_cap) or self.results_cap
        t0 = time.monotonic()
        payload = _render(self.input_template, term=query, cap=cap)
        data = await self._run_and_collect(self.actor_id, payload, cap, t0, label=f"for {query!r} ")

        items: list[RawItem] = []
        for r in data:
            if not isinstance(r, dict):
                continue
            # Crawl-mode actors (website-content-crawler) nest the page text/date/author under
            # a ``metadata`` block (+ openGraph/JSON-LD); flatten it so the same field probes
            # that work for the platform scrapers also capture publish dates for community pages.
            flat = _flatten(r)
            text = _first_str(flat, _TEXT_KEYS)
            if not text:
                continue
            link = _first_str(flat, _URL_KEYS)
            items.append(RawItem(
                source=self.name,
                url=link,
                title=None,
                domain=_domain(link) or self.channel,
                content=text,
                score=None,
                query=query,
                channel=self.channel,
                engagement_score=_first_int(flat, _LIKE_KEYS),
                comment_count=_first_int(flat, _COMMENT_KEYS),
                posted_at=_first_time(flat, _TIME_KEYS),
            ))
        logger.info("Apify %s: %d post(s) for %r in %.0fs",
                    self.channel, len(items), query, time.monotonic() - t0)
        return items

    async def fetch_comments_for_post(self, post_url: str | None, *,
                                      max_results: int | None = None) -> list[RawComment]:
        """Scrape comments/replies for ONE parent post URL via the channel's comments actor.

        Returns [] (never raises) when comments aren't configured, the URL is missing, or the
        actor errors — so a single post can't break the ingest. Comments are PII-scrubbed and
        AE-screened downstream by the pipeline, exactly like posts.
        """
        if not self.comments_configured() or not post_url:
            return []
        cap = min(max_results or self.max_comments_per_post,
                  self.max_comments_per_post) or self.max_comments_per_post
        t0 = time.monotonic()
        payload = _render(self.comments_input_template, post_url=post_url, cap=cap)
        data = await self._run_and_collect(self.comments_actor_id, payload, cap, t0,
                                           _COMMENT_MAX_WAIT_SECONDS, label="(comments) ")
        out: list[RawComment] = []
        for r in data:
            if not isinstance(r, dict):
                continue
            if r.get("dataType") == "post" or r.get("type") == "post":
                continue
            text = _first_str(r, _COMMENT_TEXT_KEYS)
            if not text:
                continue
            out.append(RawComment(
                channel=self.channel,
                text=text,
                engagement_score=_first_int(r, _LIKE_KEYS),
                posted_at=_first_time(r, _TIME_KEYS),
                url=_first_str(r, _URL_KEYS),
            ))
        logger.info("Apify %s: %d comment(s) for a post in %.0fs",
                    self.channel, len(out), time.monotonic() - t0)
        return out

    async def _run_and_collect(self, actor_id: str, payload: dict, cap: int, t0: float,
                               max_wait: float = _MAX_WAIT_SECONDS, *, label: str = "") -> list[dict]:
        """Start the actor run, poll its dataset, and return raw item dicts as soon as we have
        `cap` of them or the wait budget expires; abort a still-running actor to cap cost."""
        actor_path = actor_id.replace("/", "~")
        logger.info("Apify %s: starting actor %s %s(cap=%d)…",
                    self.channel, actor_id, label, cap)
        run_id: str | None = None
        items: list[dict] = []
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                start = await client.post(
                    f"{self._base_url}/v2/acts/{actor_path}/runs",
                    params={"token": self._token},
                    json=payload,
                )
                start.raise_for_status()
                run = (start.json() or {}).get("data") or {}
                run_id = run.get("id")
                dataset_id = run.get("defaultDatasetId")
                if not dataset_id:
                    logger.warning("Apify %s: actor run returned no dataset id", self.channel)
                    return []

                deadline = t0 + max_wait
                terminal = False
                while True:
                    await asyncio.sleep(_POLL_SECONDS)
                    try:
                        d = await client.get(
                            f"{self._base_url}/v2/datasets/{dataset_id}/items",
                            params={"token": self._token, "clean": "true", "limit": cap},
                        )
                        body = d.json() if d.status_code == 200 else None
                        if isinstance(body, list):
                            items = body
                    except Exception:  # noqa: BLE001 — transient dataset read; keep polling
                        pass
                    if len(items) >= cap or time.monotonic() >= deadline:
                        break
                    try:
                        rs = await client.get(
                            f"{self._base_url}/v2/actor-runs/{run_id}",
                            params={"token": self._token},
                        )
                        status = ((rs.json() or {}).get("data") or {}).get("status")
                    except Exception:  # noqa: BLE001
                        status = None
                    if status in _TERMINAL:
                        terminal = True
                        break

                # Cost control: stop the actor if it is still running once we have enough.
                if run_id and not terminal:
                    try:
                        await client.post(
                            f"{self._base_url}/v2/actor-runs/{run_id}/abort",
                            params={"token": self._token},
                        )
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as e:  # noqa: BLE001 — never break the run on one channel/query
            logger.warning("Apify %s actor %s failed after %.0fs: %s",
                           self.channel, actor_id, time.monotonic() - t0, e)
        return items


# --------------------------------------------------------------------------- helpers


def _render(value, *, term: str = "", cap: int = 0, post_url: str = ""):
    """Deep-substitute {term}/{post_url} (string replace) and exact "{cap}" (-> int)."""
    if isinstance(value, str):
        if value == "{cap}":
            return cap
        return value.replace("{term}", term).replace("{post_url}", post_url)
    if isinstance(value, dict):
        return {k: _render(v, term=term, cap=cap, post_url=post_url) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, term=term, cap=cap, post_url=post_url) for v in value]
    return value


def _flatten(r: dict) -> dict:
    """Surface a crawl record's nested ``metadata`` so the flat field probes can reach it.

    The website-content-crawler (crawl-mode community channels) returns the page text at the
    top level but the publish date/author only inside ``metadata`` — either as scalar fields,
    an ``openGraph`` list of ``{property, content}`` tags (``article:published_time``), or a
    ``jsonLd`` list of structured-data nodes (``datePublished``). We merge those into a single
    flat dict, with the top-level record winning, so ``_first_time``/``_first_str`` see them.
    Platform-scraper records have no ``metadata`` block and pass through unchanged.
    """
    if not isinstance(r, dict):
        return {}
    md = r.get("metadata")
    if not isinstance(md, dict):
        return r
    merged: dict = {}
    og = md.get("openGraph")
    if isinstance(og, list):
        for tag in og:
            if isinstance(tag, dict):
                prop = tag.get("property") or tag.get("name")
                if prop:
                    merged[str(prop)] = tag.get("content")
    elif isinstance(og, dict):
        merged.update(og)
    for node in _as_nodes(md.get("jsonLd") or md.get("jsonLD")):
        merged.update({k: v for k, v in node.items() if not isinstance(v, (dict, list))})
    merged.update({k: v for k, v in md.items()
                   if k not in ("openGraph", "jsonLd", "jsonLD")
                   and not isinstance(v, (dict, list))})
    merged.update(r)  # top-level record always wins over derived metadata
    return merged


def _as_nodes(value) -> list[dict]:
    """Coerce a JSON-LD value (dict, list of dicts, or None) to a list of dict nodes."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [n for n in value if isinstance(n, dict)]
    return []


def _first_str(d: dict, keys) -> str | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _first_int(d: dict, keys) -> int | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str):
            t = v.strip().replace(",", "")
            if t.isdigit():
                return int(t)
    return None


def _first_time(d: dict, keys) -> datetime | None:
    for k in keys:
        dt = _parse_time(d.get(k))
        if dt:
            return dt
    return None


def _parse_time(v) -> datetime | None:
    """Best-effort parse of epoch (s/ms) or ISO-8601 timestamps to an aware datetime."""
    if isinstance(v, bool) or v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        ts = float(v)
        if ts > 1e12:  # milliseconds since epoch
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        if s.isdigit():
            return _parse_time(int(s))
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None
