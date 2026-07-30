"""Tests for AI Prompt Volume Intelligence (FR-116).

Covers ingestion + taxonomy mapping (FR-116.1/.6), volume aggregation (FR-116.2), high-volume
gap-topic flagging (FR-116.3), demand ranking = priority_weight × SUM(deduped matched volume)
(FR-116.4), and whole-file PII rejection + required metadata (FR-116.5). Plus unit coverage
for the Pandas volume coercion, taxonomy mapping, and the PII linter's numeric-column
exemption. External settings are patched so thresholds are deterministic.
"""
import csv
import io
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api import prompt_volume as pv_api
from app.models import prompt_volume as _pv_models  # noqa: F401  (register on Base.metadata)
from app.models import question as _q_models  # noqa: F401
from app.models.database import Base, get_db
from app.models.prompt_volume import PromptVolumeBatch, PromptVolumeStaging
from app.models.prompt_volume_alert import PromptVolumeGapAlert  # noqa: F401  (register table)
from app.models.question import Question
from app.prompt_volume import gap_alerts, persona, semrush_source, synthesize
from app.prompt_volume.gap import opportunity_score
from app.prompt_volume.linter import PiiRejection, lint
from app.prompt_volume.mapping import map_query
from app.prompt_volume.parser import CsvValidationError, parse_volume, read_csv
from app.schemas import QuestionCreate, QuestionOut
from app.services import prompt_volume_service as svc
from app.services import question_service


# --- fixtures --------------------------------------------------------------------
@pytest.fixture
async def session():
    engine_ = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine_.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine_, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine_.dispose()


@pytest.fixture
async def api():
    """Minimal app (no lifespan) with an in-memory DB shared across requests via StaticPool."""
    engine_ = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine_.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine_, expire_on_commit=False)

    async def _override():
        async with maker() as s:
            yield s

    app = FastAPI()
    app.include_router(pv_api.router)
    app.dependency_overrides[get_db] = _override
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await engine_.dispose()


def _patch_settings(monkeypatch, *, abs_floor=500, top_pct=0.2, match=0.5, max_mb=10, alert_limit=25):
    s = SimpleNamespace(
        prompt_volume_abs_volume_floor=abs_floor,
        prompt_volume_top_percentile=top_pct,
        prompt_volume_match_threshold=match,
        prompt_volume_max_upload_mb=max_mb,
        prompt_volume_gap_alert_limit=alert_limit,
    )
    monkeypatch.setattr("app.prompt_volume.engine.get_settings", lambda: s)
    monkeypatch.setattr("app.services.prompt_volume_service.get_settings", lambda: s)


def _csv_bytes(rows, header=("Keyword", "Search Volume")):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for r in rows:
        writer.writerow(r)
    return buf.getvalue().encode("utf-8")


async def _seed_question(session, text, *, brand="Humira", ta="Dermatology", weight=1.0):
    q = Question(
        question_id=f"Q-{uuid4().hex[:8]}", question_text=text, persona="Patient",
        therapeutic_area=ta, brand_focus=brand, domain="Safety",
        approval_status="APPROVED", active=True, priority_weight=weight, version=1,
    )
    session.add(q)
    await session.commit()
    return q


# --- FR-116.1 / .6 : ingestion + taxonomy mapping --------------------------------
async def test_ingest_populates_and_maps(session, monkeypatch):
    _patch_settings(monkeypatch)
    content = _csv_bytes([
        ("Humira side effects", "4400"),
        ("Stelara vs Humira", "1200"),
        ("random gardening tips", "900"),
    ])
    summary = await svc.ingest(
        session, content=content, source_tool="Semrush",
        source_label="RA Q3", dataset_date="2026-07-01", filename="ra.csv",
    )
    assert summary["rows_ingested"] == 3
    assert summary["metric_type"] == "search_volume_proxy"

    rows = {r.query_text: r for r in await svc._staging_rows(session, summary["batch_id"])}
    assert rows["Humira side effects"].matched_therapeutic_area == "Dermatology"
    assert rows["Humira side effects"].matched_brand == "Humira"
    assert rows["Stelara vs Humira"].matched_competitor == "Stelara"
    # Unrecognized queries fall back to Unmapped rather than a forced category.
    assert rows["random gardening tips"].matched_therapeutic_area == "Unmapped"
    assert rows["random gardening tips"].mapping_confidence == 0.0


# --- FR-116.2 : volume aggregation by TA + competitor ----------------------------
async def test_intelligence_aggregates_by_ta_and_competitor(session, monkeypatch):
    _patch_settings(monkeypatch)
    content = _csv_bytes([
        ("Humira side effects", "4000"),
        ("Skyrizi cost", "1000"),
        ("Stelara reviews", "2000"),
    ])
    await svc.ingest(session, content=content, source_tool="Semrush",
                     source_label="x", dataset_date="2026-07-01")

    intel = await svc.intelligence(session)
    areas = {a["therapeutic_area"]: a["volume"] for a in intel["by_therapeutic_area"]}
    comps = {c["competitor"]: c["volume"] for c in intel["by_competitor"]}
    assert areas.get("Dermatology") == 7000  # 4000 + 1000 + 2000 all roll up to Dermatology
    assert comps.get("Stelara") == 2000
    assert intel["total_volume"] == 7000
    assert intel["metric_type"] == "search_volume_proxy"


# --- FR-116.3 : high-volume gap topics -------------------------------------------
async def test_gap_flagged_for_high_volume_missing_topic(session, monkeypatch):
    _patch_settings(monkeypatch, abs_floor=1000, top_pct=0.01)
    await _seed_question(session, "What are the side effects of Humira?")
    content = _csv_bytes([
        ("Humira side effects", "4000"),      # covered by the seeded question -> not a gap
        ("Skyrizi weight gain", "3000"),      # high volume, uncovered -> GAP
        ("obscure niche query xyz", "20"),    # low volume, uncovered -> not flagged
    ])
    await svc.ingest(session, content=content, source_tool="Semrush",
                     source_label="x", dataset_date="2026-07-01")

    labels = {t["label"].lower() for t in (await svc.gap_topics(session))["topics"]}
    assert any("skyrizi" in label for label in labels)
    assert not any("humira" in label for label in labels)   # covered -> excluded
    assert not any("obscure" in label for label in labels)  # below floor -> excluded


