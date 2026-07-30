"""FR-108a Disease-State / Pre-Launch mode tests.

Covers: schema validation for the two monitoring modes, the orchestrator's
mode-scoped question fetch, the landscape (multi-competitor) scoring branch, and
the mandated pre-launch label on brand-less exports.
"""
import json

import pytest
from sqlalchemy import MetaData, inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config.labels import PRELAUNCH_LABEL
from app.models.database import Base, _make_questions_brand_focus_nullable
from app.models.question import Question
from app.models.response import Response
from app.schemas import QuestionCreate, QuestionOut
from app.services import export_service, question_service


# --- Schema validation (FR-108a.1/2) --------------------------------------------
def test_brand_mode_requires_brand_focus():
    with pytest.raises(ValueError):
        QuestionCreate(
            question_text="Q?", persona="Provider", therapeutic_area="Obesity",
            domain="Efficacy", monitoring_mode="BRAND", brand_focus=None,
        )


def test_disease_state_requires_competitor_and_nulls_brand():
    q = QuestionCreate(
        question_text="What agents treat this?", persona="Provider",
        therapeutic_area="Obesity", domain="Comparative",
        monitoring_mode="DISEASE_STATE", brand_focus="ShouldBeDropped",
        competitor_focus=["Wegovy", "Zepbound"],
    )
    assert q.brand_focus is None          # brand-less by construction
    assert q.competitor_focus == ["Wegovy", "Zepbound"]


def test_disease_state_without_competitor_rejected():
    with pytest.raises(ValueError):
        QuestionCreate(
            question_text="Q?", persona="Prospect", therapeutic_area="Obesity",
            domain="General", monitoring_mode="DISEASE_STATE",
        )


