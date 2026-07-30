"""Schedule API (FR-501, FR-502, FR-506) — toggle/configure the daily run."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.schemas import ScheduleOut, ScheduleUpdate
from app.services import schedule_service as svc

router = APIRouter(prefix="/schedule", tags=["schedule"])


@router.get("", response_model=ScheduleOut)
async def get_schedule(db: AsyncSession = Depends(get_db)):
    """Current daily-run schedule (enabled flag, cron, timezone, next run)."""
    return await svc.get_or_create(db)


@router.put("", response_model=ScheduleOut)
async def update_schedule(data: ScheduleUpdate, db: AsyncSession = Depends(get_db)):
    """Enable/disable or reconfigure the daily run. Reschedules the live job."""
    return await svc.update_schedule(db, data)
