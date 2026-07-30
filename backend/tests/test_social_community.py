"""Tests for the myRAteam / Bezzy RA community-enrichment layer.

Covers the pieces added by the community pass, all with the network/LLM boundaries
monkeypatched and an in-memory SQLite DB:

  - app.social.community: vocabulary-constrained brand-mention extraction (anti-hallucination),
    signal-shape clamping (journey stage, list caps, dedup), batch index alignment + graceful
    degradation, and ``extract_and_apply`` persisting only pages that produced signal.
  - app.services.social_service._community_insights: multi-drug SOV + patient-signal
    aggregation and the brief-vs-per-post unmet-question precedence.
  - app.harvest.sources.apify._flatten: capturing crawl-page publish dates nested under
    ``metadata`` (openGraph / JSON-LD) — e.g. Bezzy article dates.
  - app.social.pipeline._fetch_community_tavily: the optional Tavily supplement — adaptive by
    default (fires only when a crawl is thin), plus off/always modes; force-scopes onto channel.
"""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.harvest.sources.tavily as tavily_mod
from app.harvest.sources import apify
from app.harvest.sources.base import RawItem
from app.models import social_brief, social_comment, social_post  # noqa: F401 — register tables
from app.models.database import Base
from app.models.social_post import SocialPost
from app.services import social_service
from app.social import community, pipeline


# --------------------------------------------------------------------------- community: mentions

def test_norm_mentions_drops_unknown_and_canonicalizes(monkeypatch):
    """Only monitored drugs survive; casing is canonicalized; dupes/invented drugs are dropped."""
    lookup = {
        "rinvoq": {"name": "Rinvoq", "generic": "upadacitinib", "company": "AbbVie", "owner": "AbbVie"},
        "enbrel": {"name": "Enbrel", "generic": "etanercept", "company": "Amgen", "owner": "Competitor"},
    }
    monkeypatch.setattr(community, "_brand_lookup", lambda: lookup)
    raw = [
        {"name": "Rinvoq", "sentiment": 0.5, "context": "helped a lot"},
        {"name": "MadeUpDrug", "sentiment": -1.0},   # not monitored -> dropped (anti-hallucination)
        {"name": "enbrel", "sentiment": 2.0},         # out-of-range sentiment clamps to 1.0
        {"name": "Rinvoq", "sentiment": 0.1},          # duplicate brand -> dropped
    ]
    out = community._norm_mentions(raw)
    assert [m["name"] for m in out] == ["Rinvoq", "Enbrel"]
    assert out[0]["owner"] == "AbbVie" and out[0]["generic"] == "upadacitinib"
    assert out[0]["sentiment"] == 0.5 and out[0]["context"] == "helped a lot"
    assert out[1]["owner"] == "Competitor" and out[1]["sentiment"] == 1.0


def test_norm_mentions_ignores_non_list():
    assert community._norm_mentions("not a list") == []
    assert community._norm_mentions(None) == []


# --------------------------------------------------------------------------- community: signals

def test_norm_signals_clamps_invalid_journey_stage():
    assert community._norm_signals({"journey_stage": "banana"})["journey_stage"] is None
    assert community._norm_signals({"journey_stage": "switching"})["journey_stage"] == "switching"


def test_str_list_dedups_and_caps():
    out = community._str_list(["Pain", "pain", "PAIN", "fatigue"] + [f"x{i}" for i in range(10)])
    assert len(out) == community._MAX_LIST            # hard cap
    assert out[0] == "Pain"                            # first-seen casing preserved
    assert [o.lower() for o in out].count("pain") == 1  # case-insensitive dedup


def test_norm_signals_full_shape():
    sig = community._norm_signals({
        "concerns": ["flares", "flares"], "journey_stage": "long_term",
        "switching_drivers": ["side effects"], "qol_impacts": ["fatigue"],
        "access_barriers": ["cost"], "questions": ["How do I taper safely?"],
    })
    assert sig["concerns"] == ["flares"]
    assert sig["journey_stage"] == "long_term"
    assert sig["switching_drivers"] == ["side effects"]
    assert sig["questions"] == ["How do I taper safely?"]


# --------------------------------------------------------------------------- community: batch

async def test_extract_batch_aligns_by_index(monkeypatch):
    """Model output is re-aligned to the input order via the per-object ``index``."""
    async def fake_chat(system, user, *, max_tokens=2600):
        return [{"index": 1, "concerns": ["second"]}, {"index": 0, "concerns": ["first"]}]

    monkeypatch.setattr(community, "chat_json", fake_chat)
    out = await community.extract_batch(["t0", "t1"], "vocab")
    assert out[0]["concerns"] == ["first"]
    assert out[1]["concerns"] == ["second"]


