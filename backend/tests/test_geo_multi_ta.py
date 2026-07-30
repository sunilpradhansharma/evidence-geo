"""GEO multi-therapeutic-area behaviour.

Two defects are guarded here.

The first is drift: the committed ``schema/*.json`` corpus is what the loader serves,
but it is GENERATED from ``source/*.yaml``. When the Immunology split edited the source
files and nobody re-ran the generator, the served corpus kept answering "Immunology"
for weeks. ``test_committed_corpus_matches_its_source`` is the guard whose absence
allowed that: ``test_llms_txt_is_in_sync_with_schema`` compares two generated artifacts
to each other, so it stays green while both are equally stale.

The second is flattening: a brand indicated in several specialties does not have one
competitive field. Serving Humira's psoriasis comparators for a Crohn's question is the
exact error the disease overlay was introduced to remove from scoring, and it survived
in the Chairman's GEO fallback because ``get_geo_context`` discarded the area it was
handed.
"""
import json

import pytest

from app.config import taxonomy
from app.geo import builder, loader
from app.geo.schema_model import CompetitorContext, DrugSchema

# Fields that come purely from the curated YAML. Label-seeded fields (labelReference,
# labelSource, dataSource, lastUpdated) are deliberately excluded: they depend on a
# live openFDA response and would make this test a network flake rather than a guard.
_CURATED_KEYS = (
    "name",
    "nonProprietaryName",
    "drugClass",
    "administrationRoute",
    "activeIngredient",
    "availableStrength",
    "indication",
    "adverseOutcome",
    "clinicalEfficacy",
    "competitorContext",
)


def _sources() -> list[dict]:
    return builder.load_sources()


def _committed(brand: str) -> dict:
    path = loader.SCHEMA_DIR / f"{builder._slug(brand)}.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---- drift: generated corpus vs its source ---------------------------------------------

@pytest.mark.parametrize("src", _sources(), ids=lambda s: s["brand"])
def test_committed_corpus_matches_its_source(src):
    """Every curated field on disk must equal a fresh build of its source YAML."""
    fresh = builder.build_jsonld(src, None, generated_at="ignored")
    on_disk = _committed(src["brand"])
    for key in _CURATED_KEYS:
        assert on_disk.get(key) == fresh.get(key), (
            f"{src['brand']}: {key!r} in schema/{builder._slug(src['brand'])}.json does not "
            f"match source/*.yaml — re-run `python -m scripts.generate_geo_schema`"
        )


@pytest.mark.parametrize("src", _sources(), ids=lambda s: s["brand"])
def test_declared_areas_are_real_taxonomy_keys(src):
    """An area that is not a brands.yaml key would be invisible to every TA filter."""
    assert builder.validate_source(src) == []
    assert builder.declared_areas(src), f"{src['brand']} declares no therapeutic area"


def test_retired_area_is_absent_from_the_served_corpus():
    """Immunology is no longer a taxonomy key, so it must not be served as one."""
    for path in sorted(loader.SCHEMA_DIR.glob("*.json")):
        assert "Immunology" not in path.read_text(encoding="utf-8"), (
            f"{path.name} still names the retired Immunology area"
        )


def test_an_unknown_area_fails_generation():
    errors = builder.validate_source({"brand": "Test", "therapeutic_areas": ["Immunology"]})
    assert errors and "Immunology" in errors[0]


# ---- multi-area declaration -------------------------------------------------------------

def test_a_multi_indication_brand_declares_every_area():
    """Rinvoq is not a single-area brand and the corpus must not claim it is."""
    areas = set(builder.declared_areas(next(s for s in _sources() if s["brand"] == "Rinvoq")))
    assert areas == {"Dermatology", "Gastroenterology", "Rheumatology"}


def test_legacy_single_and_slash_joined_forms_still_read():
    """An un-migrated source file must still generate rather than silently lose its area."""
    assert builder.declared_areas({"therapeutic_area": "Oncology"}) == ["Oncology"]
    assert builder.declared_areas(
        {"therapeutic_area": "Dermatology / Gastroenterology"}
    ) == ["Dermatology", "Gastroenterology"]


def test_areas_helper_tolerates_both_shapes():
    assert CompetitorContext(therapeuticAreas=["Dermatology"]).areas() == ["Dermatology"]
    assert CompetitorContext(therapeuticArea="A / B").areas() == ["A", "B"]
    assert CompetitorContext().areas() == []


# ---- competitors are derived, per indication --------------------------------------------

def test_competitors_are_derived_from_the_disease_overlay():
    """The GEO field and the scored field must come from the same accessor."""
    src = next(s for s in _sources() if s["brand"] == "Rinvoq")
    by_indication = builder.competitors_by_indication(src)
    for disease, names in by_indication.items():
        assert names == list(taxonomy.competitors_for_disease(disease)), disease


def test_one_brand_gets_a_different_field_per_indication():
    """The headline fix: Rinvoq's AD comparators are not its RA comparators."""
    src = next(s for s in _sources() if s["brand"] == "Rinvoq")
    by_indication = builder.competitors_by_indication(src)
    ad = set(by_indication["Atopic Dermatitis"])
    ra = set(by_indication["Rheumatoid Arthritis"])
    assert "Dupixent" in ad and "Dupixent" not in ra
    assert "Xeljanz" in ra and "Xeljanz" not in ad


def test_a_brand_is_never_its_own_competitor():
    for src in _sources():
        brand = src["brand"].lower()
        for names in builder.competitors_by_indication(src).values():
            assert brand not in {n.lower() for n in names}


