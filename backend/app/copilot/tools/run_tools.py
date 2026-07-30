"""Run + schedule action tools (confirmed writes)."""
from __future__ import annotations

import asyncio

from app.copilot.tools.schemas import ToolInput, ToolResultData, ToolSpec
from app.models.database import AsyncSessionLocal


class StartRunInput(ToolInput):
    persona: str | None = None
    therapeutic_area: str | None = None
    domain: str | None = None
    question_ids: list[str] | None = None
    dry_run: bool = False


async def start_run(payload: StartRunInput) -> ToolResultData:
    from app.schemas import RunCreate
    from app.services import run_service

    data = RunCreate(
        trigger="ADHOC",
        persona=payload.persona,
        therapeutic_area=payload.therapeutic_area,
        domain=payload.domain,
        question_ids=payload.question_ids,
        dry_run=payload.dry_run,
    )
    async with AsyncSessionLocal() as db:
        run = await run_service.create_run(db, data)
    run_id = run.run_id
    # Fire-and-forget background execution (same as the REST endpoint).
    asyncio.create_task(run_service.run_in_background(run_id, data))
    kind = "dry run" if payload.dry_run else "run"
    return ToolResultData(
        tool_name="start_run",
        ok=True,
        summary=f"Started a {kind} (id {run_id}).",
        data={"run_id": run_id, "dry_run": payload.dry_run},
        nav_target="/run-analysis",
        job={"kind": "run", "run_id": run_id},
    )


class CancelRunInput(ToolInput):
    run_id: str


async def cancel_run(payload: CancelRunInput) -> ToolResultData:
    from app.agent.cancellation import request_cancel
    from app.services import run_service

    async with AsyncSessionLocal() as db:
        run = await run_service.get_run(db, payload.run_id)
        if run is None:
            return ToolResultData(tool_name="cancel_run", ok=False, summary="Run not found.", error=f"No run {payload.run_id}")
        if run.status != "RUNNING":
            return ToolResultData(
                tool_name="cancel_run",
                ok=False,
                summary=f"Run is not running (status={run.status}).",
                error="not_running",
            )
    request_cancel(payload.run_id)
    return ToolResultData(
        tool_name="cancel_run",
        ok=True,
        summary=f"Requested cancellation of run {payload.run_id}.",
        data={"run_id": payload.run_id},
        nav_target="/run-analysis",
    )


class SetScheduleInput(ToolInput):
    enabled: bool | None = None
    cron: str | None = None
    timezone: str | None = None


async def set_schedule(payload: SetScheduleInput) -> ToolResultData:
    from app.schemas import ScheduleOut, ScheduleUpdate
    from app.services import schedule_service

    async with AsyncSessionLocal() as db:
        try:
            row = await schedule_service.update_schedule(
                db,
                ScheduleUpdate(enabled=payload.enabled, cron=payload.cron, timezone=payload.timezone),
            )
        except Exception as exc:  # noqa: BLE001 — invalid cron/tz -> HTTPException
            return ToolResultData(tool_name="set_schedule", ok=False, summary="Could not update schedule.", error=str(getattr(exc, "detail", exc)))
        data = ScheduleOut.model_validate(row).model_dump(mode="json")
    state = "enabled" if data.get("enabled") else "disabled"
    return ToolResultData(
        tool_name="set_schedule",
        ok=True,
        summary=f"Daily run {state} (cron {data.get('cron')}, {data.get('timezone')}).",
        data=data,
        nav_target="/run-analysis",
    )


SPECS: list[ToolSpec] = [
    ToolSpec(
        "start_run",
        "Start a monitoring run across the approved questions and all AI models. "
        "Optionally filter by persona, therapeutic_area, domain, or specific "
        "question_ids; set dry_run=true to preview scope without calling models.",
        StartRunInput,
        start_run,
        mutating=True,
        nav_target="/run-analysis",
    ),
    ToolSpec(
        "cancel_run",
        "Request cancellation of an in-flight (RUNNING) monitoring run.",
        CancelRunInput,
        cancel_run,
        mutating=True,
        nav_target="/run-analysis",
    ),
    ToolSpec(
        "set_schedule",
        "Enable/disable or reconfigure the daily scheduled run (cron + timezone).",
        SetScheduleInput,
        set_schedule,
        mutating=True,
        nav_target="/run-analysis",
    ),
]
