"""Multi-TA + indication-aware taxonomy (Phase 1 of the evidence/NMA programme).

Root causes this suite pins down (all verified in code before the fix):

* ``taxonomy.alias_index()`` emits three ``"rinvoq"`` entries (Dermatology,
  Gastroenterology and Rheumatology), all ``kind="brand"`` and all length 6, so
  ``prompt_volume.mapping.map_query`` ranks them identically and brands.yaml
  ordering silently decides the therapeutic area.
* ``map_query`` discarded the indication signal entirely because a drug alias
  always outranks a disease alias.
* ``scoring.scorer._context_for`` read competitors at therapeutic-area block
  level only, so a Rinvoq Atopic Dermatitis question was scored against
  Rheumatoid Arthritis competitors.
* ``services.question_service._derive_ta`` asserted "a focus brand maps to
  exactly one TA", which is false for Rinvoq and Humira.

The first test is the **regression guard** demanded by the plan: ``map_query``
feeds Prompt Volume (FR-116), so its pre-existing keys must be byte-identical
for a fixed query corpus before and after the disease work.

The snapshot was **re-baselined once**, when the flat "Immunology" key was split
into Dermatology and Gastroenterology. That split changes the stored taxonomy by
design, so a diff was expected; every subsequent diff is not.
"""
import pytest

from app.config import outcomes, taxonomy
from app.prompt_volume.mapping import map_query
from app.scoring.alert_engine import evaluate_alerts
from app.scoring.scorer import _context_for, _landscape_context
from app.services.question_service import _derive_ta

# Do not regenerate to make a test pass — a diff here means Prompt Volume mapping
# changed for real.
#
# A drug alias outranks a disease alias, and a drug that lives in several blocks is
# resolved by brands.yaml declaration order. That is why "rinvoq for rheumatoid
# arthritis" maps to Dermatology: Rinvoq is declared under Dermatology first. It is a
# search-demand hint, not a classification — ``_derive_ta`` refuses to guess in exactly
# this case (see test_derive_ta_refuses_to_guess_for_a_multi_area_brand).
MAP_QUERY_SNAPSHOT: dict[str, dict] = {
    "rinvoq for rheumatoid arthritis": {
        "therapeutic_area": "Dermatology", "competitor": None, "brand": "Rinvoq", "confidence": 1.0},
    "is rinvoq or dupixent better for atopic dermatitis": {
        "therapeutic_area": "Dermatology", "competitor": "Dupixent", "brand": None, "confidence": 1.0},
    "skyrizi psoriatic arthritis": {
        "therapeutic_area": "Dermatology", "competitor": None, "brand": "Skyrizi", "confidence": 1.0},
    "humira biosimilar cost": {
        "therapeutic_area": "Dermatology", "competitor": None, "brand": "Humira", "confidence": 1.0},
    "lupron depot endometriosis": {
        "therapeutic_area": "Endometriosis", "competitor": None, "brand": "Lupron Depot", "confidence": 1.0},
    "wegovy vs zepbound": {
        "therapeutic_area": "Obesity", "competitor": None, "brand": "Zepbound", "confidence": 1.0},
    "tremfya for plaque psoriasis": {
        "therapeutic_area": "Dermatology", "competitor": "Tremfya", "brand": None, "confidence": 1.0},
    "upadacitinib side effects": {
        "therapeutic_area": "Dermatology", "competitor": None, "brand": "Rinvoq", "confidence": 1.0},
    "what is the best treatment for crohn disease": {
        "therapeutic_area": "Unmapped", "competitor": None, "brand": None, "confidence": 0.0},
    "ozempic weight loss": {
        "therapeutic_area": "Obesity", "competitor": None, "brand": "Ozempic", "confidence": 1.0},
    "vraylar bipolar": {
        "therapeutic_area": "Neuroscience", "competitor": None, "brand": "Vraylar", "confidence": 1.0},
    "random unrelated keyword": {
        "therapeutic_area": "Unmapped", "competitor": None, "brand": None, "confidence": 0.0},
    # "Immunology" is retired: it must map to nothing rather than linger as a ghost area.
    "immunology": {
        "therapeutic_area": "Unmapped", "competitor": None, "brand": None, "confidence": 0.0},
    "dermatology": {
        "therapeutic_area": "Dermatology", "competitor": None, "brand": None, "confidence": 0.6},
    "gastroenterology": {
        "therapeutic_area": "Gastroenterology", "competitor": None, "brand": None, "confidence": 0.6},
    # Was "Immunology" when one flat block held both; PsA is unambiguously Rheumatology now.
    "psoriatic arthritis": {
        "therapeutic_area": "Rheumatology", "competitor": None, "brand": None, "confidence": 0.6},
    "stelara ulcerative colitis": {
        "therapeutic_area": "Dermatology", "competitor": "Stelara", "brand": None, "confidence": 1.0},
}

