"""Coverage-driven question curation.

The defect these guard is an absence: no ingestion source could produce a question
comparing Rinvoq to Tremfya, because harvest truncated each brand to its first two
area-level comparators and Tremfya is neither. The matrix here is built from the disease
overlay instead, so the pair exists by construction, and the generated question has to
name both agents and the right indication or it is thrown away.
"""
import pytest

from app.config import taxonomy
from app.curation import coverage, generator, service
from app.curation.coverage import Cell
from app.harvest import pipeline
from app.prompt_volume import mapping


def _cell(disease="Psoriatic Arthritis", brand="Rinvoq", competitor="Tremfya",
          persona="Patient") -> Cell:
    return Cell(disease=disease, brand=brand, competitor=competitor, persona=persona)


# ---- the pair that could not previously be asked about ---------------------------------

@pytest.mark.parametrize(
    "disease", ["Psoriatic Arthritis", "Ulcerative Colitis", "Crohn's Disease"]
)
def test_the_rinvoq_tremfya_cell_exists_in_every_shared_indication(disease):
    cells = coverage.build_matrix(brands=["Rinvoq"], diseases=[disease])
    pairs = {(c.brand, c.competitor) for c in cells}
    assert ("Rinvoq", "Tremfya") in pairs


def test_the_rinvoq_tremfya_cell_ranks_into_the_gap_list_on_an_empty_bank():
    """Not merely present — near the top, because Tremfya is carried at full depth."""
    cells = coverage.build_matrix(brands=["Rinvoq"], personas=["Patient"])
    gaps = coverage.rank(coverage.apply_coverage(cells, []))
    top = [(g.cell.brand, g.cell.competitor) for g in gaps[:12]]
    assert ("Rinvoq", "Tremfya") in top


def test_harvest_can_now_build_the_query_that_finds_it():
    """The upstream truncation fix: a Rinvoq/Tremfya query must be constructible."""
    cfg = {"therapeutic_areas": {
        "Rheumatology": taxonomy._areas()["Rheumatology"],
    }}
    queries = pipeline._expand(["{brand} vs {competitor}"], cfg, brand_filter="Rinvoq")
    assert "Rinvoq vs Tremfya" in queries


def test_harvest_comparators_prefer_the_disease_overlay():
    comps = pipeline._comparators_for("Rinvoq", "Rheumatology", ["Enbrel"])
    assert "Tremfya" in comps
    assert "Rinvoq" not in comps


def test_harvest_still_covers_every_brand_under_a_cap():
    """Rank interleaving must not let brand 1 consume the whole run."""
    cfg = {"therapeutic_areas": {"Rheumatology": taxonomy._areas()["Rheumatology"]}}
    queries = pipeline._expand(["{brand} vs {competitor}"], cfg)
    focus = taxonomy.focus_brands_for_key("Rheumatology")
    head = " ".join(queries[: len(focus) * 2])
    for brand in focus:
        assert brand in head, f"{brand} was pushed past the cap by an earlier brand"


# ---- matrix construction ----------------------------------------------------------------

def test_matrix_is_built_from_the_same_accessors_as_scoring():
    for cell in coverage.build_matrix(diseases=["Atopic Dermatitis"]):
        assert cell.brand in taxonomy.brands_for_disease("Atopic Dermatitis")
        assert cell.competitor in taxonomy.competitors_for_disease("Atopic Dermatitis")


def test_a_brand_only_gets_cells_where_it_is_indicated():
    """Scoping Rinvoq across three areas must not invent a psoriasis question for it."""
    cells = coverage.build_matrix(brands=["Rinvoq"])
    diseases = {c.disease for c in cells}
    assert "Atopic Dermatitis" in diseases
    assert "Plaque Psoriasis" not in diseases  # Rinvoq holds no PsO indication


def test_multi_area_scope_spans_all_three_specialties():
    """The multi-select case: one request, three areas, one matrix."""
    cells = coverage.build_matrix(
        brands=["Rinvoq"],
        therapeutic_areas=["Dermatology", "Gastroenterology", "Rheumatology"],
    )
    assert {c.therapeutic_area for c in cells} == {
        "Dermatology", "Gastroenterology", "Rheumatology"
    }


def test_scope_accepts_a_broad_area_name_as_well_as_a_stored_key():
    by_key = coverage.build_matrix(brands=["Rinvoq"], therapeutic_areas=["Dermatology"])
    assert by_key and {c.therapeutic_area for c in by_key} == {"Dermatology"}


