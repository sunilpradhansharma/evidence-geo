"""FR-707a Model Release Event Correlation tests.

Covers: release CRUD, the lookback-window correlation logic, drift annotation on
the diff, and the operational correlation ratio.
"""
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.database import Base
from app.models.model_release import ModelReleaseLog
from app.models.response import Response
from app.models.response_diff import ResponseDiff
from app.services import model_release_service as svc


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from app.models import audit_log, scoring  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


# --- CRUD + lookback correlation (FR-707a.1/2/3) --------------------------------
async def test_create_and_list_release(session):
    await svc.create_release(
        session, target_platform="Claude", release_date=date(2024, 10, 22),
        version="v2", url="https://example.com/notes", release_notes="upgrade",
    )
    rows = await svc.list_releases(session)
    assert len(rows) == 1 and rows[0].target_platform == "Claude"


async def test_create_evidencemd_release_yesterday(session):
    """FR-707a.1/2/6 validation: log a mock release for EvidenceMD dated yesterday and
    assert it persists with all required metadata, and correlates with EvidenceMD drift."""
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1))
    row = await svc.create_release(
        session, target_platform="EvidenceMD", release_date=yesterday,
        version="evidencemd-2026-07", url="https://evidencemd.ai/changelog",
        release_notes="Clinical reasoning model refresh.",
    )
    assert row.id is not None
    assert row.target_platform == "EvidenceMD"
    assert row.release_date == yesterday
    assert row.version == "evidencemd-2026-07"
    assert row.url == "https://evidencemd.ai/changelog"
    assert row.release_notes == "Clinical reasoning model refresh."
    assert row.created_at is not None

    # Correlation is case-insensitive, so an EvidenceMD release lines up with the
    # `evidencemd` target's responses (llm_name="evidencemd").
    hit = await svc.find_correlated_release(
        session, llm_name="evidencemd", observed_on=yesterday,
    )
    assert hit is not None and hit.target_platform == "EvidenceMD"


async def test_correlation_within_window(session):
    await svc.create_release(session, target_platform="Claude", release_date=date(2024, 10, 22))
    # Drift observed 5 days after the release -> should correlate (default 30-day window).
    hit = await svc.find_correlated_release(
        session, llm_name="claude", observed_on=date(2024, 10, 27),
    )
    assert hit is not None and hit.target_platform == "Claude"


async def test_correlation_outside_window(session):
    await svc.create_release(session, target_platform="Claude", release_date=date(2024, 10, 22))
    # 45 days later -> outside the 30-day window -> no correlation.
    miss = await svc.find_correlated_release(
        session, llm_name="Claude", observed_on=date(2024, 12, 6),
    )
    assert miss is None


async def test_correlation_platform_must_match(session):
    await svc.create_release(session, target_platform="Gemini", release_date=date(2024, 10, 22))
    miss = await svc.find_correlated_release(
        session, llm_name="Claude", observed_on=date(2024, 10, 25),
    )
    assert miss is None


# --- Diff annotation via the scorer (FR-707a.4) ---------------------------------
async def test_diff_annotates_correlated_release(session, monkeypatch):
    from app.scoring import scorer

    now = datetime.now(timezone.utc)
    await svc.create_release(
        session, target_platform="Claude", release_date=now.date() - timedelta(days=3),
    )
    # Two responses for the same (question, llm) across different runs, materially different.
    prev = Response(
        response_id="p1", run_id="run-A", llm_name="Claude", persona="Provider",
        question_id="Q1", question_text="q", therapeutic_area="Obesity",
        brand_focus="X", domain="Efficacy", response_text="Original answer about efficacy.",
        status="SUCCESS", created_at=now - timedelta(days=1),
    )
    curr = Response(
        response_id="c1", run_id="run-B", llm_name="Claude", persona="Provider",
        question_id="Q1", question_text="q", therapeutic_area="Obesity",
        brand_focus="X", domain="Efficacy",
        response_text="Totally different content now, rewritten end to end with new claims.",
        status="SUCCESS", created_at=now,
    )
    session.add_all([prev, curr])
    await session.commit()

    await scorer._compute_response_diff(session, curr)
    await session.commit()

    diff = (await session.execute(select(ResponseDiff))).scalars().one()
    assert diff.material_change is True
    assert diff.correlated_release_id is not None


