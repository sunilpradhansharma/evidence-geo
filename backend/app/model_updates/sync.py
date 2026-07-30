"""Vendor version + changelog sync orchestrator (FR-707a).

`sync_model_updates` runs the capture pipeline end-to-end:
  1. refresh model_version_observation from our own responses (no network);
  2. turn each newly-observed version into a real api-sourced update event (no network);
  3. IF opt-in enabled — fetch each vendor changelog and upsert/enrich events with the
     real "what changed" text (source="changelog");
  4. re-correlate any material drifts that still lack a linked release.

Opt-in (settings.model_update_sync_enabled) gates ONLY the network step (3); steps 1, 2
and 4 always run so the tab is anchored to real versions even with sync disabled.
"""
from __future__ import annotations

from datetime import date as date_cls

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.model_updates.changelog import ChangelogEntry, capture_vendor
from app.model_updates.sources import enabled_sources
from app.model_updates.versions import detect_version_transitions, refresh_observations
from app.models.model_release import (
    EVENT_RELEASE,
    SOURCE_API,
    SOURCE_CHANGELOG,
    SOURCE_INFERRED,
    ModelReleaseLog,
    utcnow,
)
from app.utils.logging import get_logger

logger = get_logger("model_updates.sync")


def is_enabled() -> bool:
    """True only when the opt-in changelog sync is switched on AND at least one source URL
    is configured. Version-observation anchoring runs regardless (no network)."""
    if not get_settings().model_update_sync_enabled:
        return False
    return bool(enabled_sources())


async def _find_event(
    db: AsyncSession, *, platform: str, version: str | None, effective_date: date_cls | None,
) -> ModelReleaseLog | None:
    """Locate the existing event a changelog entry should enrich: prefer an exact
    (platform, version) match; else a (platform, effective_date) match for version-less rows."""
    if version:
        row = (await db.execute(
            select(ModelReleaseLog).where(
                func.lower(ModelReleaseLog.target_platform) == platform.lower(),
                ModelReleaseLog.version == version,
            ).limit(1)
        )).scalars().first()
        if row is not None:
            return row
    if effective_date is not None:
        return (await db.execute(
            select(ModelReleaseLog).where(
                func.lower(ModelReleaseLog.target_platform) == platform.lower(),
                ModelReleaseLog.effective_date == effective_date,
                ModelReleaseLog.version.is_(None),
            ).limit(1)
        )).scalars().first()
    return None


async def _upsert_changelog_entry(db: AsyncSession, entry: ChangelogEntry) -> str:
    """Enrich a matching event with the vendor changelog, or create a new changelog event.
    Returns 'created' | 'enriched' | 'skipped'."""
    existing = await _find_event(
        db, platform=entry.platform, version=entry.version, effective_date=entry.effective_date,
    )
    if existing is not None:
        # Enrich in place. Never downgrade an api-confirmed event's provenance, but always
        # add the human-readable "what changed" + vendor effective date + link.
        existing.summary = entry.summary
        existing.event_type = entry.event_type or existing.event_type
        if entry.effective_date is not None:
            existing.effective_date = entry.effective_date
        if entry.url:
            existing.url = entry.url
        if existing.source in (SOURCE_INFERRED,) or existing.source not in (SOURCE_API,):
            # inferred/manual/seed get upgraded to changelog; api stays api (higher trust).
            existing.source = SOURCE_CHANGELOG
        # Confidence: api stays 1.0; otherwise take the extractor confidence.
        if existing.source != SOURCE_API:
            existing.confidence = entry.confidence
        return "enriched"

    release_date = entry.effective_date or utcnow().date()
    db.add(ModelReleaseLog(
        target_platform=entry.platform,
        release_date=release_date,
        version=entry.version,
        release_notes=f"{entry.vendor} changelog: {entry.summary}",
        url=entry.url,
        source=SOURCE_CHANGELOG,
        event_type=entry.event_type or EVENT_RELEASE,
        summary=entry.summary,
        effective_date=entry.effective_date,
        first_seen_at=None,
        confidence=entry.confidence,
    ))
    return "created"


async def sync_model_updates(db: AsyncSession) -> dict:
    """Run the full capture pipeline. Safe to call repeatedly (idempotent)."""
    from app.services import model_release_service as mrs

    observed = await refresh_observations(db)
    transitions = await detect_version_transitions(db)

    vendors_synced = 0
    entries_created = 0
    entries_enriched = 0
    vendor_errors: list[str] = []

    if is_enabled():
        for source in enabled_sources():
            try:
                entries = await capture_vendor(source)
            except Exception as e:  # noqa: BLE001 — one vendor failing must not abort the sync
                logger.warning("vendor sync failed for %s: %s", source.vendor, e)
                vendor_errors.append(source.vendor)
                continue
            vendors_synced += 1
            for entry in entries:
                result = await _upsert_changelog_entry(db, entry)
                if result == "created":
                    entries_created += 1
                elif result == "enriched":
                    entries_enriched += 1
        if entries_created or entries_enriched:
            await db.commit()

    linked = await mrs._recorrelate_unlinked(db)
    # Emit the high-impact alert signal for any update that just crossed the threshold.
    flagged = await mrs.flag_high_impact_updates(db)

    return {
        "versions_observed": observed,
        "version_transitions_created": transitions,
        "changelog_sync_enabled": is_enabled(),
        "vendors_synced": vendors_synced,
        "changelog_events_created": entries_created,
        "changelog_events_enriched": entries_enriched,
        "vendor_errors": vendor_errors,
        "drifts_linked": linked,
        "high_impact_flagged": flagged.get("flagged", 0),
    }


def sync_status() -> dict:
    """Config snapshot for the UI / status endpoint (no DB / network calls)."""
    settings = get_settings()
    return {
        "enabled": is_enabled(),
        "sync_flag": settings.model_update_sync_enabled,
        "scheduler_enabled": settings.model_update_sync_scheduler_enabled,
        "sync_hour_utc": settings.model_update_sync_hour_utc,
        "sources": [
            {"vendor": s.vendor, "platforms": list(s.platforms), "url": s.url(), "fmt": s.fmt}
            for s in enabled_sources()
        ],
    }
