"""Social Listening API (Obesity/GLP-1 demo — Apify).

POST /social/ingest runs the (slow, network-bound) Apify fetch + classify pipeline in the
background; the UI polls GET /social/status for progress (mirrors /harvest). GET /social/posts
lists captured posts; GET /social/insights returns the aggregated dashboard.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import AsyncSessionLocal, get_db
from app.schemas import SocialUnmetPromote
from app.services import social_service as svc
from app.social import pipeline
from app.utils.logging import get_logger

logger = get_logger("api.social")

router = APIRouter(prefix="/social", tags=["social"])

# In-memory ingest state (single-process POC). Surfaced via /social/status.
_SOCIAL: dict = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "last_result": None,
    "error": None,
    "progress": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _ingest_task(channels: list[str] | None, therapeutic_area: str | None,
                       terms: list[str] | None = None) -> None:
    # Scope label stamped on captured posts + used to fetch the brief/insights. Capped to the
    # DB column width (64) so the ad-hoc read path (frontend) and write path agree.
    scope = (therapeutic_area or "Obesity").strip()[:64] or "Obesity"
    progress: dict = {"phase": "starting", "channels_total": 0, "channels_done": 0,
                      "raw_posts": 0, "ingested": 0, "duplicates": 0, "ae": 0,
                      "comments_ingested": 0, "comment_ae": 0}
    _SOCIAL.update(running=True, started_at=_now(), finished_at=None, error=None,
                   last_result=None, progress=progress,
                   scope={"channels": channels, "therapeutic_area": scope, "terms": terms})
    try:
        async with AsyncSessionLocal() as db:
            result = await pipeline.ingest(db, channels=channels, therapeutic_area=scope,
                                           terms=terms, scope_label=scope, progress=progress)
            # Synthesize the AI narrative brief from the freshly-ingested sample (best-effort:
            # a brief failure must never fail the ingest, so it leaves the prior brief in place).
            try:
                progress["phase"] = "summarizing"
                from app.social import narrative
                await narrative.generate_social_brief(db, therapeutic_area=scope)
                await narrative.generate_platform_summaries(db, therapeutic_area=scope)
                # Voice-of-patient: cluster the community-crawl questions (no-op when none).
                await narrative.generate_unmet_questions(db, therapeutic_area=scope)
            except Exception as e:  # noqa: BLE001
                logger.warning("social brief generation skipped: %s", e)
            finally:
                progress["phase"] = "done"
        _SOCIAL["last_result"] = result
        logger.info("social ingest result: %s", result)
        # Mirror fresh posts + comments to Snowflake (fire-and-forget; safe no-op when disabled).
        try:
            from app.snowflake.mirror import run_mirror_safe
            await run_mirror_safe()
        except Exception as e:  # noqa: BLE001 — the mirror must never fail the ingest
            logger.warning("post-ingest Snowflake mirror skipped: %s", e)
    except Exception as e:  # noqa: BLE001
        _SOCIAL["error"] = str(e)
        logger.exception("social ingest failed: %s", e)
    finally:
        progress["phase"] = "done"
        _SOCIAL.update(running=False, finished_at=_now())


@router.post("/ingest", status_code=202)
async def run_ingest(
    background_tasks: BackgroundTasks,
    channels: str | None = Query(None, description="Comma-separated channels (reddit,tiktok,instagram,facebook,x). Empty = all enabled."),
    therapeutic_area: str = Query("Obesity"),
    terms: str | None = Query(None, description="Comma-separated custom seed terms for an ad-hoc free-text search. When set, captured posts are stamped with therapeutic_area as the scope label."),
):
    """Fetch + classify social posts in the background; poll /social/status for progress."""
    if _SOCIAL["running"]:
        raise HTTPException(409, "A social ingest is already running")
    ch = [c.strip() for c in (channels.split(",") if channels else []) if c.strip()]
    tl = [t.strip() for t in (terms.split(",") if terms else []) if t.strip()]
    # Ad-hoc free-text search: gate the query to pharma/medical topics BEFORE spending Apify
    # credits (dropdown areas send no `terms` and skip this). Fails open on LLM error.
    if tl:
        from app.social.guard import is_pharma_relevant
        allowed, reason = await is_pharma_relevant(", ".join(tl))
        if not allowed:
            raise HTTPException(422, detail=reason)
    background_tasks.add_task(_ingest_task, ch or None, therapeutic_area, tl or None)
    return {"status": "started"}


@router.get("/status")
async def status(db: AsyncSession = Depends(get_db)):
    base = await pipeline.stats(db)
    base["social"] = dict(_SOCIAL)
    return base


@router.get("/posts")
async def posts(
    therapeutic_area: str | None = None,
    channel: str | None = None,
    brand_focus: str | None = None,
    ae_only: bool = False,
    limit: int = Query(300, le=2000),
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    return await svc.list_posts(db, therapeutic_area=therapeutic_area, channel=channel,
                                brand_focus=brand_focus, ae_only=ae_only,
                                limit=limit, offset=offset)


@router.get("/posts/{post_id}/comments")
async def post_comments(
    post_id: int,
    limit: int = Query(200, le=1000),
    db: AsyncSession = Depends(get_db),
):
    """Captured comments for one post (the drawer's 'what people are saying' view)."""
    return await svc.list_comments(db, post_id, limit=limit)


@router.get("/insights")
async def insights(therapeutic_area: str = "Obesity", db: AsyncSession = Depends(get_db)):
    return await svc.insights(db, therapeutic_area=therapeutic_area)


@router.post("/brief")
async def generate_brief(therapeutic_area: str = Query("Obesity"),
                         db: AsyncSession = Depends(get_db)):
    """Regenerate the AI briefs from the already-captured sample (no new ingest).

    Runs the narrative synthesis plus the per-platform "AbbVie vs other brands" gists (two
    LLM calls), so it returns synchronously; the UI then reloads /social/insights to pick up
    the refreshed narrative, verbatims, and per-platform comparison.
    """
    from app.social import narrative
    brief = await narrative.generate_social_brief(db, therapeutic_area=therapeutic_area)
    platforms = await narrative.generate_platform_summaries(db, therapeutic_area=therapeutic_area)
    unmet = await narrative.generate_unmet_questions(db, therapeutic_area=therapeutic_area)
    return {"brief": brief, "platforms": platforms, "unmet_questions": unmet}


@router.post("/unmet-questions/promote", status_code=201)
async def promote_unmet_question(data: SocialUnmetPromote, db: AsyncSession = Depends(get_db)):
    """Stage a community unmet-need question into Discovery for review (voice-of-patient bridge).

    Respects the double-gate governance: the question is NOT added to the approved Question
    Repository directly — it becomes a Discovery staging row (CLASSIFIED, or QUARANTINED_AE
    for adverse-event content) that a reviewer promotes to a PENDING Question. Returns the
    staged item; re-sending an identical question returns the existing row.
    """
    return await svc.promote_unmet_question(db, data)
