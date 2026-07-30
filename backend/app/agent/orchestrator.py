"""Run engine (FR-201..211, FR-503/504, NF-001/003/005..008).

Deterministic dispatch/retry/rate-limit/resume in Python; Claude judgment (validator)
where it counts. Every external call is audited with role=TARGET (IN-302).
"""
import asyncio
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.budget import BudgetGuard, estimate_cost
from app.agent.cancellation import clear_cancel, is_cancel_requested, register_run
from app.agent.chairman import (
    ConsensusResult,
    evaluate_consensus,
    persist_consensus,
    refresh_run_consensus_counters,
)
from app.agent.intent_classifier import classify_intent
from app.agent.rate_limiter import RateLimiterRegistry
from app.agent.validator import looks_truncated
from app.config.settings import get_settings, load_yaml_config
from app.guardrails.injection import scan_injection
from app.models.question import Question
from app.models.response import Response
from app.models.run import Run
from app.providers.base import (
    ModelParams,
    ProviderResult,
    RateLimited,
    SafetyBlocked,
    Transient,
)
from app.providers.registry import Target, enabled_targets, get_provider_client, targets_for_persona
from app.utils.audit import write_audit
from app.utils.logging import get_logger, log_with_context

logger = get_logger("orchestrator")
RETRY_BACKOFF = [2, 4, 8]  # seconds (FR-206)


def _load_system_prompts() -> dict:
    return load_yaml_config("system_prompts.yaml").get("personas", {})


def _load_disease_state_prompts() -> dict:
    # FR-108a: brand-less "Landscape Matrix" persona prompts.
    return load_yaml_config("system_prompts.yaml").get("disease_state", {})


async def _fetch_questions(
    db: AsyncSession, persona=None, therapeutic_area=None, domain=None,
    question_ids: list[str] | None = None, monitoring_mode: str = "BRAND",
) -> list[Question]:
    # Explicit selection (ad-hoc test runs): run exactly the chosen questions
    # regardless of approval/active gating so operators can validate a single
    # question or a small set before launching the full repo. Soft-deleted and
    # superseded (old-version) rows are excluded so an edited/approved question
    # isn't double-counted — its question_id maps to multiple versioned rows, but
    # only the current one should run. persona/TA/domain filters are ignored here
    # because the selection is already explicit.
    if question_ids:
        stmt = select(Question).where(
            Question.deleted_at.is_(None),
            Question.superseded_by.is_(None),  # only the current version of each question
            Question.question_id.in_(question_ids),
        )
        result = await db.execute(stmt.order_by(Question.question_id))
        return list(result.scalars().all())

    stmt = select(Question).where(
        Question.active.is_(True),
        Question.approval_status == "APPROVED",
        Question.deleted_at.is_(None),
        Question.superseded_by.is_(None),
        # FR-108a: a run executes exactly one mode's slice of the bank.
        Question.monitoring_mode == monitoring_mode,
    )
    if persona:
        stmt = stmt.where(Question.persona == persona)
    if therapeutic_area:
        stmt = stmt.where(Question.therapeutic_area == therapeutic_area)
    if domain:
        stmt = stmt.where(Question.domain == domain)
    result = await db.execute(stmt.order_by(Question.question_id))
    return list(result.scalars().all())


async def _existing_pairs(db: AsyncSession, run_id: str) -> set[tuple[str, str]]:
    """For resume (FR-504): (question_id, llm_name) pairs already stored for this run."""
    result = await db.execute(
        select(Response.question_id, Response.llm_name).where(Response.run_id == run_id)
    )
    return {(qid, name) for qid, name in result.all()}


async def _interruptible_sleep(seconds: float, run_id: str) -> None:
    """Sleep in 0.5s slices so a cancel request is observed promptly (NF-005)."""
    waited = 0.0
    while waited < seconds:
        if is_cancel_requested(run_id):
            return
        await asyncio.sleep(min(0.5, seconds - waited))
        waited += 0.5