# --- FR-116.4 : demand = priority_weight × SUM(deduped matched volume), not MAX ---
async def test_ranking_uses_priority_weight_times_summed_volume(session, monkeypatch):
    _patch_settings(monkeypatch)
    q = await _seed_question(session, "Humira side effects", weight=2.0)
    content = _csv_bytes([
        ("humira side effects", "4000"),
        ("common humira side effects", "2500"),
        ("humira long term side effects", "1200"),
    ])
    await svc.ingest(session, content=content, source_tool="Semrush",
                     source_label="x", dataset_date="2026-07-01")

    top = (await svc.prioritized_questions(session))["items"][0]
    assert top["question_id"] == q.question_id
    assert top["search_volume"] == 7700          # SUM of distinct matched volume, not MAX(4000)
    assert top["demand_score"] == 15400.0         # priority_weight(2.0) × 7700


async def test_ranking_rematches_question_created_after_ingest(session, monkeypatch):
    _patch_settings(monkeypatch)
    summary = await svc.ingest(
        session,
        content=_csv_bytes(
            [("how is endometriosis diagnosed", "8100")],
            header=("Question", "Search Volume"),
        ),
        source_tool="Semrush",
        source_label="Endometriosis demand",
        dataset_date="2026-07-13",
    )
    staged = await svc._staging_rows(session, summary["batch_id"])
    assert staged[0].matched_question_id is None

    question = await question_service.create_question(session, QuestionCreate(
        question_text="how is endometriosis diagnosed",
        persona="Patient",
        therapeutic_area="Endometriosis",
        monitoring_mode="DISEASE_STATE",
        competitor_focus=["Orilissa", "Myfembree"],
        domain="General",
        approval_status="APPROVED",
        priority_weight=1.0,
        demand_origin="PROMPT",
    ))

    ranked = await svc.prioritized_questions(session, batch_id=summary["batch_id"])
    item = next(row for row in ranked["items"] if row["question_id"] == question.question_id)
    assert item["search_volume"] == 8100
    assert item["demand_score"] == 8100.0
    assert (await svc.gap_topics(session, batch_id=summary["batch_id"]))["count"] == 0


# --- FR-116.5 : whole-file PII rejection -----------------------------------------
async def test_pii_rejects_entire_upload(session, monkeypatch):
    _patch_settings(monkeypatch)
    content = _csv_bytes(
        [
            ("Humira side effects", "4000", "contact jane@example.com"),
            ("Skyrizi cost", "1000", "ref 123456789"),
        ],
        header=("Keyword", "Search Volume", "Notes"),
    )
    with pytest.raises(PiiRejection):
        await svc.ingest(session, content=content, source_tool="Semrush",
                         source_label="x", dataset_date="2026-07-01")

    batches = (await session.execute(select(func.count()).select_from(PromptVolumeBatch))).scalar()
    rows = (await session.execute(select(func.count()).select_from(PromptVolumeStaging))).scalar()
    assert batches == 0 and rows == 0  # nothing persisted


# --- FR-116.5 : API metadata requirement + PII 422 -------------------------------
async def test_upload_requires_metadata(api):
    files = {"file": ("k.csv", b"Keyword,Search Volume\nHumira side effects,4000\n", "text/csv")}
    resp = await api.post("/prompt-volume/upload", files=files)  # no source/label/date
    assert resp.status_code == 422


async def test_upload_pii_returns_422(api):
    files = {"file": ("k.csv", b"Keyword,Search Volume,Notes\nHumira,4000,mail a@b.com\n", "text/csv")}
    data = {"source_tool": "Semrush", "source_label": "RA", "dataset_date": "2026-07-01"}
    resp = await api.post("/prompt-volume/upload", files=files, data=data)
    assert resp.status_code == 422
    assert "pii_hits" in resp.json()["detail"]


async def test_upload_clean_returns_201(api):
    files = {"file": ("k.csv", b"Keyword,Search Volume\nHumira side effects,4000\n", "text/csv")}
    data = {"source_tool": "Semrush", "source_label": "RA", "dataset_date": "2026-07-01"}
    resp = await api.post("/prompt-volume/upload", files=files, data=data)
    assert resp.status_code == 201
    assert resp.json()["rows_ingested"] == 1


# --- unit: Pandas volume coercion ------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("1,200", 1200), ("1.2K", 1200), ("3.4M", 3_400_000), ("<10", 10),
    ("880", 880), ("", None), ("-5", None), ("n/a", None),
])
def test_parse_volume_variants(raw, expected):
    assert parse_volume(raw) == expected


# --- unit: taxonomy mapping (via config.taxonomy alias index) --------------------
def test_map_query_taxonomy():
    assert map_query("humira for rheumatoid arthritis")["brand"] == "Humira"
    m = map_query("is stelara better than humira")
    assert m["competitor"] == "Stelara"          # competitor alias wins (longer)
    assert map_query("how to bake sourdough bread")["therapeutic_area"] == "Unmapped"


# --- unit: PII linter numeric-column exemption -----------------------------------
def test_linter_exempts_numeric_columns_but_flags_free_text():
    import pandas as pd

    # A 9-digit value in a numeric-metric column is NOT treated as an SSN.
    lint(pd.DataFrame({"Keyword": ["humira cost"], "Search Volume": ["123456789"]}))

    # A 9-digit value in a free-text column IS treated as an SSN -> reject.
    with pytest.raises(PiiRejection):
        lint(pd.DataFrame({"Keyword": ["humira 123456789"], "Search Volume": ["100"]}))

    # An email anywhere -> reject.
    with pytest.raises(PiiRejection):
        lint(pd.DataFrame({"Keyword": ["contact a@b.com"], "Search Volume": ["100"]}))


# --- opportunity scoring (KD/CPC) ------------------------------------------------
def test_opportunity_score_discounts_by_difficulty():
    assert opportunity_score(1000, None) == 1000.0   # unknown KD -> no penalty
    assert opportunity_score(1000, 0) == 1000.0      # trivial keyword -> full demand
    assert opportunity_score(1000, 50) == 500.0      # mid difficulty -> half
    assert opportunity_score(1000, 100) == 50.0      # hardest -> 5% floor, not zero


async def test_gap_topics_carry_opportunity_and_difficulty(session, monkeypatch):
    _patch_settings(monkeypatch, abs_floor=1, top_pct=1.0)
    content = _csv_bytes(
        [("Skyrizi weight gain", "3000", "20"), ("Skyrizi weight loss", "1000", "40")],
        header=("Keyword", "Search Volume", "Keyword Difficulty"),
    )
    await svc.ingest(session, content=content, source_tool="Semrush",
                     source_label="x", dataset_date="2026-07-01")
    topics = (await svc.gap_topics(session))["topics"]
    assert topics
    t = topics[0]
    assert {"opportunity_score", "avg_difficulty", "avg_cpc"} <= set(t)
    assert t["avg_difficulty"] == 30.0                 # mean of 20 + 40
    assert t["opportunity_score"] < t["combined_volume"]  # discounted by difficulty


