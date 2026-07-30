"""AI Prompt Volume Intelligence API (FR-116).

Manual CSV ingestion of third-party SEARCH-DEMAND exports (Semrush/Ahrefs), used as a PROXY
for AI-inquiry demand. Uploads are PII-linted over the whole file and REJECTED ENTIRELY on
any hit (FR-116.5); source/label/date metadata is required. Serves the intelligence
dashboard (volume by TA/competitor), grouped high-volume gap topics, upload history, and a
CSV export.
"""
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.prompt_volume.linter import PiiRejection
from app.prompt_volume.parser import CsvValidationError
from app.prompt_volume.semrush_source import NotConfigured
from app.schemas import SemrushFetchIngestRequest, SemrushFetchPreviewRequest
from app.services import prompt_volume_service as svc

router = APIRouter(prefix="/prompt-volume", tags=["prompt-volume"])


@router.post("/upload", status_code=201)
async def upload(
    file: UploadFile = File(...),
    source_tool: str = Form(..., min_length=1),
    source_label: str = Form(..., min_length=1),
    dataset_date: str = Form(..., min_length=1),
    synthesize_questions: bool = Form(True),
    db: AsyncSession = Depends(get_db),
):
    """Ingest a keyword/volume CSV (FR-116.1/.5).

    ``source_tool``/``source_label``/``dataset_date`` are required (FastAPI returns 422 if
    missing). ``synthesize_questions`` (default true) controls whether bare-keyword gaps get an
    auto-generated question. Returns 400 for malformed CSVs and 422 (with offending rows) if
    PII is found — in which case NOTHING is persisted.
    """
    content = await file.read()
    try:
        return await svc.ingest(
            db,
            content=content,
            source_tool=source_tool.strip(),
            source_label=source_label.strip(),
            dataset_date=dataset_date.strip(),
            filename=file.filename,
            synthesize=synthesize_questions,
        )
    except PiiRejection as e:
        raise HTTPException(status_code=422, detail={"message": str(e), "pii_hits": e.hits})
    except CsvValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/semrush/status")
async def semrush_status():
    """Whether the in-app SEMrush fetch is available + its cost-guard defaults (FR-116)."""
    return svc.semrush_status()


@router.post("/semrush/preview")
async def semrush_preview(req: SemrushFetchPreviewRequest, db: AsyncSession = Depends(get_db)):
    """Pull questions + related keywords for a scope and preview them (BILLED SEMrush calls).

    Returns a fetch_id (cached ~30 min) plus a mapping/gap/novelty summary. Nothing is
    persisted until /semrush/ingest. 400 when no SEMrush key is configured.
    """
    try:
        return await svc.semrush_preview(
            db,
            therapeutic_area=req.therapeutic_area.strip(),
            brand=(req.brand or "").strip() or None,
            include_generics=req.include_generics,
            include_indications=req.include_indications,
            include_competitors=req.include_competitors,
            per_seed_limit=req.per_seed_limit,
            reports=req.reports,
        )
    except NotConfigured as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/semrush/ingest", status_code=201)
async def semrush_ingest(req: SemrushFetchIngestRequest, db: AsyncSession = Depends(get_db)):
    """Persist a previewed fetch as a full-snapshot dataset (no re-fetch/re-bill)."""
    try:
        result = await svc.semrush_ingest(
            db,
            fetch_id=req.fetch_id,
            source_label=req.source_label.strip(),
            dataset_date=req.dataset_date.strip(),
            synthesize=req.synthesize,
            only_new=req.only_new,
            limit=req.limit,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result is None:
        raise HTTPException(status_code=404, detail="Fetch expired or not found. Re-run the preview.")
    return result


@router.get("/batches")
async def batches(db: AsyncSession = Depends(get_db)):
    """Upload history, newest first (FR-116.5 auditability)."""
    return await svc.list_batches(db)


@router.get("/intelligence")
async def intelligence(batch_id: str | None = Query(None), db: AsyncSession = Depends(get_db)):
    """Relative volume by therapeutic area + competitor for a batch (FR-116.2)."""
    return await svc.intelligence(db, batch_id=batch_id)


@router.get("/gaps")
async def gaps(batch_id: str | None = Query(None), db: AsyncSession = Depends(get_db)):
    """High-volume topics missing from the Approved Question Bank, with opportunity score (FR-116.3)."""
    return await svc.gap_topics(db, batch_id=batch_id)


@router.get("/gap-alerts")
async def gap_alerts(status: str = Query("OPEN"), db: AsyncSession = Depends(get_db)):
    """Trackable coverage-gap alerts (FR-116.3). status = OPEN | RESOLVED | DISMISSED | ALL."""
    return await svc.list_gap_alerts(db, status=status)


@router.get("/gap-alerts/summary")
async def gap_alerts_summary(db: AsyncSession = Depends(get_db)):
    """Open / resolved / dismissed gap-alert counts (for the dashboard badge)."""
    return await svc.gap_alert_summary(db)


@router.post("/gap-alerts/sync")
async def gap_alerts_sync(db: AsyncSession = Depends(get_db)):
    """Reconcile gap alerts against the latest upload (populate without re-uploading)."""
    return await svc.sync_gap_alerts_latest(db)


@router.post("/gap-alerts/{alert_id}/dismiss")
async def gap_alert_dismiss(alert_id: str, db: AsyncSession = Depends(get_db)):
    """Mute a gap alert so it stays quiet even if the topic recurs."""
    result = await svc.dismiss_gap_alert(db, alert_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Gap alert not found")
    return result


@router.get("/trend")
async def trend(db: AsyncSession = Depends(get_db)):
    """Demand over time across all uploads + rising topics in the latest one (FR-116)."""
    return await svc.demand_trend(db)


@router.get("/export.csv")
async def export_csv(batch_id: str | None = Query(None), db: AsyncSession = Depends(get_db)):
    """CSV export of the enriched staged rows (formula-injection safe)."""
    rows = await svc.export_rows(db, batch_id=batch_id)
    return PlainTextResponse(
        svc.to_csv(rows),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=prompt_volume.csv"},
    )
