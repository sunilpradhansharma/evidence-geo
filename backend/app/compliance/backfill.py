"""Re-redaction backfill (G2 defense in depth).

Re-runs the central PHI/PII redactor over already-stored free text so rows captured
before a detector upgrade get cleaned in place. Because only redacted text is ever
persisted, re-running the *stronger* detector now catches identifiers (names, US
locations) the earlier layer missed. Idempotent: placeholders never re-match, so it is
safe to run repeatedly.

Covers HarvestedQuestion (question_text, raw_excerpt, source_title), SocialPost
(text, text_original) and SocialComment (text, text_original). It also re-runs the
prompt-injection screen so ``Injection:*`` flags stay in sync and rebuilds ``pii_flags``.
It never touches ``ae_flag`` (a separate signal) or dedupe hashes.
"""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance import phi
from app.guardrails import injection
from app.models.harvested_question import HarvestedQuestion, utcnow
from app.models.social_comment import SocialComment
from app.models.social_post import SocialPost
from app.utils.logging import get_logger

logger = get_logger("compliance.backfill")


async def _redact(text: str | None) -> tuple[str | None, list[str]]:
    """Redact one nullable field, preserving None/empty. Returns (clean, flags)."""
    if not text:
        return text, []
    clean, flags = await phi.redact_async(text)
    return clean, flags


def _pii_json(*flag_sets: list[str], injection_labels: list[str]) -> str | None:
    flags: set[str] = set()
    for fs in flag_sets:
        flags.update(fs)
    flags.update(f"Injection:{label}" for label in injection_labels)
    return json.dumps(sorted(flags)) if flags else None


async def redact_backfill(db: AsyncSession, *, therapeutic_area: str | None = None) -> dict:
    """Re-redact all stored harvest + social free text. Returns a counts summary.

    ``therapeutic_area`` optionally scopes the social sweep (posts + their comments).
    """
    summary = {
        "harvested_scanned": 0, "harvested_updated": 0,
        "posts_scanned": 0, "posts_updated": 0,
        "comments_scanned": 0, "comments_updated": 0,
    }

    # --- Harvested questions -----------------------------------------------------
    hqs = (await db.execute(select(HarvestedQuestion))).scalars().all()
    for hq in hqs:
        summary["harvested_scanned"] += 1
        q_clean, q_flags = await _redact(hq.question_text)
        e_clean, e_flags = await _redact(hq.raw_excerpt)
        t_clean, t_flags = await _redact(hq.source_title)
        inj = injection.scan_injection(q_clean or "")
        new_pii = _pii_json(q_flags, e_flags, t_flags, injection_labels=inj)
        if (q_clean != hq.question_text or e_clean != hq.raw_excerpt
                or t_clean != hq.source_title or new_pii != hq.pii_flags):
            hq.question_text = q_clean or hq.question_text  # NOT NULL: never blank it
            hq.raw_excerpt = e_clean
            hq.source_title = t_clean
            hq.pii_flags = new_pii
            hq.updated_at = utcnow()
            summary["harvested_updated"] += 1
    await db.commit()

    # --- Social posts ------------------------------------------------------------
    pstmt = select(SocialPost)
    if therapeutic_area:
        pstmt = pstmt.where(SocialPost.therapeutic_area == therapeutic_area)
    for p in (await db.execute(pstmt)).scalars().all():
        summary["posts_scanned"] += 1
        text_clean, tf = await _redact(p.text)
        orig_clean, of = await _redact(p.text_original)
        inj = injection.scan_injection(text_clean or "")
        new_pii = _pii_json(tf, of, injection_labels=inj)
        if (text_clean != p.text or orig_clean != p.text_original or new_pii != p.pii_flags):
            p.text = text_clean or p.text  # NOT NULL
            p.text_original = orig_clean
            p.pii_flags = new_pii
            summary["posts_updated"] += 1
    await db.commit()

    # --- Social comments ---------------------------------------------------------
    cstmt = select(SocialComment)
    if therapeutic_area:
        cstmt = (
            cstmt.join(SocialPost, SocialComment.post_id == SocialPost.id)
            .where(SocialPost.therapeutic_area == therapeutic_area)
        )
    for c in (await db.execute(cstmt)).scalars().all():
        summary["comments_scanned"] += 1
        text_clean, tf = await _redact(c.text)
        orig_clean, of = await _redact(c.text_original)
        inj = injection.scan_injection(text_clean or "")
        new_pii = _pii_json(tf, of, injection_labels=inj)
        if (text_clean != c.text or orig_clean != c.text_original or new_pii != c.pii_flags):
            c.text = text_clean or c.text  # NOT NULL
            c.text_original = orig_clean
            c.pii_flags = new_pii
            summary["comments_updated"] += 1
    await db.commit()

    logger.info("redact backfill complete: %s", summary)
    return summary
