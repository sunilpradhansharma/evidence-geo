"""FastAPI application entry point."""
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    analytics,
    claim_evaluation,
    comparisons,
    competitive,
    competitor_discovery,
    compliance,
    copilot,
    cortex,
    curation,
    digests,
    evidence,
    evidence_ingestion,
    evidence_questions,
    evidence_review,
    exports,
    geo,
    harvest,
    health,
    insights,
    interventions,
    model_releases,
    openevidence,
    openevidence_auto,
    prompt_volume,
    published_synthesis,
    questions,
    recommendations,
    responses,
    runs,
    schedule,
    scores,
    social,
    source_authority,
    taxonomy,
    variations,
)
from app.models.database import AsyncSessionLocal, init_db
from app.services import brand_taxonomy_service, run_service, schedule_service
from app.services.scheduler import shutdown_scheduler, start_scheduler
from app.snowflake import events as sf_events
from app.snowflake import mirror as sf_mirror
from app.snowflake import schema as sf_schema
from app.snowflake import views as sf_views
from app.utils.logging import configure_logging, get_logger

configure_logging()
logger = get_logger("app")


_worker_pool: ThreadPoolExecutor | None = None


def _install_thread_pool() -> None:
    """Install a right-sized default executor for all asyncio.to_thread provider calls.

    Every LLM provider client dispatches its blocking SDK call via asyncio.to_thread, which
    uses the event loop's default ThreadPoolExecutor. Python defaults that pool to only
    min(32, cpu_count + 4) threads — well below a run's real demand (max_concurrent_questions
    * # enabled targets, plus intent/Chairman/scoring). When the pool saturates, calls queue
    with no timeout and large runs stall. Size the pool from the run config so a full
    100-question run has enough workers to fan out. Called once, at startup."""
    global _worker_pool
    from app.config.settings import get_settings
    from app.providers.registry import enabled_targets

    settings = get_settings()
    size = settings.thread_pool_max_workers
    if not size or size <= 0:
        try:
            n_targets = max(len(enabled_targets()), 1)
        except Exception:  # noqa: BLE001 — never block startup on target config
            n_targets = 6
        # concurrency * (targets + intent + Chairman) + scoring pass + base headroom,
        # clamped to a sane band.
        size = (
            settings.max_concurrent_questions * (n_targets + 2)
            + settings.max_concurrent_scoring
            + 16
        )
        size = max(32, min(size, 256))

    pool = ThreadPoolExecutor(max_workers=size, thread_name_prefix="ema-worker")
    asyncio.get_running_loop().set_default_executor(pool)
    _worker_pool = pool
    logger.info("Installed worker thread pool (max_workers=%d)", size)


