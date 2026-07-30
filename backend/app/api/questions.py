"""Question Repository API (FR-101..107, FR-105 CSV import, SE-001/002)."""
import csv
import io

from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.schemas import PromptImportCommit, QuestionCreate, QuestionOut, QuestionUpdate, SoftDelete
from app.services import prompt_volume_service as pv_svc
from app.services import question_service as svc
from app.utils.pii_lint import scan_for_pii

router = APIRouter(prefix="/questions", tags=["questions"])


@router.get("", response_model=list[QuestionOut])
async def list_questions(
    persona: list[str] | None = Query(None),
    therapeutic_area: list[str] | None = Query(None),
    indication: list[str] | None = Query(None),
    brand_focus: list[str] | None = Query(None),
    domain: list[str] | None = Query(None),
    approval_status: str | None = None,
    active: bool | None = None,
    analyst: bool = False,
    limit: int = Query(500, le=10000),
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    rows = await svc.list_questions(
        db,
        persona=persona,
        therapeutic_area=therapeutic_area,
        indication=indication,
        brand_focus=brand_focus,
        domain=domain,
        approval_status=approval_status,
        active=active,
        analyst=analyst,
        limit=limit,
        offset=offset,
    )
    await svc.attach_variation_lineage(db, rows)
    await svc.attach_question_source(db, rows)
    await svc.attach_designation(db, rows)
    return rows


@router.get("/coverage-gaps")
async def coverage(db: AsyncSession = Depends(get_db)):
    return await svc.coverage_report(db)


@router.get("/brand-matrix")
async def brand_matrix():
    """Therapeutic area → indication → diseases → brands, from the brand taxonomy."""
    return svc.brand_matrix()


@router.get("/prioritized")
async def prioritized(batch_id: str | None = Query(None), db: AsyncSession = Depends(get_db)):
    """Approved bank ranked by demand = priority_weight × matched search volume (FR-116.4).

    Uses the latest Prompt Volume upload unless ``batch_id`` is pinned. Declared before the
    ``/{row_id}`` route so "prioritized" isn't parsed as a row id.
    """
    return await pv_svc.prioritized_questions(db, batch_id=batch_id)


@router.get("/{row_id}", response_model=QuestionOut)
async def get_question(row_id: int, db: AsyncSession = Depends(get_db)):
    q = await svc.get_question(db, row_id)
    if q is None:
        raise HTTPException(404, "Question not found")
    await svc.attach_variation_lineage(db, [q])
    await svc.attach_question_source(db, [q])
    await svc.attach_designation(db, [q])
    return q


@router.post("", response_model=QuestionOut, status_code=201)
async def create_question(data: QuestionCreate, db: AsyncSession = Depends(get_db)):
    pii = scan_for_pii(data.question_text)
    if pii:
        raise HTTPException(422, f"Question rejected — possible PII detected: {pii}")
    q = await svc.create_question(db, data)
    await svc.attach_variation_lineage(db, [q])
    await svc.attach_question_source(db, [q])
    await svc.attach_designation(db, [q])
    return q


@router.patch("/{row_id}", response_model=QuestionOut)
async def update_question(row_id: int, data: QuestionUpdate, db: AsyncSession = Depends(get_db)):
    if data.question_text:
        pii = scan_for_pii(data.question_text)
        if pii:
            raise HTTPException(422, f"Update rejected — possible PII detected: {pii}")
    try:
        q = await svc.update_question(db, row_id, data)
    except svc.QuestionApprovalBlocked as e:
        # Phase 7: approving an evidence-generated question over unverified evidence. A 422
        # rather than a 403 because the request is well-formed and the fix is a curation
        # action the message names, not a permission.
        raise HTTPException(
            422,
            "Cannot approve an evidence-generated question: " + "; ".join(e.blockers),
        ) from e
    if q is None:
        raise HTTPException(404, "Question not found or deleted")
    await svc.attach_variation_lineage(db, [q])
    await svc.attach_question_source(db, [q])
    await svc.attach_designation(db, [q])
    return q


@router.delete("/{row_id}", response_model=QuestionOut)
async def delete_question(row_id: int, data: SoftDelete, db: AsyncSession = Depends(get_db)):
    q = await svc.soft_delete_question(db, row_id, data.reason)
    if q is None:
        raise HTTPException(404, "Question not found")
    await svc.attach_variation_lineage(db, [q])
    await svc.attach_question_source(db, [q])
    await svc.attach_designation(db, [q])
    return q


@router.post("/import-csv")
async def import_csv(file: UploadFile, db: AsyncSession = Depends(get_db)):
    """Import questions from CSV (FR-105). Runs PII lint; flags suspect rows.

    Expected columns: question_text, persona, therapeutic_area, brand_focus, domain,
    [approval_status], [approver_name]
    """
    content = (await file.read()).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))

    imported, skipped = 0, []
    for i, row in enumerate(reader, start=2):  # row 1 = header
        text = (row.get("question_text") or "").strip()
        if not text:
            continue
        pii = scan_for_pii(text)
        if pii:
            skipped.append({"row": i, "reason": f"PII: {pii}", "text": text[:80]})
            continue
        try:
            data = QuestionCreate(
                question_text=text,
                persona=row["persona"].strip(),
                therapeutic_area=row["therapeutic_area"].strip(),
                indication=(row.get("indication") or "").strip() or None,
                disease=(row.get("disease") or "").strip() or None,
                brand_focus=row["brand_focus"].strip(),
                domain=row["domain"].strip(),
                approval_status=(row.get("approval_status") or "PENDING").strip(),
                approver_name=(row.get("approver_name") or None),
            )
        except Exception as e:  # noqa: BLE001
            skipped.append({"row": i, "reason": f"validation: {e}", "text": text[:80]})
            continue
        await svc.create_question(db, data)
        imported += 1

    return {"imported": imported, "skipped": skipped}


@router.post("/import-prompts/preview")
async def import_prompts_preview(
    file: UploadFile,
    persona: str = Form(...),
    brand_focus: str = Form(...),
    domain: str = Form("General"),
    therapeutic_area: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """Step 1 of the bulk prompt importer (FR-116): DRY RUN — extract, don't persist.

    Reads a SINGLE prompt/question (or keyword) column from a CSV (e.g. a Profound / PAA
    export), dedupes it against the file and the existing bank, PII-scans it, and returns the
    distinct questions that WOULD be added so the analyst can review them before committing.
    The therapeutic area is derived from the brand when omitted. Use ``/import-csv`` for the
    fully-specified per-row importer instead.
    """
    content = await file.read()
    try:
        return await svc.preview_prompts(
            db,
            content=content,
            persona=persona,
            brand_focus=brand_focus,
            domain=domain,
            therapeutic_area=therapeutic_area,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/import-prompts")
async def import_prompts(body: PromptImportCommit, db: AsyncSession = Depends(get_db)):
    """Step 2 of the bulk prompt importer (FR-116): persist the analyst-approved subset.

    Takes the exact list of questions the analyst kept in the preview plus the shared
    persona / brand / theme, and creates them PENDING with a demand-origin label (Real /
    From keyword). Dedupe + PII are re-checked defensively; skips are reported.
    """
    return await svc.commit_prompts(
        db,
        questions=body.questions,
        persona=body.persona,
        brand_focus=body.brand_focus,
        domain=body.domain,
        therapeutic_area=body.therapeutic_area,
        demand_origin=body.demand_origin,
    )
