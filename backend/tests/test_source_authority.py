"""Tests for Source Authority Mapping (FR-706a).

Covers URL/domain parsing (.1), two-axis classification + distribution summing to 100% (.2/.3),
competitor/unverified alerts (.4), frequency-preserving top-N per model (.5/.6), preferred-source
absence recorded during run processing with read-only GETs (.7), plus the review's hardening:
WHOIS/RDAP redaction != unverified, stub never fabricates ownership, coverage excludes parametric
targets, evidence-based LLM-fallback precedence + confidence tiers, RDAP parsing, domain-cache
dedup, and idempotent backfill. External RDAP / LLM calls are monkeypatched so tests are hermetic
(no network, no key required).
"""
import json

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api import source_authority as sa_api
from app.models.database import Base, _migrate_sqlite_schema, get_db

# Import every model so they register on Base.metadata before create_all (the legacy-migration
# test runs _migrate_sqlite_schema, which ALTERs the base tables and needs them all to exist).
from app.models import (  # noqa: F401
    alert as _alert_mod,
    audit_log as _audit_mod,
    consensus as _consensus_mod,
    harvested_question as _harvested_mod,
    preferred_source as _pref_mod,
    preferred_source_observation as _obs_mod,
    prompt_volume as _pv_mod,
    question as _question_mod,
    recommendation as _rec_mod,
    response as _response_mod,
    response_citation as _citation_mod,
    response_diff as _diff_mod,
    run as _run_mod,
    schedule as _schedule_mod,
    scoring as _scoring_mod,
    social_brief as _sb_mod,
    social_comment as _sc_mod,
    social_post as _sp_mod,
    source_domain as _domain_mod,
    theme as _theme_mod,
)
from app.models.alert import ENTITY_SOURCE_AUTHORITY, Alert
from app.models.preferred_source_observation import PreferredSourceObservation
from app.models.response import Response
from app.models.response_citation import ResponseCitation
from app.models.scoring import ScoringRecord
from app.models.source_domain import SourceDomain
from app.models.theme import ResponseTheme, Theme
from app.source_authority import domains, enrichment, taxonomy
from app.source_authority import service as svc
from app.source_authority.alerts import (
    RULE_COMPETITOR_TOP,
    RULE_ONLY_COMPETITOR,
    RULE_UNVERIFIED_TOP,
)


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
    app.include_router(sa_api.router)
    app.dependency_overrides[get_db] = _override
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, maker
    await engine_.dispose()


@pytest.fixture(autouse=True)
def _stub_enrichment(monkeypatch):
    """Default: registration returns nulls + no LLM classification — no network, no fabrication."""
    async def _registration(domain):
        return {
            "registrant_organization": None, "registrar_name": None,
            "whois_visibility": None, "created_date": None, "source": "stub",
        }

    async def _llm(domain, normalized_host=None, *, registrant_org=None):
        return None

    monkeypatch.setattr("app.source_authority.enrichment.registration_lookup", _registration)
    monkeypatch.setattr("app.source_authority.enrichment.classify_domain_llm", _llm)


# --- helpers ---------------------------------------------------------------------
def _src(domain: str, path: str = "/x") -> dict:
    return {"url": f"https://www.{domain}{path}", "title": domain, "domain": domain}


async def _seed_response(
    session, *, response_id, llm_name="gpt-4o", ta="Immunology", brand="Humira",
    persona="Provider", sources=None, status="SUCCESS", run_id="run1", model_version=None,
):
    r = Response(
        response_id=response_id, run_id=run_id, llm_name=llm_name, persona=persona,
        llm_model_version=model_version,
        question_id=f"q-{response_id}", question_text="Best biologic for RA?",
        therapeutic_area=ta, brand_focus=brand, domain="Comparative",
        response_text="...", status=status,
        sources=json.dumps(sources) if sources is not None else None,
    )
    session.add(r)
    await session.commit()
    return r


async def _domain(session, authority_domain):
    return (await session.execute(
        select(SourceDomain).where(SourceDomain.authority_domain == authority_domain)
    )).scalar_one()


# --- FR-706a.1 : URL -> clean domain ---------------------------------------------
def test_parse_url_three_complex_urls():
    a = domains.parse_url("https://www.fda.gov/drugs/approvals?q=humira#top")
    b = domains.parse_url("HTTP://PubMed.NCBI.NLM.NIH.gov/12345/?utm=x")
    c = domains.parse_url("stelara.com/psoriasis/dosing")
    assert a.registrable_domain == "fda.gov"
    assert b.normalized_host == "pubmed.ncbi.nlm.nih.gov"
    assert c.registrable_domain == "stelara.com"


def test_parse_url_robust_edge_cases():
    assert domains.parse_url("//nih.gov/x").registrable_domain == "nih.gov"      # protocol-relative
    assert domains.parse_url("https://EXAMPLE.co.uk:8443/p").registrable_domain == "example.co.uk"
    assert domains.parse_url("https://nih.gov./trailing").registrable_domain == "nih.gov"
    assert domains.parse_url("http://127.0.0.1/x") is None                        # IP
    assert domains.parse_url("http://localhost/x") is None
    assert domains.parse_url("mailto:doc@example.com") is None                    # opaque scheme
    assert domains.parse_url("not a url") is None
    assert domains.parse_url("") is None
    assert domains.parse_url(None) is None