_LEGACY_KEYS = ("therapeutic_area", "competitor", "brand", "confidence")


# --- regression guard ------------------------------------------------------------
@pytest.mark.parametrize("query", sorted(MAP_QUERY_SNAPSHOT))
def test_map_query_legacy_keys_unchanged(query):
    """FR-116 guard: the four pre-existing keys must not move for any corpus query."""
    got = map_query(query)
    expected = MAP_QUERY_SNAPSHOT[query]
    assert {k: got[k] for k in _LEGACY_KEYS} == expected, (
        f"map_query({query!r}) changed for a pre-existing key — Prompt Volume depends on this"
    )


def test_map_query_alias_index_shape_unchanged():
    """``alias_index()`` entries keep their exact key set (Prompt Volume reads them raw)."""
    expected = {"alias", "ta_key", "area", "kind", "canonical", "is_competitor"}
    entries = taxonomy.alias_index()
    assert entries, "alias_index() must not be empty"
    assert all(set(e) == expected for e in entries)


# --- disease resolution ----------------------------------------------------------
def test_map_query_exposes_disease_key():
    """Every result carries a ``disease`` key, even when nothing matched."""
    assert map_query("random unrelated keyword")["disease"] is None
    assert map_query("")["disease"] is None


@pytest.mark.parametrize(
    "query,disease",
    [
        ("is rinvoq or dupixent better for atopic dermatitis", "Atopic Dermatitis"),
        ("rinvoq for rheumatoid arthritis", "Rheumatoid Arthritis"),
        ("skyrizi psoriatic arthritis", "Psoriatic Arthritis"),
        ("tremfya for plaque psoriasis", "Plaque Psoriasis"),
        ("stelara ulcerative colitis", "Ulcerative Colitis"),
        ("what is the best treatment for crohn disease", "Crohn's Disease"),
        ("rinvoq dosing in psa", "Psoriatic Arthritis"),
        ("upadacitinib side effects", None),
    ],
)
def test_map_query_resolves_disease_independently_of_the_drug(query, disease):
    """Root cause 3: the indication hit is no longer discarded when a drug also matches."""
    assert map_query(query)["disease"] == disease


def test_disease_hit_survives_a_higher_ranked_drug_hit():
    """The AD signal in a Rinvoq question is preserved even though the drug outranks it."""
    got = map_query("is rinvoq or dupixent better for atopic dermatitis")
    assert got["brand"] is None and got["competitor"] == "Dupixent"  # unchanged legacy behaviour
    assert got["disease"] == "Atopic Dermatitis"                     # new signal


def test_longest_disease_alias_wins():
    assert map_query("plaque psoriasis biologics")["disease"] == "Plaque Psoriasis"
    assert map_query("nr-axspa treatment options")["disease"] == \
        "Non-radiographic Axial Spondyloarthritis"


# --- diseases beyond the immunology programme ------------------------------------
# The overlay now also covers Imbruvica, Venclexta, Vraylar and Lupron. Aliases are not
# cosmetic here: `curation.coverage.covers()` requires ``resolve_disease(text) == cell
# .disease``, so a disease nobody can name in prose is a disease whose existing bank
# questions are invisible — the matrix would report a 100% gap and regenerate duplicates.

@pytest.mark.parametrize(
    "query,disease",
    [
        ("imbruvica vs calquence for CLL", "Chronic Lymphocytic Leukemia"),
        ("is my SLL treatable", "Chronic Lymphocytic Leukemia"),
        ("mantle cell lymphoma second line", "Mantle Cell Lymphoma"),
        ("waldenstrom macroglobulinemia options", "Waldenstrom's Macroglobulinemia"),
        ("venclexta AML", "Acute Myeloid Leukemia"),
        ("vraylar for schizophrenia", "Schizophrenia"),
        ("vraylar for bipolar disorder", "Bipolar I Disorder"),
        ("major depressive disorder adjunct", "Major Depressive Disorder"),
        ("lupron depot endometriosis", "Endometriosis"),
        ("lupron depot for uterine fibroids", "Uterine Fibroids"),
        ("precocious puberty treatment", "Central Precocious Puberty"),
    ],
)
def test_the_new_indications_are_detectable_in_prose(query, disease):
    assert map_query(query)["disease"] == disease


