"""Keyword-based theme tagging — fast, transparent, and dependency-free.

Each theme carries an LLM-generated keyword list; a response is assigned to a theme when its
text contains those keywords (word-boundary, case-insensitive). Matched keywords are persisted
as evidence and relevance scales with the number of distinct keyword hits. Tagging is
incremental: responses already tagged for the active taxonomy version are skipped.
"""
import json
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.response import Response
from app.models.theme import ResponseTheme, Theme
from app.utils.logging import get_logger

logger = get_logger("insights.tagging")

_SCOREABLE = ("SUCCESS", "TRUNCATED")
_MAX_THEMES_PER_RESPONSE = 4
_COMMIT_EVERY = 200


def _compile_keywords(keywords: list[str]) -> list[tuple[str, re.Pattern]]:
    compiled: list[tuple[str, re.Pattern]] = []
    for kw in keywords:
        kw = (kw or "").strip().lower()
        if len(kw) < 3:
            continue
        # word-boundary match that tolerates multi-word phrases
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])")
        compiled.append((kw, pattern))
    return compiled


def match_theme(text_lower: str, compiled: list[tuple[str, re.Pattern]]) -> list[str]:
    """Return the distinct keywords from a theme that appear in the text."""
    hits: list[str] = []
    for kw, pattern in compiled:
        if pattern.search(text_lower):
            hits.append(kw)
    return hits


async def tag_responses(
    db: AsyncSession,
    taxonomy_version: int,
    *,
    response_ids: list[str] | None = None,
) -> int:
    """Assign responses to themes for the given taxonomy version. Returns # newly tagged."""
    themes = list(
        (await db.execute(select(Theme).where(Theme.taxonomy_version == taxonomy_version)))
        .scalars()
        .all()
    )
    if not themes:
        return 0

    compiled_by_theme: list[tuple[Theme, list[tuple[str, re.Pattern]]]] = []
    for t in themes:
        try:
            keywords = json.loads(t.keywords) if t.keywords else []
        except Exception:  # noqa: BLE001
            keywords = []
        compiled_by_theme.append((t, _compile_keywords(keywords)))

    # Responses to consider
    rstmt = select(Response).where(Response.status.in_(_SCOREABLE))
    if response_ids:
        rstmt = rstmt.where(Response.response_id.in_(response_ids))
    responses = list((await db.execute(rstmt)).scalars().all())

    # Skip responses already tagged for this version (incremental)
    already_stmt = select(ResponseTheme.response_id).where(
        ResponseTheme.taxonomy_version == taxonomy_version
    )
    already = {rid for (rid,) in (await db.execute(already_stmt)).all()}

    tagged_count = 0
    pending = 0
    for r in responses:
        if r.response_id in already:
            continue
        text_lower = (r.response_text or "").lower()
        if not text_lower:
            continue

        scored: list[tuple[Theme, list[str]]] = []
        for theme, compiled in compiled_by_theme:
            hits = match_theme(text_lower, compiled)
            if hits:
                scored.append((theme, hits))

        if not scored:
            continue

        scored.sort(key=lambda x: len(x[1]), reverse=True)
        for theme, hits in scored[:_MAX_THEMES_PER_RESPONSE]:
            db.add(
                ResponseTheme(
                    id=str(uuid.uuid4()),
                    response_id=r.response_id,
                    theme_id=theme.theme_id,
                    taxonomy_version=taxonomy_version,
                    relevance=min(1.0, len(hits) / 3.0),
                    matched_keywords=json.dumps(hits),
                )
            )
        tagged_count += 1
        pending += 1
        if pending >= _COMMIT_EVERY:
            await db.commit()
            pending = 0

    if pending:
        await db.commit()

    logger.info(
        "tagging: %d responses tagged for taxonomy v%d", tagged_count, taxonomy_version
    )
    return tagged_count
