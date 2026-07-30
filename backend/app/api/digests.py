"""Stakeholder-differentiated digest API (BR-008a).

Admin-configurable role profiles + rules (no code deploy), manual trigger, past-digest
listing, and HTML/PDF download of a generated digest.
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.schemas import (
    DigestProfileCreate,
    DigestProfileOut,
    DigestProfileUpdate,
    DigestRunOut,
)
from app.services import digest_service as svc

router = APIRouter(prefix="/digests", tags=["digests"])


def _with_next_run(profile):
    """Attach the profile's live next-fire time so the UI can show 'next: ...'."""
    try:
        from app.services.digest_scheduler import next_run_for
        profile.next_run_at = next_run_for(profile.id)
    except Exception:  # noqa: BLE001 — scheduler not running (tests); leave as None
        profile.next_run_at = None
    return profile


# ---- Diagnostics ----
@router.get("/ses-check")
async def ses_check():
    """Report whether SES is set up to actually deliver email (sandbox / verified sender)."""
    return svc.ses_status()


# ---- Profiles (admin CRUD, BR-008a.1/2/4) ----
@router.get("/profiles", response_model=list[DigestProfileOut])
async def list_profiles(db: AsyncSession = Depends(get_db)):
    return [_with_next_run(p) for p in await svc.list_profiles(db)]


@router.post("/profiles", response_model=DigestProfileOut, status_code=201)
async def create_profile(data: DigestProfileCreate, db: AsyncSession = Depends(get_db)):
    profile = await svc.create_profile(db, data)
    # Re-sync the weekly scheduler jobs so a new/edited cadence takes effect immediately.
    await _resync_jobs()
    return _with_next_run(profile)


@router.put("/profiles/{profile_id}", response_model=DigestProfileOut)
async def update_profile(profile_id: int, data: DigestProfileUpdate, db: AsyncSession = Depends(get_db)):
    profile = await svc.update_profile(db, profile_id, data)
    if profile is None:
        raise HTTPException(404, "Digest profile not found")
    await _resync_jobs()
    return _with_next_run(profile)


@router.delete("/profiles/{profile_id}", status_code=204)
async def delete_profile(profile_id: int, db: AsyncSession = Depends(get_db)):
    if not await svc.delete_profile(db, profile_id):
        raise HTTPException(404, "Digest profile not found")
    await _resync_jobs()


# ---- Trigger + history (BR-008a.3/5/6/7) ----
@router.post("/profiles/{profile_id}/run", response_model=DigestRunOut, status_code=202)
async def trigger_digest(profile_id: int, db: AsyncSession = Depends(get_db)):
    """Manually generate + deliver a digest now (staging validation of cadence/routing)."""
    profile = await svc.get_profile(db, profile_id)
    if profile is None:
        raise HTTPException(404, "Digest profile not found")
    return await svc.generate_digest(db, profile, deliver=True)


@router.get("/runs", response_model=list[DigestRunOut])
async def list_runs(profile_id: int | None = None, db: AsyncSession = Depends(get_db)):
    return await svc.list_runs(db, profile_id=profile_id)


@router.get("/runs/{run_id}", response_model=DigestRunOut)
async def get_run(run_id: int, db: AsyncSession = Depends(get_db)):
    run = await svc.get_run(db, run_id)
    if run is None:
        raise HTTPException(404, "Digest run not found")
    return run


@router.get("/runs/{run_id}/html", response_class=HTMLResponse)
async def get_run_html(run_id: int, db: AsyncSession = Depends(get_db)):
    run = await svc.get_run(db, run_id)
    if run is None or not run.html:
        raise HTTPException(404, "Digest HTML not found")
    return HTMLResponse(content=run.html)


@router.get("/runs/{run_id}/pdf")
async def get_run_pdf(run_id: int, db: AsyncSession = Depends(get_db)):
    run = await svc.get_run(db, run_id)
    if run is None or not run.pdf_path or not Path(run.pdf_path).exists():
        raise HTTPException(404, "Digest PDF not available (HTML-only). Use the HTML view.")
    return FileResponse(run.pdf_path, media_type="application/pdf", filename=f"digest_{run_id}.pdf")


# ---- Workshop Questions insights (live in-app panel) ----
@router.get("/workshop-insights")
async def workshop_insights(scope: str = "workshop", db: AsyncSession = Depends(get_db)):
    """Live 'current standing' of AI answers for the in-app panel, for a ``scope``.

    ``scope`` is "workshop" (the curated Workshop Questions set the digest also renders) or
    "all" (every tracked question). Same snapshot shape either way: how AI positions the brands
    by designation, per-platform summaries + sources, and citation share of voice. Returns
    available=False when the scope has no answers in this environment."""
    scope = scope if scope in ("workshop", "all") else "workshop"
    data = await svc.workshop_insights(db, scope=scope)
    # Kick off a background LLM refresh of that scope's per-platform 'general summary' when the
    # cached narratives are missing or the underlying answers changed. Fire-and-forget: the
    # current (possibly not-yet-generated) snapshot is returned now; the panel re-fetches soon.
    if data and data.get("needs_summary_refresh"):
        try:
            from app.services import workshop_narrative
            workshop_narrative.trigger_refresh_in_background(scope)
        except Exception:  # noqa: BLE001 — summaries are best-effort
            pass
    return {"available": data is not None, "insights": data, "scope": scope}


async def _resync_jobs() -> None:
    try:
        from app.services.digest_scheduler import sync_digest_jobs
        await sync_digest_jobs()
    except Exception:  # noqa: BLE001 — scheduler not running (e.g. tests); ignore
        pass
