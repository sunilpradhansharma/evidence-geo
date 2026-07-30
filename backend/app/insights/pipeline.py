"""Insights pipeline orchestration.

`rebuild` discovers a fresh theme taxonomy (new version) and tags every response against it.
`tag_new` is the cheap, incremental step run after each monitoring run — it tags only the
newly-arrived responses against the current taxonomy. `status` reports coverage for the UI.
"""
import json
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights import tagging, taxonomy, trends
from app.models.response import Response
from app.models.theme import ResponseTheme, Theme
from app.utils.logging import get_logger

logger = get_logger("insights.pipeline")

_SCOREABLE = ("SUCCESS", "TRUNCATED")


async def rebuild(db: AsyncSession, *, target_themes: int = 12, sample_cap: int = 300) -> dict:
    """Full rebuild: discover a new taxonomy version and tag all responses against it."""
    items = await taxonomy.gather_corpus(db, cap=sample_cap)
    if len(items) < 3:
        return {"status": "skipped", "reason": "not enough responses to discover themes",
                "themes": 0, "tagged": 0}

    theme_dicts = await taxonomy.build_taxonomy(items, target=target_themes)
    if not theme_dicts:
        return {"status": "skipped", "reason": "no themes discovered", "themes": 0, "tagged": 0}

    version = (await trends.current_version(db)) + 1
    for t in theme_dicts:
        db.add(Theme(
            theme_id=str(uuid.uuid4()),
            taxonomy_version=version,
            label=t["label"][:160],
            description=t.get("description"),
            keywords=json.dumps(t.get("keywords", [])),
            category=t.get("category"),
        ))
    await db.commit()

    tagged = await tagging.tag_responses(db, version)
    logger.info("insights rebuild complete: v%d, %d themes, %d tagged", version, len(theme_dicts), tagged)
    return {"status": "ok", "taxonomy_version": version, "themes": len(theme_dicts), "tagged": tagged}


async def tag_new(db: AsyncSession) -> dict:
    """Incrementally tag any untagged responses against the current taxonomy."""
    version = await trends.current_version(db)
    if version == 0:
        return {"status": "no_taxonomy", "tagged": 0}
    tagged = await tagging.tag_responses(db, version)
    return {"status": "ok", "taxonomy_version": version, "tagged": tagged}


async def status(db: AsyncSession) -> dict:
    version = await trends.current_version(db)
    themes_n = 0
    tagged_resp = 0
    if version:
        themes_n = (await db.execute(
            select(func.count()).select_from(Theme).where(Theme.taxonomy_version == version)
        )).scalar() or 0
        tagged_resp = (await db.execute(
            select(func.count(func.distinct(ResponseTheme.response_id)))
            .where(ResponseTheme.taxonomy_version == version)
        )).scalar() or 0

    total_resp = (await db.execute(
        select(func.count()).select_from(Response).where(Response.status.in_(_SCOREABLE))
    )).scalar() or 0

    return {
        "taxonomy_version": version,
        "themes": int(themes_n),
        "responses_total": int(total_resp),
        "responses_tagged": int(tagged_resp),
        "responses_untagged": max(0, int(total_resp) - int(tagged_resp)),
    }