def test_authority_domain_longest_suffix_not_collapsed():
    p = domains.parse_url("https://www.ncbi.nlm.nih.gov/pmc/articles/PMC1/")
    assert p.registrable_domain == "nih.gov"                                      # eTLD+1
    ad = taxonomy.authority_domain_for(p.normalized_host, p.registrable_domain)
    assert ad == "ncbi.nlm.nih.gov"                                              # not collapsed to nih.gov
    assert taxonomy.authority_type_for(p.normalized_host) == "PEER_REVIEWED"


async def test_legacy_gemini_redirect_uses_domain_title(session):
    source = {
        "url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token",
        "title": "nih.gov",
        "origin": "GROUNDED",
    }
    r = await _seed_response(session, response_id="r1", llm_name="gemini", sources=[source])
    result = await svc.classify_response(session, r)
    rows = (await session.execute(
        select(ResponseCitation).where(ResponseCitation.response_id == "r1")
    )).scalars().all()
    assert result["domains"] == 1
    assert rows[0].authority_domain == "nih.gov"


# --- FR-706a.1/.5 : citation frequency preserved ----------------------------------
async def test_citation_frequency_and_position_preserved(session):
    sources = [_src("fda.gov", "/a"), _src("fda.gov", "/b"), _src("fda.gov", "/c"), _src("webmd.com")]
    r = await _seed_response(session, response_id="r1", sources=sources)
    await svc.classify_response(session, r)

    rows = {c.authority_domain: c for c in (await session.execute(
        select(ResponseCitation).where(ResponseCitation.response_id == "r1")
    )).scalars().all()}
    assert rows["fda.gov"].citation_count == 3
    assert rows["webmd.com"].citation_count == 1
    assert rows["fda.gov"].first_citation_position == 0
    assert len(json.loads(rows["fda.gov"].citation_urls)) == 3


# --- FR-706a.2/.3 : enums + distribution sums to exactly 100.0 --------------------
async def test_distribution_enums_and_hundred_percent(session):
    r = await _seed_response(
        session, response_id="r1",
        sources=[_src("fda.gov"), _src("stelara.com"), _src("pubmed.ncbi.nlm.nih.gov"), _src("webmd.com"), _src("reddit.com")],
    )
    await svc.classify_response(session, r)

    dist = await svc.distribution(session)
    cats = {c["display_category"] for c in dist["categories"]}
    assert "REGULATORY" in cats
    assert "COMPETITOR_CONTROLLED" in cats
    assert "PEER_REVIEWED" in cats
    assert round(sum(c["citation_share_pct"] for c in dist["categories"]), 1) == 100.0


async def test_ownership_and_authority_axes(session):
    r = await _seed_response(
        session, response_id="r1",
        sources=[_src("humira.com"), _src("abbvie.com"), _src("jnj.com"), _src("stelara.com")],
    )
    await svc.classify_response(session, r)
    assert (await _domain(session, "humira.com")).control_type == "ABBVIE"       # brand-name token
    assert (await _domain(session, "abbvie.com")).control_type == "ABBVIE"       # config domain
    assert (await _domain(session, "jnj.com")).control_type == "COMPETITOR"      # config competitor domain
    assert (await _domain(session, "stelara.com")).control_type == "COMPETITOR"  # brand-name token


# --- FR-706a.4 : competitor-only + top-source alerts ------------------------------
async def test_competitor_only_source_fires_alerts(session):
    r = await _seed_response(session, response_id="r1", sources=[_src("stelara.com")])
    await svc.classify_response(session, r)

    alerts = (await session.execute(
        select(Alert).where(Alert.response_id == "r1")
    )).scalars().all()
    rules = {a.rule_triggered for a in alerts}
    assert RULE_ONLY_COMPETITOR in rules
    assert RULE_COMPETITOR_TOP in rules
    assert all(a.entity_type == ENTITY_SOURCE_AUTHORITY for a in alerts)
    assert all(a.score_id is None for a in alerts)


async def test_unverified_top_source_alert(session):
    r = await _seed_response(session, response_id="r1", sources=[_src("some-random-blog.xyz")])
    await svc.classify_response(session, r)
    rules = {a.rule_triggered for a in (await session.execute(
        select(Alert).where(Alert.response_id == "r1")
    )).scalars().all()}
    assert RULE_UNVERIFIED_TOP in rules


async def test_reclassify_does_not_duplicate_alerts(session):
    r = await _seed_response(session, response_id="r1", sources=[_src("stelara.com")])
    await svc.classify_response(session, r)
    await svc.classify_response(session, r)  # idempotent
    alerts = (await session.execute(select(Alert).where(Alert.response_id == "r1"))).scalars().all()
    assert len(alerts) == len({a.rule_triggered for a in alerts})