def test_an_area_with_no_disease_overlay_yields_no_cells():
    """Obesity declares no `indications:` entries, so there is no field to compare within.
    Emitting cells anyway would invent comparisons the taxonomy never asserted.

    The example moved from Women's Health to Obesity when the overlay was extended to every
    AbbVie focus brand. The RULE is unchanged — what changed is which area still has no
    overlay. Obesity is the deliberate one: its `focus_brands` are Novo and Lilly products
    tracked for category monitoring, so generating head-to-head questions there would pit
    two competitors against each other on AbbVie's behalf.
    """
    assert coverage.build_matrix(therapeutic_areas=["Obesity"]) == []


# ---- every AbbVie brand, not just the immunology programme -----------------------------
# The defect: the matrix reads the `indications:` overlay, which declared only the 9
# immunology indications, so `build_matrix` hit `if not competitors: continue` and
# Imbruvica, Venclexta, Vraylar, Lupron Depot and Lupron Depot-Ped produced exactly ZERO
# cells — while the UI's brand picker offered all of them and reported "no gaps".

# Brands the picker offers, i.e. every focus brand under an area in the TA hierarchy.
# Excludes the Obesity GLP-1 set, which is tracked for category monitoring only.
ABBVIE_FOCUS_BRANDS = [
    "Skyrizi", "Humira", "Rinvoq", "Imbruvica", "Venclexta",
    "Vraylar", "Lupron Depot", "Lupron Depot-Ped",
]


@pytest.mark.parametrize("brand", ABBVIE_FOCUS_BRANDS)
def test_every_focus_brand_the_picker_offers_has_comparisons(brand):
    """A brand the UI lets you scope to must not silently report zero gaps."""
    cells = coverage.build_matrix(brands=[brand], personas=list(coverage.ALL_PERSONAS))
    assert cells, f"{brand} is selectable in the brand picker but has no monitorable comparison"


@pytest.mark.parametrize("brand", ABBVIE_FOCUS_BRANDS)
def test_no_brand_gets_a_cell_in_a_disease_it_is_not_indicated_in(brand):
    for cell in coverage.build_matrix(brands=[brand], personas=list(coverage.ALL_PERSONAS)):
        assert cell.brand in taxonomy.brands_for_disease(cell.disease)
        assert cell.competitor in taxonomy.competitors_for_disease(cell.disease)


def test_the_obesity_brands_stay_out_of_the_matrix():
    """Wegovy and friends are Novo/Lilly products tracked for category monitoring. Giving
    them cells would generate questions comparing two competitors on AbbVie's behalf."""
    for brand in ("Wegovy", "Zepbound", "Ozempic", "Mounjaro", "Saxenda", "Rybelsus"):
        assert coverage.build_matrix(brands=[brand], personas=list(coverage.ALL_PERSONAS)) == []


# ---- the new fields are curated per disease, not inherited flat ------------------------
# Oncology declares one competitor list across four diseases and Neuroscience one across
# three. Inheriting either would reproduce the flattening the overlay exists to fix.

def test_aml_does_not_inherit_the_flat_oncology_competitor_list():
    """'Venclexta vs Brukinsa for AML' is a BTK inhibitor in a disease it is not used in."""
    competitors = set(taxonomy.competitors_for_disease("Acute Myeloid Leukemia"))
    assert competitors == {"Vidaza", "Tibsovo", "Rydapt", "Onureg"}
    assert not competitors & set(taxonomy.competitors_for_key("Oncology"))


def test_waldenstrom_drops_a_btk_inhibitor_that_holds_no_indication_there():
    """Same class is not the same competitive field: Calquence has no WM indication."""
    competitors = set(taxonomy.competitors_for_disease("Waldenstrom's Macroglobulinemia"))
    assert "Brukinsa" in competitors
    assert "Calquence" not in competitors


def test_adjunctive_mdd_is_narrower_than_the_neuroscience_block():
    """Latuda, Seroquel and Zyprexa are not adjunctive MDD agents; Schizophrenia keeps all."""
    mdd = set(taxonomy.competitors_for_disease("Major Depressive Disorder"))
    assert mdd == {"Rexulti", "Abilify", "Caplyta"}
    assert set(taxonomy.competitors_for_disease("Schizophrenia")) == set(
        taxonomy.competitors_for_key("Neuroscience")
    )


def test_bipolar_drops_the_agent_with_no_bipolar_indication():
    assert "Rexulti" not in taxonomy.competitors_for_disease("Bipolar I Disorder")


def test_the_new_aml_comparators_are_curated_with_a_route():
    """`administration_route` is what the route-mixing transitivity check consumes, so an
    uncurated comparator would enter a network with nothing to check."""
    for name in ("Vidaza", "Tibsovo", "Rydapt", "Onureg"):
        assert taxonomy.administration_route_for(name) in taxonomy.ADMINISTRATION_ROUTES
        assert taxonomy.drug_class_for(name)