def test_bipolar_depression_is_not_stolen_by_major_depressive_disorder():
    """``resolve_disease`` takes the LONGEST matching alias. A bare "depression" alias on
    MDD (10 chars) would outrank "bipolar" (7) and route every bipolar-depression question
    into the wrong competitive field — Vraylar has both indications, so the two would be
    scored against different comparator sets. This is why no such alias is declared."""
    assert map_query("bipolar depression")["disease"] == "Bipolar I Disorder"
    assert map_query("cariprazine for bipolar depression")["disease"] == "Bipolar I Disorder"


def test_no_two_indications_claim_the_same_alias():
    """``disease_index()`` dedupes aliases globally with first-declared-wins and drops the
    loser SILENTLY, so a collision is invisible at runtime. New entries are appended, which
    means any clash would always resolve against them."""
    claimed: dict[str, str] = {}
    for disease, block in taxonomy._indications().items():
        for alias in [disease, *((block or {}).get("aliases") or [])]:
            key = alias.strip().lower()
            assert claimed.setdefault(key, disease) == disease, (
                f"alias {alias!r} is claimed by both {claimed[key]!r} and {disease!r}"
            )


def test_every_declared_indication_resolves_from_its_own_key():
    for disease in taxonomy.diseases():
        assert taxonomy.canonical_disease(disease) == disease


def test_every_indication_references_a_defined_endpoint():
    """An overlay entry with a dangling outcome ID is a FATAL startup error, so it is worth
    a red test rather than a failed boot."""
    for disease in taxonomy.diseases():
        referenced = taxonomy.canonical_outcomes_for_disease(disease)
        assert referenced, f"{disease} declares no canonical_outcomes"
        for outcome_id in referenced:
            assert outcomes.is_defined(outcome_id), f"{disease} -> {outcome_id} undefined"


def test_config_validation_is_clean():
    assert taxonomy.validate_config() == []


# --- taxonomy overlay ------------------------------------------------------------
def test_disease_index_is_populated_and_normalised():
    idx = taxonomy.disease_index()
    assert idx, "brands.yaml must declare an `indications:` overlay"
    assert all(e["alias"] == e["alias"].lower() for e in idx)
    # sorted longest-alias-first so specific aliases win
    lengths = [len(e["alias"]) for e in idx]
    assert lengths == sorted(lengths, reverse=True)


def test_competitors_for_disease_is_indication_specific():
    """Root cause 5: AD and RA must not share one flat competitor list."""
    ad = set(taxonomy.competitors_for_disease("Atopic Dermatitis"))
    ra = set(taxonomy.competitors_for_disease("Rheumatoid Arthritis"))
    assert {"Dupixent", "Cibinqo", "Adbry", "Ebglyss"} <= ad
    assert {"Xeljanz", "Olumiant"} <= ra
    assert "Dupixent" not in ra
    assert "Xeljanz" not in ad


def test_competitors_for_disease_unknown_returns_empty():
    assert taxonomy.competitors_for_disease("Not A Real Disease") == ()
    assert taxonomy.competitors_for_disease(None) == ()


def test_skyrizi_is_present_in_the_psa_disease_entry():
    """brands.yaml inconsistency #8: Skyrizi has a PsA indication but was absent from
    the Rheumatology block, so it never appeared under that filter."""
    assert "Skyrizi" in taxonomy.brands_for_disease("Psoriatic Arthritis")
    assert "Rinvoq" in taxonomy.brands_for_disease("Psoriatic Arthritis")


def test_therapeutic_area_key_for_disease():
    assert taxonomy.therapeutic_area_key_for_disease("Rheumatoid Arthritis") == "Rheumatology"
    assert taxonomy.therapeutic_area_key_for_disease("Atopic Dermatitis") == "Dermatology"
    assert taxonomy.therapeutic_area_key_for_disease("Ulcerative Colitis") == "Gastroenterology"
    assert taxonomy.therapeutic_area_key_for_disease("nope") is None


