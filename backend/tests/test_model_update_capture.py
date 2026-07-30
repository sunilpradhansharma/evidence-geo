"""FR-707a vendor version + changelog capture tests.

Covers: version-observation rollup + real transition detection from our own traffic,
LLM changelog extraction (confidence/platform filtering), RSS flattening, graceful skip
when a source is unreachable, and idempotent end-to-end sync (opt-in gated).
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.database import Base
from app.models.model_release import (
    SOURCE_API,
    SOURCE_CHANGELOG,
    ModelReleaseLog,
    ModelVersionObservation,
)
from app.models.response import Response
from app.model_updates import changelog, sync
from app.model_updates import versions as ver
from app.model_updates.changelog import ChangelogEntry
from app.model_updates.sources import VendorSource


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from app.models import audit_log, scoring  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _resp(platform, version, when, *, qid="Q1"):
    return Response(
        response_id=f"r-{uuid.uuid4().hex[:10]}", run_id=f"run-{uuid.uuid4().hex[:4]}",
        llm_name=platform, llm_model_version=version, persona="Provider",
        question_id=qid, question_text="q", therapeutic_area="Obesity",
        brand_focus="Rinvoq", domain="Efficacy", response_text="answer",
        status="SUCCESS", timestamp_utc=when, created_at=when,
    )


# --- Version observation + real transition detection (no network) ---------------
async def test_refresh_observations_rolls_up_versions(session):
    now = datetime.now(timezone.utc)
    session.add_all([
        _resp("gpt-4o", "gpt-4o-2024-08-06", now - timedelta(days=10)),
        _resp("gpt-4o", "gpt-4o-2024-08-06", now - timedelta(days=9)),
        _resp("gpt-4o", "gpt-4o-2024-11-20", now - timedelta(days=2)),
    ])
    await session.commit()

    observed = await ver.refresh_observations(session)
    assert observed == 2
    obs = (await session.execute(select(ModelVersionObservation))).scalars().all()
    counts = {o.version: o.response_count for o in obs}
    assert counts["gpt-4o-2024-08-06"] == 2
    assert counts["gpt-4o-2024-11-20"] == 1


async def test_detect_transitions_skips_baseline_and_is_idempotent(session):
    now = datetime.now(timezone.utc)
    session.add_all([
        _resp("gpt-4o", "gpt-4o-2024-08-06", now - timedelta(days=10)),
        _resp("gpt-4o", "gpt-4o-2024-11-20", now - timedelta(days=2)),
    ])
    await session.commit()
    await ver.refresh_observations(session)

    created = await ver.detect_version_transitions(session)
    assert created == 1  # baseline (first version) is NOT a transition

    events = (await session.execute(
        select(ModelReleaseLog).where(ModelReleaseLog.source == SOURCE_API)
    )).scalars().all()
    assert len(events) == 1
    ev = events[0]
    assert ev.version == "gpt-4o-2024-11-20"
    assert ev.confidence == 1.0
    assert ev.first_seen_at is not None

    # Re-running creates nothing new.
    assert await ver.detect_version_transitions(session) == 0


async def test_single_version_has_no_transition(session):
    now = datetime.now(timezone.utc)
    session.add(_resp("claude", "claude-3-5-sonnet-20241022", now))
    await session.commit()
    await ver.refresh_observations(session)
    assert await ver.detect_version_transitions(session) == 0


# --- Changelog extraction (LLM filtering) ---------------------------------------
_OPENAI_SRC = VendorSource(
    vendor="OpenAI", platforms=("gpt-4o",), fmt="html",
    settings_attr="model_update_openai_changelog_url", focus="OpenAI GPT-4o",
)
_AWS_SRC = VendorSource(
    vendor="AWS Bedrock", platforms=("nova-pro", "llama"), fmt="rss",
    settings_attr="model_update_aws_whatsnew_rss_url", focus="Bedrock Nova + Llama",
)


async def test_extract_entries_filters_low_confidence(session, monkeypatch):
    async def fake_chat_json(system, user, **kw):
        return {"entries": [
            {"platform": "gpt-4o", "version": "gpt-4o-2024-11-20", "effective_date": "2024-11-20",
             "event_type": "release", "summary": "Updated GPT-4o snapshot.", "confidence": 0.9},
            {"platform": "gpt-4o", "version": None, "effective_date": None,
             "event_type": "release", "summary": "Vague note.", "confidence": 0.2},
        ]}

    monkeypatch.setattr(changelog, "chat_json", fake_chat_json)
    entries = await changelog.extract_entries(_OPENAI_SRC, "some changelog text")
    assert len(entries) == 1
    e = entries[0]
    assert e.platform == "gpt-4o"
    assert e.version == "gpt-4o-2024-11-20"
    assert e.effective_date.isoformat() == "2024-11-20"
    assert e.event_type == "release"


async def test_extract_multiplatform_requires_platform(session, monkeypatch):
    async def fake_chat_json(system, user, **kw):
        return {"entries": [
            {"platform": "llama", "version": "llama-3.3-70b", "effective_date": "2025-01-10",
             "event_type": "release", "summary": "Llama 3.3 on Bedrock.", "confidence": 0.8},
            {"platform": None, "version": "x", "effective_date": None,
             "event_type": "release", "summary": "Ambiguous vendor note.", "confidence": 0.9},
        ]}

    monkeypatch.setattr(changelog, "chat_json", fake_chat_json)
    entries = await changelog.extract_entries(_AWS_SRC, "aws whats new text")
    # The platform-less entry is dropped (multi-platform source can't guess).
    assert len(entries) == 1 and entries[0].platform == "llama"


async def test_extract_empty_text_returns_empty(session):
    assert await changelog.extract_entries(_OPENAI_SRC, "   ") == []


def test_rss_to_text_flattens_items():
    xml = """
    <rss><channel>
      <item><title>Amazon Nova Pro now available</title>
        <pubDate>Mon, 02 Dec 2024 00:00:00 GMT</pubDate>
        <description><![CDATA[<p>New Nova Pro model on Bedrock.</p>]]></description>
        <link>https://aws.amazon.com/x</link></item>
    </channel></rss>
    """
    text = changelog._rss_to_text(xml, max_items=10)
    assert "Amazon Nova Pro now available" in text
    assert "New Nova Pro model on Bedrock." in text


async def test_capture_vendor_graceful_skip_on_fetch_failure(session, monkeypatch):
    async def boom(source):
        return ""  # simulate unreachable source -> empty text
    monkeypatch.setattr(changelog, "fetch_source_text", boom)
    assert await changelog.capture_vendor(_OPENAI_SRC) == []


# --- End-to-end sync (opt-in gated, idempotent) ---------------------------------
async def test_sync_disabled_still_anchors_versions(session, monkeypatch):
    monkeypatch.setattr(sync, "is_enabled", lambda: False)
    now = datetime.now(timezone.utc)
    session.add_all([
        _resp("gemini", "gemini-1.5-pro-002", now - timedelta(days=5)),
        _resp("gemini", "gemini-2.0-flash", now - timedelta(days=1)),
    ])
    await session.commit()

    res = await sync.sync_model_updates(session)
    assert res["changelog_sync_enabled"] is False
    assert res["versions_observed"] == 2
    assert res["version_transitions_created"] == 1
    assert res["changelog_events_created"] == 0


async def test_sync_enriches_api_event_from_changelog_and_is_idempotent(session, monkeypatch):
    now = datetime.now(timezone.utc)
    session.add_all([
        _resp("gpt-4o", "gpt-4o-2024-08-06", now - timedelta(days=10)),
        _resp("gpt-4o", "gpt-4o-2024-11-20", now - timedelta(days=2)),
    ])
    await session.commit()

    entry = ChangelogEntry(
        platform="gpt-4o", version="gpt-4o-2024-11-20",
        effective_date=(now - timedelta(days=3)).date(), summary="Snapshot refresh.",
        event_type="release", confidence=0.85, url="https://openai/changelog", vendor="OpenAI",
    )

    async def fake_capture(source):
        return [entry]

    monkeypatch.setattr(sync, "is_enabled", lambda: True)
    monkeypatch.setattr(sync, "enabled_sources", lambda: [_OPENAI_SRC])
    monkeypatch.setattr(sync, "capture_vendor", fake_capture)

    res = await sync.sync_model_updates(session)
    assert res["version_transitions_created"] == 1
    # The changelog entry matches the api event on (platform, version) -> enriched, not new.
    assert res["changelog_events_enriched"] == 1
    assert res["changelog_events_created"] == 0

    ev = (await session.execute(
        select(ModelReleaseLog).where(ModelReleaseLog.version == "gpt-4o-2024-11-20")
    )).scalars().one()
    assert ev.source == SOURCE_API           # api provenance preserved (not downgraded)
    assert ev.summary == "Snapshot refresh."  # but enriched with what-changed
    assert ev.confidence == 1.0               # api confidence kept
    assert ev.effective_date is not None

    # Idempotent: nothing new the second time.
    res2 = await sync.sync_model_updates(session)
    assert res2["version_transitions_created"] == 0
    assert res2["changelog_events_created"] == 0


async def test_sync_creates_changelog_event_without_api_match(session, monkeypatch):
    now = datetime.now(timezone.utc)
    entry = ChangelogEntry(
        platform="claude", version="claude-3-5-sonnet-20241022",
        effective_date=(now - timedelta(days=1)).date(), summary="New Sonnet.",
        event_type="release", confidence=0.9, url="https://anthropic/notes", vendor="Anthropic",
    )

    async def fake_capture(source):
        return [entry]

    monkeypatch.setattr(sync, "is_enabled", lambda: True)
    monkeypatch.setattr(sync, "enabled_sources", lambda: [_OPENAI_SRC])
    monkeypatch.setattr(sync, "capture_vendor", fake_capture)

    res = await sync.sync_model_updates(session)
    assert res["changelog_events_created"] == 1

    ev = (await session.execute(
        select(ModelReleaseLog).where(ModelReleaseLog.version == "claude-3-5-sonnet-20241022")
    )).scalars().one()
    assert ev.source == SOURCE_CHANGELOG
    assert ev.summary == "New Sonnet."
    assert ev.confidence == 0.9