async def _call_target_with_retry(
    target: Target, system: str, user: str, limiter: RateLimiterRegistry, run_id: str
) -> tuple[ProviderResult | None, str, str | None]:
    """Returns (result, status, error). Handles rate-limit/transient retries (FR-206)."""
    client = get_provider_client(target.provider)
    rpm = target.rate_limit.get("rpm", 20)
    bucket = limiter.get(target.name, rpm)
    # Per-call wall-clock ceiling. A provider whose socket stalls with no response would
    # otherwise block this await forever, freezing the whole run (the question never
    # commits and its concurrency slot never frees). asyncio.wait_for turns a hung call
    # into a normal FAILED response and names the provider in the log. Fail-fast (no
    # retry) on timeout so a fully-dead provider can't multiply the wait — questions
    # still progress in waves. NF-005.
    call_timeout = get_settings().target_call_timeout_seconds

    last_error = None
    for attempt in range(len(RETRY_BACKOFF) + 1):
        if is_cancel_requested(run_id):
            return None, "CANCELLED", "Cancelled by operator."
        await bucket.acquire()
        try:
            result = await asyncio.wait_for(
                client.chat(target.model_id, system, user, target.params),
                timeout=call_timeout,
            )
            status = "TRUNCATED" if looks_truncated(result) else "SUCCESS"
            return result, status, None
        except asyncio.TimeoutError:
            log_with_context(
                logger, 30, "Target call timed out (treated as FAILED)",
                run_id=run_id, llm_target=target.name, timeout_s=call_timeout,
            )
            return None, "FAILED", f"Timed out after {call_timeout}s"
        except SafetyBlocked as e:
            return None, "BLOCKED", str(e)
        except (RateLimited, Transient) as e:
            last_error = str(e)
            if attempt < len(RETRY_BACKOFF):
                await _interruptible_sleep(RETRY_BACKOFF[attempt], run_id)
                continue
            return None, "FAILED", last_error
        except Exception as e:  # noqa: BLE001 — Fatal/Auth/unknown
            return None, "FAILED", str(e)
    return None, "FAILED", last_error


async def _handle_truncation(
    target: Target, system: str, user: str, limiter: RateLimiterRegistry, run_id: str
) -> tuple[ProviderResult | None, str, str | None]:
    """Retry once with raised max_tokens (FR-211)."""
    boosted = ModelParams(
        max_tokens=min(target.params.max_tokens * 2, 4096),
        temperature=target.params.temperature,
        extra=target.params.extra,
    )
    boosted_target = Target(
        name=target.name,
        provider=target.provider,
        model_id=target.model_id,
        params=boosted,
        enabled=target.enabled,
        role=target.role,
        rate_limit=target.rate_limit,
    )
    return await _call_target_with_retry(boosted_target, system, user, limiter, run_id)