def test_area_for_disease_rolls_up_to_the_parent_area():
    assert taxonomy.area_for_disease("Psoriatic Arthritis") == "Rheumatology"
    assert taxonomy.area_for_disease("Plaque Psoriasis") == "Dermatology"
    assert taxonomy.area_for_disease("Crohn's Disease") == "Gastroenterology"


# --- specialty split (Dermatology / Gastroenterology) ----------------------------
def test_immunology_is_fully_retired():
    """The flat area must not survive anywhere a caller could still select it."""
    assert "Immunology" not in taxonomy.keys_for_area("Immunology")
    assert taxonomy.keys_for_area("Immunology") == ()
    assert not any(e["ta_key"] == "Immunology" for e in taxonomy.alias_index())
    assert not any(e["area"] == "Immunology" for e in taxonomy.disease_index())


def test_skin_and_gut_diseases_no_longer_share_one_key():
    """The reason for the split: a brand-less psoriasis question must not fall back to
    a competitor list containing Entyvio."""
    assert set(taxonomy.diseases_for_key("Dermatology")) == {
        "Plaque Psoriasis", "Atopic Dermatitis", "Hidradenitis Suppurativa"}
    assert set(taxonomy.diseases_for_key("Gastroenterology")) == {
        "Ulcerative Colitis", "Crohn's Disease"}

    derm = set(taxonomy.competitors_for_key("Dermatology"))
    gastro = set(taxonomy.competitors_for_key("Gastroenterology"))
    assert "Entyvio" in gastro and "Entyvio" not in derm
    assert "Dupixent" in derm and "Dupixent" not in gastro


def test_hidradenitis_suppurativa_is_curated_end_to_end():
    assert taxonomy.canonical_disease("acne inversa") == "Hidradenitis Suppurativa"
    assert taxonomy.therapeutic_area_key_for_disease("Hidradenitis Suppurativa") == "Dermatology"
    # Humira only — Skyrizi holds no HS indication and must not be asserted to.
    assert taxonomy.brands_for_disease("Hidradenitis Suppurativa") == ("Humira",)
    assert set(taxonomy.competitors_for_disease("Hidradenitis Suppurativa")) == {"Cosentyx", "Bimzelx"}
    assert outcomes.is_defined("HS_HISCR50_W12")


def test_multi_area_brands_report_every_block_they_appear_in():
    assert taxonomy.area_keys_for_brand("Rinvoq") == (
        "Dermatology", "Gastroenterology", "Rheumatology")
    assert taxonomy.area_keys_for_brand("upadacitinib") == taxonomy.area_keys_for_brand("Rinvoq")
    assert taxonomy.area_keys_for_brand("Vraylar") == ("Neuroscience",)
    assert taxonomy.area_keys_for_brand("not a drug") == ()


def test_duplicated_drugs_keep_one_consistent_curated_record():
    """Humira/Skyrizi/Rinvoq/Tremfya now appear in several blocks. If a copy omitted a
    field, the resolved facts would depend on which block YAML declares first."""
    for name, drug_class, route in (
        ("Rinvoq", "JAK inhibitor", "ORAL"),
        ("Humira", "TNF inhibitor", "SC"),
        ("Skyrizi", "IL-23 inhibitor", "SC"),
        ("Tremfya", "IL-23 inhibitor", "SC"),
    ):
        assert taxonomy.drug_class_for(name) == drug_class
        assert taxonomy.administration_route_for(name) == route
        assert taxonomy.evidence_depth_for(name) == "full"


# --- curated cross-class annotation (Phase 5 B1, bounded) ------------------------
def test_curated_drug_class_and_route_are_available():
    assert taxonomy.drug_class_for("Rinvoq") == "JAK inhibitor"
    assert taxonomy.administration_route_for("Rinvoq") == "ORAL"
    assert taxonomy.drug_class_for("Skyrizi") == "IL-23 inhibitor"
    assert taxonomy.administration_route_for("Skyrizi") == "SC"
    assert taxonomy.drug_class_for("Tremfya") == "IL-23 inhibitor"
    assert taxonomy.administration_route_for("Tremfya") == "SC"