# --- share of demand (brand vs competitor) ---------------------------------------
async def test_intelligence_share_of_demand(session, monkeypatch):
    _patch_settings(monkeypatch)
    content = _csv_bytes([
        ("Humira side effects", "4000"),   # focus brand
        ("Skyrizi cost", "1000"),          # focus brand
        ("Stelara reviews", "2000"),       # competitor
    ])
    await svc.ingest(session, content=content, source_tool="Semrush",
                     source_label="x", dataset_date="2026-07-01")
    intel = await svc.intelligence(session)
    sod = intel["share_of_demand"]
    assert sod["brand_volume"] == 5000
    assert sod["competitor_volume"] == 2000
    assert sod["brand_share_pct"] == 71.4              # 5000 / 7000
    assert sod["competitor_share_pct"] == 28.6
    assert any(a["therapeutic_area"] == "Dermatology" for a in sod["by_area"])
    # by_therapeutic_area rows are now enriched with share + avg difficulty
    assert "share_pct" in intel["by_therapeutic_area"][0]
    assert "avg_difficulty" in intel["by_therapeutic_area"][0]


# --- persona / intent split ------------------------------------------------------
def test_classify_persona_heuristics():
    assert persona.classify_persona("humira dosing for adults") == "Provider"
    assert persona.classify_persona("stelara vs humira") == "Prospect"
    assert persona.classify_persona("humira side effects") == "Patient"
    assert persona.classify_persona("humira") == "Unclassified"


async def test_intelligence_dedupes_repeated_keywords(session, monkeypatch):
    _patch_settings(monkeypatch)
    # A merged SEO export that repeats the SAME keyword must not double-count demand.
    content = _csv_bytes([
        ("Humira side effects", "4000"),
        ("Humira side effects", "3500"),   # duplicate keyword (merge artifact)
        ("Stelara reviews", "2000"),
    ])
    await svc.ingest(session, content=content, source_tool="Semrush",
                     source_label="merge", dataset_date="2026-07-01")
    intel = await svc.intelligence(session)
    assert intel["raw_row_count"] == 3
    assert intel["distinct_query_count"] == 2
    assert intel["total_volume"] == 6000          # 4000 (max of dupes) + 2000, NOT 9500


async def test_intelligence_by_persona(session, monkeypatch):
    _patch_settings(monkeypatch)
    content = _csv_bytes([
        ("Humira dosing", "1000"),          # Provider
        ("Humira side effects", "2000"),    # Patient
        ("Humira vs Skyrizi", "500"),       # Prospect
    ])
    await svc.ingest(session, content=content, source_tool="Semrush",
                     source_label="x", dataset_date="2026-07-01")
    by_persona = {p["persona"]: p["volume"] for p in (await svc.intelligence(session))["by_persona"]}
    assert by_persona.get("Provider") == 1000
    assert by_persona.get("Patient") == 2000
    assert by_persona.get("Prospect") == 500


# --- demand trend + emerging topics ----------------------------------------------
async def test_demand_trend_series_and_emerging(session, monkeypatch):
    _patch_settings(monkeypatch)
    await svc.ingest(
        session, content=_csv_bytes([("Humira side effects", "1000"), ("Stelara reviews", "500")]),
        source_tool="Semrush", source_label="Q1", dataset_date="2026-01-01",
    )
    await svc.ingest(
        session, content=_csv_bytes([("Humira side effects", "3000"), ("Rinvoq cost", "800")]),
        source_tool="Semrush", source_label="Q2", dataset_date="2026-04-01",
    )
    trend = await svc.demand_trend(session)
    assert trend["count"] == 2
    assert [p["source_label"] for p in trend["series"]] == ["Q1", "Q2"]  # ordered by dataset_date
    assert trend["series"][0]["total_volume"] == 1500
    assert trend["series"][1]["total_volume"] == 3800

    em = trend["emerging"]
    assert em is not None and em["current_label"] == "Q2" and em["previous_label"] == "Q1"
    by_q = {t["query_text"].lower(): t for t in em["topics"]}
    assert by_q["humira side effects"]["delta"] == 2000        # 3000 - 1000
    assert any(t["is_new"] for t in em["topics"])              # "Rinvoq cost" is new in Q2


async def test_demand_trend_single_batch_has_no_emerging(session, monkeypatch):
    _patch_settings(monkeypatch)
    await svc.ingest(session, content=_csv_bytes([("Humira side effects", "1000")]),
                     source_tool="Semrush", source_label="only", dataset_date="2026-01-01")
    trend = await svc.demand_trend(session)
    assert trend["count"] == 1
    assert trend["emerging"] is None


# --- FR-116.3 enhancement : coverage-gap alert lifecycle -------------------------
def test_plan_gap_alert_sync_lifecycle():
    def _gap(label, opp):
        return {"label": label, "combined_volume": opp, "opportunity_score": opp,
                "therapeutic_area": "Dermatology", "competitor": None, "query_count": 1}

    alertable = [_gap("ozempic face", 5000), _gap("new topic here", 3000),
                 _gap("back again", 2000), _gap("muted topic", 1000)]
    existing = {
        "ozempic face": {"status": gap_alerts.STATUS_OPEN, "label": "ozempic face"},     # -> update
        "back again": {"status": gap_alerts.STATUS_RESOLVED, "label": "back again"},      # -> reopen
        "muted topic": {"status": gap_alerts.STATUS_DISMISSED, "label": "muted topic"},   # -> touch (stay muted)
        "wegovy cost": {"status": gap_alerts.STATUS_OPEN, "label": "wegovy cost"},         # covered -> resolve
    }
    plan = gap_alerts.plan_sync(alertable, existing, {"wegovy cost"}, batch_id="B2")
    ktopics = lambda items: {i["topic_key"] for i in items}
    assert ktopics(plan.create) == {"new topic here"}
    assert ktopics(plan.update) == {"ozempic face"}
    assert ktopics(plan.reopen) == {"back again"}
    assert ktopics(plan.touch) == {"muted topic"}
    assert plan.resolve == ["wegovy cost"]


