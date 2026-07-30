"""Retrying a run's FAILED responses in place.

Resume and retry answer different questions. Resume dispatches the (question, target)
pairs that were never attempted; retry dispatches the ones that WERE attempted and
errored. A run can finish COMPLETED and still be worth retrying, which is why retry is
not keyed on run status the way resume is.

The mechanism rests on one fact: a response is unique per (run_id, question_id, llm_name)
and execute_run treats ANY stored row for a pair as done. So a FAILED row both blocks the
re-insert and marks the pair complete, and clearing it is what makes the pair eligible
again. These tests pin that behaviour, and pin what must NOT be cleared with it.
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.cancellation import clear_cancel, is_cancel_requested, request_cancel
from app.agent.orchestrator import _existing_pairs
from app.models.database import Base
from app.models.response import Response
from app.models.run import Run
from app.services.run_service import (
    count_failed_responses,
    mark_retrying_failed,
    retry_failed_blocker,
)

SNAPSHOT = '{"monitoring_mode": "BRAND", "filters": {"persona": "Patient"}}'


def _run(status: str, snapshot: str | None = SNAPSHOT, failed: int = 49) -> Run:
    """The shape prod produced: a large run that stopped holding both answers and errors."""
    return Run(
        run_id="r-1",
        trigger="ADHOC",
        monitoring_mode="BRAND",
        status=status,
        questions_attempted=823,
        responses_success=858,
        responses_failed=failed,
        responses_truncated=0,
        responses_blocked=0,
        total_tokens=2_071_228,
        estimated_cost_usd=14.3849,
        config_snapshot=snapshot,
    )


def _resp(rid: str, run_id: str, llm: str, status: str, question_id: str = "Q1") -> Response:
    return Response(
        response_id=rid, run_id=run_id, question_id=question_id, llm_name=llm,
        persona="Patient", question_text="How much does it cost?",
        therapeutic_area="Rheumatology", domain="Access",
        response_text="..." if status != "FAILED" else "TimeoutError: read timed out",
        status=status,
    )


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from app.models import audit_log, response, run  # noqa: F401 — register tables

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


# ------------------------------------------------------------------------- blockers


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "CANCELLED", "PAUSED_BUDGET"])
def test_any_stopped_run_with_failures_can_be_retried(status):
    """Deliberately not keyed on status: a COMPLETED run that finished with 49 errors has
    exactly the same recoverable work as a cancelled one."""
    assert retry_failed_blocker(_run(status)) is None


def test_a_running_run_is_not_retried():
    blocker = retry_failed_blocker(_run("RUNNING"))
    assert blocker is not None
    assert "already in progress" in blocker


def test_a_run_awaiting_openevidence_is_not_reopened():
    """Reopening it would discard the pause it is sitting in, so the message says what to
    do first rather than silently taking the run out of the clinician's hands."""
    blocker = retry_failed_blocker(_run("AWAITING_OPENEVIDENCE"))
    assert blocker is not None
    assert "OpenEvidence" in blocker


@pytest.mark.parametrize("snapshot", [None, "{not json"])
def test_retry_needs_a_readable_snapshot(snapshot):
    """Without it the question set cannot be reconstructed, so the retry would dispatch a
    DIFFERENT set of questions under the same run_id."""
    blocker = retry_failed_blocker(_run("COMPLETED", snapshot=snapshot))
    assert blocker is not None
    assert "Re-run" in blocker


# ------------------------------------------------------------------------- counting


async def test_failures_are_counted_from_the_rows_not_the_tally(db_session):
    """run.responses_failed is a running tally that a retry mutates. The decision to spend
    money has to be made against what is really stored, or a drifted counter either
    reopens a run with nothing to do or refuses one that has work."""
    db_session.add_all([
        _resp("x1", "r-1", "Claude", "SUCCESS"),
        _resp("x2", "r-1", "Nova-Pro", "FAILED"),
        _resp("x3", "r-1", "Llama", "FAILED"),
        _resp("x4", "r-2", "Gemini", "FAILED"),  # a different run
    ])
    run = _run("COMPLETED", failed=99)  # deliberately wrong
    db_session.add(run)
    await db_session.commit()

    assert await count_failed_responses(db_session, "r-1") == 2


async def test_a_run_with_no_failed_rows_counts_zero(db_session):
    db_session.add(_resp("x1", "r-1", "Claude", "SUCCESS"))
    await db_session.commit()

    assert await count_failed_responses(db_session, "r-1") == 0


