"""Pinpoint corpus export API (Advanced Analytics / Google Pinpoint bridge).

`POST /exports/pinpoint` turns a filtered slice of the Response Repository into an ingest-ready
document corpus (+ manifest + metadata + zip) you bulk-drop into a Pinpoint collection.
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights import trends
from app.models.database import get_db
from app.models.theme import ResponseTheme, Theme
from app.schemas import PinpointExportRequest
from app.services import pinpoint_export
from app.services import response_service as svc

router = APIRouter(prefix="/exports", tags=["exports"])


async def _themes_map(db: AsyncSession, response_ids: list[str]) -> dict[str, list[str]]:
    if not response_ids:
        return {}
    version = await trends.current_version(db)
    if not version:
        return {}
    stmt = (
        select(ResponseTheme.response_id, Theme.label)
        .join(Theme, Theme.theme_id == ResponseTheme.theme_id)
        .where(
            ResponseTheme.taxonomy_version == version,
            ResponseTheme.response_id.in_(response_ids),
        )
    )
    out: dict[str, list[str]] = {}
    for rid, label in (await db.execute(stmt)).all():
        out.setdefault(rid, []).append(label)
    return out


@router.post("/pinpoint")
async def export_pinpoint(req: PinpointExportRequest, db: AsyncSession = Depends(get_db)):
    result = await svc.query_responses(
        db,
        llm_name=req.llm_name,
        persona=req.persona,
        therapeutic_area=req.therapeutic_area,
        brand_focus=req.brand_focus,
        domain=req.domain,
        status=req.status,
        run_id=req.run_id,
        alert_only=req.alert_only,
        sentiment_min=req.sentiment_min,
        sentiment_max=req.sentiment_max,
        limit=req.limit,
    )
    items = result["items"]
    if not items:
        raise HTTPException(400, "No responses match the selected filters")

    themes_map = {}
    if req.include_themes:
        themes_map = await _themes_map(db, [i["response_id"] for i in items])

    filters = req.model_dump(exclude={"label", "include_themes", "limit"})
    summary = await asyncio.to_thread(
        pinpoint_export.build_export,
        items,
        themes_map=themes_map,
        label=req.label,
        filters=filters,
    )
    summary["download_url"] = f"/exports/pinpoint/{summary['export_id']}/download"
    return summary


@router.get("/pinpoint")
async def list_pinpoint_exports():
    return {
        "exports": pinpoint_export.list_exports(),
        "base_dir": str(pinpoint_export.export_base_dir()),
    }


@router.get("/pinpoint/{export_id}/download")
async def download_pinpoint_export(export_id: str):
    path = pinpoint_export.zip_path_for(export_id)
    if path is None:
        raise HTTPException(404, "Export not found")
    return FileResponse(
        str(path),
        media_type="application/zip",
        filename=f"pinpoint_{export_id}.zip",
    )