# --- Correlation ratio (FR-707a.7) ----------------------------------------------
async def test_correlation_ratio(session):
    session.add_all([
        ResponseDiff(question_id="Q1", llm_name="Claude", current_response_id="c1",
                     similarity_ratio=0.2, material_change=True, correlated_release_id=1),
        ResponseDiff(question_id="Q2", llm_name="Claude", current_response_id="c2",
                     similarity_ratio=0.3, material_change=True, correlated_release_id=None),
        ResponseDiff(question_id="Q3", llm_name="Claude", current_response_id="c3",
                     similarity_ratio=0.95, material_change=False, correlated_release_id=None),
    ])
    await session.commit()

    result = await svc.correlation_ratio(session)
    assert result["material_drifts"] == 2
    assert result["correlated_drifts"] == 1
    assert result["unexplained_drifts"] == 1
    assert result["correlation_ratio"] == pytest.approx(0.5)


# --- Auto-detection from drift spikes (FR-707a, no manual logging) --------------
async def _add_drift(session, *, platform, qid, when,
                     prev_text="Original answer.", cur_text="Rewritten answer.",
                     material=True, similarity=0.2):
    cur_id = f"c-{uuid.uuid4().hex[:8]}"
    prev_id = f"p-{uuid.uuid4().hex[:8]}"
    for rid, text in ((prev_id, prev_text), (cur_id, cur_text)):
        session.add(Response(
            response_id=rid, run_id=f"run-{uuid.uuid4().hex[:4]}", llm_name=platform,
            persona="Provider", question_id=qid, question_text=f"question {qid}",
            therapeutic_area="Obesity", brand_focus="Rinvoq", domain="Efficacy",
            response_text=text, status="SUCCESS", created_at=when,
        ))
    session.add(ResponseDiff(
        question_id=qid, llm_name=platform, current_response_id=cur_id,
        previous_response_id=prev_id, similarity_ratio=similarity,
        material_change=material, diff_text="- old\n+ new", created_at=when,
    ))
    await session.commit()
    return cur_id, prev_id


async def test_detect_creates_event_and_links(session):
    now = datetime.now(timezone.utc)
    for i in range(3):
        await _add_drift(session, platform="Gemini", qid=f"Q{i}", when=now)

    res = await svc.detect_model_updates(session, min_drifts=3)
    assert res["events_created"] == 1
    assert res["diffs_linked"] == 3

    rels = await svc.list_releases(session, target_platform="Gemini")
    assert len(rels) == 1 and rels[0].source == "auto"
    assert rels[0].release_date == now.date()

    # Idempotent: a second run creates nothing new.
    res2 = await svc.detect_model_updates(session, min_drifts=3)
    assert res2["events_created"] == 0 and res2["diffs_linked"] == 0

    diffs = (await session.execute(select(ResponseDiff))).scalars().all()
    assert all(d.correlated_release_id is not None for d in diffs)


async def test_detect_below_threshold_no_event(session):
    now = datetime.now(timezone.utc)
    for i in range(2):
        await _add_drift(session, platform="Claude", qid=f"Q{i}", when=now)

    res = await svc.detect_model_updates(session, min_drifts=3)
    assert res["events_created"] == 0
    assert await svc.list_releases(session, target_platform="Claude") == []


# --- Version-boundary correlation (FR-707a Phase B) -----------------------------
async def test_find_correlated_prefers_version_boundary(session):
    now = datetime.now(timezone.utc).date()
    # A generic window release AND a real api version event for the same platform.
    await svc.create_release(session, target_platform="gpt-4o", release_date=now, source="auto")
    api_event = await svc.create_release(
        session, target_platform="gpt-4o", release_date=now,
        version="gpt-4o-2024-11-20", source="api",
    )
    hit = await svc.find_correlated_release(
        session, llm_name="gpt-4o", observed_on=now, version="gpt-4o-2024-11-20",
    )
    assert hit is not None and hit.id == api_event.id  # version boundary wins


async def test_find_correlated_falls_back_to_window(session):
    now = datetime.now(timezone.utc).date()
    win = await svc.create_release(
        session, target_platform="gpt-4o", release_date=now, source="auto",
    )
    # Version has no matching event -> window fallback.
    hit = await svc.find_correlated_release(
        session, llm_name="gpt-4o", observed_on=now, version="unknown-version",
    )
    assert hit is not None and hit.id == win.id


