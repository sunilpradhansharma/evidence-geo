"""Version-observation refresh + real transition detection (FR-707a).

Ground truth: every Response stores llm_model_version (OpenAI dated snapshot, Gemini
modelVersion, Anthropic model id, Bedrock model id). This module rolls those up into
model_version_observation (per platform+version: first/last seen + count) and turns each
NEW version we observe into a real, api-sourced model_release_log event — no guessing.
"""
from __future__ import annotations

from datetime import timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.model_release import (
    EVENT_RELEASE,
    SOURCE_API,
    ModelReleaseLog,
    ModelVersionObservation,
    utcnow,
)
from app.models.response import Response
from app.utils.logging import get_logger

logger = get_logger("model_updates.versions")


def _as_utc(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


async def refresh_observations(db: AsyncSession) -> int:
    """Rebuild model_version_observation from responses that carry a version string.

    Upserts one row per (platform, version) with first/last-seen + response count.
    Returns the number of distinct (platform, version) pairs observed."""
    rows = (await db.execute(
        select(
            Response.llm_name.label("platform"),
            Response.llm_model_version.label("version"),
            func.min(Response.timestamp_utc).label("first_seen"),
            func.max(Response.timestamp_utc).label("last_seen"),
            func.count().label("cnt"),
        )
        .where(
            Response.llm_model_version.is_not(None),
            Response.llm_model_version != "",
        )
        .group_by(Response.llm_name, Response.llm_model_version)
    )).all()

    existing = {
        (o.target_platform.lower(), o.version): o
        for o in (await db.execute(select(ModelVersionObservation))).scalars().all()
    }

    observed = 0
    now = utcnow()
    for r in rows:
        if not r.platform or not r.version:
            continue
        observed += 1
        first_seen = _as_utc(r.first_seen) or now
        last_seen = _as_utc(r.last_seen) or now
        key = (r.platform.lower(), r.version)
        obs = existing.get(key)
        if obs is None:
            db.add(ModelVersionObservation(
                target_platform=r.platform, version=r.version,
                first_seen_at=first_seen, last_seen_at=last_seen,
                response_count=int(r.cnt or 0), created_at=now, updated_at=now,
            ))
        else:
            obs.first_seen_at = min(_as_utc(obs.first_seen_at) or first_seen, first_seen)
            obs.last_seen_at = max(_as_utc(obs.last_seen_at) or last_seen, last_seen)
            obs.response_count = int(r.cnt or 0)
            obs.updated_at = now
    if rows:
        await db.commit()
    return observed


async def list_current_versions(db: AsyncSession) -> list[dict]:
    """Per target: the CURRENT live version (most-recently-seen) + when it first appeared,
    how many distinct versions we've observed, and total responses. Powers the UI panel."""
    obs = (await db.execute(
        select(ModelVersionObservation).order_by(
            ModelVersionObservation.target_platform,
            ModelVersionObservation.last_seen_at.desc(),
        )
    )).scalars().all()

    by_platform: dict[str, list[ModelVersionObservation]] = {}
    for o in obs:
        by_platform.setdefault(o.target_platform, []).append(o)

    out: list[dict] = []
    for platform, versions in by_platform.items():
        current = versions[0]  # most recent last_seen first
        total_responses = sum(int(v.response_count or 0) for v in versions)
        out.append({
            "target_platform": platform,
            "current_version": current.version,
            "current_since": _as_utc(current.first_seen_at),
            "last_seen_at": _as_utc(current.last_seen_at),
            "versions_observed": len(versions),
            "total_responses": total_responses,
        })
    out.sort(key=lambda x: x["target_platform"])
    return out


async def detect_version_transitions(db: AsyncSession) -> int:
    """Turn each NON-first observed version (per platform) into a real api-sourced update
    event. The FIRST version we ever saw is the baseline, not a transition. Idempotent on
    (platform, version, source="api"). Returns the number of NEW events created."""
    obs = (await db.execute(
        select(ModelVersionObservation).order_by(
            ModelVersionObservation.target_platform, ModelVersionObservation.first_seen_at
        )
    )).scalars().all()

    by_platform: dict[str, list[ModelVersionObservation]] = {}
    for o in obs:
        by_platform.setdefault(o.target_platform.lower(), []).append(o)

    # Existing api events, so we don't duplicate on re-sync.
    existing_api = {
        (r.target_platform.lower(), r.version)
        for r in (await db.execute(
            select(ModelReleaseLog).where(ModelReleaseLog.source == SOURCE_API)
        )).scalars().all()
    }

    created = 0
    for platform, versions in by_platform.items():
        # Sorted by first_seen already; skip index 0 (baseline).
        for o in versions[1:]:
            if (platform, o.version) in existing_api:
                continue
            first_seen = _as_utc(o.first_seen_at) or utcnow()
            db.add(ModelReleaseLog(
                target_platform=o.target_platform,
                release_date=first_seen.date(),
                version=o.version,
                release_notes=f"New model version {o.version} first observed in responses "
                              f"on {first_seen.date().isoformat()}.",
                url=None,
                source=SOURCE_API,
                event_type=EVENT_RELEASE,
                summary=None,
                effective_date=None,
                first_seen_at=first_seen,
                confidence=1.0,
            ))
            created += 1
    if created:
        await db.commit()
    return created