def test_an_indication_outside_the_overlay_falls_back_to_its_area():
    """An indication the overlay does not declare has no field of its own, so the area
    block is the most precise thing that can honestly be served.

    The example moved from "all of Imbruvica" to Marginal Zone Lymphoma specifically: the
    overlay now declares CLL, MCL, Waldenstrom's and AML, so those three are no longer
    fallbacks. MZL is still undeclared and is what the fallback path now looks like.
    """
    src = next(s for s in _sources() if s["brand"] == "Imbruvica")
    by_indication = builder.competitors_by_indication(src)
    assert taxonomy.competitors_for_disease("Marginal Zone Lymphoma") == (), (
        "MZL gained an overlay entry — repoint this test at another undeclared indication"
    )
    assert set(by_indication["Marginal Zone Lymphoma"]) <= set(
        taxonomy.competitors_for_key("Oncology")
    )


def test_a_declared_indication_beats_the_area_fallback():
    """The other half of the same rule, and the reason the overlay was extended: once an
    indication IS declared, the area block must stop being used. Waldenstrom's drops
    Calquence (no WM indication) and AML shares nothing with the CLL/lymphoma block."""
    src = next(s for s in _sources() if s["brand"] == "Imbruvica")
    area = set(taxonomy.competitors_for_key("Oncology"))
    wm = set(builder.competitors_by_indication(src)["Waldenstrom's Macroglobulinemia"])
    assert wm == set(taxonomy.competitors_for_disease("Waldenstrom's Macroglobulinemia"))
    assert "Calquence" not in wm, "Calquence holds no WM indication"
    # Not merely a subset of the area block: it names Rituxan, which the block omits. A
    # declared field REPLACES the fallback, it does not filter it.
    assert not wm <= area

    ven = next(s for s in _sources() if s["brand"] == "Venclexta")
    aml = set(builder.competitors_by_indication(ven)["Acute Myeloid Leukemia"])
    assert not aml & set(taxonomy.competitors_for_key("Oncology"))


def test_a_multi_area_brand_refuses_to_guess_an_unmapped_indication():
    """With three areas in play there is no non-arbitrary block to fall back to."""
    derived = builder.competitors_by_indication(
        {
            "brand": "Rinvoq",
            "therapeutic_areas": ["Dermatology", "Gastroenterology", "Rheumatology"],
            "indications": [{"name": "Not A Real Disease"}],
        }
    )
    assert derived == {}


def test_curated_competitors_survive_derivation():
    """Oncology's curated names carry generics the taxonomy does not — keep them."""
    doc = _committed("Imbruvica")
    assert "Calquence (acalabrutinib)" in doc["competitorContext"]["keyCompetitors"]


# ---- read-time narrowing ----------------------------------------------------------------

def test_geo_context_narrows_the_field_to_the_question_disease():
    loader.reload()
    ad = loader.get_geo_context("Rinvoq", "Dermatology", "Atopic Dermatitis")
    competitors = ad["schema"]["competitors"]["keyCompetitors"]
    assert "Dupixent" in competitors
    assert "Xeljanz" not in competitors, "an RA comparator leaked into an AD question"
    assert ad["schema"]["competitors"]["competitorScope"] == ["Atopic Dermatitis"]


def test_geo_context_narrows_the_same_brand_differently_per_disease():
    loader.reload()
    ra = loader.get_geo_context("Rinvoq", "Rheumatology", "Rheumatoid Arthritis")
    ad = loader.get_geo_context("Rinvoq", "Dermatology", "Atopic Dermatitis")
    ra_field = set(ra["schema"]["competitors"]["keyCompetitors"])
    ad_field = set(ad["schema"]["competitors"]["keyCompetitors"])
    assert ra_field != ad_field
    assert not ra_field & ad_field, "the two indications should not share comparators"


def test_geo_context_falls_back_to_the_area_without_a_disease():
    loader.reload()
    gi = loader.get_geo_context("Humira", "Gastroenterology")
    competitors = set(gi["schema"]["competitors"]["keyCompetitors"])
    assert {"Entyvio", "Stelara"} <= competitors
    assert "Xeljanz" not in competitors or "Ulcerative Colitis" in str(
        gi["schema"]["competitors"].get("competitorScope")
    )


def test_geo_context_serves_the_union_when_it_cannot_narrow():
    """No disease and no usable area must degrade to everything, never to nothing."""
    loader.reload()
    ctx = loader.get_geo_context("Rinvoq", "")
    competitors = ctx["schema"]["competitors"]["keyCompetitors"]
    assert len(competitors) > 5
    assert "competitorScope" not in ctx["schema"]["competitors"]


def test_an_unknown_disease_degrades_rather_than_emptying_the_field():
    loader.reload()
    ctx = loader.get_geo_context("Rinvoq", "Rheumatology", "Not A Real Disease")
    assert ctx["schema"]["competitors"]["keyCompetitors"]


def test_the_served_view_does_not_carry_the_whole_matrix():
    """The prompt gets the resolved field, not every indication's list."""
    loader.reload()
    ctx = loader.get_geo_context("Rinvoq", "Dermatology", "Atopic Dermatitis")
    assert "competitorsByIndication" not in ctx["schema"]["competitors"]


def test_legacy_documents_without_the_map_still_serve():
    """Rows persisted before this change must not start returning empty fields."""
    legacy = {
        "@context": "https://schema.org",
        "@type": "Drug",
        "name": "Legacy",
        "competitorContext": {
            "therapeuticArea": "Immunology",
            "keyCompetitors": ["Stelara", "Dupixent"],
        },
    }
    schema = DrugSchema.model_validate(legacy)
    view = schema.context_view(indications=["Atopic Dermatitis"])
    assert view["competitors"]["keyCompetitors"] == ["Stelara", "Dupixent"]
    assert schema.competitor_context.areas() == ["Immunology"]