async def _dispatch_targets(
    coros: list, cancel_event: asyncio.Event | None
) -> tuple[list, bool]:
    """Dispatch all targets for a question concurrently (NF-003), but abort the
    in-flight calls the instant a cancel is requested (preemptive — NF-005) instead
    of waiting for the whole batch to return. Returns (outcomes, cancelled)."""
    gather_task = asyncio.gather(*coros)
    if cancel_event is None:
        return list(await gather_task), False

    waiter = asyncio.ensure_future(cancel_event.wait())
    try:
        await asyncio.wait({gather_task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if cancel_event.is_set():
            gather_task.cancel()  # abort in-flight LLM calls immediately
            await asyncio.gather(gather_task, return_exceptions=True)
            return [], True
        return list(gather_task.result()), False
    finally:
        waiter.cancel()


async def _finalize_cancelled(db: AsyncSession, run: Run, run_id: str) -> Run:
    """Mark a run CANCELLED, preserving already-captured responses (NF-005)."""
    run.status = "CANCELLED"
    run.ended_at = datetime.now(timezone.utc)
    run.notes = "Cancelled by operator."
    await refresh_run_consensus_counters(db, run_id)  # commits
    await db.commit()
    await write_audit(db, role="SYSTEM", event="RUN_CANCELLED", run_id=run_id,
                      context={"completed_responses": run.responses_success + run.responses_failed})
    clear_cancel(run_id)
    log_with_context(logger, 30, "Run cancelled", run_id=run_id)
    return run


async def execute_run(
    db: AsyncSession,
    run_id: str,
    *,
    persona: str | None = None,
    therapeutic_area: str | None = None,
    domain: str | None = None,
    question_ids: list[str] | None = None,
    dry_run: bool = False,
    monitoring_mode: str = "BRAND",
) -> Run:
    """Execute (or resume) a run. Idempotent per (run, question, target) pair."""
    settings = get_settings()
    run = await db.get(Run, run_id)
    if run is None:
        raise ValueError(f"Run {run_id} not found")

    # Register the preemptive cancel Event so a cancel request can abort in-flight
    # LLM calls immediately rather than waiting for the current question to finish.
    cancel_event = register_run(run_id)

    all_targets = enabled_targets()
    prompts = _load_system_prompts()
    disease_state_prompts = _load_disease_state_prompts()  # FR-108a
    limiter = RateLimiterRegistry()
    budget = BudgetGuard(settings.max_tokens_per_run)
    budget.used = run.total_tokens  # resume-aware

    questions = await _fetch_questions(
        db, persona, therapeutic_area, domain,
        question_ids=question_ids, monitoring_mode=monitoring_mode,
    )
    done_pairs = await _existing_pairs(db, run_id)

    snapshot = {
        "targets": [{"name": t.name, "provider": t.provider, "model_id": t.model_id} for t in all_targets],
        "monitoring_mode": monitoring_mode,
        "filters": {
            "persona": persona, "therapeutic_area": therapeutic_area,
            "domain": domain, "question_ids": question_ids,
        },
        "dry_run": dry_run,
    }
    run.config_snapshot = json.dumps(snapshot)
    run.questions_attempted = len(questions)
    await db.commit()

    await write_audit(db, role="SYSTEM", event="RUN_START", run_id=run_id, context=snapshot)
    log_with_context(logger, 20, "Run started", run_id=run_id, questions=len(questions), targets=len(all_targets))

    if dry_run:
        # FR-209: validate connectivity only, no writes to repository.
        for t in all_targets:
            client = get_provider_client(t.provider)
            health = await client.health_check(t.model_id)
            await write_audit(
                db, role="TARGET", event="DRY_RUN_HEALTH", run_id=run_id,
                llm_target=t.name, context={"ok": health.ok, "detail": health.detail},
            )
        run.status = "COMPLETED"
        run.ended_at = datetime.now(timezone.utc)
        run.notes = "Dry run — connectivity validated, no responses stored."
        await db.commit()
        clear_cancel(run_id)
        return run

    # FR-204: every target for a question commits together. Questions run CONCURRENTLY
    # (bounded by settings.max_concurrent_questions) so the slow, network-bound LLM work
    # overlaps across questions. The run shares ONE session and SQLite is single-writer,
    # so all DB access is serialized under `db_lock`; the Chairman LLM call runs OFF the
    # lock (only its DB write is locked). NF-003 concurrency / NF-005 preemptive cancel.
    db_lock = asyncio.Lock()
    sem = asyncio.Semaphore(max(settings.max_concurrent_questions, 1))
    state = {"cancelled": False, "budget_paused": False}

    def _build_row(q: Question, intent_type: str, outcome) -> dict:
        target, result, status, error = outcome
        ptoks = result.prompt_tokens if result else 0
        ctoks = result.completion_tokens if result else 0
        sources_json = grounding_supports_json = search_queries_json = None
        if result:
            if result.sources:
                sources_json = json.dumps([
                    {"url": s.url, "title": s.title, "domain": s.domain,
                     "redirect_url": s.redirect_url, "snippet": s.snippet, "origin": s.origin}
                    for s in result.sources
                ])
            if result.grounding_supports:
                grounding_supports_json = json.dumps([
                    {"text": g.text, "source_indices": g.source_indices,
                     "start_index": g.start_index, "end_index": g.end_index}
                    for g in result.grounding_supports
                ])
            if result.search_queries:
                search_queries_json = json.dumps(result.search_queries)
        response = Response(
            response_id=str(uuid.uuid4()),
            run_id=run_id,
            llm_name=target.name,
            llm_model_version=result.model_version if result else target.model_id,
            persona=q.persona,
            question_id=q.question_id,
            question_text=q.question_text,
            therapeutic_area=q.therapeutic_area,
            indication=q.indication,
            disease=q.disease,
            brand_focus=q.brand_focus,
            monitoring_mode=q.monitoring_mode,
            competitor_focus=q.competitor_focus,
            domain=q.domain,
            intent_type=intent_type,
            response_text=(result.text if result else (error or "")),
            prompt_tokens=ptoks,
            response_tokens=ctoks,
            sources=sources_json,
            grounding_supports=grounding_supports_json,
            search_queries=search_queries_json,
            finish_reason=(result.finish_reason if result else "error"),
            status=status,
        )
        return {
            "response": response, "target": target, "status": status, "error": error,
            "total": ptoks + ctoks, "ptoks": ptoks, "ctoks": ctoks,
            "raw_status": result.raw_status if result else None,
        }

    async def process_question(q: Question) -> None:
        async with sem:
            # Cooperative cancellation / budget gate before any work for this question.
            if is_cancel_requested(run_id) or state["cancelled"] or state["budget_paused"]:
                return

            # Persona-based target routing. Resolved FIRST so a resume can bail out before
            # spending anything: when every target for this question already has a stored
            # response there is nothing to dispatch, and returning here avoids re-paying an
            # orchestrator (intent-classification) call per already-complete question —
            # which on a run interrupted late would be hundreds of calls for no new data.
            targets = targets_for_persona(q.persona)
            if targets and all((q.question_id, t.name) in done_pairs for t in targets):
                return

            # G3: final prompt-injection gate. Even though harvested questions are screened
            # at staging/promotion, this catches injection in ANY question (manual, CSV,
            # legacy) before its text reaches a target model. Flagged questions are skipped
            # (no dispatch, no partial rows) and audited for review.
            inj = scan_injection(q.question_text)
            if inj:
                async with db_lock:
                    await write_audit(
                        db, role="SYSTEM", event="QUESTION_BLOCKED_INJECTION", run_id=run_id,
                        question_id=q.question_id, context={"rules": inj}, commit=False,
                    )
                    await db.commit()
                log_with_context(logger, 30, "Question blocked: prompt-injection",
                                 run_id=run_id, question_id=q.question_id, rules=inj)
                return

            # Triage Gate: classify intent before dispatch (network — off the DB lock).
            # Bounded by wait_for so a stalled orchestrator-model call can't hold this
            # question's concurrency slot forever (NF-005); on timeout fall back to SCREENING.
            try:
                intent_result = await asyncio.wait_for(
                    classify_intent(q.question_text, q.persona, q.domain),
                    timeout=settings.target_call_timeout_seconds + 30,
                )
                intent_type = intent_result.intent
            except asyncio.TimeoutError:
                log_with_context(logger, 30, "Intent classification timed out — using SCREENING",
                                 run_id=run_id, question_id=q.question_id)
                intent_type = "SCREENING"
            # FR-108a: disease-state questions use the brand-less Landscape Matrix prompt,
            # falling back to the standard persona prompt if none is configured.
            if q.monitoring_mode == "DISEASE_STATE":
                system_prompt = disease_state_prompts.get(q.persona) or prompts.get(q.persona, "")
            else:
                system_prompt = prompts.get(q.persona, "")

            async def process_target(target: Target):
                if (q.question_id, target.name) in done_pairs:
                    return None  # resume: skip already-stored pair
                if is_cancel_requested(run_id):
                    return None  # cancel requested before dispatch — no partial row
                result, status, error = await _call_target_with_retry(
                    target, system_prompt, q.question_text, limiter, run_id
                )
                if status == "CANCELLED":
                    return None  # aborted mid-flight by cancel — no partial row
                if status == "TRUNCATED" and result is not None:
                    r2, s2, e2 = await _handle_truncation(target, system_prompt, q.question_text, limiter, run_id)
                    if r2 is not None and s2 != "CANCELLED":
                        result, status, error = r2, s2, e2
                return target, result, status, error

            # Cancel mid-flight aborts these immediately (NF-005).
            outcomes, was_cancelled = await _dispatch_targets(
                [process_target(t) for t in targets], cancel_event
            )
            if was_cancelled:
                state["cancelled"] = True
                return

            rows = [_build_row(q, intent_type, o) for o in outcomes if o is not None]
            response_objs = [r["response"] for r in rows]

            # What this run already holds for the question. Only a continuation has any: a
            # resume fills in targets that were never attempted, a retry replaces ones that
            # errored. Without them the Chairman would arbitrate over just the one or two
            # answers bought in THIS pass and overwrite (persist_consensus upserts on
            # run+question) a verdict that was reached across the full panel.
            prior: list[Response] = []
            if response_objs and any((q.question_id, t.name) in done_pairs for t in targets):
                async with db_lock:
                    prior = list((await db.execute(
                        select(Response).where(
                            Response.run_id == run_id,
                            Response.question_id == q.question_id,
                        )
                    )).scalars().all())

            # Chairman Consensus — computed OFF the DB lock (slow LLM call). Every persona
            # arbitrates inline: the Provider persona's clinical target (EvidenceMD) is a
            # normal API-backed LLM, so its response is folded into consensus like any other
            # target with no manual pause.
            consensus_result = consensus_meta = None
            if response_objs:
                # Bounded like the intent call: a stalled Chairman (arbitration) LLM call must
                # not hold this question's concurrency slot forever (NF-005). On timeout, record
                # a PARTIAL consensus so the question is still represented and the run proceeds.
                try:
                    consensus_result, consensus_meta = await asyncio.wait_for(
                        evaluate_consensus(q, prior + response_objs, intent_type),
                        timeout=settings.target_call_timeout_seconds + 30,
                    )
                except asyncio.TimeoutError:
                    log_with_context(logger, 30, "Chairman arbitration timed out — recording PARTIAL",
                                     run_id=run_id, question_id=q.question_id)
                    consensus_result = ConsensusResult(
                        consensus_level="PARTIAL",
                        agreed_recommendation=None,
                        divergence_points=["Arbitration timed out"],
                        confidence=0.0,
                        geo_fallback_used=False,
                    )
                    consensus_meta = {"model": "timeout", "tokens": 0,
                                      "responses_evaluated": len(prior) + len(response_objs)}

            # ---- DB critical section: serialized; commit all targets for this question
            # together (FR-204). Only one question holds the lock at a time. ----
            async with db_lock:
                if q.intent_type != intent_type:
                    q.intent_type = intent_type  # backfill on question record
                for r in rows:
                    db.add(r["response"])
                    run.total_tokens += r["total"]
                    run.estimated_cost_usd += estimate_cost(r["target"].model_id, r["ptoks"], r["ctoks"])
                    if r["status"] == "SUCCESS":
                        run.responses_success += 1
                    elif r["status"] == "TRUNCATED":
                        run.responses_truncated += 1
                    elif r["status"] == "BLOCKED":
                        run.responses_blocked += 1
                    else:
                        run.responses_failed += 1
                    budget.add(r["total"])
                    await write_audit(
                        db, role="TARGET", event="LLM_CALL", run_id=run_id,
                        question_id=q.question_id, llm_target=r["target"].name,
                        http_status=r["raw_status"], tokens=r["total"],
                        context={"status": r["status"], "error": r["error"]}, commit=False,
                    )
                await db.flush()

                if consensus_result is not None:
                    # Stamp the whole question, not just this pass: after a continuation the
                    # older rows carry the verdict the panel reached without them.
                    for sr in prior + response_objs:
                        sr.consensus_level = consensus_result.consensus_level
                    await persist_consensus(db, run_id, q, consensus_result, consensus_meta)
                    # The run-level tallies are NOT incremented here. A continued question is
                    # arbitrated twice and its record is upserted, so counting each
                    # arbitration would report more questions than the run holds. They are
                    # rebuilt from the records when the run ends.

                await db.commit()
                if budget.exceeded():
                    state["budget_paused"] = True

    # return_exceptions is load-bearing: without it the FIRST question to raise propagates
    # straight out of gather, the other in-flight questions keep writing to a session that
    # is about to be closed, and an entire run is lost to one bad row. Collect instead, so
    # a failed question costs that question and nothing else.
    outcomes = await asyncio.gather(
        *(process_question(q) for q in questions), return_exceptions=True
    )
    question_errors: list[BaseException] = []
    for outcome in outcomes:
        if not isinstance(outcome, BaseException):
            continue
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome  # real task cancellation (shutdown) must not be swallowed
        question_errors.append(outcome)
        log_with_context(
            logger, 40, "Question failed (skipped, run continues)",
            run_id=run_id, error=f"{type(outcome).__name__}: {outcome}",
        )
    if question_errors:
        # A DB-layer failure can leave the shared session mid-transaction, which would make
        # every write below raise. Clear it before finalizing; per-question work is already
        # committed, so there is nothing to lose.
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        await write_audit(
            db, role="SYSTEM", event="RUN_QUESTION_ERRORS", run_id=run_id,
            context={"count": len(question_errors), "first": str(question_errors[0])[:500]},
        )

    # Cancel may have arrived during dispatch of any question.
    if state["cancelled"] or is_cancel_requested(run_id):
        return await _finalize_cancelled(db, run, run_id)

    if state["budget_paused"]:
        run.status = "PAUSED_BUDGET"
        run.ended_at = datetime.now(timezone.utc)
        run.notes = f"Paused: token budget {settings.max_tokens_per_run} exceeded."
        await refresh_run_consensus_counters(db, run_id)  # commits
        await db.commit()
        await write_audit(db, role="SYSTEM", event="RUN_PAUSED_BUDGET", run_id=run_id,
                          context={"used": budget.used})
        log_with_context(logger, 30, "Run paused (budget)", run_id=run_id, used=budget.used)
        clear_cancel(run_id)
        return run

    run.status = "COMPLETED"
    run.ended_at = datetime.now(timezone.utc)
    if question_errors:
        run.notes = (
            f"Completed with {len(question_errors)} question(s) skipped after errors. "
            f"First: {str(question_errors[0])[:500]}"
        )
    # Tallied from the records rather than incremented per arbitration, so a run that was
    # resumed or had failures retried reports one verdict per question, not one per pass.
    await refresh_run_consensus_counters(db, run_id)  # commits
    await db.commit()
    await write_audit(db, role="SYSTEM", event="RUN_COMPLETE", run_id=run_id, context={
        "success": run.responses_success, "failed": run.responses_failed,
        "truncated": run.responses_truncated, "blocked": run.responses_blocked,
        "tokens": run.total_tokens, "cost_usd": run.estimated_cost_usd,
    })
    log_with_context(logger, 20, "Run completed", run_id=run_id,
                     success=run.responses_success, failed=run.responses_failed)
    clear_cancel(run_id)
    return run