async def test_version_impact_counts_and_sentiment(session):
    from app.models.scoring import ScoringRecord

    now = datetime.now(timezone.utc)
    rel = await svc.create_release(
        session, target_platform="gpt-4o", release_date=now.date(),
        version="gpt-4o-2024-11-20", source="api",
    )
    # Two changed questions across the boundary, each with a sentiment drop + a position flip.
    for i in range(2):
        cur_id, prev_id = await _add_drift(session, platform="gpt-4o", qid=f"Q{i}", when=now)
        # link to the release + attach before/after scores
        diff = (await session.execute(
            select(ResponseDiff).where(ResponseDiff.current_response_id == cur_id)
        )).scalars().one()
        diff.correlated_release_id = rel.id
        session.add_all([
            ScoringRecord(score_id=f"s-prev-{i}", response_id=prev_id, sentiment_score=0.5,
                          competitive_position="FIRST_LINE_RECOMMENDED"),
            ScoringRecord(score_id=f"s-cur-{i}", response_id=cur_id, sentiment_score=0.1,
                          competitive_position="AMONG_OPTIONS"),
        ])
    await session.commit()

    impact = await svc.version_impact(session, target_platform="gpt-4o")
    assert len(impact) == 1
    it = impact[0]
    assert it["version"] == "gpt-4o-2024-11-20"
    assert it["questions_changed"] == 2
    assert it["sentiment_before"] == pytest.approx(0.5)
    assert it["sentiment_after"] == pytest.approx(0.1)
    assert it["sentiment_delta"] == pytest.approx(-0.4)
    assert it["position_changes"] == 2


# --- High-impact alert/digest hook (FR-707a) ------------------------------------
async def _high_impact_release(session):
    from app.models.scoring import ScoringRecord

    now = datetime.now(timezone.utc)
    rel = await svc.create_release(
        session, target_platform="gpt-4o", release_date=now.date(),
        version="gpt-4o-2024-11-20", source="api",
    )
    # 3 changed questions -> crosses the default min-questions (3) threshold.
    for i in range(3):
        cur_id, prev_id = await _add_drift(session, platform="gpt-4o", qid=f"Q{i}", when=now)
        diff = (await session.execute(
            select(ResponseDiff).where(ResponseDiff.current_response_id == cur_id)
        )).scalars().one()
        diff.correlated_release_id = rel.id
        session.add_all([
            ScoringRecord(score_id=f"s-prev-{i}", response_id=prev_id, sentiment_score=0.4),
            ScoringRecord(score_id=f"s-cur-{i}", response_id=cur_id, sentiment_score=0.2),
        ])
    await session.commit()
    return rel


async def test_flag_high_impact_is_idempotent_and_audits(session):
    from app.models.audit_log import AuditLog
    from app.models.model_release import ModelReleaseLog

    rel = await _high_impact_release(session)

    res1 = await svc.flag_high_impact_updates(session)
    assert res1["flagged"] == 1

    refreshed = await session.get(ModelReleaseLog, rel.id)
    assert refreshed.alerted_at is not None

    audits = (await session.execute(
        select(AuditLog).where(AuditLog.event == "MODEL_UPDATE_HIGH_IMPACT")
    )).scalars().all()
    assert len(audits) == 1

    # Second run flags nothing new (alerted_at already set).
    res2 = await svc.flag_high_impact_updates(session)
    assert res2["flagged"] == 0


async def test_high_impact_updates_filter(session):
    await _high_impact_release(session)
    items = await svc.high_impact_updates(session)
    assert len(items) == 1
    assert items[0]["is_high_impact"] is True
    assert items[0]["questions_changed"] == 3


async def test_low_impact_not_flagged(session):
    now = datetime.now(timezone.utc)
    rel = await svc.create_release(
        session, target_platform="gemini", release_date=now.date(),
        version="gemini-2.0-flash", source="api",
    )
    # Only 1 changed question, small sentiment move -> below thresholds.
    cur_id, prev_id = await _add_drift(session, platform="gemini", qid="Q0", when=now)
    diff = (await session.execute(
        select(ResponseDiff).where(ResponseDiff.current_response_id == cur_id)
    )).scalars().one()
    diff.correlated_release_id = rel.id
    await session.commit()

    res = await svc.flag_high_impact_updates(session)
    assert res["flagged"] == 0
    assert await svc.high_impact_updates(session) == []


async def test_list_and_detail_before_after(session):
    now = datetime.now(timezone.utc)
    await _add_drift(
        session, platform="Gemini", qid="Q1", when=now,
        prev_text="Original answer.", cur_text="Rewritten answer.",
    )
    items = await svc.list_drifts(session)
    assert len(items) == 1
    it = items[0]
    assert it["llm_name"] == "Gemini"
    assert it["question_text"] == "question Q1"
    assert it["previous_snippet"] == "Original answer."
    assert it["current_snippet"] == "Rewritten answer."

    detail = await svc.get_drift_detail(session, it["id"])
    assert detail["previous_response_text"] == "Original answer."
    assert detail["current_response_text"] == "Rewritten answer."
    assert detail["material_change"] is True