async def test_gap_alert_created_and_deduped_on_ingest(session, monkeypatch):
    _patch_settings(monkeypatch, abs_floor=1, top_pct=1.0)
    csv1 = _csv_bytes([("ozempic face", "5000"), ("ozempic lawsuit", "3000")])
    r1 = await svc.ingest(session, content=csv1, source_tool="Semrush",
                          source_label="Q1", dataset_date="2026-01-01")
    assert r1["gap_alerts"]["created"] == 2
    n_open = (await svc.list_gap_alerts(session, status="OPEN"))["count"]
    assert n_open == 2

    # Re-uploading the SAME gaps updates in place — no duplicate alerts (anti-fatigue).
    r2 = await svc.ingest(session, content=csv1, source_tool="Semrush",
                          source_label="Q2", dataset_date="2026-02-01")
    assert r2["gap_alerts"]["created"] == 0
    assert r2["gap_alerts"]["updated"] == 2
    assert (await svc.list_gap_alerts(session, status="OPEN"))["count"] == n_open


async def test_gap_alert_auto_resolves_when_bank_covers(session, monkeypatch):
    _patch_settings(monkeypatch, abs_floor=1, top_pct=1.0)
    await svc.ingest(session, content=_csv_bytes([("humira hair loss", "5000")]),
                     source_tool="Semrush", source_label="Q1", dataset_date="2026-01-01")
    assert (await svc.list_gap_alerts(session, status="OPEN"))["count"] == 1

    # Analyst acts: an approved question now covers the topic.
    await _seed_question(session, "humira hair loss")
    r2 = await svc.ingest(session, content=_csv_bytes([("skyrizi cost", "2000")]),
                          source_tool="Semrush", source_label="Q2", dataset_date="2026-02-01")
    assert r2["gap_alerts"]["resolved"] == 1

    resolved = await svc.list_gap_alerts(session, status="RESOLVED")
    assert resolved["count"] == 1
    assert resolved["alerts"][0]["resolved_reason"] == "COVERED"
    # The newly-seen "skyrizi cost" gap is now the only OPEN alert.
    assert (await svc.list_gap_alerts(session, status="OPEN"))["count"] == 1


async def test_gap_alert_dismiss_stays_quiet(session, monkeypatch):
    _patch_settings(monkeypatch, abs_floor=1, top_pct=1.0)
    csv1 = _csv_bytes([("ozempic lawsuit", "5000")])
    await svc.ingest(session, content=csv1, source_tool="Semrush",
                     source_label="Q1", dataset_date="2026-01-01")
    alert_id = (await svc.list_gap_alerts(session, status="OPEN"))["alerts"][0]["alert_id"]

    dismissed = await svc.dismiss_gap_alert(session, alert_id)
    assert dismissed["status"] == "DISMISSED"

    # Re-uploading the same gap must NOT reopen or duplicate a muted alert.
    r2 = await svc.ingest(session, content=csv1, source_tool="Semrush",
                          source_label="Q2", dataset_date="2026-02-01")
    assert r2["gap_alerts"]["created"] == 0
    assert r2["gap_alerts"]["reopened"] == 0
    assert (await svc.list_gap_alerts(session, status="OPEN"))["count"] == 0
    assert (await svc.list_gap_alerts(session, status="DISMISSED"))["count"] == 1


async def test_gap_alert_api_list_and_dismiss(api):
    files = {"file": ("k.csv", b"Keyword,Search Volume\nozempic face,5000\n", "text/csv")}
    data = {"source_tool": "Semrush", "source_label": "RA", "dataset_date": "2026-07-01"}
    up = await api.post("/prompt-volume/upload", files=files, data=data)
    assert up.status_code == 201

    listed = await api.get("/prompt-volume/gap-alerts?status=OPEN")
    assert listed.status_code == 200
    alerts = listed.json()["alerts"]
    assert len(alerts) == 1 and alerts[0]["is_new"] is True

    summary = await api.get("/prompt-volume/gap-alerts/summary")
    assert summary.json()["open"] == 1

    aid = alerts[0]["alert_id"]
    dis = await api.post(f"/prompt-volume/gap-alerts/{aid}/dismiss")
    assert dis.status_code == 200 and dis.json()["status"] == "DISMISSED"
    assert (await api.get("/prompt-volume/gap-alerts?status=OPEN")).json()["count"] == 0
    assert (await api.post("/prompt-volume/gap-alerts/nope/dismiss")).status_code == 404


async def test_sync_gap_alerts_latest_repopulates(session, monkeypatch):
    _patch_settings(monkeypatch, abs_floor=1, top_pct=1.0)
    await svc.ingest(session, content=_csv_bytes([("ozempic face", "5000")]),
                     source_tool="Semrush", source_label="Q1", dataset_date="2026-01-01")
    # Simulate a batch that predates the alert feature by clearing its alerts.
    for r in (await session.execute(select(PromptVolumeGapAlert))).scalars().all():
        await session.delete(r)
    await session.commit()
    assert (await svc.list_gap_alerts(session, status="OPEN"))["count"] == 0

    # Re-sync against the latest upload repopulates WITHOUT a new upload.
    res = await svc.sync_gap_alerts_latest(session)
    assert res["batch_id"] is not None and res["created"] == 1
    assert (await svc.list_gap_alerts(session, status="OPEN"))["count"] == 1


# --- real prompt/question support (keyword -> real question) ---------------------
def test_parser_accepts_prompt_column():
    # A prompt/question export (Profound / AlsoAsked) with no keyword column.
    _df, rows, volume_present = read_csv(b"Question,Search Volume\nHow long does Skyrizi take to work?,1200\n")
    assert rows[0]["query_text"] == "How long does Skyrizi take to work?"   # fills the mapping key
    assert rows[0]["prompt_text"] == "How long does Skyrizi take to work?"
    assert volume_present is True


def test_parser_keeps_both_keyword_and_prompt():
    content = b"Keyword,Prompt,Search Volume\nskyrizi psoriasis,Is Skyrizi effective for psoriasis?,900\n"
    _df, rows, _vp = read_csv(content)
    assert rows[0]["query_text"] == "skyrizi psoriasis"                     # keyword drives mapping
    assert rows[0]["prompt_text"] == "Is Skyrizi effective for psoriasis?"  # prompt is the real question


def test_parser_requires_a_text_column():
    # Neither a keyword nor a prompt column -> still rejected (nothing to analyze).
    with pytest.raises(CsvValidationError):
        read_csv(b"Foo,Bar\nx,y\n")


