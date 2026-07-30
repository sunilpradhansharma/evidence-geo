"""Unattended OpenEvidence capture worker.

Drives the Playwright harness over every still-pending Provider question in a run,
then feeds each scraped answer through the EXISTING manual-capture bridge
(``openevidence_service.capture`` + ``finalize_capture``) — so scoring, Chairman
re-arbitration, and auto-completion of an AWAITING_OPENEVIDENCE run all happen exactly
as they do for a human paste. The bot is just another caller.

A single asyncio lock serialises browser usage (one session at a time). Failures are
isolated per question: one bad scrape never aborts the batch, and the run simply stays
AWAITING_OPENEVIDENCE for a later retry (sweep) or the manual fallback.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.util
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

from app.config.settings import PROJECT_ROOT, get_settings
from app.models.database import AsyncSessionLocal
from app.schemas import OpenEvidenceCapture, OpenEvidenceSource
from app.services import openevidence_service as oe_service
from app.utils.audit import write_audit
from app.utils.logging import get_logger

logger = get_logger("openevidence.worker")

_lock = asyncio.Lock()  # serialise browser sessions (bound to the worker loop)
_last_result: dict | None = None


# --- dedicated subprocess-capable event loop ---------------------------------------
# Playwright launches its Node driver as a *subprocess*. On Windows, uvicorn runs the
# server on a SelectorEventLoop whenever --reload or --workers is used, and a
# SelectorEventLoop CANNOT spawn subprocesses: async_playwright().start() dies instantly
# with a bare NotImplementedError. So we never drive Playwright from the server loop --
# all browser work runs on our own dedicated loop that supports subprocesses
# (ProactorEventLoop on Windows; a fresh default loop already supports them on POSIX),
# owned by a daemon thread. Coroutines are submitted cross-loop via
# run_coroutine_threadsafe, which makes unattended capture work even under
# `uvicorn --reload`.
_worker_loop: asyncio.AbstractEventLoop | None = None
_worker_thread: threading.Thread | None = None
_worker_init_lock = threading.Lock()
_futures: set[concurrent.futures.Future] = set()  # strong refs until each task finishes


def _new_subprocess_loop() -> asyncio.AbstractEventLoop:
    """A new event loop that can spawn subprocesses on this platform."""
    if sys.platform == "win32":
        return asyncio.ProactorEventLoop()
    return asyncio.new_event_loop()


def _ensure_worker_loop() -> asyncio.AbstractEventLoop:
    """Lazily start (once) the daemon thread that owns the subprocess-capable loop."""
    global _worker_loop, _worker_thread
    with _worker_init_lock:
        if _worker_loop is not None and not _worker_loop.is_closed():
            return _worker_loop
        loop = _new_subprocess_loop()
        thread = threading.Thread(target=loop.run_forever, name="oe-playwright-loop", daemon=True)
        thread.start()
        _worker_loop, _worker_thread = loop, thread
        logger.info("OpenEvidence worker loop started (%s)", type(loop).__name__)
        return loop


def _submit(coro) -> concurrent.futures.Future:
    """Schedule a coroutine on the subprocess-capable worker loop from any thread/loop."""
    return asyncio.run_coroutine_threadsafe(coro, _ensure_worker_loop())


def _on_future_done(fut: concurrent.futures.Future) -> None:
    """Drop the strong ref and surface any exception that escaped the task body.

    auto_capture_run/auto_capture_sweep already record their own failures, so this is a
    belt-and-suspenders net: a crashing background task is logged and reflected in
    /status instead of vanishing silently."""
    _futures.discard(fut)
    if fut.cancelled():
        return
    exc = fut.exception()
    if exc is not None:
        logger.error("Background auto-capture task crashed: %s", exc, exc_info=exc)
        _record({"error": f"task crashed: {type(exc).__name__}: {exc}"})


def _fire(coro) -> None:
    """Fire-and-forget a coroutine on the worker loop, keeping a strong ref."""
    fut = _submit(coro)
    _futures.add(fut)
    fut.add_done_callback(_on_future_done)


def schedule_auto_capture(run_id: str) -> None:
    """Fire-and-forget auto-capture for one run (used by the orchestrator hook + API)."""
    _fire(auto_capture_run(run_id))


def schedule_sweep() -> None:
    """Fire-and-forget sweep over all runs with pending Provider questions."""
    _fire(auto_capture_sweep())


async def run_test_login() -> dict:
    """Run test_login() on the worker loop, awaited from the caller's (server) loop."""
    return await asyncio.wrap_future(_submit(test_login()))