# --- FR-706a.5/.6 : top-10 per model + filter isolation ---------------------------
async def test_top_domains_exactly_ten_per_model_and_filters(session):
    await _seed_response(
        session, response_id="g1", llm_name="gpt-4o",
        sources=[_src(f"site{i}.com") for i in range(12)],
    )
    await _seed_response(
        session, response_id="gem1", llm_name="gemini", ta="Oncology",
        sources=[_src(f"ref{i}.org") for i in range(3)],
    )
    for rid in ("g1", "gem1"):
        await svc.classify_response(session, await session.get(Response, rid))

    grouped = await svc.top_domains(session, group_by="llm_name", limit=10)
    by_model = {g["llm_name"]: g["items"] for g in grouped["groups"]}
    assert len(by_model["gpt-4o"]) == 10          # exactly 10 when >=10 exist
    assert len(by_model["gemini"]) == 3           # all when fewer than 10
    assert all(len(items) <= 10 for items in by_model.values())

    only_gem = await svc.top_domains(session, llm_name="gemini", limit=10)
    assert {i["authority_domain"] for i in only_gem["items"]} == {f"ref{i}.org" for i in range(3)}

    only_oncology = await svc.top_domains(session, therapeutic_area="Oncology", limit=10)
    assert {i["authority_domain"] for i in only_oncology["items"]} == {f"ref{i}.org" for i in range(3)}


async def test_source_authority_api_contract(api):
    client, maker = api
    async with maker() as session:
        response = await _seed_response(
            session, response_id="api-r1", llm_name="gemini", ta="Oncology",
            sources=[_src("fda.gov"), _src("stelara.com")],
        )
        await svc.classify_response(session, response)

    dist = await client.get(
        "/source-authority/distribution",
        params={"llm_name": "gemini", "therapeutic_area": "Oncology"},
    )
    assert dist.status_code == 200
    assert dist.json()["total_citations"] == 2
    assert sum(c["citation_share_pct"] for c in dist.json()["categories"]) == 100.0

    top = await client.get(
        "/source-authority/top-domains",
        params={"group_by": "llm_name", "limit": 10, "therapeutic_area": "Oncology"},
    )
    assert top.status_code == 200
    assert [g["llm_name"] for g in top.json()["groups"]] == ["gemini"]

    added = await client.post(
        "/source-authority/preferred",
        json={"therapeutic_area": "Oncology", "domain": "https://www.fda.gov/drugs"},
    )
    assert added.status_code == 201
    assert added.json()["authority_domain"] == "fda.gov"

    async with maker() as session:
        response = await _seed_response(
            session, response_id="api-r2", llm_name="gemini", ta="Oncology",
            sources=[_src("webmd.com")],
        )
        await svc.classify_response(session, response)

    observations = await client.get(
        "/source-authority/preferred/observations",
        params={"therapeutic_area": "Oncology", "llm_name": "gemini"},
    )
    assert observations.status_code == 200
    assert observations.json()["items"][0]["absent"] == 1

    removed = await client.delete(f"/source-authority/preferred/{added.json()['pref_id']}")
    assert removed.status_code == 200


# --- FR-706a.7 : preferred-source absence recorded during run (GET read-only) -----
async def test_preferred_absence_recorded_during_classification(session):
    await svc.add_preferred(session, therapeutic_area="Immunology", domain="https://www.fda.gov")
    r = await _seed_response(session, response_id="r1", ta="Immunology", sources=[_src("webmd.com")])
    await svc.classify_response(session, r)

    obs = (await session.execute(select(PreferredSourceObservation))).scalars().all()
    assert len(obs) == 1
    assert obs[0].was_present is False
    assert obs[0].authority_domain == "fda.gov"


async def test_preferred_get_is_read_only(session):
    await svc.add_preferred(session, therapeutic_area="Immunology", domain="fda.gov")
    r = await _seed_response(session, response_id="r1", ta="Immunology", sources=[_src("webmd.com")])
    await svc.classify_response(session, r)

    before = len((await session.execute(select(PreferredSourceObservation))).scalars().all())
    out = await svc.preferred_observations(session, therapeutic_area="Immunology")
    out2 = await svc.preferred_observations(session, therapeutic_area="Immunology")
    after = len((await session.execute(select(PreferredSourceObservation))).scalars().all())
    assert before == after == 1                    # GET created no rows
    item = out2["items"][0]
    assert item["absent"] == 1 and item["present"] == 0
    assert out["items"][0]["authority_domain"] == "fda.gov"


async def test_preferred_present_and_idempotent(session):
    await svc.add_preferred(session, therapeutic_area="Immunology", domain="fda.gov")
    r = await _seed_response(session, response_id="r1", ta="Immunology", sources=[_src("fda.gov")])
    await svc.classify_response(session, r)
    await svc.classify_response(session, r)  # re-run: still one observation, now present

    obs = (await session.execute(select(PreferredSourceObservation))).scalars().all()
    assert len(obs) == 1
    assert obs[0].was_present is True


# --- Review hardening -------------------------------------------------------------
async def test_whois_redaction_is_not_unverified(session, monkeypatch):
    async def _registration(domain):
        return {
            "registrant_organization": None, "registrar_name": "Contact Privacy Inc.",
            "whois_visibility": "REDACTED", "created_date": None, "source": "live",
        }

    monkeypatch.setattr("app.source_authority.enrichment.registration_lookup", _registration)
    r = await _seed_response(session, response_id="r1", sources=[_src("fda.gov")])
    await svc.classify_response(session, r)
    sd = await _domain(session, "fda.gov")
    assert sd.verification == "VERIFIED"           # curated -> verified despite redaction
    assert sd.whois_visibility == "REDACTED"       # recorded separately