async def test_extract_batch_degrades_on_error(monkeypatch):
    async def boom(system, user, *, max_tokens=2600):
        raise RuntimeError("no LLM key in test")

    monkeypatch.setattr(community, "chat_json", boom)
    assert await community.extract_batch(["a", "b", "c"], "v") == [{}, {}, {}]


async def test_extract_batch_empty_input():
    assert await community.extract_batch([], "v") == []


# --------------------------------------------------------------------------- community: apply

@pytest.fixture
async def session():
    engine_ = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine_.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine_, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine_.dispose()


async def test_extract_and_apply_persists_only_pages_with_signal(session, monkeypatch):
    p1 = SocialPost(channel="myrateam", source="apify:myrateam", text="RA page one", dedupe_hash="h1")
    p2 = SocialPost(channel="myrateam", source="apify:myrateam", text="RA page two", dedupe_hash="h2")
    session.add_all([p1, p2])
    await session.commit()

    async def fake_batch(texts, vocab, *, max_tokens=2600):
        return [
            {"brand_mentions": [{"name": "Rinvoq", "sentiment": 0.3}],
             "concerns": ["pain"], "journey_stage": "switching", "questions": ["How to switch safely?"]},
            {"brand_mentions": [], "concerns": [], "journey_stage": None, "questions": []},  # empty -> skipped
        ]

    monkeypatch.setattr(community, "extract_batch", fake_batch)
    monkeypatch.setattr(community, "_brand_lookup", lambda: {
        "rinvoq": {"name": "Rinvoq", "generic": "upadacitinib", "company": "AbbVie", "owner": "AbbVie"}})

    result = await community.extract_and_apply(session, [p1, p2], vocab="v", batch_size=5)
    assert result == {"enriched": 1, "brand_mentions": 1, "questions": 1}
    assert json.loads(p1.brand_mentions)[0]["name"] == "Rinvoq"
    assert json.loads(p1.patient_signals)["journey_stage"] == "switching"
    assert p2.brand_mentions is None and p2.patient_signals is None  # no-signal page left untouched


async def test_extract_and_apply_no_posts():
    assert await community.extract_and_apply(None, [], vocab="v") == {
        "enriched": 0, "brand_mentions": 0, "questions": 0}


# --------------------------------------------------------------------------- insights aggregation

def _post(channel, *, signals=None, mentions=None):
    return SocialPost(
        channel=channel,
        text="x",
        dedupe_hash=channel + str(id(signals)),
        patient_signals=json.dumps(signals) if signals is not None else None,
        brand_mentions=json.dumps(mentions) if mentions is not None else None,
    )


def test_community_insights_none_without_enrichment():
    assert social_service._community_insights([SocialPost(channel="reddit", text="x", dedupe_hash="r")], None) is None


def test_community_insights_aggregates_signals_and_sov():
    p1 = _post("myrateam",
               signals={"concerns": ["pain", "fatigue"], "journey_stage": "switching",
                        "switching_drivers": ["side effects"], "qol_impacts": ["sleep"],
                        "access_barriers": ["cost"], "questions": ["How to switch safely?"]},
               mentions=[{"name": "Rinvoq", "owner": "AbbVie", "company": "AbbVie", "sentiment": 0.4},
                         {"name": "Enbrel", "owner": "Competitor", "company": "Amgen", "sentiment": -0.2}])
    p2 = _post("bezzy",
               signals={"concerns": ["pain"], "journey_stage": "long_term",
                        "questions": ["How to switch safely?", "Is it safe long term?"]},
               mentions=[{"name": "Rinvoq", "owner": "AbbVie", "company": "AbbVie", "sentiment": 0.6}])

    out = social_service._community_insights([p1, p2], None)
    assert out["posts"] == 2
    assert set(out["channels"]) == {"myrateam", "bezzy"}

    concerns = {c["label"]: c["count"] for c in out["concerns"]}
    assert concerns == {"pain": 2, "fatigue": 1}
    assert {s["label"]: s["count"] for s in out["journey_stages"]} == {"switching": 1, "long_term": 1}

    sov = out["drug_sov"]
    assert sov["total_mentions"] == 3           # Rinvoq x2 pages + Enbrel x1
    assert sov["abbvie_mentions"] == 2 and sov["competitor_mentions"] == 1
    assert sov["abbvie_present"] is True
    rinvoq = next(d for d in out["drug_mentions"] if d["name"] == "Rinvoq")
    assert rinvoq["mentions"] == 2 and rinvoq["avg_sentiment"] == 0.5

    # No brief -> fall back to deduped per-post questions.
    q_texts = [u["question"] for u in out["unmet_questions"]]
    assert "How to switch safely?" in q_texts and "Is it safe long term?" in q_texts
    assert len(q_texts) == len(set(q_texts))