async def run_proxy_check() -> dict:
    """Run proxy_check() on the worker loop, awaited from the caller's (server) loop."""
    return await asyncio.wrap_future(_submit(proxy_check()))


# --- core ---------------------------------------------------------------------------
async def auto_capture_run(run_id: str) -> dict:
    settings = get_settings()
    if not settings.oe_auto_enabled:
        return _record({"run_id": run_id, "skipped": "OE auto-capture disabled (OE_AUTO_ENABLED)"})
    try:
        async with _lock:
            return await _run_locked(run_id, settings)
    except Exception as e:  # noqa: BLE001 - a background task must never die unrecorded
        logger.exception("Auto-capture crashed for run %s", run_id)
        return _record({
            "run_id": run_id, "captured": 0, "failed": 0, "errors": [],
            "error": f"crashed: {type(e).__name__}: {e}",
        })


async def _run_locked(run_id: str, settings) -> dict:
    from app.openevidence_auto.browser import OpenEvidenceBrowser, OpenEvidenceError

    result: dict = {
        "run_id": run_id, "captured": 0, "failed": 0, "errors": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with AsyncSessionLocal() as db:
            wl = await oe_service.worklist(db, run_id)
        pending = [it for it in wl["items"] if not it["captured"]]
        result["pending_at_start"] = len(pending)
        if not pending:
            result["note"] = "nothing pending"
            return _record(result)

        logger.info("Auto-capturing %d pending Provider question(s) for run %s", len(pending), run_id)

        async def _do_capture() -> None:
            async with OpenEvidenceBrowser(settings) as browser:
                try:
                    await browser.ensure_logged_in()
                except OpenEvidenceError as e:
                    result["error"] = f"login: {e}"
                    logger.warning("Auto-capture aborted (login) for run %s: %s", run_id, e)
                    return
                for it in pending:
                    await _capture_one(browser, run_id, it, settings, result)
                    await asyncio.sleep(settings.oe_question_pause_ms / 1000)

        # Hard wall-clock cap: a stalled remote session (Bright Data latency, a CAPTCHA
        # that never clears, a missing selector) must never hang forever holding the
        # single browser lock. Budget = login + one answer per question + margin.
        overall_ms = (settings.oe_nav_timeout_ms
                      + len(pending) * (settings.oe_answer_timeout_ms + settings.oe_question_pause_ms)
                      + 60000)
        try:
            await asyncio.wait_for(_do_capture(), timeout=overall_ms / 1000)
        except asyncio.TimeoutError:
            result["error"] = f"timed out after {overall_ms // 1000}s (remote flow stalled)"
            logger.warning("Auto-capture timed out for run %s after %ss", run_id, overall_ms // 1000)
    except OpenEvidenceError as e:
        result["error"] = f"browser: {e}"
        logger.warning("Auto-capture aborted (browser) for run %s: %s", run_id, e)
    except Exception as e:  # noqa: BLE001 - launch lock, DB, etc.: record it, don't vanish silently
        result["error"] = f"{type(e).__name__}: {e}"
        logger.exception("Auto-capture failed unexpectedly for run %s", run_id)

    return _record(result)


async def _capture_one(browser, run_id: str, item: dict, settings, result: dict) -> None:
    qid = item["question_id"]
    qtext = item.get("question_text") or ""
    try:
        answer, sources = await browser.ask(qtext)
        payload = OpenEvidenceCapture(
            run_id=run_id,
            question_id=qid,
            answer_text=answer,
            model_version=settings.oe_model_version,
            sources=[OpenEvidenceSource(url=s["url"], title=s.get("title")) for s in sources],
        )
        async with AsyncSessionLocal() as db:
            res = await oe_service.capture(db, payload)
            await write_audit(
                db, role="SYSTEM", event="OPEN_EVIDENCE_AUTO_CAPTURE",
                run_id=run_id, question_id=qid, llm_target=oe_service.OE_TARGET,
                context={"chars": len(answer), "sources": len(sources)},
            )
        # Score + re-run consensus (own session); auto-completes the run when done.
        try:
            await oe_service.finalize_capture(res["response_id"])
        except Exception as fe:  # noqa: BLE001 - best effort, never abort the batch
            logger.warning("finalize_capture failed for question %s: %s", qid, fe)
        result["captured"] += 1
    except HTTPException as he:
        if he.status_code == 409:  # already captured (responses are immutable)
            logger.info("Question %s already has an OpenEvidence answer; skipping", qid)
            return
        result["failed"] += 1
        result["errors"].append({"question_id": qid, "error": f"HTTP {he.status_code}: {he.detail}"})
        logger.warning("Capture rejected for question %s: %s", qid, he.detail)
    except Exception as e:  # noqa: BLE001 - isolate per-question failures
        result["failed"] += 1
        result["errors"].append({"question_id": qid, "error": str(e)})
        logger.warning("Auto-capture failed for question %s: %s", qid, e)


async def auto_capture_sweep() -> dict:
    """Process every run that still has pending Provider questions (retry/catch-up)."""
    settings = get_settings()
    if not settings.oe_auto_enabled:
        return {"skipped": "OE auto-capture disabled (OE_AUTO_ENABLED)"}
    async with AsyncSessionLocal() as db:
        runs = await oe_service.list_runs_with_provider(db)
    targets = [r["run_id"] for r in runs if r.get("pending", 0) > 0]
    results = [await auto_capture_run(rid) for rid in targets]
    return {"processed": len(results), "results": results}


async def test_login() -> dict:
    """Seed/verify the OpenEvidence session by performing a login now (foreground).

    Also reports the browser's public egress IP so you can confirm a residential proxy
    is routing (and see the exact IP OpenEvidence sees)."""
    settings = get_settings()
    if not (settings.oe_email and settings.oe_password):
        return {"ok": False, "detail": "OE_EMAIL / OE_PASSWORD are not set."}
    from app.openevidence_auto.browser import OpenEvidenceBrowser, OpenEvidenceError
    async with _lock:
        egress_ip = None
        try:
            async with OpenEvidenceBrowser(settings) as browser:
                egress_ip = await browser.egress_ip()
                await browser.ensure_logged_in()
            return {"ok": True, "egress_ip": egress_ip,
                    "detail": "Logged in; session persisted for reuse."}
        except OpenEvidenceError as e:
            return {"ok": False, "egress_ip": egress_ip, "detail": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "egress_ip": egress_ip, "detail": f"unexpected error: {e}"}


async def proxy_check() -> dict:
    """Diagnostic: launch the bot browser and report the public egress IP (routed
    through OE_PROXY_* if set), WITHOUT logging in. Confirms a residential proxy is
    actually routing before blaming OpenEvidence's anti-bot wall."""
    settings = get_settings()
    from app.openevidence_auto.browser import (
        OpenEvidenceBrowser,
        OpenEvidenceError,
        parse_proxy,
    )
    configured = bool(settings.oe_proxy_server)
    proxy = parse_proxy(settings)
    proxy_server = proxy["server"] if proxy else None
    async with _lock:
        try:
            async with OpenEvidenceBrowser(settings) as browser:
                ip = await browser.egress_ip()
            return {
                "ok": ip is not None,
                "proxy_configured": configured,
                "proxy_server": proxy_server,
                "egress_ip": ip,
                "detail": (
                    "Egress IP resolved." if ip
                    else "Could not resolve egress IP — the proxy may be down, "
                         "unreachable, or blocking the check."
                ),
            }
        except OpenEvidenceError as e:
            return {"ok": False, "proxy_configured": configured,
                    "proxy_server": proxy_server, "egress_ip": None, "detail": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "proxy_configured": configured, "proxy_server": proxy_server,
                    "egress_ip": None, "detail": f"unexpected error: {e}"}


def auto_status() -> dict:
    settings = get_settings()
    user_dir = Path(settings.oe_user_data_dir.strip() or (PROJECT_ROOT / ".oe_session"))
    seeded = user_dir.exists() and any(user_dir.iterdir()) if user_dir.exists() else False
    from app.openevidence_auto.browser import parse_proxy
    proxy = parse_proxy(settings)
    cdp = (settings.oe_scraping_browser_cdp or "").strip()
    return {
        "enabled": settings.oe_auto_enabled,
        "has_credentials": bool(settings.oe_email and settings.oe_password),
        "base_url": settings.oe_base_url,
        "headless": settings.oe_headless,
        "session_seeded": seeded,
        "mode": "scraping_browser" if cdp else "local",
        "scraping_browser_configured": bool(cdp),
        "proxy_configured": bool(settings.oe_proxy_server),
        "proxy_server": proxy["server"] if proxy else None,
        "playwright_installed": importlib.util.find_spec("playwright") is not None,
        "last_result": _last_result,
    }


def _record(result: dict) -> dict:
    global _last_result
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    _last_result = result
    return result