# --------------------------------------------------------------------------- clearing


async def test_only_this_run_s_failed_rows_are_cleared(db_session):
    """BLOCKED is left alone on purpose (a safety refusal refuses again), TRUNCATED has its
    own continuation path, and another run's failure is not this run's business."""
    db_session.add_all([
        _resp("x1", "r-1", "Claude", "SUCCESS"),
        _resp("x2", "r-1", "Nova-Pro", "FAILED"),
        _resp("x3", "r-1", "Llama", "BLOCKED"),
        _resp("x4", "r-1", "Gemini", "TRUNCATED"),
        _resp("x5", "r-2", "Claude", "FAILED"),
    ])
    run = _run("COMPLETED", failed=1)
    db_session.add(run)
    await db_session.commit()

    cleared = await mark_retrying_failed(db_session, run)

    assert cleared == 1
    remaining = set((await db_session.execute(
        select(Response.response_id, Response.run_id)
    )).all())
    assert remaining == {("x1", "r-1"), ("x3", "r-1"), ("x4", "r-1"), ("x5", "r-2")}


async def test_the_cleared_pair_stops_counting_as_done(db_session):
    """The contract the retry rests on: execute_run skips any pair in _existing_pairs, so
    the failed pair has to leave that set while every captured answer stays in it."""
    db_session.add_all([
        _resp("x1", "r-1", "Claude", "SUCCESS"),
        _resp("x2", "r-1", "Nova-Pro", "FAILED"),
    ])
    run = _run("COMPLETED", failed=1)
    db_session.add(run)
    await db_session.commit()

    assert await _existing_pairs(db_session, "r-1") == {("Q1", "Claude"), ("Q1", "Nova-Pro")}
    await mark_retrying_failed(db_session, run)
    assert await _existing_pairs(db_session, "r-1") == {("Q1", "Claude")}


async def test_the_run_reopens_without_discarding_captured_work(db_session):
    db_session.add_all([
        _resp("x1", "r-1", "Claude", "SUCCESS"),
        _resp("x2", "r-1", "Nova-Pro", "FAILED"),
    ])
    run = _run("COMPLETED", failed=1)
    run.notes = "Cancelled by operator."
    db_session.add(run)
    await db_session.commit()

    await mark_retrying_failed(db_session, run)

    assert run.status == "RUNNING"
    assert run.ended_at is None             # otherwise the UI shows it as already finished
    assert run.responses_failed == 0        # the row it counted is gone
    assert run.responses_success == 858     # answers already bought are untouched
    assert "Retrying 1" in run.notes
    assert "Cancelled by operator." in run.notes  # why it stopped stays legible


async def test_spend_that_happened_is_not_rewritten(db_session):
    """total_tokens re-seeds the budget guard and estimated_cost_usd is the money actually
    spent. A retry buys more; it does not un-buy what failed."""
    db_session.add(_resp("x2", "r-1", "Nova-Pro", "FAILED"))
    run = _run("COMPLETED", failed=1)
    db_session.add(run)
    await db_session.commit()

    await mark_retrying_failed(db_session, run)

    assert run.total_tokens == 2_071_228
    assert run.estimated_cost_usd == 14.3849


async def test_a_second_click_is_refused_rather_than_run_twice(db_session):
    db_session.add(_resp("x2", "r-1", "Nova-Pro", "FAILED"))
    run = _run("COMPLETED", failed=1)
    db_session.add(run)
    await db_session.commit()

    assert retry_failed_blocker(run) is None
    await mark_retrying_failed(db_session, run)
    assert "already in progress" in (retry_failed_blocker(run) or "")


async def test_retrying_a_cancelled_run_clears_its_own_stop_request(db_session):
    """The cancel flag lives in an in-process registry, not the DB. Left set, it would
    abort the retry on the orchestrator's first check: the run would flip to RUNNING,
    dispatch nothing, and land straight back in CANCELLED."""
    db_session.add(_resp("x2", "r-1", "Nova-Pro", "FAILED"))
    run = _run("CANCELLED", failed=1)
    db_session.add(run)
    await db_session.commit()
    request_cancel(run.run_id)
    assert is_cancel_requested(run.run_id)

    try:
        await mark_retrying_failed(db_session, run)
        assert not is_cancel_requested(run.run_id)
    finally:
        clear_cancel(run.run_id)  # never leak a flag into another test
