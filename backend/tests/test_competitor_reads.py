"""Competitor reads: a rival is never a ``brand_focus``.

The bug these cover: asking the copilot to summarise a competitor's sentiment made it
filter ``brand_focus="Tremfya"``, which is structurally guaranteed to return zero rows —
that column holds the monitored AbbVie brand. It then fell back to the whole-corpus
sentiment figure and invented an explanation ("their questions haven't been run yet").

So the tests here assert three separate things:
1. the axis itself (``brand_focus`` really cannot match a competitor),
2. the reads that CAN answer the question (mention rollup, head-to-head, landscape,
   and the ``competitor`` response filter), and
3. the counting honesty those reads promise — aliases collapse, ``mentioned: false`` is
   not a mention, and a mean always arrives with the number of mentions behind it.

The copilot read tools open their OWN ``AsyncSessionLocal``, so the fixture repoints that
factory at a shared in-memory SQLite DB (StaticPool keeps both sessions on one connection).
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.database import Base

# Import every model so the full schema registers before create_all (mirrors init_db).
from app.models import (  # noqa: F401
    alert,
    audit_log,
    consensus,
    harvested_question,
    question,
    response,
    response_diff,
    run,
    scoring,
)
from app.competitive import mentions as mentions_mod
from app.copilot.tools import read_tools
from app.copilot.tools.registry import TOOLS, anthropic_tool_schemas
from app.models.response import Response
from app.models.scoring import ScoringRecord
from app.services import response_service

BASE_TS = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def maker(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(read_tools, "AsyncSessionLocal", factory)
    # Force the SQLite path. When Snowflake is configured the analytics KPIs are served
    # from the warehouse, which would (a) make this suite non-hermetic and dependent on
    # live credentials and (b) hide the exact bug pinned below, since a warehouse answer
    # never touches the mis-bound session argument. Patched on `fallback` rather than on
    # `client`, because fallback.py binds the name at import time.
    monkeypatch.setattr("app.snowflake.fallback.is_enabled", lambda: False)
    # `mentions` binds is_enabled at import time too, and it decides which store-disclosure
    # branch runs. Pinned off by default so the assertions do not depend on whether the
    # machine running the suite happens to have Snowflake credentials.
    monkeypatch.setattr(mentions_mod, "is_enabled", lambda: False)
    yield factory
    await engine.dispose()


async def _add(
    factory,
    *,
    response_id,
    llm_name="claude",
    persona="Provider",
    brand_focus="Skyrizi",
    therapeutic_area="Dermatology",
    domain="Efficacy",
    monitoring_mode="BRAND",
    run_id="RUN-1",
    question_id=None,
    mentions=None,
    position="AMONG_OPTIONS",
    sentiment=0.4,
    scored=True,
    offset_minutes=0,
):
    """Seed one response and (optionally) its latest score."""
    async with factory() as db:
        db.add(Response(
            response_id=response_id,
            run_id=run_id,
            timestamp_utc=BASE_TS + timedelta(minutes=offset_minutes),
            llm_name=llm_name,
            persona=persona,
            question_id=question_id or f"Q-{response_id}",
            question_text=f"question for {response_id}",
            therapeutic_area=therapeutic_area,
            brand_focus=brand_focus,
            domain=domain,
            monitoring_mode=monitoring_mode,
            response_text="an answer",
            status="SUCCESS",
        ))
        if scored:
            db.add(ScoringRecord(
                score_id=f"S-{response_id}",
                response_id=response_id,
                score_version=1,
                sentiment_score=sentiment,
                competitive_position=position,
                brand_mentions=json.dumps(mentions or []),
                key_claims=json.dumps(["a claim"]),
                scoring_rationale="because",
            ))
        await db.commit()


# =================================================================================
# The naming rule (pure)
# =================================================================================
def test_a_generic_name_matches_its_brand():
    assert mentions_mod.mention_matches("guselkumab", "Tremfya")
    assert mentions_mod.mention_matches("TREMFYA", "tremfya")
    assert not mentions_mod.mention_matches("Stelara", "Tremfya")
    assert not mentions_mod.mention_matches("", "Tremfya")
    assert not mentions_mod.mention_matches("Tremfya", "")


def test_aliases_collapse_onto_one_canonical_agent():
    assert mentions_mod.canonical_agent("guselkumab") == "Tremfya"
    assert mentions_mod.canonical_agent("upadacitinib") == "Rinvoq"
    # An agent nobody has curated is kept verbatim rather than dropped — an unknown rival
    # showing up in answers is exactly the thing worth seeing.
    assert mentions_mod.canonical_agent("Notadrug") == "Notadrug"


def test_ownership_comes_from_the_config_not_the_scorer():
    assert mentions_mod.side_of("Tremfya") == mentions_mod.SIDE_COMPETITOR
    assert mentions_mod.side_of("Rinvoq") == mentions_mod.SIDE_OURS
    assert mentions_mod.side_of("Notadrug") == mentions_mod.SIDE_UNTRACKED


def test_a_landscape_row_the_answer_never_raised_is_not_a_mention():
    """``mentioned: false`` is evidence of ABSENCE. Counting it would invent share of voice."""
    payload = [{"brand": "Tremfya", "mentioned": False, "sentiment": 0.0}]
    assert not mentions_mod.names_agent(payload, "Tremfya")
    assert mentions_mod.names_agent([{"brand": "Tremfya", "sentiment": 0.5}], "Tremfya")


# =================================================================================
# The axis that caused the bug
# =================================================================================
async def test_a_competitor_can_never_be_a_brand_focus(maker):
    """The original failure, pinned: the filter the copilot reached for returns nothing."""
    await _add(maker, response_id="R1", brand_focus="Skyrizi",
               mentions=[{"brand": "Tremfya", "is_competitor": True, "sentiment": 0.6}])
    async with maker() as db:
        data = await response_service.query_responses(db, brand_focus="Tremfya")
    assert data["total"] == 0

    # ...while the answer plainly discusses them.
    async with maker() as db:
        named = await response_service.query_responses(db, competitor="Tremfya")
    assert named["total"] == 1
    assert named["items"][0]["competitor_sentiment"] == 0.6
    assert named["items"][0]["competitor"] == "Tremfya"


async def test_the_competitor_filter_is_alias_aware_and_totals_honestly(maker):
    await _add(maker, response_id="R1",
               mentions=[{"brand": "guselkumab", "sentiment": 0.2}])
    await _add(maker, response_id="R2",
               mentions=[{"brand": "Stelara", "sentiment": 0.1}])
    await _add(maker, response_id="R3", mentions=[])
    async with maker() as db:
        data = await response_service.query_responses(db, competitor="Tremfya", limit=1)
    # `total` describes the filtered set, not the whole table — the filter is resolved
    # before the count, unlike the post-projection sentiment filters.
    assert data["total"] == 1
    assert data["count"] == 1
    assert data["items"][0]["response_id"] == "R1"


async def test_an_unmentioned_competitor_returns_an_empty_page_not_an_error(maker):
    await _add(maker, response_id="R1", mentions=[{"brand": "Stelara", "sentiment": 0.1}])
    async with maker() as db:
        data = await response_service.query_responses(db, competitor="Tremfya")
    assert data == {"total": 0, "count": 0, "items": []}


# =================================================================================
# The rollup
# =================================================================================
async def test_rollup_ranks_every_agent_the_models_named(maker):
    await _add(maker, response_id="R1", llm_name="claude", mentions=[
        {"brand": "Tremfya", "sentiment": 0.6},
        {"brand": "Skyrizi", "sentiment": 0.8},
    ])
    await _add(maker, response_id="R2", llm_name="gpt-4o", mentions=[
        {"brand": "guselkumab", "sentiment": 0.2},
    ])
    async with maker() as db:
        data = await mentions_mod.rollup(db)

    by_agent = {a["agent"]: a for a in data["agents"]}
    # Both spellings folded into one row.
    assert by_agent["Tremfya"]["answers_naming_it"] == 2
    assert sorted(by_agent["Tremfya"]["spellings"]) == ["Tremfya", "guselkumab"]
    assert by_agent["Tremfya"]["side"] == "COMPETITOR"
    assert by_agent["Tremfya"]["avg_sentiment"] == 0.4
    assert by_agent["Tremfya"]["sentiment_n"] == 2
    assert by_agent["Skyrizi"]["side"] == "OURS"
    # Ranked by exposure, so the most-named agent leads.
    assert data["agents"][0]["agent"] == "Tremfya"


async def test_rollup_reports_unscored_answers_rather_than_hiding_them(maker):
    """Scoring is best-effort after a run, so a quiet rollup must not look like a quiet market."""
    await _add(maker, response_id="R1", mentions=[{"brand": "Tremfya", "sentiment": 0.5}])
    await _add(maker, response_id="R2", scored=False)
    async with maker() as db:
        data = await mentions_mod.rollup(db)
    assert data["answers_total"] == 2
    assert data["answers_scored"] == 1
    assert data["answers_unscored"] == 1


async def test_rollup_does_not_count_a_landscape_row_that_was_never_raised(maker):
    await _add(maker, response_id="R1", monitoring_mode="DISEASE_STATE", brand_focus=None,
               position="LANDSCAPE", mentions=[
                   {"brand": "Tremfya", "mentioned": False, "sentiment": 0.0},
                   {"brand": "Stelara", "mentioned": True, "sentiment": 0.3,
                    "position": "FIRST_LINE_RECOMMENDED"},
               ])
    async with maker() as db:
        data = await mentions_mod.rollup(db)
    by_agent = {a["agent"]: a for a in data["agents"]}
    assert by_agent["Tremfya"]["answers_naming_it"] == 0
    assert by_agent["Tremfya"]["considered_but_not_named_answers"] == 1
    assert by_agent["Stelara"]["answers_naming_it"] == 1
    # Positions only exist in landscape scoring, and they are carried through.
    assert by_agent["Stelara"]["positions"] == {"FIRST_LINE_RECOMMENDED": 1}


async def test_rollup_scope_narrows_to_one_area(maker):
    await _add(maker, response_id="R1", therapeutic_area="Dermatology",
               mentions=[{"brand": "Tremfya", "sentiment": 0.5}])
    await _add(maker, response_id="R2", therapeutic_area="Gastroenterology",
               mentions=[{"brand": "Tremfya", "sentiment": -0.5}])
    async with maker() as db:
        data = await mentions_mod.rollup(db, therapeutic_area="Gastroenterology")
    assert data["answers_scored"] == 1
    assert data["agents"][0]["avg_sentiment"] == -0.5


async def test_rollup_can_isolate_agents_missing_from_the_config(maker):
    await _add(maker, response_id="R1", mentions=[
        {"brand": "Tremfya", "sentiment": 0.5},
        {"brand": "Notadrug", "sentiment": 0.9},
    ])
    async with maker() as db:
        data = await mentions_mod.rollup(db, side="UNTRACKED")
    assert [a["agent"] for a in data["agents"]] == ["Notadrug"]


# =================================================================================
# One agent in full
# =================================================================================
async def test_agent_detail_breaks_the_mentions_down_and_samples_the_worst(maker):
    await _add(maker, response_id="R1", llm_name="claude", persona="Provider",
               offset_minutes=0, mentions=[{"brand": "Tremfya", "sentiment": 0.8}])
    await _add(maker, response_id="R2", llm_name="gpt-4o", persona="Patient",
               offset_minutes=1, mentions=[{"brand": "Tremfya", "sentiment": -0.6}])
    async with maker() as db:
        data = await mentions_mod.agent_detail(db, "guselkumab")

    assert data["found"] is True
    assert data["agent"] == "Tremfya"          # resolved from the alias the caller typed
    assert data["requested"] == "guselkumab"
    s = data["summary"]
    assert s["answers_naming_it"] == 2
    assert s["sentiment_mix"] == {"positive": 1, "neutral": 0, "negative": 1}
    assert {r["llm_name"] for r in s["by_model"]} == {"claude", "gpt-4o"}
    assert {r["persona"] for r in s["by_persona"]} == {"Provider", "Patient"}
    # Worst first: the reader is here to find where the rival is winning.
    assert data["sample_answers"][0]["response_id"] == "R2"
    assert data["sample_answers"][0]["their_sentiment"] == -0.6
    # Our brand's own read travels beside theirs so they cannot be confused.
    assert data["sample_answers"][0]["our_position"] == "AMONG_OPTIONS"


async def test_agent_detail_says_nobody_named_them_instead_of_erroring(maker):
    await _add(maker, response_id="R1", mentions=[{"brand": "Stelara", "sentiment": 0.1}])
    async with maker() as db:
        data = await mentions_mod.agent_detail(db, "Tremfya")
    assert data["found"] is False
    assert data["answers_scored"] == 1
    assert "brand_focus" in data["note"]
    assert data["sample_answers"] == []


# =================================================================================
# Which store answered
# =================================================================================
async def test_a_single_store_read_says_its_denominators_are_comparable(maker):
    await _add(maker, response_id="R1", mentions=[{"brand": "Tremfya", "sentiment": 0.5}])
    async with maker() as db:
        data = await mentions_mod.rollup(db)
    corpus = data["corpus"]
    assert corpus["store"] == mentions_mod.STORE_APP_DB
    assert corpus["warehouse_enabled"] is False
    assert "comparable" in corpus["note"]


async def test_a_warehouse_beside_us_is_declared_on_every_read(maker, monkeypatch):
    """Snowflake is a batched mirror fed by every environment, so it can hold far more
    answers than this store (7.6k vs 0.9k observed). These reads never query it, so a share
    taken here must not be read against a warehouse-served KPI."""
    monkeypatch.setattr(mentions_mod, "is_enabled", lambda: True)
    await _add(maker, response_id="R1", mentions=[{"brand": "Tremfya", "sentiment": 0.5}])
    async with maker() as db:
        rolled = await mentions_mod.rollup(db)
        detail = await mentions_mod.agent_detail(db, "Tremfya")
    for data in (rolled, detail):
        assert data["corpus"]["warehouse_enabled"] is True
        assert "do NOT query it" in data["corpus"]["note"]
        assert "sentiment_distribution" in data["corpus"]["note"]


async def test_the_caveat_reaches_the_line_the_model_narrates(maker, monkeypatch):
    """The payload alone is not enough — the summary is what gets read back to the user."""
    monkeypatch.setattr(mentions_mod, "is_enabled", lambda: True)
    await _add(maker, response_id="R1", mentions=[{"brand": "Tremfya", "sentiment": 0.5}])

    named = await read_tools.get_competitor_mentions(
        read_tools.GetCompetitorMentionsInput(agent="Tremfya"))
    ranked = await read_tools.get_competitor_mentions(
        read_tools.GetCompetitorMentionsInput())
    absent = await read_tools.get_competitor_mentions(
        read_tools.GetCompetitorMentionsInput(agent="Cosentyx"))

    for result in (named, ranked, absent):
        assert result.ok
        assert "application database only" in result.summary


async def test_no_caveat_when_there_is_no_second_store_to_confuse_it_with(maker):
    await _add(maker, response_id="R1", mentions=[{"brand": "Tremfya", "sentiment": 0.5}])
    result = await read_tools.get_competitor_mentions(
        read_tools.GetCompetitorMentionsInput(agent="Tremfya"))
    assert result.ok
    assert "application database only" not in result.summary


# =================================================================================
# Copilot tools
# =================================================================================
def test_the_new_tools_are_registered_and_their_schemas_build():
    for name in ("get_competitor_mentions", "get_head_to_head"):
        assert name in TOOLS, f"{name} not registered"
    names = {s["name"] for s in anthropic_tool_schemas()}
    assert {"get_competitor_mentions", "get_head_to_head"} <= names


def test_the_head_to_head_page_is_navigable():
    """It is a real route; without this the copilot could not send anyone there."""
    from app.copilot.tools.help_tools import _VALID_ROUTES

    assert "/dashboard/head-to-head" in _VALID_ROUTES


async def test_the_question_that_started_this_now_answers(maker):
    """"Summarize the sentiment for Tremfya from the AI responses we have"."""
    await _add(maker, response_id="R1", mentions=[{"brand": "Tremfya", "sentiment": 0.6}])
    await _add(maker, response_id="R2", mentions=[{"brand": "guselkumab", "sentiment": 0.2}])
    await _add(maker, response_id="R3", mentions=[{"brand": "Stelara", "sentiment": 0.1}])

    result = await read_tools.get_competitor_mentions(
        read_tools.GetCompetitorMentionsInput(agent="Tremfya")
    )
    assert result.ok
    assert "Tremfya" in result.summary
    assert "2 of 3 scored answers" in result.summary
    # The mention count travels with the average, always.
    assert "over 2 scored mention(s)" in result.summary
    assert result.data["summary"]["avg_sentiment"] == 0.4


async def test_the_competitor_tool_reports_silence_as_a_finding(maker):
    await _add(maker, response_id="R1", mentions=[{"brand": "Stelara", "sentiment": 0.1}])
    result = await read_tools.get_competitor_mentions(
        read_tools.GetCompetitorMentionsInput(agent="Tremfya")
    )
    # ok=True: "no model named them" is an answer, not a tool failure.
    assert result.ok
    assert result.data["found"] is False
    assert "No scored answer" in result.summary


async def test_list_responses_tool_takes_a_competitor(maker):
    await _add(maker, response_id="R1", mentions=[{"brand": "Tremfya", "sentiment": 0.6}])
    await _add(maker, response_id="R2", mentions=[{"brand": "Stelara", "sentiment": 0.1}])
    result = await read_tools.list_responses(
        read_tools.ListResponsesInput(competitor="Tremfya")
    )
    assert result.ok
    assert result.data["total"] == 1
    assert "NAME Tremfya" in result.summary


async def test_the_head_to_head_tool_reads_the_board(maker):
    await _add(maker, response_id="R1", domain="Comparative",
               question_id="Q-CMP",
               mentions=[{"brand": "Tremfya", "sentiment": 0.6}])
    result = await read_tools.get_head_to_head(read_tools.GetHeadToHeadInput())
    assert result.ok
    assert "answers_examined" in result.data


async def test_an_empty_board_points_at_the_wider_read(maker):
    """A rival can be absent from the board yet loud everywhere else — say so."""
    await _add(maker, response_id="R1", domain="Efficacy",
               mentions=[{"brand": "Tremfya", "sentiment": 0.6}])
    result = await read_tools.get_head_to_head(read_tools.GetHeadToHeadInput())
    assert result.ok
    assert result.data["pairs_total"] == 0
    assert "get_competitor_mentions" in result.summary


async def test_the_head_to_head_tool_speaks_the_boards_multi_select_api(maker):
    """The board's filters are lists. Pinned because the tool calls that signature directly,
    and a singular kwarg would only surface as a swallowed exception at runtime."""
    await _add(maker, response_id="R1", domain="Comparative",
               mentions=[{"brand": "Tremfya", "sentiment": 0.6}])
    result = await read_tools.get_head_to_head(read_tools.GetHeadToHeadInput(
        competitor=["Tremfya", "Stelara"], persona=["Provider"], verdict=["LOSING"],
    ))
    assert result.ok, result.error
    # Echoed back in the caller's order, so a misspelt or stale value is visible.
    applied = result.data["filters_applied"]
    assert applied["competitors"] == ["Tremfya", "Stelara"]
    assert applied["personas"] == ["Provider"]
    assert applied["verdicts"] == ["LOSING"]


async def test_head_to_head_detail_requires_a_pair_key(maker):
    result = await read_tools.get_head_to_head(
        read_tools.GetHeadToHeadInput(view="detail")
    )
    assert not result.ok
    assert result.error == "pair_key is required"


# =================================================================================
# Analytics
# =================================================================================
async def test_the_landscape_kpi_is_reachable(maker):
    await _add(maker, response_id="R1", monitoring_mode="DISEASE_STATE", brand_focus=None,
               position="LANDSCAPE", mentions=[
                   {"brand": "Tremfya", "mentioned": True, "sentiment": 0.4,
                    "position": "AMONG_OPTIONS"},
               ])
    result = await read_tools.get_analytics(read_tools.GetAnalyticsInput(kind="landscape"))
    assert result.ok
    matrix = result.data["result"]["matrix"]
    assert [row["brand"] for row in matrix] == ["Tremfya"]


@pytest.mark.parametrize("kind", ["sentiment_distribution", "positioning", "llm_comparison"])
async def test_scoped_kpis_pass_the_session_by_keyword(maker, kind):
    """Regression: these three were called with ``db`` positionally, so the session bound to
    the first FILTER parameter instead (``therapeutic_area``, and ``monitoring_mode`` for
    llm_comparison) while ``db`` kept its ``Depends`` default.

    It only ever appeared to work because Snowflake was answering — the warehouse callable
    takes no arguments, so the mis-bound ones were never used. The fixture disables
    Snowflake precisely so the SQLite fallback runs and the mistake is visible."""
    await _add(maker, response_id="R1", mentions=[{"brand": "Tremfya", "sentiment": 0.6}])
    result = await read_tools.get_analytics(read_tools.GetAnalyticsInput(kind=kind))
    assert result.ok, result.error


async def test_an_unscopeable_kpi_says_it_ignored_the_filter(maker):
    """Silently dropping a filter would let the reader believe a number was narrowed."""
    await _add(maker, response_id="R1", mentions=[])
    result = await read_tools.get_analytics(
        read_tools.GetAnalyticsInput(kind="volume", therapeutic_area="Dermatology")
    )
    assert result.ok
    assert result.data["scope_ignored"] == ["therapeutic_area"]
    assert "was ignored" in result.summary


async def test_a_scoped_kpi_actually_narrows(maker):
    await _add(maker, response_id="R1", therapeutic_area="Dermatology", sentiment=0.8)
    await _add(maker, response_id="R2", therapeutic_area="Gastroenterology", sentiment=-0.8)
    result = await read_tools.get_analytics(
        read_tools.GetAnalyticsInput(kind="sentiment_distribution",
                                     therapeutic_area="Gastroenterology")
    )
    assert result.ok
    assert result.data["scope_ignored"] == []
    assert result.data["result"]["buckets"] == {"positive": 0, "neutral": 0, "negative": 1}