async def test_legacy_questions_migration_allows_brandless_rows(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    legacy_metadata = MetaData()
    legacy_questions = Question.__table__.to_metadata(legacy_metadata)
    legacy_questions.c.brand_focus.nullable = False

    async with engine.begin() as conn:
        await conn.run_sync(legacy_metadata.create_all)
        await conn.execute(legacy_questions.insert().values(
            question_id="Q-LEGACY",
            question_text="Does Lupron treat endometriosis?",
            persona="Patient",
            therapeutic_area="Endometriosis",
            brand_focus="Lupron Depot",
            domain="Efficacy",
            monitoring_mode="BRAND",
            active=True,
            priority_weight=1.0,
            approval_status="PENDING",
            version=1,
            is_variation=False,
        ))
        await _make_questions_brand_focus_nullable(conn)
        columns = await conn.run_sync(lambda c: inspect(c).get_columns("questions"))
        assert next(c for c in columns if c["name"] == "brand_focus")["nullable"] is True
        assert (await conn.execute(text(
            "SELECT brand_focus FROM questions WHERE question_id = 'Q-LEGACY'"
        ))).scalar_one() == "Lupron Depot"

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        created = await question_service.create_question(session, QuestionCreate(
            question_text="How is endometriosis diagnosed?",
            persona="Patient",
            therapeutic_area="Endometriosis",
            domain="General",
            monitoring_mode="DISEASE_STATE",
            competitor_focus=["Orilissa", "Myfembree"],
        ))
        output = QuestionOut.model_validate(created)
        assert output.brand_focus is None
        assert output.competitor_focus == ["Orilissa", "Myfembree"]

    await engine.dispose()


# --- Export carries the mandated label (FR-108a.6/7) ----------------------------
def test_export_labels_disease_state_rows():
    items = [{"response_id": "r1", "monitoring_mode": "DISEASE_STATE",
              "competitor_focus": ["Wegovy"], "brand_focus": None}]
    csv_out = export_service.to_csv(items)
    assert PRELAUNCH_LABEL in csv_out
    json_out = json.loads(export_service.to_json(items))
    assert json_out["pre_launch_notice"] == PRELAUNCH_LABEL


def test_export_brand_rows_have_no_label():
    items = [{"response_id": "r1", "monitoring_mode": "BRAND", "brand_focus": "Rinvoq"}]
    assert PRELAUNCH_LABEL not in export_service.to_csv(items)
    assert isinstance(json.loads(export_service.to_json(items)), list)


# --- DB-backed fixtures ----------------------------------------------------------
@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    # Import models so every table (incl. audit_log) registers on the metadata.
    from app.models import alert, audit_log, scoring  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


# --- Orchestrator mode-scoped question fetch (FR-108a) --------------------------
async def test_fetch_questions_filters_by_mode(session):
    from app.agent.orchestrator import _fetch_questions

    session.add(Question(
        question_id="Q-B1", question_text="brand q", persona="Provider",
        therapeutic_area="Obesity", brand_focus="Rinvoq", domain="Efficacy",
        monitoring_mode="BRAND", approval_status="APPROVED", active=True,
    ))
    session.add(Question(
        question_id="Q-D1", question_text="landscape q", persona="Provider",
        therapeutic_area="Obesity", brand_focus=None, domain="Comparative",
        monitoring_mode="DISEASE_STATE", competitor_focus=json.dumps(["Wegovy"]),
        approval_status="APPROVED", active=True,
    ))
    await session.commit()

    brand = await _fetch_questions(session, monitoring_mode="BRAND")
    disease = await _fetch_questions(session, monitoring_mode="DISEASE_STATE")
    assert [q.question_id for q in brand] == ["Q-B1"]
    assert [q.question_id for q in disease] == ["Q-D1"]


# --- Governance: unapproved questions are blocked (FR-108a.5) -------------------
async def test_unapproved_disease_state_question_is_blocked(session):
    """FR-108a.5: a PENDING (unapproved) disease-state question must NOT be picked up
    by the governed bank-driven run path, while its APPROVED sibling is."""
    from app.agent.orchestrator import _fetch_questions

    session.add(Question(
        question_id="Q-DS-PENDING", question_text="unapproved landscape q", persona="Provider",
        therapeutic_area="Obesity", brand_focus=None, domain="Comparative",
        monitoring_mode="DISEASE_STATE", competitor_focus=json.dumps(["Wegovy"]),
        approval_status="PENDING", active=True,
    ))
    session.add(Question(
        question_id="Q-DS-APPROVED", question_text="approved landscape q", persona="Provider",
        therapeutic_area="Obesity", brand_focus=None, domain="Comparative",
        monitoring_mode="DISEASE_STATE", competitor_focus=json.dumps(["Zepbound"]),
        approval_status="APPROVED", active=True,
    ))
    # An inactive (paused) approved question must also be excluded.
    session.add(Question(
        question_id="Q-DS-INACTIVE", question_text="paused landscape q", persona="Provider",
        therapeutic_area="Obesity", brand_focus=None, domain="Comparative",
        monitoring_mode="DISEASE_STATE", competitor_focus=json.dumps(["Mounjaro"]),
        approval_status="APPROVED", active=False,
    ))
    await session.commit()

    fetched = await _fetch_questions(session, monitoring_mode="DISEASE_STATE")
    ids = [q.question_id for q in fetched]
    assert "Q-DS-PENDING" not in ids       # governance blocks the unapproved question
    assert "Q-DS-INACTIVE" not in ids      # and the paused one
    assert ids == ["Q-DS-APPROVED"]        # only the approved, active question runs


# --- Landscape scoring branch (FR-108a.3/4) -------------------------------------
class _FakeResult:
    def __init__(self, text):
        self.text = text
        self.prompt_tokens = 10
        self.completion_tokens = 20


class _FakeClient:
    def __init__(self, text):
        self._text = text

    async def chat(self, *_a, **_k):
        return _FakeResult(self._text)


class _FakeCfg:
    provider = "bedrock"
    model_id = "fake-model"


async def test_landscape_scoring_produces_matrix(session, monkeypatch):
    from app.scoring import scorer

    landscape_json = json.dumps({
        "landscape": [
            {"brand": "Wegovy", "is_competitor": True, "mentioned": True,
             "sentiment": 0.6, "position": "FIRST_LINE_RECOMMENDED"},
            {"brand": "Zepbound", "is_competitor": True, "mentioned": True,
             "sentiment": 0.8, "position": "AMONG_OPTIONS"},
        ],
        "key_claims": ["strong weight loss"],
        "scoring_rationale": "both agents positioned favorably",
    })
    monkeypatch.setattr(scorer, "get_provider_client", lambda _p: _FakeClient(landscape_json))
    monkeypatch.setattr(scorer, "get_scoring_config", lambda: _FakeCfg())

    resp = Response(
        response_id="resp-ds-1", run_id="run-1", llm_name="Claude",
        persona="Provider", question_id="Q-D1", question_text="landscape q",
        therapeutic_area="Obesity", brand_focus=None,
        monitoring_mode="DISEASE_STATE", competitor_focus=json.dumps(["Wegovy", "Zepbound"]),
        domain="Comparative", response_text="Wegovy and Zepbound are both used...",
        status="SUCCESS",
    )
    session.add(resp)
    await session.commit()

    record = await scorer.score_response(session, resp)
    assert record is not None
    assert record.competitive_position == "LANDSCAPE"
    matrix = json.loads(record.brand_mentions)
    assert {m["brand"] for m in matrix} == {"Wegovy", "Zepbound"}
    # mean of 0.6 and 0.8
    assert record.sentiment_score == pytest.approx(0.7, abs=1e-6)

    # No focus-brand alerts fire in disease-state mode.
    from app.models.alert import Alert
    from sqlalchemy import select
    alerts = (await session.execute(select(Alert))).scalars().all()
    assert alerts == []
