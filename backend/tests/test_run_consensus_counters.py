"""Run-level consensus tallies, and why they are rebuilt rather than incremented.

A question can be arbitrated more than once in the same run. A resume arbitrates a
question again once its missing targets answer; a failed-response retry does the same.
`_persist_consensus` UPSERTS on (run_id, question_id), so the second arbitration replaces
the record instead of adding one. An incrementing tally counted it as a second question,
which is how a run could report more Full/Partial/Missing verdicts than it holds
questions. The tally now has one owner, `chairman.refresh_run_consensus_counters`, and it
derives the numbers from the records.
"""
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.chairman import (
    ConsensusResult,
    evaluate_consensus,
    persist_consensus,
    refresh_run_consensus_counters,
)
from app.models.consensus import ConsensusRecord
from app.models.database import Base
from app.models.question import Question
from app.models.response import Response
from app.models.run import Run


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from app.models import consensus, response, run  # noqa: F401 — register tables

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _run(**counters) -> Run:
    return Run(run_id="r-1", trigger="ADHOC", monitoring_mode="BRAND", status="RUNNING",
               **counters)


def _record(qid: str, level: str, run_id: str = "r-1") -> ConsensusRecord:
    return ConsensusRecord(
        consensus_id=f"c-{run_id}-{qid}", run_id=run_id, question_id=qid,
        consensus_level=level,
    )


def _question(qid: str = "Q1") -> Question:
    """Transient, not session-added: _persist_consensus only reads question_id."""
    return Question(
        question_id=qid, question_text="How much does it cost?", persona="Patient",
        therapeutic_area="Rheumatology", brand_focus="Rinvoq", domain="Access",
    )


def _result(level: str) -> ConsensusResult:
    return ConsensusResult(
        consensus_level=level, agreed_recommendation=None, divergence_points=[],
        confidence=0.9, geo_fallback_used=False,
    )


async def test_counters_are_derived_from_the_records(db_session):
    db_session.add_all([
        _run(),
        _record("Q1", "FULL"), _record("Q2", "FULL"), _record("Q3", "PARTIAL"),
        _record("Q4", "MISSING"),
        _record("Q5", "FULL", run_id="r-2"),  # another run's verdict is not this run's
    ])
    await db_session.commit()

    await refresh_run_consensus_counters(db_session, "r-1")

    run = await db_session.get(Run, "r-1")
    assert (run.consensus_full, run.consensus_partial, run.consensus_missing) == (2, 1, 1)


async def test_a_tally_inflated_by_an_earlier_resume_is_healed(db_session):
    """The state prod is already in: runs that were resumed under the incrementing tally
    carry counts above the number of questions they hold. Re-finalizing corrects them."""
    db_session.add_all([
        _run(consensus_full=145, consensus_partial=31, consensus_missing=1),
        _record("Q1", "FULL"), _record("Q2", "FULL"),
    ])
    await db_session.commit()

    await refresh_run_consensus_counters(db_session, "r-1")

    run = await db_session.get(Run, "r-1")
    assert (run.consensus_full, run.consensus_partial, run.consensus_missing) == (2, 0, 0)


async def test_a_question_arbitrated_twice_is_still_one_question(db_session):
    """The exact sequence a retry produces: the question is arbitrated once when the run
    first passes over it, and again after its failed target finally answers."""
    db_session.add(_run())
    await db_session.commit()
    meta = {"model": "test", "tokens": 0, "responses_evaluated": 4}

    await persist_consensus(db_session, "r-1", _question(), _result("PARTIAL"), meta)
    await db_session.commit()
    await persist_consensus(db_session, "r-1", _question(), _result("FULL"), meta)
    await db_session.commit()
    await refresh_run_consensus_counters(db_session, "r-1")

    run = await db_session.get(Run, "r-1")
    # The later verdict replaces the earlier one; it does not add to it.
    assert (run.consensus_full, run.consensus_partial, run.consensus_missing) == (1, 0, 0)


async def test_a_run_with_no_records_reports_no_verdicts(db_session):
    db_session.add(_run(consensus_full=3))
    await db_session.commit()

    await refresh_run_consensus_counters(db_session, "r-1")

    run = await db_session.get(Run, "r-1")
    assert (run.consensus_full, run.consensus_partial, run.consensus_missing) == (0, 0, 0)


async def test_a_missing_run_is_not_an_error(db_session):
    """Called on every finalization path, so it must never be the thing that fails one."""
    await refresh_run_consensus_counters(db_session, "does-not-exist")


def _response(llm: str) -> Response:
    return Response(
        response_id=f"x-{llm}", run_id="r-1", question_id="Q1", llm_name=llm,
        persona="Patient", question_text="How much does it cost?",
        therapeutic_area="Rheumatology", domain="Access",
        response_text="It depends on coverage.", status="SUCCESS",
    )


async def test_one_answer_alone_cannot_carry_a_verdict():
    """Why the orchestrator now arbitrates a continued question over its STORED responses
    plus the new ones. A retry typically buys back a single answer, and the Chairman
    refuses to call consensus on fewer than two: arbitrating that answer on its own would
    overwrite a verdict the full panel had already reached with a bare MISSING."""
    result, meta = await evaluate_consensus(_question(), [_response("Nova-Pro")], "CLINICAL")

    assert result.consensus_level == "MISSING"
    assert meta["responses_evaluated"] == 1
    assert "Insufficient" in result.divergence_points[0]


async def test_the_full_panel_is_what_gets_evaluated():
    """Same single new answer, now carrying the four responses the run already stored."""
    prior = [_response(n) for n in ("Claude", "Llama", "Gemini", "GPT-4o")]

    result, meta = await evaluate_consensus(
        _question(), prior + [_response("Nova-Pro")], "SHORTHAND",
    )

    # SHORTHAND short-circuits before any model call, so this asserts the input set only.
    assert meta["responses_evaluated"] == 5
    assert result.consensus_level == "FULL"