def test_the_new_comparators_are_policed_by_the_generator_scope_guard():
    """A generated cell may name only its own two agents. That guard reads
    `comparison_agents()`, so a comparator missing from it would be invisible to it."""
    agents = {n.strip().lower() for n in taxonomy.comparison_agents()}
    for disease in taxonomy.diseases():
        for competitor in taxonomy.competitors_for_disease(disease):
            assert competitor.strip().lower() in agents, (
                f"{competitor} is a declared comparator but not a comparison agent"
            )


def test_the_new_indications_are_monitoring_scope_not_evidence_scope():
    """Adding them must not enrol anything in trial ingestion: that is gated on
    `evidence_depth: full`, which stays the four immunology agents."""
    assert set(taxonomy.full_depth_drugs()) == {"Rinvoq", "Skyrizi", "Humira", "Tremfya"}


def test_the_config_validates():
    """`main._validate_configuration()` is fatal, so a bad overlay entry is a failed boot
    rather than a failed request. Pinning it here makes that a red test instead."""
    assert taxonomy.validate_config() == []


def test_cells_carry_the_stored_therapeutic_area_key():
    cell = _cell(disease="Ulcerative Colitis")
    assert cell.therapeutic_area == "Gastroenterology"
    assert cell.area == "Gastroenterology"


@pytest.mark.parametrize(
    "disease,ta_key",
    [
        ("Chronic Lymphocytic Leukemia", "Oncology"),
        ("Schizophrenia", "Neuroscience"),
        ("Endometriosis", "Endometriosis"),
        ("Uterine Fibroids", "Uterine Fibroids"),
        ("Central Precocious Puberty", "Central Precocious Puberty"),
    ],
)
def test_new_cells_carry_a_stored_key_that_actually_exists(disease, ta_key):
    """`therapeutic_area_key` must be a key of `therapeutic_areas:` or questions generated
    for the cell would store an area no filter can ever select."""
    cell = _cell(disease=disease, brand=taxonomy.brands_for_disease(disease)[0],
                 competitor=taxonomy.competitors_for_disease(disease)[0])
    assert cell.therapeutic_area == ta_key
    assert ta_key in taxonomy._areas()


def test_cell_hash_is_stable_and_distinct():
    assert _cell().dedupe_hash() == _cell().dedupe_hash()
    assert _cell().dedupe_hash() != _cell(competitor="Cosentyx").dedupe_hash()
    assert _cell().dedupe_hash() != _cell(disease="Ulcerative Colitis").dedupe_hash()


# ---- coverage detection ------------------------------------------------------------------

def test_a_matching_question_covers_the_cell():
    text = "For psoriatic arthritis, how does Rinvoq compare to Tremfya?"
    assert coverage.covers(_cell(), text, persona="Patient")


def test_coverage_is_alias_aware():
    text = "For psoriatic arthritis, is upadacitinib better than guselkumab?"
    assert coverage.covers(_cell(), text, persona="Patient")


def test_the_same_pair_in_another_indication_is_not_coverage():
    """Different competitive field, different answer — it is a different question."""
    text = "For plaque psoriasis, how does Rinvoq compare to Tremfya?"
    assert not coverage.covers(_cell(disease="Ulcerative Colitis"), text)


def test_naming_only_one_agent_is_not_coverage():
    assert not coverage.covers(_cell(), "How effective is Rinvoq for psoriatic arthritis?")


def test_a_different_persona_is_not_coverage():
    text = "For psoriatic arthritis, how does Rinvoq compare to Tremfya?"
    assert not coverage.covers(_cell(persona="Provider"), text, persona="Patient")


def test_apply_coverage_and_summary_agree():
    cells = coverage.build_matrix(brands=["Rinvoq"], diseases=["Psoriatic Arthritis"],
                                  personas=["Patient"])
    items = coverage.apply_coverage(
        cells,
        [{"question_text": "For psoriatic arthritis, is Rinvoq or Tremfya better?",
          "persona": "Patient"}],
    )
    summary = coverage.summarize(items)
    assert summary["covered"] == 1
    assert summary["gaps"] == len(cells) - 1
    assert summary["total_cells"] == len(cells)
    assert len(coverage.rank(items)) == summary["gaps"]


# ---- generator: prompt, parsing, and refusal ---------------------------------------------

def test_prompt_names_both_agents_and_the_indication():
    system, user = generator.build_prompt([_cell()])
    assert "Rinvoq" in user and "Tremfya" in user and "Psoriatic Arthritis" in user
    assert "ONLY the two treatments" in system


def test_prompt_forbids_claims_and_doses():
    system, _ = generator.build_prompt([_cell()])
    assert "never an answer" in system
    assert "dose" in system


def test_parse_tolerates_fenced_json_and_bare_lists():
    assert generator.parse_questions('```json\n{"questions": ["Is A or B better?"]}\n```') == [
        "Is A or B better?"
    ]
    assert generator.parse_questions('["Is A or B better?"]') == ["Is A or B better?"]


