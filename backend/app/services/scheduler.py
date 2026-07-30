"""In-process APScheduler that drives the unattended daily run (FR-501, FR-502).

The deploy is a single container with a single uvicorn worker
(deploy/supervisord.conf), so one in-process AsyncIOScheduler is safe — there is
no second worker that would double-fire the job. The schedule's on/off state and
cron live in the DB (app.models.schedule.Schedule), so this module only owns the
live APScheduler instance and the job wiring; persistence is in schedule_service.

NOTE: if uvicorn is ever run with --workers > 1, each worker would start its own
scheduler and the daily run would fire N times. Keep it single-worker, or move
to an external trigger, if that changes.
"""
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.utils.logging import get_logger

logger = get_logger("scheduler")

JOB_ID = "daily_full_bank_run"

_scheduler: AsyncIOScheduler | None = None
# Hold strong refs to in-flight run tasks so the event loop can't GC (cancel) them.
_background_tasks: set[asyncio.Task] = set()


def start_scheduler() -> AsyncIOScheduler:
    """Create and start the singleton scheduler. Must run inside the event loop."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = AsyncIOScheduler(timezone=timezone.utc)
    _scheduler.start()
    logger.info("APScheduler started")
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("APScheduler shut down")


def _build_trigger(cron: str, tz_name: str) -> CronTrigger:
    """Build a DST-aware cron trigger. Raises ValueError on a bad expression."""
    return CronTrigger.from_crontab(cron, timezone=ZoneInfo(tz_name))


def apply_schedule(*, enabled: bool, cron: str, tz_name: str) -> datetime | None:
    """(Re)configure the daily job from the given settings.

    Removes any existing job, then adds a fresh one if enabled. Returns the next
    fire time (UTC) or None when disabled.
    """
    sched = _scheduler
    if sched is None:
        raise RuntimeError("Scheduler not started")

    if sched.get_job(JOB_ID) is not None:
        sched.remove_job(JOB_ID)

    if enabled:
        sched.add_job(
            _run_scheduled_job,
            trigger=_build_trigger(cron, tz_name),
            id=JOB_ID,
            name="Daily full question-bank run",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,  # still fire if the box was briefly down
        )

    return get_next_run_time()


def get_next_run_time() -> datetime | None:
    """Next fire time of the daily job in UTC, or None if not scheduled."""
    if _scheduler is None:
        return None
    job = _scheduler.get_job(JOB_ID)
    if job is None or job.next_run_time is None:
        return None
    return job.next_run_time.astimezone(timezone.utc)


async def _run_scheduled_job() -> None:
    """Fire a full-bank SCHEDULED run, unless disabled or one is already running."""
    from sqlalchemy import select

    from app.models.database import AsyncSessionLocal
    from app.models.run import Run
    from app.models.schedule import Schedule, utcnow
    from app.schemas import RunCreate
    from app.services import run_service

    async with AsyncSessionLocal() as db:
        row = await db.get(Schedule, 1)
        if row is None or not row.enabled:
            logger.info("Scheduled run skipped: schedule is disabled")
            return

        running = await db.execute(select(Run).where(Run.status == "RUNNING").limit(1))
        if running.scalar_one_or_none() is not None:
            logger.warning("Scheduled run skipped: another run is already RUNNING")
            return

        run = await run_service.create_run(db, RunCreate(trigger="SCHEDULED"))
        run_id = run.run_id
        row.last_run_id = run_id
        row.last_run_at = utcnow()
        row.next_run_at = get_next_run_time()
        await db.commit()

    logger.info("Scheduled run %s started (full question bank)", run_id)
    task = asyncio.create_task(
        run_service.run_in_background(run_id, RunCreate(trigger="SCHEDULED"))
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