def test_parser_accepts_text_without_volume_column():
    # A keyword/prompt column with NO volume column is now ACCEPTED (previously rejected);
    # search_volume is left None so the engine derives demand from prompt recurrence.
    _df, rows, volume_present = read_csv(b"Keyword\nskyrizi side effects\n")
    assert volume_present is False
    assert rows[0]["query_text"] == "skyrizi side effects"
    assert rows[0]["search_volume"] is None


async def test_ingest_without_volume_column_derives_recurrence(session, monkeypatch):
    _patch_settings(monkeypatch, abs_floor=1, top_pct=1.0)
    # A Profound-style prompt log: a `prompt` column, NO volume. The same prompt recurs across
    # LLM rows, so its FREQUENCY becomes the demand proxy and duplicates collapse.
    content = (
        b"prompt,llm\n"
        b"Does Skyrizi cause weight gain?,chatgpt\n"
        b"Does Skyrizi cause weight gain?,gemini\n"
        b"Does Skyrizi cause weight gain?,perplexity\n"
        b"What are the side effects of Skyrizi?,chatgpt\n"
    )
    summary = await svc.ingest(session, content=content, source_tool="Profound",
                               source_label="Skyrizi prompts", dataset_date="2026-07-01")
    assert summary["metric_type"] == "prompt_frequency"      # labelled distinctly, honestly
    assert summary["rows_ingested"] == 2                     # collapsed to 2 DISTINCT prompts

    rows = {r.query_text: r for r in await svc._staging_rows(session, summary["batch_id"])}
    assert rows["Does Skyrizi cause weight gain?"].search_volume == 3   # recurrence count
    assert rows["What are the side effects of Skyrizi?"].search_volume == 1


def test_synthesize_keyword_to_question():
    q = synthesize.to_question
    assert q("skyrizi", brand="Skyrizi") == "What is Skyrizi and what is it used for?"
    assert q("skyrizi side effects", brand="Skyrizi") == "What are the side effects of Skyrizi?"
    assert q("skyrizi weight gain", brand="Skyrizi") == "Does Skyrizi cause weight gain?"
    assert q("skyrizi vs humira", brand="Skyrizi", competitor="Humira") == "How does Skyrizi compare to Humira?"
    assert q("skyrizi cost", brand="Skyrizi").startswith("How much does Skyrizi cost")
    assert q("skyrizi dosing", brand="Skyrizi").startswith("What is the recommended dosing")
    assert q("skyrizi during pregnancy", brand="Skyrizi") == "Is Skyrizi safe during pregnancy?"
    # Already phrased as a question -> preserved, just polished (trailing '?').
    assert q("how long does skyrizi take to work") == "How long does skyrizi take to work?"
    # No mapped brand + no specific intent -> grammatical fallback that keeps the topic.
    assert q("psoriasis biologics") == "What should patients know about psoriasis biologics?"
    # Alternatives intent (with a mapped brand).
    assert q("humira alternatives", brand="Humira") == "What are the alternatives to Humira?"


async def test_gap_topic_synthesizes_question_from_keyword(session, monkeypatch):
    _patch_settings(monkeypatch, abs_floor=1, top_pct=1.0)
    await svc.ingest(session, content=_csv_bytes([("skyrizi side effects", "3000")]),
                     source_tool="Semrush", source_label="x", dataset_date="2026-07-01")
    topics = (await svc.gap_topics(session))["topics"]
    t = next(t for t in topics if "skyrizi" in t["label"].lower())
    assert t["label"] == "skyrizi side effects"                       # keyword preserved as context
    assert t["question"] == "What are the side effects of Skyrizi?"   # real, monitorable question
    assert t["brand"] == "Skyrizi"
    assert t["question_origin"] == "synthesized"                      # keyword-only + synthesis on
    # A keyword-only upload has no prompt-backed rows.
    assert (await svc.intelligence(session))["prompt_backed_count"] == 0


async def test_gap_topic_uses_real_prompt_when_present(session, monkeypatch):
    _patch_settings(monkeypatch, abs_floor=1, top_pct=1.0)
    content = _csv_bytes([("skyrizi psoriasis", "Will Skyrizi clear my psoriasis?", "2500")],
                         header=("Keyword", "Prompt", "Search Volume"))
    await svc.ingest(session, content=content, source_tool="Other",
                     source_label="Profound", dataset_date="2026-07-01")
    topics = (await svc.gap_topics(session))["topics"]
    assert topics[0]["question"] == "Will Skyrizi clear my psoriasis?"   # verbatim, not synthesized
    assert topics[0]["question_origin"] == "prompt"                      # backed by a real prompt
    # The batch reports its prompt-backed coverage for the UI nudge.
    assert (await svc.intelligence(session))["prompt_backed_count"] == 1


async def test_gap_topic_keeps_raw_keyword_when_synthesis_off(session, monkeypatch):
    _patch_settings(monkeypatch, abs_floor=1, top_pct=1.0)
    # Analyst declines synthesis: keyword-only gaps keep the raw keyword instead of an
    # auto-written question, and the choice is persisted on the batch so GET /gaps honours it.
    await svc.ingest(session, content=_csv_bytes([("skyrizi side effects", "3000")]),
                     source_tool="Semrush", source_label="x", dataset_date="2026-07-01",
                     synthesize=False)
    topics = (await svc.gap_topics(session))["topics"]
    t = next(t for t in topics if "skyrizi" in t["label"].lower())
    assert t["question"] == "skyrizi side effects"    # raw keyword, verbatim (no synthesis)
    assert t["question_origin"] == "keyword"
    assert (await svc.list_batches(session))["batches"][0]["synthesize_questions"] is False


async def test_gap_alert_carries_monitorable_question(session, monkeypatch):
    _patch_settings(monkeypatch, abs_floor=1, top_pct=1.0)
    await svc.ingest(session, content=_csv_bytes([("skyrizi weight gain", "4000")]),
                     source_tool="Semrush", source_label="x", dataset_date="2026-07-01")
    alerts = (await svc.list_gap_alerts(session, status="OPEN"))["alerts"]
    assert alerts and alerts[0]["question"] == "Does Skyrizi cause weight gain?"


# --- demand provenance on created questions (FR-116) -----------------------------
def test_question_create_rejects_bad_demand_origin():
    with pytest.raises(ValueError):
        QuestionCreate(question_text="Q?", persona="Patient", therapeutic_area="Dermatology",
                       brand_focus="Skyrizi", domain="Safety", demand_origin="BOGUS")