def _validate_configuration() -> None:
    """Check the taxonomy, canonical_outcomes.yaml and analysis_protocols.yaml.

    **Fatal for the files, reported for the database.** That split is the whole point.
    canonical_outcomes.yaml and analysis_protocols.yaml are checked into git and reviewed,
    so a problem in one is a deploy-time typo and refusing to boot is the right answer — an
    endpoint with no definition yields an evidence network that cannot be reasoned about,
    which is exactly the silent error the evidence hierarchy exists to prevent.

    The taxonomy is no longer such a file. It lives in SQLite and a UI can write to it, so
    the same fatality would let one bad row stop the container from starting AND take the
    surface needed to fix it down with it — unrecoverable without shell access to the host.
    A taxonomy problem is therefore logged and published on ``/status`` instead. The real
    defence moved upstream: writes are validated before they are committed, so an invalid
    row should never exist to be found here.

    Protocols are validated here rather than inside ``taxonomy.validate_config`` so the
    config layer keeps no dependency on ``app.evidence`` — inverting that direction would
    make a future import in ``app/evidence/__init__.py`` a startup cycle.
    """
    from app.config.taxonomy import validate_config
    from app.evidence import protocols

    fatal = protocols.validate()
    if fatal:
        for error in fatal:
            logger.error("Configuration error: %s", error)
        raise RuntimeError(
            f"{len(fatal)} configuration error(s) in canonical_outcomes.yaml / "
            "analysis_protocols.yaml; see the log above."
        )

    reported = validate_config()
    taxonomy.record_startup_errors(reported)
    if reported:
        for error in reported:
            logger.error("Taxonomy configuration error: %s", error)
        logger.error(
            "%d taxonomy problem(s) found. The application is UP and serving; the affected "
            "entries are wrong until corrected. See GET /taxonomy/status.",
            len(reported),
        )
    else:
        logger.info(
            "Configuration validated (taxonomy + canonical outcomes + analysis protocols)"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Evidence Monitoring Agent backend")
    # Size the shared to_thread pool BEFORE any provider calls so runs can fan out (must be
    # first: fixes large runs stalling on Python's tiny default executor).
    try:
        _install_thread_pool()
    except Exception as e:  # noqa: BLE001 — fall back to the default executor on any error
        logger.warning("Worker thread pool sizing skipped (using default executor): %s", e)
    await init_db()
    logger.info("Database initialized")
    # The taxonomy lives in the database now, so it has to be loaded before anything reads
    # it AND before it can be validated — which is why this sits after init_db rather than
    # before it as the file-based check used to. Seeds from the reviewed baseline on a fresh
    # database, then installs the snapshot every synchronous accessor serves.
    async with AsyncSessionLocal() as session:
        await brand_taxonomy_service.hydrate(session)
    logger.info("Brand taxonomy loaded")
    _validate_configuration()
    # Reconcile runs orphaned in RUNNING by a previous crash/restart so the UI doesn't
    # show a phantom in-progress run (background tasks don't survive a restart).
    interrupted = await run_service.fail_interrupted_runs()
    if interrupted:
        logger.warning("Marked %d interrupted run(s) as FAILED on startup", interrupted)
    start_scheduler()
    await schedule_service.sync_on_startup()
    # BR-008a: register weekly stakeholder-digest jobs (best-effort; no-op when disabled).
    try:
        from app.services.digest_scheduler import sync_digest_jobs
        await sync_digest_jobs()
    except Exception as e:  # noqa: BLE001
        logger.warning("Digest scheduler sync skipped: %s", e)
    # GEO: periodic openFDA re-seed of the verified corpus (best-effort; no-op when disabled).
    try:
        _start_geo_refresh_job()
    except Exception as e:  # noqa: BLE001
        logger.warning("GEO refresh scheduler skipped: %s", e)
    # FR-707a: capture real vendor versions + changelogs on startup, then daily
    # (best-effort; version anchoring always runs, changelog fetch is opt-in).
    try:
        await _bootstrap_model_updates()
        _start_model_update_sync_job()
    except Exception as e:  # noqa: BLE001
        logger.warning("Model-update sync startup skipped: %s", e)
    # Activation & Impact: advance in-flight intervention measurements (finalize baselines,
    # launch post-publication reruns, compute results). Best-effort; no-op with no interventions.
    try:
        _start_intervention_sweep_job()
    except Exception as e:  # noqa: BLE001
        logger.warning("Intervention sweep scheduler skipped: %s", e)
    # Snowflake: schema evolution + views + initial mirror + periodic job, launched OFF the
    # startup path. Uvicorn serves no request (not even /healthz) until this lifespan reaches
    # `yield`, so AWAITING a large first backfill here made the post-deploy health check 502
    # for the whole sync. Fire-and-forget instead — the task self-guards and never gates
    # app liveness.
    _launch_background(_snowflake_bootstrap())
    yield
    logger.info("Shutting down")
    shutdown_scheduler()
    if _worker_pool is not None:
        # Don't block shutdown on in-flight provider calls; cancel anything still queued.
        _worker_pool.shutdown(wait=False, cancel_futures=True)


_background_tasks: set = set()


def _launch_background(coro) -> None:
    """Fire-and-forget a startup coroutine, keeping a strong reference until it finishes so
    the event loop cannot garbage-collect the task mid-flight (per the asyncio docs)."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _snowflake_bootstrap() -> None:
    """Schema + views + initial mirror + periodic job, run off the startup path.

    Best-effort and fully self-guarding: any failure (or Snowflake being disabled) is logged
    and never affects app liveness. The periodic job is registered before the potentially
    heavy initial mirror so scheduling is not delayed by a large first backfill.
    """
    try:
        await sf_schema.ensure_schema(startup_backfill=True)
        await sf_views.ensure_views()
        _start_mirror_job()
        await sf_mirror.run_mirror_safe()
    except Exception as e:  # noqa: BLE001
        logger.warning("Snowflake bootstrap skipped: %s", e)


def _start_mirror_job() -> None:
    """Schedule a periodic incremental mirror via the running APScheduler."""
    from app.snowflake import client as sf_client
    from app.services import scheduler as sched_mod

    if not sf_client.is_enabled() or sched_mod._scheduler is None:
        return

    async def _job() -> None:
        await sf_mirror.run_mirror_safe()

    sched_mod._scheduler.add_job(
        _job, "interval", minutes=10, id="snowflake_mirror",
        replace_existing=True, max_instances=1, coalesce=True,
    )
    logger.info("Scheduled Snowflake mirror job (every 10 min)")


def _start_geo_refresh_job() -> None:
    """Schedule a periodic openFDA re-seed of the GEO ground-truth corpus.

    Re-runs the generator (curated YAML + openFDA label seed), then hot-reloads the
    in-memory cache. Offline-safe: a failed refresh is logged and the last-good corpus
    keeps serving. No-op when disabled or the scheduler is not running.
    """
    from app.config.settings import get_settings
    from app.services import scheduler as sched_mod

    settings = get_settings()
    if not settings.geo_refresh_enabled or sched_mod._scheduler is None:
        return

    async def _job() -> None:
        from app.geo import builder, loader
        try:
            report, _docs = await builder.generate(seed=True)
            loader.reload()
            logger.info(
                "GEO corpus refreshed: %d brand(s), llms.txt=%s",
                len(report.brands), report.llms_txt_written,
            )
        except Exception as e:  # noqa: BLE001 — a failed refresh must not crash the scheduler
            logger.warning("Scheduled GEO refresh failed (keeping last-good corpus): %s", e)

    days = max(1, settings.geo_refresh_interval_days)
    sched_mod._scheduler.add_job(
        _job, "interval", days=days, id="geo_refresh",
        replace_existing=True, max_instances=1, coalesce=True,
    )
    logger.info("Scheduled GEO corpus refresh job (every %d day(s))", days)


async def _bootstrap_model_updates() -> None:
    """FR-707a: anchor AI Update Impact to real vendor versions on startup.

    Always refreshes version observations + api-sourced transition events from our own
    traffic (no network); additionally fetches vendor changelogs when the opt-in sync is
    enabled. Best-effort: a failure is logged and never blocks startup.
    """
    from app.models.database import AsyncSessionLocal
    from app.model_updates import sync_model_updates

    async with AsyncSessionLocal() as db:
        result = await sync_model_updates(db)
    logger.info(
        "Model-update bootstrap: %d version(s) observed, %d transition(s), changelog_sync=%s",
        result.get("versions_observed", 0),
        result.get("version_transitions_created", 0),
        result.get("changelog_sync_enabled", False),
    )


def _start_model_update_sync_job() -> None:
    """Schedule the daily vendor version + changelog sync (FR-707a).

    Registered whenever the scheduler flag is on; the job itself is a safe no-op for the
    network step when model_update_sync_enabled is off (version anchoring still runs).
    """
    from app.config.settings import get_settings
    from app.services import scheduler as sched_mod

    settings = get_settings()
    if not settings.model_update_sync_scheduler_enabled or sched_mod._scheduler is None:
        return

    async def _job() -> None:
        from app.models.database import AsyncSessionLocal
        from app.model_updates import sync_model_updates
        try:
            async with AsyncSessionLocal() as db:
                await sync_model_updates(db)
            logger.info("Daily model-update sync complete")
        except Exception as e:  # noqa: BLE001 — a failed sync must not crash the scheduler
            logger.warning("Scheduled model-update sync failed: %s", e)

    hour = max(0, min(23, settings.model_update_sync_hour_utc))
    sched_mod._scheduler.add_job(
        _job, "cron", hour=hour, minute=0, id="model_update_sync",
        replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=3600,
    )
    logger.info("Scheduled daily model-update sync job (%02d:00 UTC)", hour)


def _start_intervention_sweep_job() -> None:
    """Schedule the recurring Activation & Impact measurement sweep.

    One interval job (not per-intervention) advances every in-flight intervention's
    measurement state machine — finalizing baselines once their runs are scored, launching
    post-publication reruns when the adoption window elapses, and computing before/after
    results. Runs every 15 min so a demo's baseline/result appears promptly; the "measure
    now" endpoint forces a step immediately. Self-guarding: no-op when there are no
    in-flight interventions.
    """
    from app.services import scheduler as sched_mod

    if sched_mod._scheduler is None:
        return

    async def _job() -> None:
        from app.activation import sweep
        try:
            await sweep.run_sweep()
        except Exception as e:  # noqa: BLE001 — a failed sweep must not crash the scheduler
            logger.warning("Scheduled intervention sweep failed: %s", e)

    sched_mod._scheduler.add_job(
        _job, "interval", minutes=15, id="intervention_measurement_sweep",
        replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=600,
    )
    logger.info("Scheduled intervention measurement sweep (every 15 min)")


app = FastAPI(
    title="Evidence Monitoring Agent — POC",
    description="Monitors what LLMs say about pharmaceutical therapies.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AppEventsMiddleware:
    """Pure-ASGI middleware capturing every API request/response into APP_EVENTS.

    Buffers the request and response bodies by wrapping ``receive``/``send`` (so it never
    consumes the stream the endpoint needs), then records a credential-redacted event as a
    fire-and-forget task — user latency is unaffected. No-op when capture is disabled.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if not sf_events.should_capture() or sf_events.is_skipped(path):
            return await self.app(scope, receive, send)

        start = time.monotonic()
        req_chunks: list[bytes] = []
        resp_chunks: list[bytes] = []
        status_holder = {"code": 0}

        async def recv():
            message = await receive()
            if message["type"] == "http.request":
                req_chunks.append(message.get("body", b""))
            return message

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
            elif message["type"] == "http.response.body":
                resp_chunks.append(message.get("body", b""))
            await send(message)

        await self.app(scope, recv, send_wrapper)

        duration_ms = int((time.monotonic() - start) * 1000)
        req_bytes = b"".join(req_chunks)
        body_bytes = b"".join(resp_chunks)
        client = scope.get("client")
        try:
            sf_events.fire_and_forget(sf_events.record_event(
                method=scope.get("method", ""),
                path=path,
                status_code=status_holder["code"],
                duration_ms=duration_ms,
                request_body=req_bytes.decode("utf-8", "replace") if req_bytes else None,
                response_body=body_bytes.decode("utf-8", "replace") if body_bytes else None,
                client_host=client[0] if client else None,
            ))
        except Exception:  # noqa: BLE001 — capture must never break the request
            pass


app.add_middleware(AppEventsMiddleware)

app.include_router(health.router)
app.include_router(questions.router)
app.include_router(runs.router)
app.include_router(responses.router)
app.include_router(scores.router)
app.include_router(analytics.router)
app.include_router(insights.router)
app.include_router(recommendations.router)
app.include_router(interventions.router)
app.include_router(prompt_volume.router)
app.include_router(openevidence.router)
app.include_router(openevidence_auto.router)
app.include_router(harvest.router)
app.include_router(social.router)
app.include_router(exports.router)
app.include_router(compliance.router)
app.include_router(source_authority.router)
app.include_router(geo.router)
app.include_router(schedule.router)
app.include_router(model_releases.router)
app.include_router(digests.router)
app.include_router(cortex.router)
app.include_router(copilot.router)
app.include_router(variations.router)
app.include_router(evidence.router)
app.include_router(competitor_discovery.router)
app.include_router(evidence_review.router)
app.include_router(evidence_ingestion.router)
app.include_router(published_synthesis.router)
app.include_router(comparisons.router)
app.include_router(evidence_questions.router)
app.include_router(claim_evaluation.router)
app.include_router(curation.router)
app.include_router(competitive.router)
app.include_router(taxonomy.router)


@app.get("/")
async def root():
    return {"service": "evidence-monitoring-agent", "status": "ok"}
