"""Unattended OpenEvidence auto-capture API.

Drives the Playwright bot that answers Provider questions without a human. The actual
ingestion reuses the manual-capture bridge, so these endpoints only schedule/observe
the bot. OFF unless OE_AUTO_ENABLED=true and OE_EMAIL/OE_PASSWORD are set.
"""
from fastapi import APIRouter

from app.openevidence_auto import worker

router = APIRouter(prefix="/openevidence/auto", tags=["openevidence-auto"])


@router.get("/status")
async def status():
    """Config + last-run snapshot (does not expose credentials)."""
    return worker.auto_status()


@router.post("/runs/{run_id}", status_code=202)
async def auto_capture_run(run_id: str):
    """Schedule unattended capture of all pending Provider questions in a run."""
    worker.schedule_auto_capture(run_id)
    return {"scheduled": True, "run_id": run_id}


@router.post("/sweep", status_code=202)
async def sweep():
    """Schedule unattended capture across every run with pending Provider questions."""
    worker.schedule_sweep()
    return {"scheduled": True}


@router.post("/login")
async def login():
    """Seed/verify the OpenEvidence login session now (runs on the capture loop)."""
    return await worker.run_test_login()


@router.post("/proxy-check")
async def proxy_check():
    """Verify the residential proxy: report the public egress IP the bot presents,
    WITHOUT logging in. Confirms OE_PROXY_* is routing before blaming OpenEvidence."""
    return await worker.run_proxy_check()
