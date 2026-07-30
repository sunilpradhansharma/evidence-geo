"""Schedule persistence + orchestration (FR-501, FR-502).

Owns the singleton Schedule row and keeps the live APScheduler job in sync with
it. The API layer calls get_or_create / update_schedule; the app lifespan calls
sync_on_startup to re-apply the persisted state after a (re)start.
"""
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.models.database import AsyncSessionLocal
from app.models.schedule import Schedule
from app.schemas import ScheduleUpdate
from app.services import scheduler as sched
from app.utils.logging import get_logger

logger = get_logger("schedule_service")
settings = get_settings()


async def get_or_create(db: AsyncSession) -> Schedule:
    """Return the singleton schedule row, seeding it from settings on first call."""
    row = await db.get(Schedule, 1)
    if row is None:
        row = Schedule(
            id=1,
            enabled=False,
            cron=settings.default_schedule_cron,
            timezone=settings.schedule_timezone,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


def _validate(cron: str, tz_name: str) -> None:
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise HTTPException(400, f"Invalid timezone: {tz_name!r}") from e
    try:
        CronTrigger.from_crontab(cron, timezone=tz)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, f"Invalid cron expression: {cron!r}") from e


async def update_schedule(db: AsyncSession, data: ScheduleUpdate) -> Schedule:
    """Apply a partial update, validate it, then (re)configure the live job."""
    row = await get_or_create(db)
    if data.cron is not None:
        row.cron = data.cron
    if data.timezone is not None:
        row.timezone = data.timezone
    if data.enabled is not None:
        row.enabled = data.enabled

    _validate(row.cron, row.timezone)
    row.next_run_at = sched.apply_schedule(
        enabled=row.enabled, cron=row.cron, tz_name=row.timezone
    )
    await db.commit()
    await db.refresh(row)
    logger.info(
        "Schedule updated: enabled=%s cron=%r tz=%s next_run=%s",
        row.enabled, row.cron, row.timezone, row.next_run_at,
    )
    return row


async def sync_on_startup() -> None:
    """Re-apply the persisted schedule to the freshly started scheduler."""
    async with AsyncSessionLocal() as db:
        row = await get_or_create(db)
        try:
            row.next_run_at = sched.apply_schedule(
                enabled=row.enabled, cron=row.cron, tz_name=row.timezone
            )
            await db.commit()
            logger.info(
                "Schedule restored on startup: enabled=%s cron=%r tz=%s next_run=%s",
                row.enabled, row.cron, row.timezone, row.next_run_at,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Failed to apply persisted schedule: %s", e)
