"""Response Repository query API (FR-303, FR-305, FR-306, FR-605, FR-606)."""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.services import export_service, response_service as svc

router = APIRouter(prefix="/responses", tags=["responses"])


@router.get("")
async def list_responses(
    llm_name: str | None = None,
    persona: str | None = None,
    # Repeatable query params (?therapeutic_area=A&therapeutic_area=B) enable
    # multi-select filtering; a single value stays backward compatible.
    therapeutic_area: list[str] | None = Query(None),
    brand_focus: str | None = None,
    competitor: str | None = Query(
        None,
        description="Answers whose scored mentions NAME this agent (alias-aware). Distinct "
                    "from brand_focus, which is the monitored AbbVie brand.",
    ),
    domain: str | None = None,
    status: str | None = None,
    run_id: str | None = None,
    intent_type: str | None = None,
    consensus_level: str | None = None,
    sentiment_min: float | None = None,
    sentiment_max: float | None = None,
    alert_only: bool = False,
    analyst: bool = False,
    designation: list[str] | None = Query(None),
    limit: int = Query(100, le=1000),
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    tas = [t for t in (therapeutic_area or []) if t]
    desigs = [d for d in (designation or []) if d]
    return await svc.query_responses(
        db, llm_name=llm_name, persona=persona, therapeutic_areas=tas or None,
        brand_focus=brand_focus, competitor=competitor,
        domain=domain, status=status, run_id=run_id,
        intent_type=intent_type, consensus_level=consensus_level,
        sentiment_min=sentiment_min, sentiment_max=sentiment_max,
        alert_only=alert_only, analyst=analyst, designations=desigs or None,
        limit=limit, offset=offset,
    )


@router.get("/compare")
async def compare(question_id: str, run_id: str | None = None, db: AsyncSession = Depends(get_db)):
    return await svc.compare_question(db, question_id, run_id)


@router.get("/export")
async def export(
    format: str = Query("csv", pattern="^(csv|json)$"),
    llm_name: str | None = None,
    persona: str | None = None,
    # Multi-select: filter the download to one or more therapeutic areas / designations
    # so only the selected slice is exported (repeatable query params).
    therapeutic_area: list[str] | None = Query(None),
    domain: str | None = None,
    run_id: str | None = None,
    alert_only: bool = False,
    analyst: bool = False,
    designation: list[str] | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    tas = [t for t in (therapeutic_area or []) if t]
    desigs = [d for d in (designation or []) if d]
    result = await svc.query_responses(
        db, llm_name=llm_name, persona=persona, therapeutic_areas=tas or None,
        domain=domain, run_id=run_id, alert_only=alert_only, analyst=analyst,
        designations=desigs or None, limit=10000,
    )
    items = result["items"]
    if format == "csv":
        # The Designation column (Persona + indication) is added right after persona
        # whenever the export is workshop-scoped: either the Workshop Questions toggle
        # (analyst) or an explicit designation filter. query_responses tags each item.
        return PlainTextResponse(
            export_service.to_csv(items, include_designation=analyst or bool(desigs)),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=responses.csv"},
        )
    return PlainTextResponse(
        export_service.to_json(items),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=responses.json"},
    )


@router.get("/{response_id}")
async def get_response(response_id: str, db: AsyncSession = Depends(get_db)):
    detail = await svc.get_response_detail(db, response_id)
    if detail is None:
        raise HTTPException(404, "Response not found")
    return detail