async def test_stub_never_fabricates_owner_or_verifies(session):
    r = await _seed_response(session, response_id="r1", sources=[_src("some-random-blog.xyz")])
    await svc.classify_response(session, r)
    sd = await _domain(session, "some-random-blog.xyz")
    assert sd.registrant_organization is None      # no fabricated ownership
    assert sd.verification == "UNVERIFIED"


async def test_llm_enrichment_precedence(session, monkeypatch):
    async def _llm(domain, normalized_host=None, *, registrant_org=None):
        return {
            "authority_type": "MEDICAL_REFERENCE", "publisher": "Unknown Health Portal",
            "confidence": 0.88, "evidence": ["about page describes a health information site"],
            "requires_review": False, "source": "live",
        }

    monkeypatch.setattr("app.source_authority.enrichment.classify_domain_llm", _llm)
    r = await _seed_response(session, response_id="r1", sources=[_src("unknown-health-portal.io")])
    await svc.classify_response(session, r)
    sd = await _domain(session, "unknown-health-portal.io")
    assert sd.authority_type == "MEDICAL_REFERENCE"
    assert sd.classification_source == "LLM"
    assert sd.classification_status == "AUTO_CLASSIFIED"
    assert sd.verification == "UNKNOWN"            # inferred, not curated
    assert sd.requires_review                      # 0.88 is in the apply..auto band
    assert sd.publisher_name == "Unknown Health Portal"


async def test_curated_beats_llm(session, monkeypatch):
    calls = {"n": 0}

    async def _llm(domain, normalized_host=None, *, registrant_org=None):
        calls["n"] += 1
        return {"authority_type": "SOCIAL_UGC", "publisher": None, "confidence": 0.99,
                "evidence": [], "requires_review": False, "source": "live"}

    monkeypatch.setattr("app.source_authority.enrichment.classify_domain_llm", _llm)
    r = await _seed_response(session, response_id="r1", sources=[_src("fda.gov")])
    await svc.classify_response(session, r)
    sd = await _domain(session, "fda.gov")
    assert sd.authority_type == "REGULATORY"       # curated wins; LLM never consulted
    assert sd.classification_source == "CONFIG"
    assert calls["n"] == 0                          # curated domains skip the LLM entirely


async def test_domain_cache_prevents_repeat_enrichment(session, monkeypatch):
    calls = {"n": 0}

    async def _registration(domain):
        calls["n"] += 1
        return {"registrant_organization": None, "registrar_name": None,
                "whois_visibility": None, "created_date": None, "source": "stub"}

    monkeypatch.setattr("app.source_authority.enrichment.registration_lookup", _registration)
    await _seed_response(session, response_id="r1", sources=[_src("fda.gov")])
    await _seed_response(session, response_id="r2", sources=[_src("fda.gov")])
    for rid in ("r1", "r2"):
        await svc.classify_response(session, await session.get(Response, rid))
    assert calls["n"] == 1                          # classified once, cached thereafter


async def test_llm_low_confidence_not_applied_but_flagged(session, monkeypatch):
    async def _llm(domain, normalized_host=None, *, registrant_org=None):
        return {"authority_type": "HEALTH_MEDIA", "publisher": None, "confidence": 0.55,
                "evidence": ["weak signal"], "requires_review": True, "source": "live"}

    monkeypatch.setattr("app.source_authority.enrichment.classify_domain_llm", _llm)
    r = await _seed_response(session, response_id="r1", sources=[_src("mystery-site.io")])
    await svc.classify_response(session, r)
    sd = await _domain(session, "mystery-site.io")
    assert sd.authority_type == "OTHER"            # below the apply threshold -> not applied
    assert sd.verification == "UNVERIFIED"
    assert sd.requires_review                      # still surfaced for manual classification


async def test_llm_high_confidence_auto_no_review(session, monkeypatch):
    async def _llm(domain, normalized_host=None, *, registrant_org=None):
        return {"authority_type": "PEER_REVIEWED", "publisher": "Example Journal",
                "confidence": 0.96, "evidence": ["masthead lists an ISSN and editorial board"],
                "requires_review": False, "source": "live"}

    monkeypatch.setattr("app.source_authority.enrichment.classify_domain_llm", _llm)
    r = await _seed_response(session, response_id="r1", sources=[_src("unlisted-journal.org")])
    await svc.classify_response(session, r)
    sd = await _domain(session, "unlisted-journal.org")
    assert sd.authority_type == "PEER_REVIEWED"
    assert sd.classification_source == "LLM"
    assert not sd.requires_review                  # >= auto threshold -> auto-applied
    assert sd.classification_confidence == pytest.approx(0.96)
    evidence = json.loads(sd.classification_evidence)
    assert evidence and "issn" in evidence[0].lower()