def test_community_insights_prefers_brief_unmet_questions():
    p = _post("myrateam", signals={"questions": ["raw per-post q"]})
    brief = SimpleNamespace(unmet_questions=json.dumps(
        [{"question": "Clustered Q", "theme": "Access", "brand": "Rinvoq"}]))
    out = social_service._community_insights([p], brief)
    assert [u["question"] for u in out["unmet_questions"]] == ["Clustered Q"]


# --------------------------------------------------------------------------- apify: date capture

def test_flatten_captures_opengraph_publish_date():
    rec = {
        "url": "https://www.bezzyra.com/discover/x",
        "text": "Article body about living with RA.",
        "metadata": {
            "title": "X",
            "openGraph": [
                {"property": "og:title", "content": "X"},
                {"property": "article:published_time", "content": "2024-03-15T10:00:00Z"},
            ],
        },
    }
    flat = apify._flatten(rec)
    assert flat["text"] == "Article body about living with RA."   # top-level wins
    dt = apify._first_time(flat, apify._TIME_KEYS)
    assert dt is not None and dt.year == 2024 and dt.month == 3


def test_flatten_captures_jsonld_publish_date():
    rec = {"url": "u", "text": "body",
           "metadata": {"jsonLd": [{"@type": "Article", "datePublished": "2023-01-02"}]}}
    dt = apify._first_time(apify._flatten(rec), apify._TIME_KEYS)
    assert dt is not None and dt.year == 2023 and dt.month == 1


def test_flatten_platform_record_passthrough():
    rec = {"text": "a tweet", "url": "u", "timestamp": 1700000000}
    assert apify._flatten(rec) is rec           # no metadata block -> unchanged


# --------------------------------------------------------------------------- pipeline: Tavily gate

class _FakeSource:
    def __init__(self, channel):
        self.channel = channel


class _RecordingTavily:
    """Configured Tavily stub that records whether ``search`` was reached."""
    calls = 0

    def __init__(self, *a, **k):
        pass

    def is_configured(self):
        return True

    async def search(self, q, *, max_results=8, include_domains=None):
        type(self).calls += 1
        return [RawItem(source="tavily", url="https://www.myrateam.com/resources/a",
                        title="A", domain="myrateam.com", content=f"text {q}", query=q)]


_TV_CFG = {"apify": {"myrateam": {"tavily": {"domains": ["myrateam.com"],
                                             "queries": ["q1", "q2", "q3"]}}}}


async def _run_tavily(scfg, *, crawl_counts, monkeypatch, tavily=_RecordingTavily):
    _RecordingTavily.calls = 0
    monkeypatch.setattr(tavily_mod, "TavilySource", tavily)
    return await pipeline._fetch_community_tavily(
        _TV_CFG, scfg, [_FakeSource("myrateam")], {"myrateam"},
        ta="Rheumatology", seed=[], crawl_counts=crawl_counts, progress=None)


async def test_community_tavily_defaults_to_adaptive_and_skips_healthy_crawl(monkeypatch):
    # Empty scfg -> adaptive default (min_posts 8); a healthy crawl fires nothing (no Tavily spend).
    out = await _run_tavily({}, crawl_counts={"myrateam": 50}, monkeypatch=monkeypatch)
    assert out == [] and _RecordingTavily.calls == 0


async def test_community_tavily_off_mode_never_runs(monkeypatch):
    out = await _run_tavily({"community_tavily_mode": "off"},
                            crawl_counts={"myrateam": 0}, monkeypatch=monkeypatch)
    assert out == [] and _RecordingTavily.calls == 0


async def test_community_tavily_adaptive_fires_only_when_thin(monkeypatch):
    scfg = {"community_tavily_mode": "adaptive", "community_tavily_min_posts": 8,
            "community_tavily_max_results": 5, "community_tavily_max_queries": 2}
    out = await _run_tavily(scfg, crawl_counts={"myrateam": 2}, monkeypatch=monkeypatch)
    assert len(out) == 2                                  # capped to max_queries
    assert all(it.channel == "myrateam" for it in out)    # force-scoped onto the channel
    assert all(it.source == "tavily:myrateam" for it in out)


async def test_community_tavily_always_mode_ignores_crawl_yield(monkeypatch):
    scfg = {"community_tavily_mode": "always", "community_tavily_max_queries": 1}
    out = await _run_tavily(scfg, crawl_counts={"myrateam": 999}, monkeypatch=monkeypatch)
    assert len(out) == 1                                  # fires despite a healthy crawl


async def test_community_tavily_skips_when_key_missing(monkeypatch):
    class _Unconfigured:
        def __init__(self, *a, **k):
            pass

        def is_configured(self):
            return False

    out = await _run_tavily({"community_tavily_mode": "always"},
                            crawl_counts={"myrateam": 0}, monkeypatch=monkeypatch, tavily=_Unconfigured)
    assert out == []