def test_a_candidate_naming_a_third_drug_is_rejected():
    accepted, rejected = generator.postprocess(
        [_cell()],
        ["For psoriatic arthritis should I take Rinvoq, Tremfya or Cosentyx?"],
    )
    assert accepted == []
    assert "out-of-scope drug" in rejected[0]["reason"]


def test_a_candidate_naming_background_therapy_is_accepted():
    """Methotrexate is the regimen the question is asked about, not a third comparison.

    The observed defect: a live generate run discarded two of ten candidates as
    "names an out-of-scope drug: methotrexate", so those cells stayed unfilled gaps
    while the model kept writing the clinically natural phrasing for them.
    """
    text = ("After methotrexate stopped working for my psoriatic arthritis, "
            "should I ask about Rinvoq or Tremfya?")
    accepted, rejected = generator.postprocess([_cell()], [text])
    assert rejected == []
    assert accepted[0][1] == text


def test_the_mtx_abbreviation_is_exempt_on_the_same_terms():
    """One curated record answers for every spelling, so MTX cannot behave differently."""
    text = "Is Rinvoq or Tremfya the better next step after MTX for psoriatic arthritis?"
    accepted, rejected = generator.postprocess([_cell()], [text])
    assert rejected == []
    assert accepted[0][1] == text


def test_a_rival_curated_only_in_the_catalog_is_still_rejected():
    """The exemption is background therapy, NOT "absent from the competitor list".

    Taltz sits in `drug_catalog:` exactly as methotrexate does. Reading block membership
    as permission would quietly let a genuine commercial rival into a two-agent question.
    """
    accepted, rejected = generator.postprocess(
        [_cell()], ["For psoriatic arthritis, is Rinvoq, Tremfya or Taltz better?"],
    )
    assert accepted == []
    assert "out-of-scope drug: taltz" in rejected[0]["reason"]


def test_the_background_therapy_exemption_stays_deliberate():
    """A new catalog entry defaults to policed; widening the exemption is a decision.

    Fails the moment someone marks another agent `background_therapy`, which is the
    point: an investigational or unbranded biosimilar agent is not a competitor either,
    but naming one in a patient-facing question is worse rather than better.
    """
    exempt = sorted({
        record["canonical"] for record in taxonomy.drug_index().values()
        if record["background_therapy"]
    })
    assert exempt == ["Methotrexate"]

    agents = set(taxonomy.comparison_agents())
    assert "Methotrexate" not in agents
    assert {"Rinvoq", "Tremfya", "Taltz", "Bimzelx", "Entyvio", "Sonelokimab"} <= agents


def test_a_candidate_missing_an_agent_is_rejected():
    accepted, rejected = generator.postprocess(
        [_cell()], ["How well does Rinvoq work for psoriatic arthritis?"]
    )
    assert accepted == []
    assert "does not name Tremfya" in rejected[0]["reason"]


def test_a_candidate_that_is_not_a_question_is_rejected():
    accepted, rejected = generator.postprocess(
        [_cell()], ["Rinvoq is more effective than Tremfya."]
    )
    assert accepted == []
    assert rejected[0]["reason"] == "not phrased as a question"


def test_a_short_reply_loses_its_tail_rather_than_misattaching():
    cells = [_cell(), _cell(competitor="Cosentyx")]
    accepted, rejected = generator.postprocess(
        cells, ["For psoriatic arthritis, is Rinvoq or Tremfya the better choice?"]
    )
    assert len(accepted) == 1 and accepted[0][0] is cells[0]
    assert rejected[0]["reason"] == "model returned fewer questions than cells"


def test_a_valid_candidate_is_accepted():
    accepted, rejected = generator.postprocess(
        [_cell()], ["For psoriatic arthritis, is Rinvoq or Tremfya the better choice?"]
    )
    assert rejected == []
    assert accepted[0][1].startswith("For psoriatic arthritis")


def test_generating_no_cells_makes_no_call():
    import asyncio
    assert asyncio.run(generator.generate_for_cells([])) == ([], [], "")


# ---- cost accounting ----------------------------------------------------------------------

def test_model_calls_are_batched_and_exactly_reported():
    assert service.estimate_model_calls(0) == 0
    assert service.estimate_model_calls(1) == 1
    assert service.estimate_model_calls(service.BATCH_SIZE) == 1
    assert service.estimate_model_calls(service.BATCH_SIZE + 1) == 2


def test_alias_helper_resolves_generics_without_forking_the_matcher():
    assert "upadacitinib" in mapping.aliases_for_drug("Rinvoq")
    assert mapping.mentions("switching to guselkumab soon", "Tremfya")
    assert not mapping.mentions("my RA is flaring", "Rinvoq")