def test_parse_rdap_public_redacted_and_nodata():
    public = enrichment._parse_rdap({
        "objectClassName": "domain", "ldhName": "EXAMPLE.ORG",
        "entities": [
            {"roles": ["registrant"],
             "vcardArray": ["vcard", [["version", {}, "text", "4.0"],
                                      ["fn", {}, "text", "Example Foundation"],
                                      ["org", {}, "text", "Example Foundation Inc"]]]},
            {"roles": ["registrar"],
             "vcardArray": ["vcard", [["fn", {}, "text", "Example Registrar LLC"]]]},
        ],
        "events": [{"eventAction": "registration", "eventDate": "2001-01-01T00:00:00Z"}],
    })
    assert public["registrant_organization"] == "Example Foundation Inc"
    assert public["registrar_name"] == "Example Registrar LLC"
    assert public["whois_visibility"] == "PUBLIC"
    assert public["created_date"].startswith("2001")
    assert public["source"] == "live"

    redacted = enrichment._parse_rdap({
        "ldhName": "PRIVATE.COM",
        "entities": [{"roles": ["registrant"],
                      "vcardArray": ["vcard", [["fn", {}, "text", "REDACTED FOR PRIVACY"]]]}],
        "remarks": [{"title": "REDACTED FOR PRIVACY",
                     "description": ["Some fields have been redacted."]}],
    })
    assert redacted["registrant_organization"] is None   # never surface the mask as an owner
    assert redacted["whois_visibility"] == "REDACTED"

    empty = enrichment._parse_rdap({})
    assert empty["registrant_organization"] is None
    assert empty["whois_visibility"] == "NO_DATA"


def test_parse_site_metadata_extracts_evidence():
    html = (
        "<html><head><title>Mayo Clinic - Trusted Health Information</title>"
        '<meta name="description" content="Medical info from Mayo Clinic.">'
        '<meta property="og:site_name" content="Mayo Clinic">'
        '<script type="application/ld+json">{"@type":"MedicalOrganization",'
        '"name":"Mayo Foundation for Medical Education and Research"}</script>'
        "</head><body>...</body></html>"
    )
    meta = enrichment._parse_site_metadata(html)
    assert "Mayo Clinic" in meta["title"]
    assert meta["description"] == "Medical info from Mayo Clinic."
    assert meta["site_name"] == "Mayo Clinic"
    assert meta["organization"] == "Mayo Foundation for Medical Education and Research"


async def test_guideline_domain_is_curated_and_trusted(session):
    # nccn.org is in the curated `guideline` list -> GUIDELINE, INDEPENDENT, VERIFIED.
    r = await _seed_response(session, response_id="r1", sources=[_src("nccn.org")])
    await svc.classify_response(session, r)
    sd = await _domain(session, "nccn.org")
    assert sd.authority_type == "GUIDELINE"
    assert sd.display_category == "GUIDELINE"
    assert sd.control_type == "INDEPENDENT"
    assert sd.verification == "VERIFIED"
    assert sd.classification_source == "CONFIG"


async def test_curation_candidates_ranks_uncurated_cited_domains(session, monkeypatch):
    async def _llm(domain, normalized_host=None, *, registrant_org=None):
        if domain == "flagged-portal.io":
            return {"authority_type": "MEDICAL_REFERENCE", "publisher": "Portal",
                    "confidence": 0.8, "evidence": ["about page says health info"],
                    "requires_review": False, "source": "live"}
        return None  # unknown-junk.io -> stays UNCLASSIFIED

    monkeypatch.setattr("app.source_authority.enrichment.classify_domain_llm", _llm)
    r1 = await _seed_response(session, response_id="r1", sources=[
        _src("flagged-portal.io"), _src("flagged-portal.io", "/a"), _src("fda.gov"),
    ])
    await svc.classify_response(session, r1)
    r2 = await _seed_response(session, response_id="r2", sources=[_src("unknown-junk.io")])
    await svc.classify_response(session, r2)

    q = await svc.curation_candidates(session)
    listed = [i["authority_domain"] for i in q["items"]]
    assert "fda.gov" not in listed                 # curated -> excluded from the queue
    assert listed[0] == "flagged-portal.io"         # most-cited candidate ranks first
    assert "unknown-junk.io" in listed

    flagged = next(i for i in q["items"] if i["authority_domain"] == "flagged-portal.io")
    assert flagged["requires_review"] is True       # 0.8 is in the apply..auto band
    assert flagged["suggested_authority"] == "MEDICAL_REFERENCE"
    assert flagged["citation_count"] == 2
    assert flagged["evidence"]
    junk = next(i for i in q["items"] if i["authority_domain"] == "unknown-junk.io")
    assert junk["classification_status"] == "UNCLASSIFIED"


async def test_coverage_excludes_parametric_targets(session):
    r1 = await _seed_response(session, response_id="r1", llm_name="gpt-4o", sources=[_src("fda.gov")])
    await svc.classify_response(session, r1)
    r2 = await _seed_response(session, response_id="r2", llm_name="claude", sources=None)  # parametric
    await svc.classify_response(session, r2)

    cov = await svc.coverage(session)
    assert cov["states"]["NO_CITATION_CAPABILITY"] == 1
    assert cov["citation_capable"] == 1
    assert cov["with_citations"] == 1
    assert cov["coverage_pct"] == 100.0


