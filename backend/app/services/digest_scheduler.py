"""Weekly stakeholder-digest jobs (BR-008a.1/4).

Registers one APScheduler cron job per enabled DigestProfile on the SAME in-process
scheduler that drives the daily run (single-worker deploy — see services/scheduler.py).
Cadence/routing is admin-configured in the DB, so cron changes take effect on the next
sync with NO code deploy. Safe no-op when digests or the scheduler are disabled."""
import asyncio
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.config.settings import get_settings
from app.models.database import AsyncSessionLocal
from app.models.digest import DigestProfile
from app.utils.logging import get_logger

logger = get_logger("digest_scheduler")

_JOB_PREFIX = "digest_profile_"
_background_tasks: set[asyncio.Task] = set()


def _job_id(profile_id: int) -> str:
    return f"{_JOB_PREFIX}{profile_id}"


def next_run_for(profile_id: int):
    """Next fire time (UTC, tz-aware) of a profile's weekly job, or None if not scheduled."""
    from datetime import timezone

    from app.services import scheduler as sched_mod

    sched = sched_mod._scheduler
    if sched is None:
        return None
    job = sched.get_job(_job_id(profile_id))
    if job is None or job.next_run_time is None:
        return None
    return job.next_run_time.astimezone(timezone.utc)


def _run_profile(profile_id: int) -> None:
    """APScheduler callback — launch generation as a tracked background task."""
    from app.services import digest_service

    async def _go() -> None:
        try:
            await digest_service.generate_for_profile_id(profile_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("Scheduled digest for profile %s failed: %s", profile_id, e)

    task = asyncio.create_task(_go())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def sync_digest_jobs() -> None:
    """(Re)register weekly jobs to match the current enabled profiles."""
    settings = get_settings()
    from app.services import scheduler as sched_mod

    sched = sched_mod._scheduler
    if sched is None:
        return  # scheduler not started (e.g. under tests)

    # Remove all existing digest jobs, then re-add for enabled profiles.
    for job in list(sched.get_jobs()):
        if job.id.startswith(_JOB_PREFIX):
            sched.remove_job(job.id)

    if not (settings.digest_enabled and settings.digest_scheduler_enabled):
        logger.info("Digest scheduler disabled — no digest jobs registered")
        return

    async with AsyncSessionLocal() as db:
        profiles = list((await db.execute(
            select(DigestProfile).where(DigestProfile.enabled.is_(True))
        )).scalars().all())

    registered = 0
    for p in profiles:
        try:
            trigger = CronTrigger.from_crontab(p.cron, timezone=ZoneInfo(p.timezone))
        except Exception as e:  # noqa: BLE001 — bad cron/tz: skip that profile, keep others
            logger.warning("Digest profile %s has invalid cron/tz (%r/%r): %s", p.id, p.cron, p.timezone, e)
            continue
        sched.add_job(
            _run_profile, trigger=trigger, id=_job_id(p.id), name=f"Digest: {p.role}",
            args=[p.id], replace_existing=True, max_instances=1, coalesce=True,
            misfire_grace_time=3600,
        )
        registered += 1
    logger.info("Registered %d weekly digest job(s)", registered)
