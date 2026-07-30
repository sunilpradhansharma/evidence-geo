"""Tests for the Rheumatology-only community crawl channels (myRAteam / Bezzy RA).

Covers the new social_sources.yaml wiring surfaced by app.social.pipeline:
  - ``_build_sources`` therapeutic-area gate (``only_areas``) + opt-in behavior,
  - ``_fetch_channel`` runs a ``mode='crawl'`` channel exactly once (ignores seed terms),
  - the ingest force-tags site-scoped (``only_areas``) posts to the ingest scope even when
    the classifier marks them not-relevant.

All network/LLM boundaries are monkeypatched; the DB is in-memory SQLite.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config.settings import load_yaml_config
from app.harvest.sources.base import RawItem
from app.models import social_comment, social_post  # noqa: F401 — register tables on Base
from app.models.database import Base
from app.models.social_post import SocialPost
from app.social import pipeline


def _cfg() -> dict:
    return load_yaml_config("social_sources.yaml")


def _names(sources) -> set[str]:
    return {s.channel for s in sources}


class _FakeSource:
    """Minimal stand-in for ApifySource (only what the pipeline touches)."""

    def __init__(self, channel: str, mode: str, items: list[RawItem]):
        self.channel = channel
        self.mode = mode
        self.results_cap = 50
        self.max_posts = 50
        self._items = items
        self.calls = 0

    async def search(self, term, *, max_results=None):
        self.calls += 1
        return list(self._items)

    def comments_configured(self) -> bool:
        return False


# --- _build_sources gating ---------------------------------------------------------

def test_community_channels_excluded_for_non_rheumatology_default_run():
    sources, force_scope = pipeline._build_sources(_cfg(), None, ta="Obesity")
    names = _names(sources)
    assert "reddit" in names           # ordinary platform channels still build
    assert "myrateam" not in names     # RA-only + opt-in: never in an Obesity run
    assert "bezzy" not in names
    assert force_scope == set()


def test_community_channels_hard_gated_even_when_requested_for_wrong_area():
    # Explicitly asking for myrateam under Obesity must STILL skip it (hard TA gate).
    sources, force_scope = pipeline._build_sources(_cfg(), ["myrateam", "bezzy"], ta="Obesity")
    assert _names(sources) == set()
    assert force_scope == set()


def test_opt_in_channels_excluded_from_default_rheumatology_run():
    sources, _ = pipeline._build_sources(_cfg(), None, ta="Rheumatology")
    names = _names(sources)
    assert "reddit" in names           # default run = the 5 platform channels
    assert "myrateam" not in names     # opt-in: not in a default "all channels" run
    assert "bezzy" not in names


def test_community_channels_included_when_selected_under_rheumatology():
    sources, force_scope = pipeline._build_sources(_cfg(), ["myrateam", "bezzy"], ta="Rheumatology")
    assert _names(sources) == {"myrateam", "bezzy"}
    assert force_scope == {"myrateam", "bezzy"}
    my = next(s for s in sources if s.channel == "myrateam")
    assert my.mode == "crawl"
    assert my.max_posts == 50
    assert my.results_cap == 50


# --- _fetch_channel run-once for crawl mode ----------------------------------------

async def test_crawl_channel_is_fetched_exactly_once():
    item = RawItem(source="apify:myrateam", url="https://www.myrateam.com/resources/x",
                   title=None, domain="myrateam.com", content="RA community article.",
                   channel="myrateam")
    src = _FakeSource("myrateam", "crawl", [item])
    out = await pipeline._fetch_channel(src, ["Rinvoq", "Humira"], 50, None)
    assert src.calls == 1              # ignores the 2 seed terms — a single crawl
    assert len(out) == 1


async def test_search_channel_loops_its_seed_terms():
    item = RawItem(source="apify:reddit", url="https://reddit.com/x", title=None,
                   domain="reddit.com", content="post text", channel="reddit")
    src = _FakeSource("reddit", "search", [item])
    out = await pipeline._fetch_channel(src, ["Rinvoq", "Humira"], 50, None)
    assert src.calls == 2              # one Apify run per seed term
    assert len(out) == 2


# --- ingest force-tags site-scoped posts -------------------------------------------

@pytest.fixture
async def session():
    engine_ = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine_.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine_, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine_.dispose()


async def test_crawl_post_is_force_tagged_to_scope_even_when_not_relevant(session, monkeypatch):
    item = RawItem(
        source="apify:myrateam",
        url="https://www.myrateam.com/resources/staying-active",
        title=None, domain="myrateam.com",
        content="A community article about staying active while living with rheumatoid arthritis.",
        channel="myrateam",
    )
    fake = _FakeSource("myrateam", "crawl", [item])

    monkeypatch.setattr(pipeline, "get_settings",
                        lambda: SimpleNamespace(apify_enabled=True, apify_api_token="test"))
    monkeypatch.setattr(pipeline, "_build_sources",
                        lambda cfg, channels, *, ta=None: ([fake], {"myrateam"}))
    monkeypatch.setattr(pipeline.classify, "build_vocab", lambda: ("", set()))
    # Classifier deems it NOT relevant -> without force-scope, therapeutic_area would be null.
    monkeypatch.setattr(pipeline.classify, "classify_posts",
                        AsyncMock(return_value=[{"relevant": False}]))
    # Force-scoped community posts now trigger the community-enrichment pass AND (adaptive by
    # default) the Tavily backfill when the crawl is thin; stub both so this test stays hermetic
    # (their own coverage lives in test_social_community.py).
    monkeypatch.setattr(pipeline.community, "extract_and_apply",
                        AsyncMock(return_value={"enriched": 0, "brand_mentions": 0, "questions": 0}))
    monkeypatch.setattr(pipeline, "_fetch_community_tavily", AsyncMock(return_value=[]))

    async def _identity_redact(text):
        return text, []

    monkeypatch.setattr(pipeline.scrub, "redact_async", _identity_redact)
    monkeypatch.setattr(pipeline.scrub, "scrub_source_url", lambda u: u)

    result = await pipeline.ingest(session, channels=["myrateam"], therapeutic_area="Rheumatology")
    assert result["status"] == "ok"
    assert result["ingested"] == 1

    rows = (await session.execute(select(SocialPost))).scalars().all()
    assert len(rows) == 1
    assert rows[0].channel == "myrateam"
    assert rows[0].therapeutic_area == "Rheumatology"  # force-tagged despite relevant=False