async def test_coverage_excludes_legacy_bedrock_claude(session, monkeypatch):
    """After `claude` becomes citation-capable (direct Anthropic API), pre-cutover Bedrock
    Claude answers (no web search, no sources) must stay OUT of the coverage denominator so
    they don't drag coverage down. New Anthropic-API Claude answers count normally."""
    monkeypatch.setattr(svc, "_citation_capable_llm_names", lambda: {"gpt-4o", "gemini", "claude"})
    # New direct-Anthropic Claude (bare model id) WITH citations -> classified + counted.
    new = await _seed_response(
        session, response_id="c-new", llm_name="claude",
        model_version="claude-sonnet-4-5-20250929", sources=[_src("fda.gov")],
    )
    await svc.classify_response(session, new)
    # Legacy Bedrock Claude (the "anthropic." ARN namespace) with no sources -> excluded.
    await _seed_response(
        session, response_id="c-old", llm_name="claude",
        model_version="us.anthropic.claude-sonnet-4-5-20250929-v1:0", sources=None,
    )

    cov = await svc.coverage(session)
    assert cov["states"]["NO_CITATION_CAPABILITY"] == 1   # the legacy Bedrock row
    assert cov["citation_capable"] == 1                    # only the new grounded row counts
    assert cov["with_citations"] == 1
    assert cov["coverage_pct"] == 100.0


async def test_backfill_sweep_is_idempotent(session):
    await _seed_response(session, response_id="r1", sources=[_src("fda.gov")])
    first = await svc.classify_unclassified_sweep(session)
    second = await svc.classify_unclassified_sweep(session)
    assert first["processed"] == 1
    assert first["remaining"] == 0
    assert second["processed"] == 0


async def test_backfill_ignores_empty_source_lists(session):
    await _seed_response(session, response_id="r1", sources=[])
    first = await svc.classify_unclassified_sweep(session)
    second = await svc.classify_unclassified_sweep(session)
    assert first["processed"] == 0
    assert first["remaining"] == 0
    assert second["processed"] == 0


