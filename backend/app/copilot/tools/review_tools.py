"""Response-review action tools: score override, rescore, sweep, export."""
from __future__ import annotations

import asyncio
from urllib.parse import urlencode

from app.copilot.tools.schemas import ToolInput, ToolResultData, ToolSpec
from app.models.database import AsyncSessionLocal


class OverrideScoreInput(ToolInput):
    response_id: str
    sentiment_score: float
    competitive_position: str
    rationale: str
    reviewer_name: str


async def override_score(payload: OverrideScoreInput) -> ToolResultData:
    from app.api import scores as scores_api
    from app.schemas import ScoreOverride

    data = ScoreOverride(
        sentiment_score=payload.sentiment_score,
        competitive_position=payload.competitive_position,
        rationale=payload.rationale,
        reviewer_name=payload.reviewer_name,
    )
    async with AsyncSessionLocal() as db:
        try:
            result = await scores_api.override_score(payload.response_id, data, db)
        except Exception as exc:  # noqa: BLE001 — HTTPException 404/422
            return ToolResultData(tool_name="override_score", ok=False, summary="Could not override score.", error=str(getattr(exc, "detail", exc)))
    return ToolResultData(
        tool_name="override_score",
        ok=True,
        summary=f"Recorded human score override v{result.get('score_version')} by {payload.reviewer_name}.",
        data=result,
        nav_target="/results",
    )


class RescoreInput(ToolInput):
    prompt_version: str = "v2"
    run_id: str | None = None


async def rescore(payload: RescoreInput) -> ToolResultData:
    from app.api import scores as scores_api

    asyncio.create_task(scores_api._rescore_task(payload.prompt_version, payload.run_id))
    scope = f"run {payload.run_id}" if payload.run_id else "all responses"
    return ToolResultData(
        tool_name="rescore",
        ok=True,
        summary=f"Started re-scoring {scope} with prompt {payload.prompt_version}.",
        data={"prompt_version": payload.prompt_version, "run_id": payload.run_id},
        nav_target="/results",
    )


class ScoreSweepInput(ToolInput):
    pass


async def score_sweep(payload: ScoreSweepInput) -> ToolResultData:
    from app.scoring.scorer import score_unscored_sweep

    async with AsyncSessionLocal() as db:
        result = await score_unscored_sweep(db)
    return ToolResultData(
        tool_name="score_sweep",
        ok=True,
        summary="Scored any unscored responses.",
        data=result if isinstance(result, dict) else {"result": result},
        nav_target="/results",
    )


class ExportDataInput(ToolInput):
    target: str = "responses"  # responses | pinpoint
    format: str = "csv"  # responses only: csv | json
    label: str = ""
    llm_name: str | None = None
    persona: str | None = None
    therapeutic_area: str | None = None
    brand_focus: str | None = None
    domain: str | None = None
    run_id: str | None = None
    alert_only: bool = False


async def export_data(payload: ExportDataInput) -> ToolResultData:
    target = (payload.target or "responses").strip().lower()
    if target == "responses":
        params = {
            "format": payload.format,
            "llm_name": payload.llm_name,
            "persona": payload.persona,
            "therapeutic_area": payload.therapeutic_area,
            "domain": payload.domain,
            "run_id": payload.run_id,
            "alert_only": str(payload.alert_only).lower(),
        }
        qs = urlencode({k: v for k, v in params.items() if v not in (None, "")})
        url = f"/api/responses/export?{qs}"
        return ToolResultData(
            tool_name="export_data",
            ok=True,
            summary=f"Your {payload.format.upper()} export is ready to download.",
            data={"download_url": url, "target": "responses"},
            nav_target="/results",
        )
    if target == "pinpoint":
        from app.api import exports as exports_api
        from app.schemas import PinpointExportRequest

        req = PinpointExportRequest(
            label=payload.label,
            llm_name=payload.llm_name,
            persona=payload.persona,
            therapeutic_area=payload.therapeutic_area,
            brand_focus=payload.brand_focus,
            domain=payload.domain,
            run_id=payload.run_id,
            alert_only=payload.alert_only,
        )
        async with AsyncSessionLocal() as db:
            try:
                summary = await exports_api.export_pinpoint(req, db)
            except Exception as exc:  # noqa: BLE001 — HTTPException (no matches)
                return ToolResultData(tool_name="export_data", ok=False, summary="Could not build the Pinpoint export.", error=str(getattr(exc, "detail", exc)))
        dl = summary.get("download_url")
        if dl and not str(dl).startswith("/api"):
            dl = f"/api{dl}"
            summary["download_url"] = dl
        return ToolResultData(
            tool_name="export_data",
            ok=True,
            summary="Built a Pinpoint corpus export.",
            data=summary,
            nav_target="/results",
        )
    return ToolResultData(tool_name="export_data", ok=False, summary=f"Unknown export target {target!r}.", error="target must be responses or pinpoint")


SPECS: list[ToolSpec] = [
    ToolSpec("override_score", "Record a human override of a response's AI score. Requires sentiment_score (-1..1), competitive_position, rationale, and reviewer_name.", OverrideScoreInput, override_score, mutating=True, governance=True, nav_target="/results"),
    ToolSpec("rescore", "Re-score historical responses as new versioned records with a given prompt_version (optionally scoped to one run_id).", RescoreInput, rescore, mutating=True, nav_target="/results"),
    ToolSpec("score_sweep", "Score any responses that are currently unscored.", ScoreSweepInput, score_sweep, mutating=True, nav_target="/results"),
    ToolSpec("export_data", "Export a filtered slice of responses. target=responses returns a CSV/JSON download link; target=pinpoint builds a Pinpoint corpus.", ExportDataInput, export_data, mutating=True, nav_target="/results"),
]