def test_generic_names_resolve_to_the_same_curated_labels():
    assert taxonomy.drug_class_for("upadacitinib") == "JAK inhibitor"
    assert taxonomy.administration_route_for("guselkumab") == "SC"


def test_uncurated_drug_returns_none_rather_than_a_guess():
    """B2 (inferred class over an open drug set) is deliberately out of scope."""
    assert taxonomy.drug_class_for("Xenical") is None
    assert taxonomy.drug_class_for("not a drug") is None


# --- evidence depth vs monitoring identity ---------------------------------------
def test_tremfya_is_carried_at_full_evidence_depth():
    assert taxonomy.evidence_depth_for("Tremfya") == "full"
    assert "Tremfya" in taxonomy.full_depth_drugs()


def test_evidence_depth_defaults_to_standard():
    assert taxonomy.evidence_depth_for("Cosentyx") == "standard"
    assert "Cosentyx" not in taxonomy.full_depth_drugs()
    assert taxonomy.evidence_depth_for("not a drug") == "standard"


def test_full_depth_scope_is_exactly_the_evidence_programme():
    """Full depth is an explicit opt-in, never a side effect of being a focus brand.

    If focus brands defaulted to full, Lupron, Vraylar and the GLP-1 set would be
    enrolled in the evidence programme by accident and start ingesting trials.
    """
    assert set(taxonomy.full_depth_drugs()) == {"Rinvoq", "Skyrizi", "Humira", "Tremfya"}
    for out_of_scope in ("Wegovy", "Vraylar", "Lupron Depot", "Imbruvica"):
        assert taxonomy.evidence_depth_for(out_of_scope) == "standard"


def test_tremfya_is_not_promoted_into_focus_brands():
    """Promoting it would make alert_engine skip it and kill COMPETITOR_ADVANTAGE."""
    for key in ("Dermatology", "Gastroenterology"):
        focus = taxonomy.focus_brands_for_key(key)
        assert "Tremfya" not in focus
        assert {"Humira", "Skyrizi", "Rinvoq"} <= set(focus)


def test_full_evidence_depth_still_raises_competitor_advantage():
    """Direct regression guard against the focus-brand promotion trap."""
    alerts = evaluate_alerts(
        response_id="r1", score_id="s1",
        sentiment_score=0.1,
        competitive_position="AMONG_OPTIONS",
        brand_mentions=[{"brand": "Tremfya", "is_competitor": True, "sentiment": 0.8}],
        focus_brand="Skyrizi",
    )
    assert [a.rule_triggered for a in alerts] == ["COMPETITOR_ADVANTAGE"]


# --- scoring context -------------------------------------------------------------
def test_context_for_prefers_disease_level_competitors():
    ctx = _context_for("Dermatology", "Rinvoq", disease="Atopic Dermatitis")
    assert "Atopic Dermatitis" in ctx
    assert "Dupixent" in ctx and "Cibinqo" in ctx
    assert "Xeljanz" not in ctx  # RA competitor must not leak into an AD question


def test_context_for_resolves_the_same_brand_differently_per_indication():
    """The headline acceptance criterion: one brand, two indications, two fields."""
    ra = _context_for("Rheumatology", "Rinvoq", disease="Rheumatoid Arthritis")
    assert "Xeljanz" in ra and "Olumiant" in ra
    assert "Dupixent" not in ra and "Cibinqo" not in ra

    psa = _context_for("Rheumatology", "Skyrizi", disease="Psoriatic Arthritis")
    assert "Tremfya" in psa and "Taltz" in psa
    assert "Olumiant" not in psa  # RA-only comparator


def test_context_for_falls_back_to_the_ta_block_without_a_disease():
    ctx = _context_for("Dermatology", "Rinvoq", disease=None)
    assert "Known competitors" in ctx
    assert "Stelara" in ctx


def test_context_for_block_fallback_is_specialty_scoped():
    """The payoff of the split: with no disease to go on, a skin question falls back to
    skin competitors and a gut question to gut competitors."""
    derm = _context_for("Dermatology", "Skyrizi", disease=None)
    assert "Dupixent" in derm and "Entyvio" not in derm

    gastro = _context_for("Gastroenterology", "Skyrizi", disease=None)
    assert "Entyvio" in gastro and "Dupixent" not in gastro


def test_context_for_ignores_an_unknown_disease():
    """An unmapped disease must degrade to the TA block, never to an empty field."""
    ctx = _context_for("Dermatology", "Rinvoq", disease="Not A Real Disease")
    assert "Stelara" in ctx


