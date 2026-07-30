"""Resume + deploy-lock guards.

Regression cover for the incident where an auto-deploy replaced the container 3m49s into
an 823-question run: 244 responses were kept but the run was swept to FAILED and the only
recovery was a full Re-run that re-paid for all of them.

Two independent guarantees are tested here:
  1. an interrupted run is RESUMABLE in place (same run_id), and the statuses that must
     NOT be resumable stay blocked with an explanation, and
  2. the deploy marker written by scripts/ec2_deploy.sh is observed by the backend, and a
     stale one can never wedge the platform.
"""
import os
import time
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.cancellation import clear_cancel, is_cancel_requested, request_cancel
from app.agent.orchestrator import _existing_pairs
from app.models.database import Base
from app.models.response import Response
from app.models.run import Run
from app.services import deploy_lock
from app.services.run_service import RESUMABLE_STATUSES, mark_resuming, resume_blocker

SNAPSHOT = '{"monitoring_mode": "BRAND", "filters": {"persona": "Patient"}}'


def _run(status: str, snapshot: str | None = SNAPSHOT, success: int = 244) -> Run:
    return Run(
        run_id="r-1",
        trigger="ADHOC",
        monitoring_mode="BRAND",
        status=status,
        questions_attempted=823,
        responses_success=success,
        responses_failed=0,
        responses_truncated=0,
        responses_blocked=0,
        total_tokens=2_071_228,
        estimated_cost_usd=4.0396,
        config_snapshot=snapshot,
    )


# --------------------------------------------------------------------------- resume


@pytest.mark.parametrize("status", RESUMABLE_STATUSES)
def test_interrupted_run_is_resumable(status):
    """The exact shape prod produced: a partially complete run with a config snapshot."""
    assert resume_blocker(_run(status)) is None


def test_running_run_is_not_resumable():
    blocker = resume_blocker(_run("RUNNING"))
    assert blocker is not None
    assert "already in progress" in blocker


@pytest.mark.parametrize("status", ["COMPLETED", "AWAITING_OPENEVIDENCE"])
def test_non_recoverable_statuses_are_blocked_and_point_at_rerun(status):
    """A finished run (or one parked for clinician input) must not be silently restarted.
    The message is shown verbatim to the operator, so it has to say what to do instead."""
    blocker = resume_blocker(_run(status))
    assert blocker is not None
    assert "Re-run" in blocker


def test_an_operator_stop_is_a_pause_not_a_dead_end():
    """Stopping a run and later wanting the rest of it is an ordinary sequence. Re-run is
    not a substitute: it re-pays for every response the cancelled run already bought."""
    assert "CANCELLED" in RESUMABLE_STATUSES
    assert resume_blocker(_run("CANCELLED")) is None


def test_run_without_snapshot_cannot_be_resumed():
    """Without the snapshot the original question set cannot be reconstructed, so resuming
    would silently run a DIFFERENT set of questions under the same run_id."""
    blocker = resume_blocker(_run("FAILED", snapshot=None))
    assert blocker is not None
    assert "Re-run" in blocker


def test_run_with_unreadable_snapshot_cannot_be_resumed():
    blocker = resume_blocker(_run("FAILED", snapshot="{not json"))
    assert blocker is not None
    assert "Re-run" in blocker


# ----------------------------------------------------------------------- deploy lock


@pytest.fixture
def lock_path(tmp_path, monkeypatch):
    """Point the lock at a temp file, bypassing the SQLite-directory derivation."""
    path = tmp_path / ".deploy-in-progress"
    monkeypatch.setattr(deploy_lock, "deploy_lock_path", lambda: path)
    return path


def test_not_deploying_when_marker_absent(lock_path):
    assert deploy_lock.is_deploying() is False


def test_deploying_when_marker_present(lock_path):
    lock_path.touch()
    assert deploy_lock.is_deploying() is True


def test_stale_marker_is_ignored(lock_path):
    """A deploy script hard-killed before its EXIT trap ran would otherwise leave the
    platform permanently unable to start a run — worse than the failure being prevented."""
    lock_path.touch()
    old = time.time() - (deploy_lock.STALE_AFTER_SECONDS + 60)
    os.utime(lock_path, (old, old))
    assert deploy_lock.is_deploying() is False


def test_lock_check_never_raises(monkeypatch):
    """A lock-check problem must degrade to "not deploying", never take the API down."""
    def _boom():
        raise OSError("filesystem gone")

    monkeypatch.setattr(deploy_lock, "deploy_lock_path", _boom)
    assert deploy_lock.is_deploying() is False


