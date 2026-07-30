"""LLM-synthesized "what each AI platform is saying in general" for the Workshop Questions
insights (BR-008a).

One short narrative per AI platform, grounded STRICTLY on that platform's already-scored
answer synopses (never fabricated), cached in ``workshop_platform_summaries`` and refreshed in
the background when the underlying answers change (signature mismatch). Mirrors
``app/social/narrative.py``: reuses the configured scoring model via ``insights.llm.chat_json``
and is best-effort — any failure leaves the previous cached summaries in place.
"""
import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights.llm import chat_json
from app.models.database import AsyncSessionLocal
from app.models.workshop_summary import WorkshopPlatformSummary
from app.providers.registry import get_scoring_config
from app.services import digest_service as ds
from app.utils.logging import get_logger

logger = get_logger("digests.workshop_narrative")

_MAX_ANSWERS_PER_PLATFORM = 12
_SNIPPET = 220
_SUMMARY_MAXLEN = 480
_refresh_locks: dict[str, asyncio.Lock] = {}


_SYSTEM = (
    "You are a pharmaceutical AI-monitoring analyst for a brand and marketing team. Below are "
    "one or more AI PLATFORMS (chatbots). Each platform shows its name on a 'Platform:' line, "
    "then that platform's most recent answers to a set of monitored questions about "
    "AbbVie's brands. Each answer line shows the audience and indication, the competitive "
    "positioning our scorer assigned (for example FIRST_LINE_RECOMMENDED, AMONG_OPTIONS, "
    "SECOND_LINE, NOT_RECOMMENDED), and a factual synopsis of what that platform said.\n\n"
    "For EACH platform, write a GENERAL summary (2 to 3 sentences, about 55 words max) of how "
    "that platform currently positions AbbVie's brands across these questions: the overall "
    "stance (favorable versus cautious or negative), the recurring themes it emphasizes (for "
    "example safety or boxed-warning framing, efficacy, access, or a competitor preference), "
    "and any notable pattern by audience or indication. Name only the brands and competitors "
    "that appear in that platform's own answer lines. Do NOT invent facts, numbers, or quotes. "
    "Do NOT use em dashes; use periods, commas, or parentheses instead.\n\n"
    "Return STRICT JSON only (no prose outside the JSON): {\"platforms\": {\"<platform>\": "
    "\"summary text\"}} using the EXACT platform name from each 'Platform:' line as the key."
)


def _clip(text: str | None, n: int) -> str:
    t = " ".join((text or "").split())
    return t[:n].rstrip() + ("\u2026" if len(t) > n else "")


async def refresh_workshop_platform_summaries(
    db: AsyncSession, *, scope: str = ds.SCOPE_WORKSHOP, force: bool = False
) -> dict:
    """Regenerate a scope's cached per-platform summaries whose inputs changed
    (best-effort, never raises)."""
    try:
        context = await ds.gather_workshop_platform_context(db, scope=scope)
    except Exception as e:  # noqa: BLE001
        logger.warning("workshop summary context gather failed: %s", e)
        return {"status": "error", "reason": str(e)}
    if not context:
        return {"status": "empty"}

    existing = {
        r.llm_name: r
        for r in (await db.execute(
            select(WorkshopPlatformSummary).where(WorkshopPlatformSummary.scope == scope)
        )).scalars().all()
    }
    stale = {
        llm: ctx
        for llm, ctx in context.items()
        if force
        or existing.get(llm) is None
        or existing[llm].input_signature != ctx["signature"]
        or not (existing[llm].summary or "").strip()
    }
    if not stale:
        return {"status": "fresh", "platforms": 0}

    sections: list[str] = []
    for llm, ctx in stale.items():
        lines = []
        for a in ctx["answers"][:_MAX_ANSWERS_PER_PLATFORM]:
            syn = _clip(a.get("summary") or "", _SNIPPET)
            if not syn:
                continue
            desig = a.get("designation") or "General"
            pos = a.get("competitive_position") or "n/a"
            lines.append(f"  - [{desig}, {pos}] {syn}")
        if lines:
            sections.append(f"Platform: {llm}\n" + "\n".join(lines))
    if not sections:
        return {"status": "empty"}

    user = (
        "AI platforms and their recent workshop answers:\n\n"
        + "\n\n".join(sections)
        + "\n\nReturn STRICT JSON: {\"platforms\": {\"<platform>\": \"2-3 sentence summary\"}}."
    )

    try:
        data = await chat_json(_SYSTEM, user, max_tokens=1200)
    except Exception as e:  # noqa: BLE001 — leave any previous summaries in place
        logger.warning("workshop platform summaries generation failed: %s", e)
        return {"status": "error", "reason": str(e)}
    if not isinstance(data, dict) or not isinstance(data.get("platforms"), dict):
        return {"status": "error", "reason": "model returned no platforms object"}

    try:
        model_id = get_scoring_config().model_id
    except Exception:  # noqa: BLE001 — provenance only
        model_id = None

    written = 0
    platforms = data["platforms"]
    for llm, ctx in stale.items():
        text_val = platforms.get(llm)
        if not (isinstance(text_val, str) and text_val.strip()):
            continue
        row = existing.get(llm)
        if row is None:
            row = WorkshopPlatformSummary(scope=scope, llm_name=llm)
            db.add(row)
        row.summary = text_val.strip()[:_SUMMARY_MAXLEN]
        row.input_signature = ctx["signature"]
        row.responses_analyzed = len(ctx["answers"])
        row.model = model_id
        row.updated_at = datetime.now(timezone.utc)
        written += 1
    await db.commit()
    logger.info("workshop platform summaries generated: %d/%d stale", written, len(stale))
    return {"status": "ok", "platforms": written}


async def refresh_now(*, scope: str = ds.SCOPE_WORKSHOP, force: bool = False) -> dict:
    """Synchronous refresh on an isolated session (used before rendering a digest)."""
    async with AsyncSessionLocal() as db:
        return await refresh_workshop_platform_summaries(db, scope=scope, force=force)


def trigger_refresh_in_background(scope: str = ds.SCOPE_WORKSHOP) -> None:
    """Fire-and-forget refresh on its own DB session, guarded so only one per scope runs."""

    async def _run() -> None:
        lock = _refresh_locks.setdefault(scope, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            try:
                async with AsyncSessionLocal() as db:
                    await refresh_workshop_platform_summaries(db, scope=scope)
            except Exception as e:  # noqa: BLE001 — background best-effort
                logger.warning("workshop summary background refresh skipped: %s", e)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # no running loop (e.g. called outside async context) — skip
    asyncio.create_task(_run())