async def test_legacy_alerts_table_is_rebuilt_for_nullable_score_id(tmp_path):
    """A pre-FR-706a alerts table (score_id NOT NULL, no entity_type) must be rebuilt in place,
    preserving existing rows and then allowing NULL score_id source-authority alerts."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}"
    engine_ = create_async_engine(url)

    # 1. Simulate the legacy schema: old columns, NOT NULL score_id, named ix_alerts_* indexes.
    async with engine_.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE alerts ("
            " alert_id VARCHAR(64) PRIMARY KEY, score_id VARCHAR(64) NOT NULL,"
            " response_id VARCHAR(64) NOT NULL, rule_triggered VARCHAR(48) NOT NULL,"
            " detail TEXT, acknowledged BOOLEAN DEFAULT 0, created_at DATETIME)"
        ))
        await conn.execute(text("CREATE INDEX ix_alerts_score_id ON alerts (score_id)"))
        await conn.execute(text("CREATE INDEX ix_alerts_response_id ON alerts (response_id)"))
        await conn.execute(text("CREATE INDEX ix_alerts_rule_triggered ON alerts (rule_triggered)"))
        await conn.execute(text(
            "INSERT INTO alerts (alert_id, score_id, response_id, rule_triggered, detail,"
            " acknowledged, created_at) VALUES"
            " ('a1', 's1', 'r1', 'LOW_SENTIMENT', 'legacy', 0, '2024-01-01 00:00:00')"
        ))

    # 2. create_all skips the existing (legacy) alerts table; the migration then rebuilds it.
    async with engine_.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_sqlite_schema(conn)

    # 3. New columns exist, the legacy row is preserved & stamped SCORE, and NULL score_id inserts.
    async with engine_.begin() as conn:
        cols = await conn.run_sync(lambda c: {col["name"] for col in inspect(c).get_columns("alerts")})
        assert {"entity_type", "entity_id"} <= cols
        etype, sid = (await conn.execute(
            text("SELECT entity_type, score_id FROM alerts WHERE alert_id='a1'")
        )).one()
        assert etype == "SCORE" and sid == "s1"
        await conn.execute(text(
            "INSERT INTO alerts (alert_id, score_id, response_id, entity_type, entity_id,"
            " rule_triggered, acknowledged, created_at) VALUES"
            " ('a2', NULL, 'r2', 'SOURCE_AUTHORITY', 'r2', 'ONLY_COMPETITOR_SOURCES', 0,"
            " '2024-01-02 00:00:00')"
        ))
        count = (await conn.execute(text("SELECT COUNT(*) FROM alerts"))).scalar_one()
        assert count == 2

    await engine_.dispose()


# --- Retired OpenEvidence: kept in DB, hidden from every Source Authority view ----
async def test_open_evidence_citations_hidden_from_source_authority(session):
    """OpenEvidence's manual capture was retired for the automated EvidenceMD target. Its
    historical citations stay in the DB (nothing deleted) but must not surface in ANY Source
    Authority view, while other grounded targets are unaffected."""
    gpt = await _seed_response(session, response_id="r-gpt", llm_name="gpt-4o", sources=[_src("fda.gov")])
    oe = await _seed_response(session, response_id="r-oe", llm_name="open-evidence", sources=[_src("nejm.org")])
    for r in (gpt, oe):
        await svc.classify_response(session, r)

    # Data is preserved — the OpenEvidence citation row still exists.
    oe_rows = (await session.execute(
        select(ResponseCitation).where(ResponseCitation.llm_name == "open-evidence")
    )).scalars().all()
    assert oe_rows, "OpenEvidence citations must remain in the DB (hidden, not deleted)"

    # Per-model breakdown: no open-evidence column; visible target still present.
    grouped = await svc.top_domains(session, group_by="llm_name", limit=10)
    models = {g["llm_name"] for g in grouped["groups"]}
    assert "open-evidence" not in models
    assert "gpt-4o" in models

    # nejm.org is cited ONLY by OpenEvidence, so it must not appear in the flat domain list.
    flat = await svc.top_domains(session, limit=50)
    assert {i["authority_domain"] for i in flat["items"]} == {"fda.gov"}

    # Totals and explicit-filter selection both exclude OpenEvidence.
    assert (await svc.distribution(session))["total_citations"] == 1
    assert (await svc.top_domains(session, llm_name="open-evidence", limit=10))["items"] == []

    # Coverage denominator (response-based) also drops the OpenEvidence response.
    assert (await svc.coverage(session))["total_responses"] == 1


# --- Influence Graph : Source -> Claim -> Theme -> Position provenance web --------
async def _ensure_theme(session, label):
    tid = f"theme-{label}"
    if await session.get(Theme, tid) is None:
        session.add(Theme(theme_id=tid, taxonomy_version=1, label=label))
        await session.commit()
    return tid


async def _seed_grounded(
    session, *, response_id, sources, supports, llm_name="gpt-4o", ta="Immunology",
    brand="Humira", persona="Provider", position=None, sentiment=None, themes=(),
    response_text="...",
):
    r = Response(
        response_id=response_id, run_id="run1", llm_name=llm_name, persona=persona,
        question_id=f"q-{response_id}", question_text="Best biologic for RA?",
        therapeutic_area=ta, brand_focus=brand, domain="Comparative",
        response_text=response_text, status="SUCCESS",
        sources=json.dumps(sources), grounding_supports=json.dumps(supports),
    )
    session.add(r)
    await session.commit()
    await svc.classify_response(session, r)
    if position is not None or sentiment is not None:
        session.add(ScoringRecord(
            score_id=f"s-{response_id}", response_id=response_id,
            competitive_position=position, sentiment_score=sentiment,
        ))
    for label in themes:
        tid = await _ensure_theme(session, label)
        session.add(ResponseTheme(
            id=f"rt-{response_id}-{tid}", response_id=response_id, theme_id=tid, taxonomy_version=1,
        ))
    await session.commit()


async def _seed_two_grounded(session):
    # r1 cites a regulatory source for one claim and a competitor for another.
    await _seed_grounded(
        session, response_id="r1",
        sources=[_src("fda.gov"), _src("stelara.com")],
        supports=[
            {"text": "Efficacy is strong", "source_indices": [0]},
            {"text": "Competitor is preferred", "source_indices": [1]},
        ],
        position="NOT_RECOMMENDED", sentiment=-0.4, themes=["Efficacy"],
    )
    # r2 repeats the SAME competitor-backed claim (tests claim dedup across responses).
    await _seed_grounded(
        session, response_id="r2",
        sources=[_src("stelara.com")],
        supports=[{"text": "Competitor is preferred", "source_indices": [0]}],
        position="NOT_RECOMMENDED", sentiment=-0.6, themes=["Efficacy"],
    )


def test_claim_display_label_cleanup_and_fallback():
    assert svc._claim_display_label(None, {"text": "*   Low-dose **cytarabine** (LDAC)"}) == "Low-dose cytarabine (LDAC)"
    citation = "([fda.gov](https://www.fda.gov/example?utm_source=openai))"
    assert svc._claim_display_label(None, {"text": citation}) == "Supporting citation from fda.gov"
    assert svc._claim_display_label("short", {"text": citation, "start_index": 999}) == "Supporting citation from fda.gov"
    assert svc._pick_claim_display({"Second": 1, "First": 1}) == "First"
    assert svc._pick_claim_display({"Second": 2, "First": 1}) == "Second"
    assert len(svc._clip_claim_display("word " * 100)) <= 181


async def test_influence_graph_claim_display_label_preserves_raw_identity(session):
    statement = "- **Infliximab** is recommended as a first-line biologic. "
    citation = "([fda.gov](https://www.fda.gov/example?utm_source=openai))"
    await _seed_grounded(
        session,
        response_id="claim-display",
        sources=[_src("fda.gov")],
        supports=[{
            "text": citation,
            "source_indices": [0],
            "start_index": len(statement),
            "end_index": len(statement) + len(citation),
        }],
        themes=["Efficacy"],
        response_text=statement + citation,
    )

    graph = await svc.influence_graph(session)
    claim = next(node for node in graph["nodes"] if node["type"] == "claim")
    assert claim["display_label"] == "Infliximab is recommended as a first-line biologic."
    assert claim["id"] == f"claim:{svc._norm_claim(citation)}"
    assert claim["label"] == citation[:90]
    assert claim["text"] == citation
    assert claim["weight"] == 1
    assert any(link["source"] == "src:fda.gov" and link["target"] == claim["id"] for link in graph["links"])


async def test_influence_graph_coverage_and_chain(session):
    await _seed_two_grounded(session)
    # A parametric answer (no sources): counts toward the denominator, invisible in the web.
    await _seed_response(session, response_id="r3", sources=None)

    g = await svc.influence_graph(session)
    m = g["meta"]
    assert m["total_responses"] == 3
    assert m["grounded_responses"] == 2
    assert m["coverage_pct"] == 66.7

    types: dict[str, int] = {}
    for n in g["nodes"]:
        types[n["type"]] = types.get(n["type"], 0) + 1
    assert types["source"] == 2       # fda.gov + stelara.com
    assert types["theme"] == 1        # Efficacy
    assert types["position"] == 1     # NOT_RECOMMENDED

    # Claim dedup: the shared "Competitor is preferred" claim is ONE node spanning r1 + r2.
    claims = [n for n in g["nodes"] if n["type"] == "claim"]
    assert len(claims) == 2
    shared = next(n for n in claims if "competitor" in n["label"].lower())
    assert shared["weight"] == 2

    # Link weights: stelara -> shared claim carried by 2 answers; theme -> position by 2.
    assert any(
        l["source"] == "src:stelara.com" and l["target"] == "claim:competitor is preferred" and l["value"] == 2
        for l in g["links"]
    )
    assert any(
        l["source"] == "theme:Efficacy" and l["target"] == "pos:NOT_RECOMMENDED" and l["value"] == 2
        for l in g["links"]
    )

    # Theme drivers (the punchline): stelara.com drives 100% of the Efficacy narrative.
    td = next(t for t in m["theme_drivers"] if t["theme"] == "Efficacy")
    assert td["theme_responses"] == 2
    tops = {s["authority_domain"]: s for s in td["top_sources"]}
    assert tops["stelara.com"]["responses"] == 2
    assert tops["stelara.com"]["share_pct"] == 100.0
    assert tops["stelara.com"]["control_type"] == "COMPETITOR"
    assert tops["fda.gov"]["share_pct"] == 50.0


async def test_influence_graph_theme_and_focus_filters(session):
    await _seed_two_grounded(session)

    # Narrative focus keeps only answers tagged with that theme.
    assert (await svc.influence_graph(session, theme="Efficacy"))["meta"]["grounded_responses"] == 2
    empty = await svc.influence_graph(session, theme="Nonexistent")
    assert empty["meta"]["grounded_responses"] == 0
    assert empty["nodes"] == []

    # Focusing one source drops claims that domain does not back.
    gf = await svc.influence_graph(session, focus_domain="stelara.com")
    src_ids = {n["id"] for n in gf["nodes"] if n["type"] == "source"}
    assert "src:stelara.com" in src_ids
    assert "src:fda.gov" not in src_ids  # its only claim was not stelara-backed
    claim_labels = [n["label"].lower() for n in gf["nodes"] if n["type"] == "claim"]
    assert all("efficacy is strong" not in c for c in claim_labels)


async def test_influence_graph_empty_db(session):
    g = await svc.influence_graph(session)
    assert g["nodes"] == [] and g["links"] == []
    assert g["meta"]["total_responses"] == 0
    assert g["meta"]["coverage_pct"] == 0.0
    assert g["meta"]["theme_drivers"] == []


async def test_influence_graph_api(api):
    client, maker = api
    async with maker() as s:
        await _seed_two_grounded(s)
    resp = await client.get("/source-authority/influence-graph")
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["grounded_responses"] == 2
    assert any(n["type"] == "source" for n in body["nodes"])
    # top_n is bounded (ge=10): a too-small value is rejected.
    assert (await client.get("/source-authority/influence-graph?top_n=5")).status_code == 422


async def test_node_evidence_theme_and_position(session):
    await _seed_two_grounded(session)

    # Narrative drill-down: both Efficacy answers, each carrying its latest score.
    theme_ev = await svc.node_evidence(session, node_type="theme", key="Efficacy")
    assert theme_ev["response_count"] == 2
    assert {i["response_id"] for i in theme_ev["items"]} == {"r1", "r2"}
    assert all(i["competitive_position"] == "NOT_RECOMMENDED" for i in theme_ev["items"])

    # Position drill-down: answers whose latest position is NOT_RECOMMENDED.
    pos_ev = await svc.node_evidence(session, node_type="position", key="NOT_RECOMMENDED")
    assert pos_ev["response_count"] == 2
    assert {i["response_id"] for i in pos_ev["items"]} == {"r1", "r2"}

    # Misses / unsupported types / non-matching filters return empty (never error).
    assert (await svc.node_evidence(session, node_type="theme", key="Nonexistent"))["response_count"] == 0
    assert (await svc.node_evidence(session, node_type="position", key="FIRST_LINE_RECOMMENDED"))["response_count"] == 0
    assert (await svc.node_evidence(session, node_type="source", key="fda.gov"))["items"] == []
    assert (
        await svc.node_evidence(session, node_type="theme", key="Efficacy", llm_name="nonexistent-model")
    )["response_count"] == 0


async def test_node_evidence_api(api):
    client, maker = api
    async with maker() as s:
        await _seed_two_grounded(s)
    resp = await client.get(
        "/source-authority/influence-graph/node-evidence",
        params={"node_type": "position", "key": "NOT_RECOMMENDED"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["response_count"] == 2
    assert {i["response_id"] for i in body["items"]} == {"r1", "r2"}
    # node_type + key are required query params.
    assert (await client.get("/source-authority/influence-graph/node-evidence")).status_code == 422