def test_lock_path_derives_from_sqlite_data_dir(monkeypatch):
    """The deploy writes the marker into the host dir bind-mounted at /app/data, which is
    where the SQLite file lives — so prod needs no extra env var for the two to agree."""
    from app.config import settings as settings_mod

    class _S:
        deploy_lock_path = ""
        database_url = "sqlite+aiosqlite:////app/data/evidence_monitoring.db"

    monkeypatch.setattr(settings_mod, "get_settings", lambda: _S())
    monkeypatch.setattr(deploy_lock, "get_settings", lambda: _S())
    resolved = deploy_lock.deploy_lock_path()
    assert resolved is not None
    assert resolved.name == deploy_lock.LOCK_FILENAME
    assert resolved.parent.as_posix().endswith("/app/data")


def test_lock_path_honours_explicit_override(monkeypatch):
    class _S:
        deploy_lock_path = "/tmp/custom-lock"
        database_url = "sqlite+aiosqlite:///./evidence_monitoring.db"

    monkeypatch.setattr(deploy_lock, "get_settings", lambda: _S())
    assert deploy_lock.deploy_lock_path().as_posix() == "/tmp/custom-lock"


# ------------------------------------------------------------------ resume mechanics


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


async def test_existing_pairs_keys_match_the_skip_predicate(db_session):
    """The contract the whole resume feature rests on.

    _existing_pairs returns (question_id, llm_name), and BOTH skip checks in the
    orchestrator look up (question.question_id, target.name). If those key shapes ever
    drift apart the lookup silently misses, and a resume re-dispatches — and re-pays for —
    work that is already stored. Locked down here because nothing else covers it.
    """
    def _resp(rid: str, run_id: str, llm: str) -> Response:
        return Response(
            response_id=rid, run_id=run_id, question_id="Q1", llm_name=llm,
            persona="Patient", question_text="How much does it cost?",
            therapeutic_area="Rheumatology", domain="Access",
            response_text="...", status="SUCCESS",
        )

    db_session.add_all([
        _resp("x1", "r-1", "Claude"),
        _resp("x2", "r-1", "Nova-Pro"),
        _resp("x3", "r-2", "Llama"),  # a different run
    ])
    await db_session.commit()

    pairs = await _existing_pairs(db_session, "r-1")

    assert pairs == {("Q1", "Claude"), ("Q1", "Nova-Pro")}
    assert ("Q1", "Llama") not in pairs  # another run's work must not be treated as done


async def test_mark_resuming_reopens_run_without_discarding_captured_work(db_session):
    """Resuming must not reset the counters: the tokens and responses already paid for are
    what make a resume cheaper than a re-run, and total_tokens re-seeds the budget."""
    run = _run("FAILED")
    run.ended_at = datetime(2026, 7, 28, 4, 6, 11, tzinfo=timezone.utc)
    run.notes = "Interrupted by server restart (run did not resume)."
    db_session.add(run)
    await db_session.commit()

    await mark_resuming(db_session, run)

    assert run.status == "RUNNING"
    assert run.ended_at is None            # otherwise the UI shows it as already finished
    assert run.responses_success == 244    # nothing already captured is thrown away
    assert run.total_tokens == 2_071_228   # re-seeds the budget, so spend is not double-counted
    assert "244" in run.notes
    assert "Interrupted by server restart" in run.notes  # original cause still legible


async def test_resuming_a_cancelled_run_clears_its_own_stop_request(db_session):
    """The failure mode unique to resuming a CANCELLED run.

    The cancel flag lives in an in-process registry, not the DB. A resume that left it set
    would be aborted by the operator's earlier stop on the orchestrator's first check,
    before a single question dispatched: the run would flip to RUNNING, do nothing, and
    land back in CANCELLED."""
    run = _run("CANCELLED")
    db_session.add(run)
    await db_session.commit()
    request_cancel(run.run_id)
    assert is_cancel_requested(run.run_id)

    try:
        await mark_resuming(db_session, run)
        assert not is_cancel_requested(run.run_id)
        assert run.status == "RUNNING"
    finally:
        clear_cancel(run.run_id)  # never leak a flag into another test


async def test_resume_blocker_clears_once_a_run_is_reopened(db_session):
    """A resumed run is RUNNING, so a second Resume click is refused rather than starting
    a duplicate execution of the same run_id."""
    run = _run("FAILED")
    db_session.add(run)
    await db_session.commit()

    assert resume_blocker(run) is None
    await mark_resuming(db_session, run)
    assert "already in progress" in (resume_blocker(run) or "")
