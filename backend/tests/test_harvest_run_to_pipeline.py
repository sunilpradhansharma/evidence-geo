"""Discover -> Run to Pipeline (promote + approve + run) service tests.

Covers harvest_service.promote_and_approve_batch, which powers
POST /harvest/run-to-pipeline: clean items are promoted as APPROVED and returned as
runnable question_ids; adverse-event / incomplete items are skipped with reasons; and an
already-promoted item reuses its question (idempotent) rather than duplicating it.
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.database import Base
from app.models.harvested_question import HarvestedQuestion
from app.models.question import Question
from app.services import harvest_service as svc


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    # Import models so every table (incl. audit_log) registers on the metadata.
    from app.models import audit_log  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _hq(question_text: str, **kw) -> HarvestedQuestion:
    base = dict(
        source="tavily",
        question_text=question_text,
        dedupe_hash=f"h-{abs(hash(question_text)) % 10_000_000}",
        persona="Provider",
        therapeutic_area="Immunology",
        brand_focus="Rinvoq",
        domain="Efficacy",
        relevance_score=0.9,
        ae_flag=False,
        status="CLASSIFIED",
    )
    base.update(kw)
    return HarvestedQuestion(**base)


async def _add(session, hq: HarvestedQuestion) -> HarvestedQuestion:
    session.add(hq)
    await session.commit()
    await session.refresh(hq)
    return hq


async def test_batch_promotes_and_approves_clean_item(session):
    hq = await _add(session, _hq("What is the approved dosing for Rinvoq in RA?"))

    res = await svc.promote_and_approve_batch(session, [hq.id], reviewer_name="Tester")

    assert len(res["question_ids"]) == 1
    assert len(res["promoted"]) == 1
    assert res["skipped"] == []

    qid = res["question_ids"][0]
    q = (await session.execute(select(Question).where(Question.question_id == qid))).scalars().first()
    assert q is not None
    assert q.approval_status == "APPROVED"
    assert q.active is True
    assert q.approver_name == "Tester"

    await session.refresh(hq)
    assert hq.status == "PROMOTED"
    assert hq.promoted_question_id == qid


async def test_batch_skips_adverse_event_item(session):
    hq = await _add(session, _hq(
        "Severe rash and swelling after starting this drug, is this dangerous?",
        ae_flag=True, status="QUARANTINED_AE",
    ))
    item_id = hq.id  # capture before the batch (a skip rolls back + expires ORM objects)

    res = await svc.promote_and_approve_batch(session, [item_id])

    assert res["question_ids"] == []
    assert len(res["skipped"]) == 1
    assert res["skipped"][0]["id"] == item_id
    assert "adverse" in res["skipped"][0]["reason"].lower()
    # AE item is NOT promoted (re-fetch by id rather than touch an expired instance).
    refreshed = await session.get(HarvestedQuestion, item_id)
    assert refreshed.status == "QUARANTINED_AE"


async def test_batch_skips_incomplete_item(session):
    # No brand_focus -> promotion validation fails; the item is reported, not run.
    hq = await _add(session, _hq("Is this treatment covered by insurance generally?", brand_focus=None))

    res = await svc.promote_and_approve_batch(session, [hq.id])

    assert res["question_ids"] == []
    assert len(res["skipped"]) == 1
    assert "brand_focus" in res["skipped"][0]["reason"]


async def test_batch_reuses_already_promoted_item(session):
    hq = await _add(session, _hq("What is the maintenance dose for this indication?"))

    first = await svc.promote_and_approve_batch(session, [hq.id])
    qid = first["question_ids"][0]

    # Running the same item again reuses its question rather than creating a duplicate.
    second = await svc.promote_and_approve_batch(session, [hq.id])
    assert second["question_ids"] == [qid]
    assert len(second["promoted"]) == 1
    assert second["skipped"] == []

    rows = (await session.execute(select(Question).where(Question.question_id == qid))).scalars().all()
    assert len(rows) == 1


async def test_batch_mixes_runnable_and_skipped(session):
    ok = await _add(session, _hq("How effective is Rinvoq for moderate-to-severe RA?"))
    ae = await _add(session, _hq(
        "I had a serious allergic reaction, should I stop?", ae_flag=True, status="QUARANTINED_AE",
    ))
    ok_id, ae_id = ok.id, ae.id  # capture before the batch (skip path expires ORM objects)

    res = await svc.promote_and_approve_batch(session, [ok_id, ae_id])

    assert len(res["question_ids"]) == 1
    assert [p["id"] for p in res["promoted"]] == [ok_id]
    assert [s["id"] for s in res["skipped"]] == [ae_id]
