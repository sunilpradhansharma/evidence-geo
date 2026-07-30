"""Tests for the GEO schema data layer: validation, loader auto-discovery, the
generator's curated-overrides-seed merge, llms.txt sync, and openFDA graceful failure."""
import json

import httpx
import pytest

from app.geo import builder, loader
from app.geo.schema_model import DrugSchema
from app.geo.sources import openfda
from app.geo.sources.openfda import LabelSeed

EXPECTED_BRANDS = {"Humira", "Skyrizi", "Rinvoq", "Imbruvica", "Venclexta"}

_CANNED_LABEL = {
    "effective_time": "20240115",
    "boxed_warning": ["WARNING: SERIOUS INFECTIONS. Increased risk ..."],
    "indications_and_usage": ["1 INDICATIONS AND USAGE. Indicated for ..."],
    "adverse_reactions": ["6 ADVERSE REACTIONS. Most common (>10%) ..."],
    "dosage_and_administration": ["2 DOSAGE. 40 mg every other week ..."],
    "set_id": "abc-123",
    "openfda": {
        "brand_name": ["HUMIRA"],
        "generic_name": ["ADALIMUMAB"],
        "manufacturer_name": ["AbbVie Inc."],
        "route": ["SUBCUTANEOUS"],
        "substance_name": ["ADALIMUMAB"],
        "pharm_class_epc": ["Tumor Necrosis Factor Blocker [EPC]"],
    },
}


def _all_schema_docs() -> list[dict]:
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(loader.SCHEMA_DIR.glob("*.json"))
    ]


# ---- validation of the committed corpus -------------------------------------------------

def test_all_committed_schema_files_validate():
    docs = _all_schema_docs()
    assert docs, "no generated schema files found — run scripts.generate_geo_schema"
    for doc in docs:
        schema = DrugSchema.model_validate(doc)  # raises on invalid
        assert schema.name
        assert schema.lookup_aliases()


def test_llms_txt_is_in_sync_with_schema():
    """llms.txt must be a pure function of the schema docs (no manual drift)."""
    rendered = builder.render_llms_txt(_all_schema_docs())
    on_disk = (builder.LLMS_TXT_PATH).read_text(encoding="utf-8")
    assert rendered == on_disk


# ---- loader auto-discovery + aliases ----------------------------------------------------

def test_loader_discovers_all_brands():
    loader.reload()
    available = set(loader.list_available_brands())
    assert EXPECTED_BRANDS.issubset(available)


@pytest.mark.parametrize("alias", ["humira", "HUMIRA", "adalimumab", "Adalimumab"])
def test_get_brand_schema_resolves_brand_and_generic(alias):
    loader.reload()
    schema = loader.get_brand_schema(alias)
    assert schema is not None
    assert schema["name"] == "Humira"


def test_get_geo_context_has_stable_shape():
    loader.reload()
    ctx = loader.get_geo_context("Humira", "Immunology")
    assert ctx is not None
    assert ctx["brand"] == "Humira"
    assert ctx["therapeutic_area"] == "Immunology"
    assert ctx["source"]
    schema = ctx["schema"]
    assert schema["name"] == "Humira"
    assert schema["genericName"] == "adalimumab"
    assert "Rheumatoid Arthritis" in schema["indications"]
    assert all({"name", "severity"} <= set(s) for s in schema["safety"])
    assert "keyCompetitors" in schema["competitors"]


def test_get_geo_context_none_for_unknown_brand():
    loader.reload()
    assert loader.get_geo_context("NoSuchDrug", "Immunology") is None


def test_invalid_schema_file_is_skipped_not_served(tmp_path, monkeypatch):
    valid = {"@context": "https://schema.org", "@type": "Drug", "name": "Zeta", "nonProprietaryName": "zetamab"}
    (tmp_path / "zeta.json").write_text(json.dumps(valid), encoding="utf-8")
    (tmp_path / "broken.json").write_text("{ not valid json", encoding="utf-8")
    (tmp_path / "missing_name.json").write_text(json.dumps({"@type": "Drug"}), encoding="utf-8")

    monkeypatch.setattr(loader, "SCHEMA_DIR", tmp_path)
    loader.reload()
    try:
        assert loader.list_available_brands() == ["Zeta"]
        assert loader.get_brand_schema("zetamab")["name"] == "Zeta"
    finally:
        monkeypatch.undo()
        loader.reload()