def test_landscape_context_prefers_disease_level_field():
    ctx = _landscape_context("Dermatology", None, disease="Atopic Dermatitis")
    assert "Dupixent" in ctx and "Ebglyss" in ctx
    assert "Xeljanz" not in ctx


def test_landscape_context_still_merges_question_tagged_competitors():
    ctx = _landscape_context("Dermatology", '["Bimzelx"]', disease="Plaque Psoriasis")
    assert "Bimzelx" in ctx


# --- TA derivation ---------------------------------------------------------------
def test_derive_ta_resolves_via_disease_first():
    """Root cause 4: Rinvoq is not single-TA — the disease decides."""
    assert _derive_ta("Rinvoq", None, disease="Rheumatoid Arthritis") == "Rheumatology"
    assert _derive_ta("Rinvoq", None, disease="Atopic Dermatitis") == "Dermatology"
    assert _derive_ta("Rinvoq", None, disease="Ulcerative Colitis") == "Gastroenterology"
    assert _derive_ta("Skyrizi", None, disease="Psoriatic Arthritis") == "Rheumatology"


def test_derive_ta_honours_an_explicitly_pinned_area():
    assert _derive_ta("Rinvoq", "Dermatology", disease="Rheumatoid Arthritis") == "Dermatology"


def test_derive_ta_refuses_to_guess_for_a_multi_area_brand():
    """Rinvoq spans three blocks, so with no disease there is no answer to give.

    ``map_query`` still returns Dermatology here (declaration order), which is fine for
    a search-demand hint and wrong for a stored classification — filing the question
    under whichever block sits at the top of brands.yaml is the bug
    ``hotfix_rhem_therapeutic_area.py`` had to repair. "Unmapped" is reviewable.
    """
    assert _derive_ta("Rinvoq", None) == "Unmapped"
    assert _derive_ta("Humira", None) == "Unmapped"
    assert _derive_ta("Skyrizi", None) == "Unmapped"


def test_derive_ta_still_resolves_a_single_area_brand_from_the_brand_alone():
    assert _derive_ta("Vraylar", None) == "Neuroscience"
    assert _derive_ta("Imbruvica", None) == "Oncology"


def test_derive_ta_never_returns_empty():
    assert _derive_ta("", None) == "Unmapped"


# --- canonical outcomes ----------------------------------------------------------
def test_canonical_outcomes_load_with_required_fields():
    ids = outcomes.outcome_ids()
    assert {"PSO_PASI90_W16", "RA_ACR50_W12", "PSA_ACR50_W16", "AD_EASI75_W16",
            "AS_ASAS40_W14"} <= set(ids)
    for oid in ids:
        o = outcomes.outcome(oid)
        assert o["endpoint"] and o["effect_measure"]
        assert o["allowed_window"]["min_week"] <= o["nominal_timepoint_week"] \
            <= o["allowed_window"]["max_week"]


def test_ibd_outcomes_separate_induction_from_maintenance():
    """Tier 3 gate: mixing induction and maintenance in one network is a hard error."""
    induction = outcomes.outcome("UC_REMISSION_INDUCTION_W8")
    maintenance = outcomes.outcome("UC_REMISSION_MAINTENANCE_W52")
    assert induction["treatment_phase"] == "INDUCTION"
    assert maintenance["treatment_phase"] == "MAINTENANCE"


def test_population_strata_are_owned_by_canonical_outcomes():
    assert {"BIO_NAIVE", "BIO_EXPERIENCED", "TNF_IR", "MTX_IR"} <= set(outcomes.strata())


def test_unknown_outcome_id_returns_none():
    assert outcomes.outcome("NOPE_W1") is None


# --- config validation -----------------------------------------------------------
def test_config_validates_clean():
    """Startup guard: every referenced canonical outcome ID must exist."""
    assert taxonomy.validate_config() == []


def test_missing_canonical_outcome_is_a_validation_error(monkeypatch):
    monkeypatch.setattr(
        taxonomy, "_indications",
        lambda: {"Fake Disease": {"area": "Dermatology", "canonical_outcomes": ["NOT_REAL_W9"]}},
    )
    errors = taxonomy.validate_config()
    assert any("NOT_REAL_W9" in e for e in errors)
