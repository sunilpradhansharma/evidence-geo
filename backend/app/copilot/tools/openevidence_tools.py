"""OpenEvidence (Clinician Input) action tools."""
from __future__ import annotations

import asyncio

from app.copilot.tools.schemas import ToolInput, ToolResultData, ToolSpec
from app.models.database import AsyncSessionLocal


class OeCaptureInput(ToolInput):
    run_id: str
    question_id: str
    answer_text: str
    sources: list[dict] | None = None


async def oe_capture(payload: OeCaptureInput) -> ToolResultData:
    from app.schemas import OpenEvidenceCapture, OpenEvidenceSource
    from app.services import openevidence_service as oe

    sources = [OpenEvidenceSource(**s) for s in (payload.sources or []) if s.get("url")]
    data = OpenEvidenceCapture(
        run_id=payload.run_id,
        question_id=payload.question_id,
        answer_text=payload.answer_text,
        sources=sources,
    )
    async with AsyncSessionLocal() as db:
        try:
            result = await oe.capture(db, data)
        except Exception as exc:  # noqa: BLE001
            return ToolResultData(tool_name="oe_capture", ok=False, summary="Could not capture the answer.", error=str(getattr(exc, "detail", exc)))
    rid = result.get("response_id")
    if rid:
        asyncio.create_task(oe.finalize_capture(rid))
    return ToolResultData(
        tool_name="oe_capture",
        ok=True,
        summary=f"Captured the OpenEvidence answer for question {payload.question_id}.",
        data=result,
        nav_target="/run-analysis",
    )


class OeFinalizeInput(ToolInput):
    run_id: str


async def oe_finalize_run(payload: OeFinalizeInput) -> ToolResultData:
    from app.services import openevidence_service as oe

    async with AsyncSessionLocal() as db:
        try:
            result = await oe.finalize_without_oe(db, payload.run_id)
        except Exception as exc:  # noqa: BLE001
            return ToolResultData(tool_name="oe_finalize_run", ok=False, summary="Could not finalize the run.", error=str(getattr(exc, "detail", exc)))
    return ToolResultData(
        tool_name="oe_finalize_run",
        ok=True,
        summary=f"Finalized run {payload.run_id} without OpenEvidence.",
        data=result if isinstance(result, dict) else {"result": result},
        nav_target="/run-analysis",
    )


class OeAutoInput(ToolInput):
    action: str  # capture | sweep | login
    run_id: str | None = None


async def oe_auto(payload: OeAutoInput) -> ToolResultData:
    from app.config.settings import get_settings
    from app.openevidence_auto import worker

    if not get_settings().oe_auto_enabled:
        return ToolResultData(tool_name="oe_auto", ok=False, summary="OpenEvidence auto-capture is disabled.", error="OE_AUTO_ENABLED is false")
    action = (payload.action or "").strip().lower()
    if action == "capture":
        if not payload.run_id:
            return ToolResultData(tool_name="oe_auto", ok=False, summary="capture needs a run_id.", error="run_id required")
        worker.schedule_auto_capture(payload.run_id)
        return ToolResultData(tool_name="oe_auto", ok=True, summary=f"Scheduled unattended capture for run {payload.run_id}.", data={"run_id": payload.run_id}, nav_target="/run-analysis")
    if action == "sweep":
        worker.schedule_sweep()
        return ToolResultData(tool_name="oe_auto", ok=True, summary="Scheduled an unattended capture sweep across pending runs.", data={}, nav_target="/run-analysis")
    if action == "login":
        result = await worker.run_test_login()
        return ToolResultData(tool_name="oe_auto", ok=True, summary="Ran the OpenEvidence login check.", data=result if isinstance(result, dict) else {"result": result}, nav_target="/run-analysis")
    return ToolResultData(tool_name="oe_auto", ok=False, summary=f"Unknown action {action!r}.", error="action must be capture, sweep, or login")


SPECS: list[ToolSpec] = [
    ToolSpec("oe_capture", "Ingest a clinician's pasted OpenEvidence answer for a Provider question (run_id + question_id + answer_text, optional sources). It is scored and folded into model consensus.", OeCaptureInput, oe_capture, mutating=True, nav_target="/run-analysis"),
    ToolSpec("oe_finalize_run", "Close an AWAITING_OPENEVIDENCE run without OpenEvidence, computing Provider consensus from the automated models only.", OeFinalizeInput, oe_finalize_run, mutating=True, nav_target="/run-analysis"),
    ToolSpec("oe_auto", "Drive the unattended OpenEvidence browser bot. action=capture (needs run_id) | sweep | login. Only works when OE auto-capture is enabled.", OeAutoInput, oe_auto, mutating=True, nav_target="/run-analysis"),
]