async def test_created_question_persists_and_serializes_demand_origin(session):
    q = await question_service.create_question(session, QuestionCreate(
        question_text="Does Skyrizi cause weight gain?", persona="Patient",
        therapeutic_area="Dermatology", brand_focus="Skyrizi", domain="Safety",
        demand_origin="SYNTHESIZED",
    ))
    assert q.demand_origin == "SYNTHESIZED"                        # stored on the model
    assert QuestionOut.model_validate(q).demand_origin == "SYNTHESIZED"  # exposed to the UI


async def test_created_question_demand_origin_defaults_null(session):
    q = await question_service.create_question(session, QuestionCreate(
        question_text="Manual question?", persona="Provider",
        therapeutic_area="Dermatology", brand_focus="Skyrizi", domain="General",
    ))
    assert q.demand_origin is None                                # ordinary manual question


# --- bulk prompt import into the Question Bank: preview -> commit (FR-116) --------
def _prompt_csv(header: tuple[str, ...], rows: list[tuple]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


async def test_preview_prompts_extracts_distinct_without_persisting(session):
    # A Profound-style export: real questions in a `prompt` column plus answer-monitoring
    # columns that carry NO search volume (so the Prompt Volume uploader would reject it).
    content = _prompt_csv(
        ("prompt", "llm", "brief_response", "sources_count"),
        [
            ("What are the side effects of Skyrizi?", "chatgpt", "...", "16"),
            ("What are the side effects of Skyrizi?", "gemini", "...", "12"),   # dup across LLMs
            ("How effective is Skyrizi for Crohn's disease?", "gemini", "...", "9"),
        ],
    )
    p = await question_service.preview_prompts(
        session, content=content, persona="Patient", brand_focus="Skyrizi"
    )
    assert p["questions"] == [
        "What are the side effects of Skyrizi?",
        "How effective is Skyrizi for Crohn's disease?",
    ]                                                     # distinct, first-seen order
    assert p["duplicates"] == 1                           # the repeated prompt collapsed
    assert p["demand_origin"] == "PROMPT"                 # real question column
    # Skyrizi spans Dermatology AND Gastroenterology, so the batch-level label cannot be
    # derived from the brand alone. Rows still resolve individually at commit time.
    assert p["therapeutic_area"] == "Unmapped"
    assert p["prompt_column"] == "prompt"
    # Nothing was written — preview is a DRY RUN.
    assert await question_service.list_questions(session, brand_focus="Skyrizi", limit=100) == []


async def test_preview_prompts_flags_pii_rows(session):
    content = _prompt_csv(
        ("prompt",),
        [
            ("What are the side effects of Skyrizi?",),
            ("Skyrizi questions email me at jane@example.com",),
        ],
    )
    p = await question_service.preview_prompts(
        session, content=content, persona="Patient", brand_focus="Skyrizi"
    )
    assert p["questions"] == ["What are the side effects of Skyrizi?"]   # clean one only
    assert len(p["skipped"]) == 1 and "PII" in p["skipped"][0]["reason"]


async def test_preview_prompts_keyword_column_labels_origin_keyword(session):
    # A bare SEO keyword export (no prompt column) -> labeled KEYWORD ("From keyword").
    content = _prompt_csv(("Keyword", "Search Volume"),
                          [("skyrizi side effects", "3000"), ("skyrizi cost", "1200")])
    p = await question_service.preview_prompts(
        session, content=content, persona="Prospect", brand_focus="Skyrizi"
    )
    assert p["prompt_column"] == "Keyword"
    assert p["demand_origin"] == "KEYWORD"
    assert set(p["questions"]) == {"skyrizi side effects", "skyrizi cost"}


async def test_preview_prompts_without_text_column_raises(session):
    content = _prompt_csv(("llm", "sources_count", "relevance_score"),
                          [("chatgpt", "16", "96")])
    with pytest.raises(ValueError):
        await question_service.preview_prompts(
            session, content=content, persona="Patient", brand_focus="Skyrizi"
        )


async def test_commit_prompts_persists_selected_subset_as_pending(session):
    # The analyst kept only the 2 questions they ticked in the preview.
    chosen = [
        "What are the side effects of Skyrizi?",
        "Does Skyrizi cause weight gain?",
    ]
    r = await question_service.commit_prompts(
        session, questions=chosen, persona="Patient", brand_focus="Skyrizi",
        demand_origin="PROMPT",
    )
    assert r["imported"] == 2
    qs = await question_service.list_questions(session, brand_focus="Skyrizi", limit=100)
    assert {q.question_text for q in qs} == set(chosen)
    assert all(q.approval_status == "PENDING" for q in qs)     # governance: MA still reviews
    assert all(q.demand_origin == "PROMPT" for q in qs)        # "Real" provenance badge
    # Neither prompt names an indication, and Skyrizi spans two areas — "Unmapped" is the
    # honest answer, not whichever brands.yaml block happens to be declared first.
    assert all(q.persona == "Patient" and q.therapeutic_area == "Unmapped" for q in qs)
    assert all(q.monitoring_mode == "BRAND" for q in qs)


async def test_commit_prompts_resolves_the_area_per_row_from_the_indication(session):
    """A batch shares one brand but its prompts span indications, so the stored area is
    decided per row by the disease the question names."""
    r = await question_service.commit_prompts(
        session,
        questions=[
            "How effective is Skyrizi for Crohn's disease?",
            "How well does Skyrizi clear plaque psoriasis?",
        ],
        persona="Patient", brand_focus="Skyrizi", demand_origin="PROMPT",
    )
    assert r["imported"] == 2
    assert r["therapeutic_areas"] == {"Gastroenterology": 1, "Dermatology": 1}

    qs = await question_service.list_questions(session, brand_focus="Skyrizi", limit=100)
    by_area = {q.therapeutic_area: q.disease for q in qs}
    assert by_area == {"Gastroenterology": "Crohn's Disease", "Dermatology": "Plaque Psoriasis"}


async def test_commit_prompts_dedupes_against_existing_bank(session):
    chosen = ["Does Skyrizi cause hair loss?"]
    r1 = await question_service.commit_prompts(
        session, questions=chosen, persona="Patient", brand_focus="Skyrizi"
    )
    assert r1["imported"] == 1
    # Re-committing the same question adds nothing (already in the bank).
    r2 = await question_service.commit_prompts(
        session, questions=chosen, persona="Patient", brand_focus="Skyrizi"
    )
    assert r2["imported"] == 0 and r2["duplicates"] == 1
    assert len(await question_service.list_questions(session, brand_focus="Skyrizi", limit=100)) == 1


async def test_commit_prompts_stores_keyword_origin(session):
    r = await question_service.commit_prompts(
        session, questions=["skyrizi cost"], persona="Prospect", brand_focus="Skyrizi",
        demand_origin="KEYWORD",
    )
    assert r["demand_origin"] == "KEYWORD"
    qs = await question_service.list_questions(session, brand_focus="Skyrizi", limit=100)
    assert qs and qs[0].demand_origin == "KEYWORD"


# --- FR-116: in-app SEMrush fetch (preview -> ingest) ----------------------------
def _patch_semrush(monkeypatch, *, api_key="test-key", per_seed=25, max_seeds=40,
                   reports="both", database="us", match=0.5, abs_floor=1, top_pct=1.0):
    """Patch get_settings across engine/service/source with ALL fields the fetch path reads."""
    s = SimpleNamespace(
        prompt_volume_abs_volume_floor=abs_floor,
        prompt_volume_top_percentile=top_pct,
        prompt_volume_match_threshold=match,
        prompt_volume_max_upload_mb=10,
        prompt_volume_gap_alert_limit=25,
        prompt_volume_semrush_per_seed_limit=per_seed,
        prompt_volume_semrush_max_seeds=max_seeds,
        prompt_volume_semrush_reports=reports,
        semrush_api_key=api_key,
        semrush_base_url="https://api.semrush.com",
        semrush_database=database,
    )
    monkeypatch.setattr("app.prompt_volume.engine.get_settings", lambda: s)
    monkeypatch.setattr("app.services.prompt_volume_service.get_settings", lambda: s)
    monkeypatch.setattr("app.prompt_volume.semrush_source.get_settings", lambda: s)
    return s


# Canned SEMrush report rows. "is Humira safe" (question) and "Humira safe" (related) share a
# normalized query, so they MUST merge to one row at the MAX volume — never summed.
_SEM_QUESTIONS = [
    {"query_text": "is Humira safe", "prompt_text": "is Humira safe", "search_volume": 1000,
     "keyword_difficulty": None, "cpc": None, "report": "questions"},
]
_SEM_RELATED = [
    {"query_text": "Humira safe", "prompt_text": None, "search_volume": 1500,
     "keyword_difficulty": None, "cpc": 2.5, "report": "related"},
    {"query_text": "Humira cost", "prompt_text": None, "search_volume": 800,
     "keyword_difficulty": None, "cpc": None, "report": "related"},
]


def _fake_report(questions, related):
    async def _fake(client, seed, report_type, **kw):
        src = questions if report_type == "phrase_questions" else related
        return [dict(x) for x in src]
    return _fake


def test_expand_seeds_dermatology_full():
    seeds = {s["term"].lower() for s in semrush_source.expand_seeds("Dermatology")}
    assert {"humira", "skyrizi", "rinvoq"} <= seeds        # focus brands
    assert "adalimumab" in seeds                            # a generic
    assert "plaque psoriasis" in seeds                     # an indication
    assert "stelara" in seeds and "ustekinumab" in seeds   # competitor + its generic


def test_expand_seeds_is_scoped_to_one_specialty():
    """Seeding Dermatology must not pull the GI competitive field into the query set."""
    derm = {s["term"].lower() for s in semrush_source.expand_seeds("Dermatology")}
    gastro = {s["term"].lower() for s in semrush_source.expand_seeds("Gastroenterology")}
    assert "entyvio" in gastro and "entyvio" not in derm
    assert "dupixent" in derm and "dupixent" not in gastro
    assert "ulcerative colitis" in gastro and "ulcerative colitis" not in derm


def test_expand_seeds_respects_toggles():
    seeds = {s["term"].lower() for s in semrush_source.expand_seeds(
        "Dermatology", include_generics=False, include_indications=False, include_competitors=False)}
    assert {"humira", "skyrizi", "rinvoq"} <= seeds
    assert "adalimumab" not in seeds                        # generics off
    assert "plaque psoriasis" not in seeds                 # indications off
    assert "stelara" not in seeds                           # competitors off


def test_expand_seeds_single_brand_narrows():
    seeds = {s["term"].lower() for s in semrush_source.expand_seeds(
        "Dermatology", brand="Humira",
        include_generics=False, include_indications=False, include_competitors=False)}
    assert seeds == {"humira"}                              # only the chosen focus brand


def test_expand_seeds_caps_at_max_seeds(monkeypatch):
    _patch_semrush(monkeypatch, max_seeds=3)
    assert len(semrush_source.expand_seeds("Dermatology")) == 3


def test_semrush_parse_rows_multiline():
    text = (
        "Keyword;Search Volume;CPC;Competition;Number of Results\n"
        "humira cost;2400;1.50;0.50;1000\n"
        "humira dosing;800;0.90;0.30;500\n"
    )
    rows = semrush_source._parse_rows(text)
    assert len(rows) == 2
    assert rows[0]["Keyword"] == "humira cost" and rows[0]["Search Volume"] == "2400"
    assert rows[1]["Keyword"] == "humira dosing"


def test_novelty_compute_splits_new_seen_covered():
    rows = [
        {"query_text": "humira safe", "normalized_query": "humira safe", "search_volume": 100},
        {"query_text": "humira new topic here", "normalized_query": "humira new topic here", "search_volume": 200},
        {"query_text": "humira covered question", "normalized_query": "humira covered question", "search_volume": 50},
    ]
    out = svc._novelty_compute(
        rows, prev_norms={"humira safe"}, prev_tokens=[{"humira", "safe"}],
        qtokens=[{"humira", "covered", "question"}], threshold=0.5,
    )
    assert out["covered_count"] == 1                        # matches an approved question
    assert out["seen_in_last_count"] == 1                  # exact repeat of last dataset
    assert out["new_count"] == 1                            # net-new
    assert out["novel_volume"] == 200                       # only the new row's volume


async def test_semrush_fetch_merges_volume_safe(monkeypatch):
    _patch_semrush(monkeypatch)
    monkeypatch.setattr(semrush_source, "_fetch_report", _fake_report(_SEM_QUESTIONS, _SEM_RELATED))
    out = await semrush_source.fetch(
        "Dermatology", brand="Humira",
        include_generics=False, include_indications=False, include_competitors=False,
    )
    assert out["seeds_queried"] == 1
    assert out["lines_returned"] == 3                       # 1 question + 2 related
    rows = out["rows"]
    assert len(rows) == 2                                    # "humira safe" deduped across reports
    top = rows[0]
    assert top["search_volume"] == 1500                     # MAX(1000, 1500), NOT summed to 2500
    assert top["prompt_text"] == "is Humira safe"           # questions row preferred as representative
    assert top["report"] == "questions"
    assert top["cpc"] == 2.5                                 # inherited from the related row
    assert rows[1]["query_text"] == "Humira cost" and rows[1]["prompt_text"] is None


async def test_semrush_preview_then_ingest(session, monkeypatch):
    _patch_semrush(monkeypatch)
    monkeypatch.setattr(semrush_source, "_fetch_report", _fake_report(_SEM_QUESTIONS, _SEM_RELATED))

    preview = await svc.semrush_preview(
        session, therapeutic_area="Dermatology", brand="Humira",
        include_generics=False, include_indications=False, include_competitors=False,
    )
    assert preview["fetch_id"] is not None
    assert preview["distinct_query_count"] == 2
    assert preview["total_volume"] == 2300                  # 1500 + 800 (volume-safe merge)
    assert preview["novelty"]["new_count"] == 2             # fresh DB -> everything is net-new
    assert preview["estimated_units"] == 50                 # 1 seed x 25 per-seed x 2 reports

    # Preview is a DRY RUN — nothing persisted yet.
    n_batches = (await session.execute(select(func.count()).select_from(PromptVolumeBatch))).scalar()
    assert n_batches == 0

    result = await svc.semrush_ingest(
        session, fetch_id=preview["fetch_id"],
        source_label="Immunology (SEMrush)", dataset_date="2026-07-01",
    )
    assert result["rows_ingested"] == 2
    assert result["metric_type"] == "search_volume_proxy"   # real Nq volumes
    assert "gap_alerts" in result

    batches = list((await session.execute(select(PromptVolumeBatch))).scalars().all())
    assert len(batches) == 1 and batches[0].source_tool == "Semrush API"

    # A stale/unknown fetch_id -> None (the API surfaces 404).
    assert await svc.semrush_ingest(
        session, fetch_id="PVF-nope", source_label="x", dataset_date="2026-07-01"
    ) is None


async def test_semrush_preview_requires_key(session, monkeypatch):
    # No key configured -> raise rather than fabricate demand (unlike the enrichment stub).
    _patch_semrush(monkeypatch, api_key="")
    with pytest.raises(semrush_source.NotConfigured):
        await svc.semrush_preview(session, therapeutic_area="Dermatology")


async def test_semrush_preview_clamps_per_seed_limit(session, monkeypatch):
    _patch_semrush(monkeypatch, per_seed=25)
    monkeypatch.setattr(semrush_source, "_fetch_report", _fake_report(_SEM_QUESTIONS, _SEM_RELATED))
    preview = await svc.semrush_preview(
        session, therapeutic_area="Dermatology", brand="Humira",
        include_generics=False, include_indications=False, include_competitors=False,
        per_seed_limit=999,
    )
    assert preview["estimated_units"] == 200                # 999 clamped to 100 -> 1 x 100 x 2


async def test_semrush_preview_reports_filter(session, monkeypatch):
    # Analyst picks Questions-only -> only that report is pulled (half the billed units).
    _patch_semrush(monkeypatch, per_seed=25)
    monkeypatch.setattr(semrush_source, "_fetch_report", _fake_report(_SEM_QUESTIONS, _SEM_RELATED))
    preview = await svc.semrush_preview(
        session, therapeutic_area="Dermatology", brand="Humira",
        include_generics=False, include_indications=False, include_competitors=False,
        reports="questions",
    )
    assert preview["reports"] == ["phrase_questions"]       # related report skipped
    assert preview["estimated_units"] == 25                 # 1 seed x 25 x 1 report (not 2)
    assert preview["sample"] and all(r["report"] == "questions" for r in preview["sample"])


async def test_semrush_ingest_limit_keeps_top_by_demand(session, monkeypatch):
    # Analyst wants only the strongest N: ingest keeps the top-by-demand rows.
    _patch_semrush(monkeypatch)
    monkeypatch.setattr(semrush_source, "_fetch_report", _fake_report(_SEM_QUESTIONS, _SEM_RELATED))
    preview = await svc.semrush_preview(
        session, therapeutic_area="Dermatology", brand="Humira",
        include_generics=False, include_indications=False, include_competitors=False,
    )
    assert preview["distinct_query_count"] == 2
    result = await svc.semrush_ingest(
        session, fetch_id=preview["fetch_id"],
        source_label="x", dataset_date="2026-07-01", limit=1,
    )
    assert result["rows_ingested"] == 1                     # dropped the lower-demand row
    rows = list((await session.execute(select(PromptVolumeStaging))).scalars().all())
    assert len(rows) == 1 and (rows[0].search_volume or 0) == 1500  # kept the 1500-vol search


async def test_semrush_ingest_only_new_skips_repeats(session, monkeypatch):
    # A second identical fetch is all "seen last time" -> only-new keeps nothing.
    _patch_semrush(monkeypatch)
    monkeypatch.setattr(semrush_source, "_fetch_report", _fake_report(_SEM_QUESTIONS, _SEM_RELATED))
    first = await svc.semrush_preview(
        session, therapeutic_area="Dermatology", brand="Humira",
        include_generics=False, include_indications=False, include_competitors=False,
    )
    await svc.semrush_ingest(session, fetch_id=first["fetch_id"], source_label="x", dataset_date="2026-07-01")
    again = await svc.semrush_preview(
        session, therapeutic_area="Dermatology", brand="Humira",
        include_generics=False, include_indications=False, include_competitors=False,
    )
    assert again["novelty"]["new_count"] == 0               # everything repeats the last import
    with pytest.raises(ValueError):
        await svc.semrush_ingest(
            session, fetch_id=again["fetch_id"], source_label="x",
            dataset_date="2026-07-02", only_new=True,
        )


async def test_semrush_status_and_preview_endpoints(api, monkeypatch):
    _patch_semrush(monkeypatch, api_key="")
    st = await api.get("/prompt-volume/semrush/status")
    assert st.status_code == 200 and st.json()["configured"] is False
    # Unconfigured -> preview rejected with 400 (never fabricates demand).
    resp = await api.post("/prompt-volume/semrush/preview", json={"therapeutic_area": "Dermatology"})
    assert resp.status_code == 400