# ---- generator merge: curated overrides seed --------------------------------------------

def _seed() -> LabelSeed:
    return LabelSeed(
        manufacturer="WRONG PHARMA",
        drug_class="WRONG CLASS",
        administration_route="Wrong",
        set_id="s1",
        prescribing_information="https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=s1",
        effective_time="2024-01-15",
        seeded_fields=["manufacturer", "drug_class"],
    )


def test_curated_values_override_seed():
    src = {
        "brand": "Humira",
        "generic": "adalimumab",
        "manufacturer": "AbbVie Inc.",
        "drug_class": "TNF-alpha inhibitor",
        "therapeutic_area": "Immunology",
        "prescribing_information": "https://www.rxabbvie.com/pdf/humira.pdf",
    }
    doc = builder.build_jsonld(src, _seed(), generated_at="2025-01-01")
    assert doc["manufacturer"]["name"] == "AbbVie Inc."
    assert doc["drugClass"] == "TNF-alpha inhibitor"
    assert doc["prescribingInformation"] == "https://www.rxabbvie.com/pdf/humira.pdf"
    applied = doc["provenance"]["appliedFromLabel"]
    assert "manufacturer" not in applied
    assert "drug_class" not in applied
    DrugSchema.model_validate(doc)


def test_seed_fills_curated_gaps():
    src = {"brand": "TestDrug", "therapeutic_area": "Immunology"}
    doc = builder.build_jsonld(src, _seed(), generated_at="2025-01-01")
    assert doc["manufacturer"]["name"] == "WRONG PHARMA"
    assert doc["drugClass"] == "WRONG CLASS"
    assert doc["prescribingInformation"].endswith("setid=s1")
    assert "manufacturer" in doc["provenance"]["appliedFromLabel"]
    assert doc["provenance"]["labelSource"] == "openFDA/DailyMed SPL s1"
    assert doc["provenance"]["labelEffectiveTime"] == "2024-01-15"
    DrugSchema.model_validate(doc)


def test_build_jsonld_without_seed_is_valid():
    src = {"brand": "Solo", "generic": "soloxib", "manufacturer": "Acme", "therapeutic_area": "Immunology"}
    doc = builder.build_jsonld(src, None, generated_at="2025-01-01")
    assert doc["name"] == "Solo"
    assert doc["provenance"]["appliedFromLabel"] == []
    assert doc["provenance"]["labelProvided"] == []
    assert doc["provenance"]["labelSource"] is None
    DrugSchema.model_validate(doc)


def test_clinical_values_verified_flag_flows_to_provenance():
    base = {"brand": "Solo", "generic": "soloxib", "manufacturer": "Acme", "therapeutic_area": "Immunology"}
    unverified = builder.build_jsonld(base, None, generated_at="2025-01-01")
    assert unverified["provenance"]["clinicalValuesVerified"] is False
    assert "pending" in unverified["dataSource"].lower()

    verified = builder.build_jsonld({**base, "clinical_values_verified": True}, None, generated_at="2025-01-01")
    assert verified["provenance"]["clinicalValuesVerified"] is True
    assert "verified" in verified["dataSource"].lower()
    assert "pending" not in verified["dataSource"].lower()


# ---- openFDA fetcher --------------------------------------------------------------------

async def test_fetch_label_maps_fields(monkeypatch):
    async def _fake_query(search):
        return _CANNED_LABEL

    monkeypatch.setattr(openfda, "_query", _fake_query)
    seed = await openfda.fetch_label("Humira", "adalimumab")
    assert seed is not None
    assert seed.manufacturer == "AbbVie Inc."
    assert seed.administration_route == "Subcutaneous"       # title-cased
    assert seed.drug_class == "Tumor Necrosis Factor Blocker"  # [EPC] stripped
    assert seed.has_boxed_warning is True
    assert seed.effective_time == "2024-01-15"
    assert seed.prescribing_information.endswith("setid=abc-123")
    assert "manufacturer" in seed.seeded_fields


async def test_fetch_label_returns_none_when_no_data(monkeypatch):
    async def _none_query(search):
        return None

    monkeypatch.setattr(openfda, "_query", _none_query)
    assert await openfda.fetch_label("Ghost", "ghostium") is None


async def test_query_swallows_transport_errors(monkeypatch):
    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(openfda.httpx, "AsyncClient", _FakeClient)
    assert await openfda._query('openfda.brand_name:"X"') is None
