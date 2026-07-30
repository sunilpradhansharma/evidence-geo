"""Multi-value bank filters (Approved Question Bank dropdowns).

Each filter accepts several values at once. The contract these tests pin:
  * several values on ONE field mean OR  (persona in Patient, Provider)
  * different fields still intersect     (Patient|Provider AND Rinvoq)
  * a bare string keeps its old exact-match behaviour, because every other caller
    in the codebase passes one
  * blanks are not a filter — an all-empty list must not exclude every row
"""
import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api import questions as questions_api
from app.models.database import Base, get_db
from app.models.question import Question
from app.services import question_service as svc


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.fixture
async def api():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _override():
        async with maker() as s:
            yield s

    app = FastAPI()
    app.include_router(questions_api.router)
    app.dependency_overrides[get_db] = _override
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await engine.dispose()


async def _add(session, qid, **kw) -> Question:
    q = Question(
        question_id=qid,
        question_text=kw.get("question_text", f"Question {qid}?"),
        persona=kw.get("persona", "Patient"),
        therapeutic_area=kw.get("therapeutic_area", "Rheumatology"),
        brand_focus=kw.get("brand_focus", "Rinvoq"),
        domain=kw.get("domain", "General"),
        monitoring_mode="BRAND",
        approval_status=kw.get("approval_status", "APPROVED"),
        active=True,
    )
    session.add(q)
    await session.commit()
    return q


async def _seed(session):
    await _add(session, "Q-PAT", persona="Patient", brand_focus="Rinvoq", domain="Safety")
    await _add(session, "Q-PROV", persona="Provider", brand_focus="Skyrizi", domain="Efficacy")
    await _add(session, "Q-PROS", persona="Prospect", brand_focus="Humira", domain="Access")


async def test_several_values_on_one_field_are_ored(session):
    await _seed(session)
    rows = await svc.list_questions(session, persona=["Patient", "Provider"], limit=100)
    assert {q.question_id for q in rows} == {"Q-PAT", "Q-PROV"}


async def test_different_fields_still_intersect(session):
    await _seed(session)
    rows = await svc.list_questions(
        session, persona=["Patient", "Provider"], brand_focus=["Skyrizi"], limit=100
    )
    assert {q.question_id for q in rows} == {"Q-PROV"}
    # An intersection with no overlap is empty, not "whichever field matched".
    empty = await svc.list_questions(
        session, persona=["Prospect"], domain=["Safety"], limit=100
    )
    assert empty == []


async def test_bare_string_still_exact_matches(session):
    await _seed(session)
    rows = await svc.list_questions(session, persona="Patient", limit=100)
    assert {q.question_id for q in rows} == {"Q-PAT"}


async def test_blank_values_are_not_a_filter(session):
    await _seed(session)
    # "All" in the UI sends nothing; a list of blanks must behave the same way.
    rows = await svc.list_questions(session, persona=["", "  "], limit=100)
    assert len(rows) == 3
    # A blank alongside a real pick is dropped, not treated as a fourth persona.
    mixed = await svc.list_questions(session, persona=["", "Patient"], limit=100)
    assert {q.question_id for q in mixed} == {"Q-PAT"}


async def test_route_accepts_repeated_query_params(api):
    for qid, persona, brand in (
        ("Q-PAT", "Patient", "Rinvoq"),
        ("Q-PROV", "Provider", "Skyrizi"),
        ("Q-PROS", "Prospect", "Humira"),
    ):
        r = await api.post("/questions", json={
            "question_text": f"What about {brand}?",
            "persona": persona,
            "therapeutic_area": "Rheumatology",
            "brand_focus": brand,
            "domain": "General",
        })
        assert r.status_code in (200, 201), r.text

    r = await api.get("/questions", params=[("persona", "Patient"), ("persona", "Provider")])
    assert r.status_code == 200
    assert {q["persona"] for q in r.json()} == {"Patient", "Provider"}

    # One value still works — every other caller sends exactly one.
    r = await api.get("/questions", params={"persona": "Prospect"})
    assert [q["persona"] for q in r.json()] == ["Prospect"]
