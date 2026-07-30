"""Phase 3B — ClinicalTrials.gov adapter + the shared endpoint matcher.

**No network in any test.** `parse` is pure, so it runs against committed fixture JSON;
only `fetch` touches the wire and it is never called here.

Named acceptance criteria pinned:

* parsers tested against committed fixture JSON with no network
* every extracted row has a non-null source and timepoint provenance
* a simulated API failure returns a degraded result and never propagates an exception
  (covered in `test_evidence_extraction.py` against the shared `get_json` boundary)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import taxonomy
from app.evidence import endpoints as ep
from app.evidence import treatments
from app.evidence.endpoints import (
    EndpointMatch,
    match_endpoint,
    normalise,
    parse_timepoint_weeks,
    timepoint_weeks_in,
)
from app.evidence.sources import clinicaltrials as ctg
from app.evidence.sources.base import FetchResult
from app.models.clinical_study import BINARY, CONTINUOUS, StudyArm
from app.services import evidence_ingestion_service as svc

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def select_psa() -> ctg.ParsedStudy:
    payload = json.loads((FIXTURES / "ctg_select_psa_1.json").read_text(encoding="utf-8"))
    result = FetchResult(
        ok=True, source_type=ctg.SOURCE_TYPE, source_identifier="NCT03104400", payload=payload
    )
    parsed = ctg.parse(result, indication="Psoriatic Arthritis")
    assert parsed is not None
    return parsed


def _flags(row) -> set[str]:
    return set(json.loads(row.mismatch_flags)) if row.mismatch_flags else set()


# =====================================================================================
# Endpoint matching
# =====================================================================================
def test_compact_endpoint_tokens_match_a_registry_title():
    match = match_endpoint(
        "Percentage of Participants Achieving ACR20 Response at Week 12",
        indication="Psoriatic Arthritis",
        week=12,
    )
    assert match.outcome_id == "PSA_ACR20_W16"
    assert match.confidence == 1.0


def test_the_same_endpoint_resolves_differently_per_indication():
    """PASI90 is a separate ID in PsO and PsA — same endpoint, different population."""
    pso = match_endpoint("PASI90 response at week 16", indication="Plaque Psoriasis", week=16)
    psa = match_endpoint("PASI90 response at week 16", indication="Psoriatic Arthritis", week=16)
    assert pso.outcome_id == "PSO_PASI90_W16"
    assert psa.outcome_id == "PSA_PASI90_W16"


def test_prose_endpoints_match_via_config_tokens():
    """The registry writes "Adapted Mayo Score"; the canonical label says "modified Mayo"."""
    match = match_endpoint(
        "Percentage of Participants Achieving Clinical Remission Per Adapted Mayo Score",
        indication="Ulcerative Colitis",
        week=8,
    )
    assert match.outcome_id == "UC_REMISSION_INDUCTION_W8"


def test_the_timepoint_window_separates_induction_from_maintenance():
    """Identical wording, non-comparable populations. Only the week tells them apart."""
    induction = match_endpoint("clinical remission", indication="Ulcerative Colitis", week=8)
    maintenance = match_endpoint("clinical remission", indication="Ulcerative Colitis", week=52)
    assert induction.outcome_id == "UC_REMISSION_INDUCTION_W8"
    assert maintenance.outcome_id == "UC_REMISSION_MAINTENANCE_W52"


def test_iga_synonyms_all_resolve():
    for spelling in ("vIGA-AD 0/1", "IGA 0/1", "IGA 0 or 1", "IGA score of 0 or 1"):
        match = match_endpoint(
            f"Proportion Achieving {spelling} at Week 16", indication="Atopic Dermatitis", week=16
        )
        assert match.outcome_id == "AD_IGA01_W16", spelling


def test_an_ambiguous_title_is_never_resolved_by_guessing():
    """Picking the first hit would attach real trial numbers to the wrong endpoint."""
    match = match_endpoint(
        "Percentage Achieving ACR20 and ACR50 Response at Week 12",
        indication="Rheumatoid Arthritis",
        week=12,
    )
    assert match.outcome_id is None
    assert "ambiguous" in match.reason
    assert set(match.candidates) == {"RA_ACR20_W12", "RA_ACR50_W12"}
    assert match.needs_curation


def test_a_timepoint_outside_every_window_does_not_match():
    match = match_endpoint("ACR20 response", indication="Rheumatoid Arthritis", week=52)
    assert match.outcome_id is None
    assert "outside every allowed window" in match.reason


def test_an_unknown_timepoint_yields_a_low_confidence_proposal():
    match = match_endpoint("EASI75 response", indication="Atopic Dermatitis", week=None)
    assert match.outcome_id == "AD_EASI75_W16"
    assert match.confidence < 1.0
    assert match.needs_curation


def test_matching_requires_an_indication_scope():
    assert match_endpoint("ACR20", indication=None, week=12).outcome_id is None


def test_normalise_collapses_punctuation():
    assert normalise("vIGA-AD 0/1") == "vigaad01"
    assert normalise("IGA 0/1") == "iga01"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Week 12", 12.0),
        ("Baseline to Week 16", 16.0),
        ("Weeks 12 to 24", 24.0),
        ("At 52 Weeks", 52.0),
        ("Up to Week 56", 56.0),
        ("Month 12", pytest.approx(52.14, abs=0.1)),
        ("", None),
        ("Duration of study", None),
        # A time frame listing several visits identifies no single timepoint.
        ("Weeks 2, 4, 8, 12, 16, 20 and 24", None),
        ("Weeks 24, 28, 36, 44 and 52", None),
        ("Week 2 and Months 1, 2, 3, 4, 6, 9, and 12", None),
        ("Weeks 12 and 24", None),
        # Still anchored on the unit word: an NCT id is not seven million weeks.
        ("ACR20 at Week 12 in NCT03104400", 12.0),
    ],
)
def test_timepoint_parsing(text, expected):
    assert parse_timepoint_weeks(text) == expected


def test_a_visit_list_is_reported_as_several_timepoints_not_one():
    """The defect behind 91% of harvested rows carrying no canonical endpoint.

    Registries post repeated-measures outcomes under one time frame naming every visit.
    Reading the first number called this measure week 2 and no PsA window admits week 2,
    so all 21 rows were discarded; reading the largest would have called the same rows
    week 24 and admitted values measured at week 2. **Both assign a week the source never
    claimed to that row.** The honest measure-level answer is that there isn't one.
    """
    listed = "Weeks 2, 4, 8, 12, 16, 20 and 24"
    assert timepoint_weeks_in(listed) == (2.0, 4.0, 8.0, 12.0, 16.0, 20.0, 24.0)
    assert parse_timepoint_weeks(listed) is None
    # A range is one assessment through its upper bound, and must stay one.
    assert timepoint_weeks_in("Weeks 12 to 24") == (24.0,)


def test_a_title_naming_no_canonical_endpoint_reports_no_candidates():
    """``candidates`` used to hold the whole indication's vocabulary in this case.

    Callers read ambiguity off ``len(candidates) > 1``, so every unmatched row in an
    indication with more than one canonical endpoint was reported as ambiguous — which is
    how a corpus of PK, SF-36 and ACR-component measures produced a 91% "ambiguity" rate
    in an indication that has exactly three endpoints.
    """
    match = match_endpoint(
        "Change From Baseline in Norm Based Scores of SF-36 Scales",
        indication="Psoriatic Arthritis",
        week=16,
    )
    assert match.outcome_id is None
    assert match.reason_code == ep.NO_CANONICAL_WORDING
    assert match.candidates == (), "nothing in this title is a candidate for anything"
    assert not match.is_ambiguous
    # The vocabulary is still reported, for the curator deciding whether to add a synonym.
    assert "PSA_ACR50_W16" in match.scoped


def test_ambiguity_is_distinguished_from_a_rejected_timepoint():
    """Three different failures, three different reason codes, one flag each."""
    ambiguous = match_endpoint(
        "Percentage Achieving ACR 20, ACR 50, and ACR 70 Responses",
        indication="Psoriatic Arthritis", week=16,
    )
    assert ambiguous.is_ambiguous
    assert ambiguous.reason_code == ep.AMBIGUOUS_WORDING_AND_TIMEPOINT

    out_of_window = match_endpoint(
        "Percentage Achieving ACR50 Response", indication="Psoriatic Arthritis", week=52
    )
    assert out_of_window.reason_code == ep.TIMEPOINT_OUTSIDE_ALL_WINDOWS
    assert not out_of_window.is_ambiguous, "we recognised the endpoint and rejected the week"


# =====================================================================================
# Study-level parsing
# =====================================================================================
def test_study_metadata_parses(select_psa):
    study = select_psa.study
    assert study.study_id == "NCT03104400"
    assert study.acronym == "SELECT-PsA 1"
    assert study.indication == "Psoriatic Arthritis"
    assert study.phase == "PHASE3"
    assert study.sponsor == "AbbVie"
    assert study.enrollment == 1705
    assert study.is_randomised
    assert study.start_date.isoformat() == "2017-04-26"
    assert study.results_first_posted.isoformat() == "2021-05-06"


def test_ids_are_deterministic_so_reingestion_updates_rather_than_duplicates():
    payload = json.loads((FIXTURES / "ctg_select_psa_1.json").read_text(encoding="utf-8"))
    result = FetchResult(
        ok=True, source_type=ctg.SOURCE_TYPE, source_identifier="NCT03104400", payload=payload
    )
    first = ctg.parse(result, indication="Psoriatic Arthritis")
    second = ctg.parse(result, indication="Psoriatic Arthritis")
    assert first.study.study_id == second.study.study_id
    assert [a.arm_id for a in first.arms] == [a.arm_id for a in second.arms]
    assert [o.result_id for o in first.outcomes] == [o.result_id for o in second.outcomes]


# =====================================================================================
# Arms
# =====================================================================================
def test_arms_resolve_to_curated_network_nodes(select_psa):
    """"Upadacitinib 15 mg QD" and "Adalimumab 40 mg EOW" must land on Rinvoq/Humira."""
    by_label = {a.label: a for a in select_psa.arms}
    assert by_label["Upadacitinib 15 mg QD"].treatment == "Rinvoq"
    assert by_label["Adalimumab 40 mg EOW"].treatment == "Humira"
    assert by_label["Placebo"].treatment == "Placebo"
    assert by_label["Placebo"].is_placebo


def test_route_and_class_are_snapshotted_onto_each_arm(select_psa):
    """Route is what the Phase 6 route-mixing transitivity check consumes."""
    by_label = {a.label: a for a in select_psa.arms}
    assert by_label["Upadacitinib 15 mg QD"].administration_route == "ORAL"
    assert by_label["Upadacitinib 15 mg QD"].drug_class == "JAK inhibitor"
    assert by_label["Adalimumab 40 mg EOW"].administration_route == "SC"
    assert by_label["Adalimumab 40 mg EOW"].drug_class == "TNF inhibitor"


def test_this_trial_is_route_mixed(select_psa):
    """Oral Rinvoq against SC Humira — a transitivity threat to disclose downstream."""
    routes = {a.administration_route for a in select_psa.arms if not a.is_placebo}
    assert routes == {"ORAL", "SC"}


def test_dose_is_kept_structured_because_dose_policy_depends_on_it(select_psa):
    """The 15 mg and 30 mg arms must remain separable — pooling them silently is fatal."""
    doses = {a.label: (a.dose_value, a.dose_unit, a.dose_frequency) for a in select_psa.arms}
    assert doses["Upadacitinib 15 mg QD"] == (15.0, "mg", "QD")
    assert doses["Upadacitinib 30 mg QD"] == (30.0, "mg", "QD")


def test_an_uncurated_drug_is_kept_but_reduced_to_its_molecule():
    """Dropping an unrecognised arm would silently shrink the network."""
    treatment, is_placebo = ctg._canonical_treatment("Experimental Agent XYZ-123 200 mg")
    assert treatment == "Experimental Agent XYZ-123"
    assert not is_placebo


def test_uncurated_dose_variants_collapse_to_one_node():
    """The node-inflation regression.

    A curated drug collapses to a dose-free node, so an uncurated one must too. Leaving
    the dose in the label made every dose and regimen variant its own network node,
    inflating an ~20-node psoriatic arthritis network to 91 and fragmenting it into
    slivers that could never connect.
    """
    labels = [
        "Fictumab 150 mg",
        "Fictumab 300 mg",
        "Fictumab 150 mg with loading dose",
        "Fictumab 300 mg SC every 4 weeks",
        "Fictumab 150 mg subcutaneous injection Q4W",
    ]
    assert {ctg._canonical_treatment(x)[0] for x in labels} == {"Fictumab"}


def test_a_curated_drug_resolves_through_the_catalog_not_the_stripper():
    """Secukinumab is in the catalog, so it must reach its brand node, not its generic."""
    assert ctg._canonical_treatment("Secukinumab 300 mg")[0] == "Cosentyx"


def test_hyphenated_development_codes_survive_stripping():
    """ABT-494 is upadacitinib; splitting the hyphen would split one node into two."""
    assert ctg._canonical_treatment("ABT-494 15 mg QD")[0] == "ABT-494"
    assert ctg._canonical_treatment("BI-655066 180 mg SC")[0] == "BI-655066"


def test_a_dash_used_as_a_separator_is_still_removed():
    assert ctg._canonical_treatment("Fictumab - 150 mg")[0] == "Fictumab"


def test_stripping_never_returns_an_empty_node_name():
    """A label made entirely of noise must fall back, not vanish."""
    treatment, _ = ctg._canonical_treatment("150 mg QD oral tablet")
    assert treatment.strip()


def test_dose_survives_stripping_on_the_arm_record(select_psa):
    """The node name loses the dose; the arm record must not."""
    arm = next(a for a in select_psa.arms if a.label == "Upadacitinib 15 mg QD")
    assert arm.treatment == "Rinvoq"       # dose-free node
    assert arm.dose_value == 15.0           # dose preserved
    assert arm.label == "Upadacitinib 15 mg QD"
    assert arm.dose_description == "Upadacitinib 15 mg QD"


# =====================================================================================
# Dose attribution
#
# Every label below is verbatim from the live PsA harvest, where ten placebo arms carried an
# active drug's strength. They arrive with four different separators — "/", "to",
# "Followed by" and "Plus" — which is why attribution is by exclusivity rather than by
# splitting the label on punctuation.
# =====================================================================================
@pytest.mark.parametrize("label", [
    "Placebo / Upadacitinib 15 mg",
    "Placebo / Upadacitinib 30 mg",
    # The crossover partner is not in the drug catalog, so a curated-agent check alone would
    # miss this one. Placebo having no dose of its own is what catches it.
    "Placebo/Tofacitinib, 5 mg, Twice Daily",
    "Placebo to Guselkumab 100 mg q4w",
    "Placebo Followed by Guselkumab 100 mg",
    "Placebo/BKZ 160 mg Q4W",
])
def test_a_placebo_arm_never_carries_the_dose_of_the_drug_it_crossed_over_to(label):
    """``dose_policy`` decides whether doses may be pooled, so this is not cosmetic.

    A Placebo node keyed at 15 mg describes an arm that received no active drug at all, and
    the pooling decision is made on that key.
    """
    treatment, is_placebo = ctg._canonical_treatment(label)
    assert is_placebo
    dose, unit, frequency, warning = ctg._arm_dose(
        label, treatment=treatment, is_placebo=is_placebo
    )
    assert (dose, unit, frequency) == (None, None, None)
    assert warning and "Placebo" in warning


def test_an_arm_stating_two_doses_records_neither():
    """Verbatim from the harvest. No single dose_value is true of a two-agent regimen."""
    dose, unit, _, warning = ctg._arm_dose(
        "Group 1: Guselkumab 100 mg q4w Plus Golimumab 50 mg q4w",
        treatment="Tremfya", is_placebo=False,
    )
    assert (dose, unit) == (None, None)
    assert warning and "2 distinct doses" in warning


def test_a_dose_is_not_attributed_when_the_label_names_another_agent():
    """One strength, two agents: the label does not say which of them it belongs to."""
    dose, _, _, warning = ctg._arm_dose(
        "Adalimumab 40 mg Plus Methotrexate", treatment="Humira", is_placebo=False
    )
    assert dose is None
    assert warning and "Methotrexate" in warning


def test_a_monotherapy_arm_still_records_its_dose():
    """The false-positive guard: withholding must not cost the common case its pooling key."""
    assert ctg._arm_dose(
        "Upadacitinib 15 mg QD", treatment="Rinvoq", is_placebo=False
    ) == (15.0, "mg", "QD", None)
    # Uncurated too. ``agents_in`` reporting nothing is not evidence of a second agent.
    assert ctg._arm_dose(
        "Fictumab 150 mg Q4W", treatment="Fictumab", is_placebo=False
    ) == (150.0, "mg", "Q4W", None)


def test_a_placebo_arm_keeps_a_frequency_it_states_on_its_own():
    """Withholding is about attribution, not about distrusting placebo labels."""
    assert ctg._arm_dose(
        "Placebo QD", treatment="Placebo", is_placebo=True
    ) == (None, None, "QD", None)


# =====================================================================================
# Which node a label mentioning placebo actually belongs to
#
# Placebo is the anchor every indirect estimate is chained through, so an active arm folded
# into it does not cost one node — it inflates the comparator every other node is measured
# against. The discriminator is label ORDER, because registries name the randomised
# allocation first. Preferring a curated agent instead would break the two cases below that
# are genuinely placebo arms.
# =====================================================================================
@pytest.mark.parametrize("label,node", [
    # Verbatim from the live PsA harvest. This one was resolving to Placebo.
    ("Group 2: Guselkumab 100 mg q4w Plus Placebo", "Tremfya"),
    ("Guselkumab 100 mg Plus Placebo", "Tremfya"),
    ("Adalimumab 40 mg EOW + Placebo", "Humira"),
    # Withdrawal design: active drug first, placebo after. Still the drug's arm.
    ("Upadacitinib 15 mg / Placebo", "Rinvoq"),
])
def test_an_add_on_arm_is_not_the_placebo_node(label, node):
    """A double-dummy placebo does not make an actively treated arm a placebo arm.

    ``_collect_placebo_rates`` in the coverage audit reads ``StudyArm.is_placebo``, which is
    this second return value, so the Phase 0 placebo-response-by-route measurement is
    downstream of exactly this call.
    """
    treatment, is_placebo = ctg._canonical_treatment(label)
    assert treatment == node
    assert not is_placebo


@pytest.mark.parametrize("label", [
    # Background therapy shared by every arm — the comparison is drug+MTX vs placebo+MTX.
    "Placebo Plus MTX",
    "Placebo + Methotrexate",
    "Placebo and Methotrexate 15 mg Weekly",
    # Crossover: placebo through the primary window, active drug afterwards.
    "Placebo / Upadacitinib 15 mg",
    "Placebo to Guselkumab 100 mg q4w",
    "Placebo Followed by Guselkumab 100 mg",
])
def test_a_placebo_arm_naming_another_agent_is_still_the_placebo_node(label):
    """The false-positive guard, and the reason order beats "prefer the curated agent".

    Methotrexate is curated precisely because it is the usual background arm, so a rule that
    handed the node to any named agent would empty the placebo node in most PsA and RA
    trials — losing the common comparator rather than mislabelling it.
    """
    treatment, is_placebo = ctg._canonical_treatment(label)
    assert treatment == treatments.PLACEBO
    assert is_placebo


def test_the_placebo_predicate_and_the_resolver_cannot_drift():
    """``is_placebo`` is read on raw labels while the resolver's flag is stored on the arm.

    Two answers to "is this a placebo arm" is how the network builder and the coverage audit
    would come to disagree about the same study.
    """
    for label in ("Placebo", "IR PBO Washout", "Placebo Plus MTX",
                  "Placebo / Upadacitinib 15 mg", "Group 2: Guselkumab 100 mg q4w Plus Placebo",
                  "Upadacitinib 15 mg QD", "Total"):
        assert treatments.is_placebo(label) is ctg._canonical_treatment(label)[1]


def test_an_add_on_arm_still_records_the_dose_of_the_drug_it_received():
    """Resolving to Tremfya makes the 100 mg attributable, where Placebo made it withheld."""
    treatment, is_placebo = ctg._canonical_treatment(
        "Group 2: Guselkumab 100 mg q4w Plus Placebo"
    )
    assert ctg._arm_dose(
        "Group 2: Guselkumab 100 mg q4w Plus Placebo",
        treatment=treatment, is_placebo=is_placebo,
    ) == (100.0, "mg", "Q4W", None)


# =====================================================================================
# Labels seen in the live registry
#
# Every label below is verbatim from a ClinicalTrials.gov psoriatic arthritis search. The
# first live run reported 44 uncurated labels; these are the classes they fell into. Two
# of them were our own focus drugs and one was placebo, so this is not a long-tail
# coverage problem — it is the network's anchor and its two most important nodes.
# =====================================================================================
@pytest.mark.parametrize("label,node", [
    ("Interruption UPA", "Rinvoq"),
    ("Part 2 ADA", "Humira"),
    ("Part 1 ADA MTX", "Humira"),
    ("IR Ixe", "Taltz"),
    ("Inadequate Responders Ixe", "Taltz"),
    ("BKZ", "Bimzelx"),
    ("ADA", "Humira"),
])
def test_a_registry_abbreviation_reaches_its_curated_node(label, node):
    """Registries label arms ``UPA``/``ADA``/``IXE`` far more often than brand or generic."""
    assert ctg._canonical_treatment(label)[0] == node


def test_pbo_is_the_placebo_node():
    """The anchor for every indirect comparison. Missing it disconnects the network."""
    node, is_placebo = ctg._canonical_treatment("IR PBO Washout")
    assert node == treatments.PLACEBO
    assert is_placebo


def test_sham_is_deliberately_not_folded_into_placebo():
    """``PBO`` is an abbreviation of placebo; a sham procedure is a different control.

    Merging them is a claim about comparability, so it stays a curation decision.
    """
    assert ctg._canonical_treatment("Sham Comparator")[0] != treatments.PLACEBO


@pytest.mark.parametrize("label", ["Group A", "Arm B", "Cohort II", "Treatment 1"])
def test_a_bare_enumerator_is_flagged_rather_than_becoming_a_node(label):
    """Stripping the scaffolding word off "Group A" leaves "A", which is not a treatment.

    It is still reported rather than dropped: the arm is real, and only the source document
    can say what it received.
    """
    node, _ = ctg._canonical_treatment(label)
    assert treatments.is_uninformative_label(node)


def test_a_real_molecule_is_never_flagged_as_uninformative():
    for node in ("Rinvoq", "ABT-122", "MSB11022", "Placebo", "Standard Care"):
        assert not treatments.is_uninformative_label(node)


def test_a_dose_switch_arm_is_one_node_not_a_repeated_one():
    """"IXE Q2W/IXE Q4W" named the molecule twice once the doses came off."""
    assert ctg._canonical_treatment("IXE Q2W/IXE Q4W")[0] == "Taltz"
    assert ctg._canonical_treatment(
        "Fictumab 80 mg Q2W/Fictumab 80 mg Q4W"
    )[0] == "Fictumab"


def test_a_device_comparison_collapses_to_one_molecule():
    """Two arms of one agent via different devices are one node.

    ``\\bautoinjector\\b`` never matched "Auto-injector", because hyphens are deliberately
    not separators here.
    """
    labels = ["MSB11022 via Auto-injector", "MSB11022 via Pre-filled Syringe"]
    assert {ctg._canonical_treatment(x)[0] for x in labels} == {"MSB11022"}


def test_an_investigational_code_stays_its_own_node():
    """ABT-122 is a bispecific, not upadacitinib. ``EW`` was the unrecognised part."""
    assert ctg._canonical_treatment("ABT-122 EW")[0] == "ABT-122"


def test_a_strategy_arm_is_left_uncurated_rather_than_forced_into_the_drug_table():
    """"Standard Care" is a care strategy, not an agent.

    Giving it a drug-catalog entry would make it look comparable to a drug node. It stays
    uncurated and visible so a human decides whether the study belongs in a drug network.
    """
    for label in ("Standard Care", "Early TNF Inhibition", "Combination csDMARD"):
        node, _ = ctg._canonical_treatment(label)
        assert node == label
        assert not treatments.is_uninformative_label(node)


def test_dose_levels_of_one_molecule_do_not_become_three_nodes():
    """The second run's worst finding, and self-inflicted.

    Stripping the word "Dose" and leaving its ordinal turned one molecule into
    ``sonelokimab 1``, ``sonelokimab 2`` and ``sonelokimab 3`` — the exact fragmentation
    the stripper exists to prevent.
    """
    labels = ["Sonelokimab Dose 1", "Sonelokimab Dose 2", "sonelokimab 3",
              "Sonelokimab Dose Level 2"]
    assert {ctg._canonical_treatment(x)[0] for x in labels} == {"Sonelokimab"}


def test_an_uncurated_molecule_also_loses_its_dose_ordinal():
    """The rule has to hold without a catalog entry, or the next drug repeats the bug."""
    labels = ["Fictumab Dose 1", "Fictumab 2", "Fictumab Dose Level 3"]
    assert {ctg._canonical_treatment(x)[0] for x in labels} == {"Fictumab"}


@pytest.mark.parametrize("label,node", [
    # A development code written with a space instead of a hyphen keeps its number: the
    # digits are the molecule's identity, not a dose index.
    ("LY 3074828", "LY 3074828"),
    ("ABT 494", "ABT 494"),
    # Roman numerals and single letters are excluded, or these lose the half that names them.
    ("Vitamin D", "Vitamin D"),
    ("Factor VIII", "Factor VIII"),
])
def test_the_trailing_ordinal_rule_does_not_eat_part_of_a_name(label, node):
    assert ctg._canonical_treatment(label)[0] == node


@pytest.mark.parametrize("label", ["Healthy", "Healthy Volunteers", "Patients", "C control"])
def test_a_population_label_is_not_a_treatment(label):
    """"Healthy" names who was enrolled. It must not become a node beside a drug."""
    node, _ = ctg._canonical_treatment(label)
    assert treatments.is_uninformative_label(node)


@pytest.mark.parametrize("label", [
    "TNFi", "Standard bDMARD", "Combination csDMARD", "Early TNF Inhibition",
    "Standard Care", "Standard hold", "Shorter hold",
    # Registry typo, verbatim. Caught by the DMARD rule rather than by enumerating spellings.
    "Prescreen Based bDMARD Stategic",
])
def test_a_class_or_strategy_arm_is_detected(label):
    """These name a class or a care strategy, so their study leaves the molecule network."""
    assert treatments.is_class_level_label(label)


@pytest.mark.parametrize("label", [
    # Molecules, including one that IS a csDMARD — the drug is not its class.
    "Rinvoq", "Humira", "upadacitinib", "Methotrexate", "MTX", "Placebo",
    "ABT-122", "MSB11022", "Sonelokimab",
    # Population and enumerator labels belong in the other bucket, not this one.
    "Group A", "Healthy",
])
def test_a_molecule_is_never_mistaken_for_a_class(label):
    """A false positive here deletes a whole study, so the guard matters more than the rule."""
    assert not treatments.is_class_level_label(label)


@pytest.mark.parametrize("label", [
    "Total", "Total of all reporting groups", "Overall", "All Participants",
    "All Patients Randomized", "Combined", "Entire Cohort",
])
def test_a_total_row_is_detected_as_an_aggregate(label):
    """None of the other predicates reject these, so they would become plausible nodes.

    Pooled across studies on label identity, one "Total" node manufactures a shared
    comparator and closes loops the evidence never contained.
    """
    assert treatments.is_aggregate_label(label)


@pytest.mark.parametrize("label", [
    # A real arm may CONTAIN an aggregate word. Anchoring at the start is what saves these.
    "Placebo Total Daily Dose", "Overall Survival Cohort Rinvoq", "Rinvoq", "Placebo",
    "Combination csDMARD",
])
def test_a_real_arm_is_not_mistaken_for_an_aggregate(label):
    """A false positive here silently deletes a real arm's rows."""
    assert not treatments.is_aggregate_label(label)


def _parsed_with(*treatments_: str) -> ctg.ParsedStudy:
    return ctg.ParsedStudy(
        study=None,
        arms=[StudyArm(treatment=t, is_placebo=(t == "Placebo")) for t in treatments_],
    )


def test_a_class_level_arm_is_found_even_beside_real_drugs():
    """The unit of exclusion is the study, because the comparison is what is unusable.

    A "Humira versus TNFi" trial has one perfectly good node in it. Keeping that arm and
    dropping its comparator would leave an edge pointing at a node that is no longer there.
    """
    assert svc.class_level_arms(_parsed_with("Humira", "TNFi", "Placebo")) == ["TNFi"]


def test_a_study_of_only_real_drugs_is_not_screened():
    assert svc.class_level_arms(_parsed_with("Humira", "Rinvoq", "Placebo")) == []


def test_methotrexate_is_a_molecule_even_though_it_is_a_csdmard():
    """The curated check runs first, so the drug is not confused with its own class."""
    assert svc.class_level_arms(_parsed_with("Methotrexate", "MTX")) == []


def test_a_placebo_arm_never_triggers_screening():
    assert svc.class_level_arms(_parsed_with("Placebo")) == []


def test_a_combination_arm_still_resolves_to_one_node():
    """The node is one agent, and that is now a protocol decision rather than an accident.

    ``combination_policy: POOL_WITH_BACKBONE`` says so for PsA, where background csDMARD is
    permitted across the network rather than defining arms. What changed is that the other
    agent is no longer destroyed — see ``agents_in`` below.
    """
    assert ctg._canonical_treatment("Part 1 ADA MTX")[0] == "Humira"


def test_a_combination_arm_no_longer_loses_its_other_agent():
    """The information a combination_policy needs has to survive parsing to be usable.

    Dose is kept structured on the arm record for exactly this reason; a combination is the
    same kind of decision and was getting silently reduced to one agent.
    """
    assert treatments.agents_in("Part 1 ADA MTX") == ("Humira", "Methotrexate")
    assert treatments.is_combination("Part 1 ADA MTX")
    assert treatments.agents_in("ADA ew MTX") == ("Humira", "Methotrexate")


@pytest.mark.parametrize("label,agents", [
    ("Upadacitinib 15 mg QD", ("Rinvoq",)),
    ("RINVOQ", ("Rinvoq",)),
    ("Placebo", ()),                       # not an agent; reported by canonical_treatment
    ("Group A", ()),
    # Now curated, so it names its molecule. Doubles as proof that a hyphenated development
    # code survives normalisation while the "EW" frequency is stripped off it.
    ("ABT-122 EW", ("ABT-122",)),
])
def test_a_monotherapy_arm_names_exactly_one_agent(label, agents):
    assert treatments.agents_in(label) == agents


def test_a_biosimilar_is_not_mistaken_for_a_combination():
    """A longer alias claims its span, so a shorter one cannot fire inside it.

    Without that, "adalimumab-aacf" would report both the biosimilar and Humira and look
    like a two-agent arm, which would then be pooled or split by the wrong policy.
    """
    assert treatments.agents_in("MSB11022") == ("MSB11022",)
    assert not treatments.is_combination("MSB11022")
    assert treatments.agents_in("MSB11022 via Auto-injector") == ("MSB11022",)


def test_a_biosimilar_records_its_originator_without_pooling_onto_it():
    """Recorded, not acted on: pooling is a clinical claim for ``biosimilar_policy``."""
    assert taxonomy.biosimilar_of("MSB11022") == "Humira"
    assert taxonomy.biosimilar_of("Humira") is None
    # Still its own node by default, which is the honest behaviour until a protocol says else.
    assert ctg._canonical_treatment("MSB11022")[0] == "MSB11022"


# =====================================================================================
# Treatment phase
# =====================================================================================
def _phase_of(title: str) -> str:
    return ctg._infer_treatment_phase({"identificationModule": {"officialTitle": title}})[0]


def test_induction_and_maintenance_are_inferred_from_the_title():
    assert _phase_of("A Study of Risankizumab Induction Therapy in Crohn's Disease") == "INDUCTION"
    assert _phase_of("Maintenance Therapy With Upadacitinib in Ulcerative Colitis") == "MAINTENANCE"


def test_a_trial_naming_both_phases_is_not_assigned_to_either():
    """Pooling re-randomised maintenance responders with induction patients is a hard gate."""
    phase, warning = ctg._infer_treatment_phase({
        "identificationModule": {
            "officialTitle": "A Study of Induction and Maintenance Therapy in Ulcerative Colitis"
        }
    })
    assert phase == "PRIMARY"
    assert warning and "separated" in warning


def test_a_trial_naming_neither_phase_defaults_to_primary():
    assert _phase_of("A Study of Upadacitinib in Psoriatic Arthritis") == "PRIMARY"


def test_the_inferred_phase_reaches_the_study_record():
    payload = {
        "protocolSection": {
            "identificationModule": {
                "nctId": "NCT77777777",
                "officialTitle": "Induction Study of Skyrizi in Crohn's Disease",
            },
            "conditionsModule": {"conditions": ["Crohn's Disease"]},
        }
    }
    parsed = ctg.parse(
        FetchResult(ok=True, source_type=ctg.SOURCE_TYPE, source_identifier="NCT77777777", payload=payload),
        indication="Crohn's Disease",
    )
    assert parsed.study.treatment_phase == "INDUCTION"


def test_a_short_alias_does_not_fire_inside_an_unrelated_molecule():
    """"alli" (orlistat) must not match inside another drug name."""
    treatment, _ = ctg._canonical_treatment("Adalimumab 40 mg EOW")
    assert treatment == "Humira"


# =====================================================================================
# Outcome measures — the fiddly part
# =====================================================================================
def test_binary_events_are_derived_from_percentage_and_flagged(select_psa):
    """An NMA needs events and N; the registry posted a rounded percentage."""
    acr20 = [o for o in select_psa.outcomes if "ACR20" in o.endpoint]
    assert len(acr20) == 4

    placebo = next(o for o in acr20 if o.result_id.endswith("OG000"))
    assert placebo.outcome_type == BINARY
    assert placebo.sample_size == 423
    assert placebo.events == round(36.2 / 100 * 423)  # 153
    assert ctg.FLAG_DERIVED_EVENTS in _flags(placebo)


def test_the_primary_outcome_maps_to_a_canonical_id(select_psa):
    acr20 = next(o for o in select_psa.outcomes if "ACR20" in o.endpoint)
    assert acr20.canonical_outcome_id == "PSA_ACR20_W16"
    assert acr20.timepoint_week == 12
    assert ctg.FLAG_UNMAPPED_ENDPOINT not in _flags(acr20)


def test_continuous_outcomes_carry_their_dispersion(select_psa):
    haq = [o for o in select_psa.outcomes if "HAQ-DI" in o.endpoint]
    assert len(haq) == 2
    placebo = next(o for o in haq if o.result_id.endswith("OG000"))
    assert placebo.outcome_type == CONTINUOUS
    assert placebo.mean == -0.14
    assert placebo.standard_error == 0.02
    assert placebo.standard_deviation is None  # dispersionType was STANDARD_ERROR


def test_a_stratified_outcome_yields_one_row_per_class_not_a_silent_collapse(select_psa):
    """Two BSA strata x two groups = four rows, each labelled with its stratum."""
    pasi = [o for o in select_psa.outcomes if "PASI90" in o.endpoint]
    assert len(pasi) == 4
    assert all(ctg.FLAG_STRATIFIED in _flags(o) for o in pasi)
    definitions = " ".join(o.endpoint_definition or "" for o in pasi)
    assert "BSA >= 3%" in definitions and "BSA < 3%" in definitions


def test_an_unrecognised_param_type_is_flagged_never_guessed(select_psa):
    hazard = [o for o in select_psa.outcomes if "Minimal Disease Activity" in o.endpoint]
    assert len(hazard) == 1
    assert ctg.FLAG_UNKNOWN_PARAM_TYPE in _flags(hazard[0])
    assert any("HAZARD_RATIO" in w for w in select_psa.warnings)


def test_an_unposted_outcome_is_skipped(select_psa):
    """reportingStatus NOT_POSTED means there is nothing to extract."""
    assert not any("ACR50" in o.endpoint for o in select_psa.outcomes)


def test_a_missing_denominator_is_flagged(select_psa):
    hazard = next(o for o in select_psa.outcomes if "Minimal Disease Activity" in o.endpoint)
    assert ctg.FLAG_NO_DENOMINATOR in _flags(hazard)
    assert hazard.sample_size is None


def test_every_row_carries_source_text_and_provenance(select_psa):
    """No extracted row may exist without a traceable origin."""
    for row in select_psa.outcomes:
        assert row.study_id == "NCT03104400"
        assert row.source_text
        assert row.extraction_confidence is not None
        assert row.endpoint


def test_rows_link_back_to_their_arm(select_psa):
    arm_ids = {a.arm_id for a in select_psa.arms}
    linked = [o for o in select_psa.outcomes if o.arm_id]
    assert linked
    assert all(o.arm_id in arm_ids for o in linked)


def test_every_row_is_attached_to_an_arm_across_the_two_id_spaces(select_psa):
    """The arm-linking regression.

    Participant-flow groups are FG###; each outcome measure's own groups are OG###. The
    parser originally looked measurement group IDs up in the participant-flow map, so on
    real registry data every row came back unattached — and a result with no arm cannot
    enter a network. The fixture used to reuse FG### in both places and hid this.
    """
    assert {a.arm_id.split(":")[1] for a in select_psa.arms} == {
        "FG000", "FG001", "FG002", "FG003"
    }
    assert all(o.result_id.split(":")[-1].startswith("OG") for o in select_psa.outcomes)

    unattached = [o for o in select_psa.outcomes if o.arm_id is None]
    assert not unattached, f"{len(unattached)} rows lost their arm across the ID spaces"
    assert not any(ctg.FLAG_UNRECONCILED_GROUP in _flags(o) for o in select_psa.outcomes)


def test_placebo_rows_are_identifiable_after_reconciliation(select_psa):
    """What the Phase 0 placebo-response-by-route measurement depends on."""
    placebo_arm_ids = {a.arm_id for a in select_psa.arms if a.is_placebo}
    placebo_rows = [o for o in select_psa.outcomes if o.arm_id in placebo_arm_ids]
    assert placebo_rows
    acr20 = next(o for o in placebo_rows if "ACR20" in o.endpoint)
    assert acr20.events and acr20.sample_size


def _one_measure_payload(nct_id: str, group_title: str, *, flow: list | None = None) -> dict:
    """One POSTED binary measure whose single group is *group_title*."""
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id},
            "conditionsModule": {"conditions": ["Psoriatic Arthritis"]},
        },
        "resultsSection": {
            "participantFlowModule": {
                "groups": flow if flow is not None else [
                    {"id": "FG000", "title": "Placebo / Upadacitinib 15 mg"},
                ],
            },
            "outcomeMeasuresModule": {
                "outcomeMeasures": [{
                    "title": "Percentage of Participants Achieving ACR20 Response",
                    "reportingStatus": "POSTED",
                    "paramType": "NUMBER",
                    "unitOfMeasure": "percentage of participants",
                    "timeFrame": "Week 16",
                    "groups": [{"id": "OG009", "title": group_title}],
                    "classes": [{"categories": [{"measurements": [
                        {"groupId": "OG009", "value": "40.0"}
                    ]}]}],
                }]
            },
        },
    }


def _parse_payload(nct_id: str, payload: dict) -> ctg.ParsedStudy:
    return ctg.parse(
        FetchResult(ok=True, source_type=ctg.SOURCE_TYPE, source_identifier=nct_id,
                    payload=payload),
        indication="Psoriatic Arthritis",
    )


def test_a_measure_group_participant_flow_never_named_becomes_its_own_arm():
    """The two ID spaces also describe two different PARTITIONS of the same patients.

    Participant flow names a group by its whole journey ("Placebo / Upadacitinib 15 mg");
    an outcome measure names it as it stood at that timepoint ("Placebo"). Exact-title
    reconciliation therefore failed for precisely the arms whose assignment changed later —
    disproportionately placebo, the comparator a star network is anchored on. On a live PsA
    harvest that silently orphaned 664 rows (10.5%), invisible to every network because
    ``_reporting_arms`` skips a row with no arm.

    Attaching it to one of the journey arms would halve it, so it gets an arm of its own.
    """
    parsed = _parse_payload("NCT55555555", _one_measure_payload("NCT55555555", "Placebo"))

    row = parsed.outcomes[0]
    assert row.arm_id is not None, "an unmatched group must not be silently orphaned"
    assert ctg.FLAG_UNRECONCILED_GROUP not in _flags(row)

    arm = next(a for a in parsed.arms if a.arm_id == row.arm_id)
    assert arm.treatment == "Placebo"
    assert arm.is_placebo
    # Its own arm, not one of the participant-flow journey arms.
    assert ":MG" in arm.arm_id


def test_an_aggregate_group_is_refused_an_arm_and_stays_flagged():
    """"Total" would become a fabricated shared comparator, which is worse than a lost row.

    No other predicate rejects it: it is not an enumerator, names no class, and
    ``canonical_treatment`` hands it back as a plausible node name. Pooled across studies on
    label identity it closes loops the evidence never contained.
    """
    parsed = _parse_payload("NCT55555556", _one_measure_payload("NCT55555556", "Total"))

    assert not any(":MG" in a.arm_id for a in parsed.arms)
    row = parsed.outcomes[0]
    assert row.arm_id is None
    assert ctg.FLAG_UNRECONCILED_GROUP in _flags(row)


def test_an_arm_carries_the_randomised_n_from_the_first_period():
    """``row.sample_size or arm.sample_size`` was a dead fallback — nothing ever set the arm.

    Period 1's ``STARTED`` milestone is the randomised denominator. A later period counts
    re-randomised completers, so reading it would shrink an arm to its responders.
    """
    payload = _one_measure_payload(
        "NCT55555557", "Placebo",
        flow=[{"id": "FG000", "title": "Placebo"}, {"id": "FG001", "title": "Rinvoq"}],
    )
    payload["resultsSection"]["participantFlowModule"]["periods"] = [
        {"title": "Period 1", "milestones": [
            {"type": "STARTED", "achievements": [
                {"groupId": "FG000", "numSubjects": "211"},
                {"groupId": "FG001", "numSubjects": "429"},
            ]},
            {"type": "COMPLETED", "achievements": [
                {"groupId": "FG000", "numSubjects": "177"},
                {"groupId": "FG001", "numSubjects": "370"},
            ]},
        ]},
    ]
    parsed = _parse_payload("NCT55555557", payload)

    sizes = {a.treatment: a.sample_size for a in parsed.arms}
    assert sizes["Placebo"] == 211, "must be STARTED, not COMPLETED"
    assert sizes["Rinvoq"] == 429


def test_a_group_absent_from_period_one_has_no_sample_size_rather_than_zero():
    """Period 1 posts 0 for groups that only exist later. Storing it asserts an empty arm."""
    payload = _one_measure_payload(
        "NCT55555558", "Placebo",
        flow=[{"id": "FG000", "title": "Placebo"},
              {"id": "FG001", "title": "Rinvoq Period 2"}],
    )
    payload["resultsSection"]["participantFlowModule"]["periods"] = [
        {"title": "Period 1", "milestones": [
            {"type": "STARTED", "achievements": [
                {"groupId": "FG000", "numSubjects": "211"},
                {"groupId": "FG001", "numSubjects": "0"},
            ]},
        ]},
    ]
    parsed = _parse_payload("NCT55555558", payload)

    by_id = {a.arm_id: a for a in parsed.arms}
    assert by_id["NCT55555558:FG000"].sample_size == 211
    assert by_id["NCT55555558:FG001"].sample_size is None


# =====================================================================================
# Per-visit classes — the timepoint lives on the class, not the measure
# =====================================================================================
def _repeated_measures_payload(nct_id: str, title: str, time_frame: str, visits: list[str]) -> dict:
    """One measure posted as a series: one class per visit, each with one value.

    This is how ClinicalTrials.gov reports "ACR20 response by visit over time" — the
    shape behind most of the harvested corpus.
    """
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id},
            "conditionsModule": {"conditions": ["Psoriatic Arthritis"]},
        },
        "resultsSection": {
            "participantFlowModule": {"groups": [{"id": "FG000", "title": "Rinvoq"}]},
            "outcomeMeasuresModule": {
                "outcomeMeasures": [{
                    "title": title,
                    "reportingStatus": "POSTED",
                    "paramType": "NUMBER",
                    "unitOfMeasure": "percentage of participants",
                    "timeFrame": time_frame,
                    "groups": [{"id": "OG000", "title": "Rinvoq"}],
                    "classes": [
                        {"title": visit, "categories": [{"measurements": [
                            {"groupId": "OG000", "value": "40.0"}
                        ]}]}
                        for visit in visits
                    ],
                }]
            },
        },
    }


def test_each_visit_class_resolves_its_own_timepoint():
    """The recoverable half of the 91%-unmapped defect.

    Every row of this measure previously inherited one measure-level week (2, the first
    number in the time frame), which no PsA window admits — so a trial's week-12, -16, -20
    and -24 ACR20 values were all discarded while sitting in the database.
    """
    parsed = _parse_payload("NCT60000001", _repeated_measures_payload(
        "NCT60000001",
        "Percentage of Participants Who Achieved ACR20 Response Through Week 24",
        "Weeks 2, 4, 8, 12, 16, 20 and 24",
        ["Week 2", "Week 4", "Week 8", "Week 12", "Week 16", "Week 20", "Week 24"],
    ))

    by_week = {row.timepoint_week: row for row in parsed.outcomes}
    assert set(by_week) == {2.0, 4.0, 8.0, 12.0, 16.0, 20.0, 24.0}
    # PSA_ACR20_W16 admits weeks 12-24; the earlier visits are correctly refused.
    assert by_week[16.0].canonical_outcome_id == "PSA_ACR20_W16"
    assert by_week[12.0].canonical_outcome_id == "PSA_ACR20_W16"
    assert by_week[2.0].canonical_outcome_id is None
    assert by_week[8.0].canonical_outcome_id is None
    # And the row discloses where its week came from.
    assert ctg.FLAG_VISIT_TIMEPOINT in _flags(by_week[16.0])


def test_a_visit_outside_the_window_is_not_swept_in_with_the_one_inside_it():
    """The same defect in the other direction, which is the worse one.

    "at Weeks 24, 28, 36, 44 and 52" parsed to 24 \u2014 inside the PsA window \u2014 so **all 30
    rows** of a live measure were stamped PSA_ACR20_W16, including the values measured at
    weeks 28 through 52. An unmapped row is a gap; a wrongly mapped one is a wrong number
    in a network.
    """
    parsed = _parse_payload("NCT60000002", _repeated_measures_payload(
        "NCT60000002",
        "Percentage of Participants Who Achieved ACR20 Response at Weeks 24, 28, 36, 44 and 52",
        "Weeks 24, 28, 36, 44 and 52",
        ["Week 24", "Week 28", "Week 36", "Week 44", "Week 52"],
    ))

    mapped = {row.timepoint_week for row in parsed.outcomes if row.canonical_outcome_id}
    assert mapped == {24.0}, "only the visit the window admits may map"


def test_visit_classes_are_not_reported_as_population_strata():
    """A class titled "Week 12" is a timepoint. Calling it a stratum overstates how much
    of the corpus is subgroup data, and the scoping report's population filter reads that
    flag."""
    per_visit = _parse_payload("NCT60000003", _repeated_measures_payload(
        "NCT60000003", "Percentage Achieving ACR20 Response", "Weeks 12 and 16",
        ["Week 12", "Week 16"],
    ))
    assert not any(ctg.FLAG_STRATIFIED in _flags(row) for row in per_visit.outcomes)

    subgroups = _parse_payload("NCT60000004", _repeated_measures_payload(
        "NCT60000004", "Percentage Achieving ACR20 Response at Week 16", "Week 16",
        ["Biologic-naive", "TNF inadequate responder"],
    ))
    assert all(ctg.FLAG_STRATIFIED in _flags(row) for row in subgroups.outcomes)


def test_a_non_canonical_measure_is_flagged_unmapped_rather_than_ambiguous():
    """5785 of 6342 live rows carried ENDPOINT_AMBIGUOUS. 36 of them were ambiguous.

    The flag was chosen by ``len(match.candidates) > 1``, and an unmatched title used to
    hand back the whole indication's vocabulary as its candidates. The two need different
    responses: ambiguity needs a curator to choose, a non-canonical measure needs either a
    ``match_tokens`` synonym or nothing at all.
    """
    parsed = _parse_payload("NCT60000005", _repeated_measures_payload(
        "NCT60000005", "Change From Baseline in ACR Components", "Week 16", ["Week 16"],
    ))
    flags = _flags(parsed.outcomes[0])
    assert ctg.FLAG_UNMAPPED_ENDPOINT in flags
    assert ctg.FLAG_AMBIGUOUS_ENDPOINT not in flags

    both = _parse_payload("NCT60000006", _repeated_measures_payload(
        "NCT60000006", "Percentage Achieving ACR 20 and ACR 50 Responses", "Week 16",
        ["Week 16"],
    ))
    assert ctg.FLAG_AMBIGUOUS_ENDPOINT in _flags(both.outcomes[0])


def test_a_crossover_label_keeps_its_strength_everywhere_except_the_dose_field():
    """Nothing is lost when attribution is withheld — only the claim that it is this dose."""
    parsed = _parse_payload("NCT55555559", _one_measure_payload("NCT55555559", "Placebo"))

    arm = next(a for a in parsed.arms if a.arm_id.endswith("FG000"))
    assert arm.label == "Placebo / Upadacitinib 15 mg"
    assert arm.dose_description == "Placebo / Upadacitinib 15 mg"
    assert arm.dose_value is None
    assert any("Placebo node" in w for w in parsed.warnings)


def test_a_group_counted_only_in_a_later_period_is_not_a_randomised_arm():
    """NCT03104400 is a **four**-arm trial with **ten** participant-flow groups.

    Five of them are ``… Period 2 (Weeks 56 to 260)`` — the same patients re-counted after
    re-randomisation, which is already why ``_arm_sample_sizes`` refuses period 2's N. Left
    in, they overstate the trial in every report that counts arms and give a later-period
    group its own chance to answer for that treatment in a network.
    """
    payload = _one_measure_payload(
        "NCT55555560", "Placebo",
        flow=[{"id": "FG000", "title": "Placebo"},
              {"id": "FG001", "title": "Upadacitinib 15 mg"},
              {"id": "FG002", "title": "Placebo Period 2 (Weeks 56 to 260)"},
              {"id": "FG003", "title": "Upadacitinib 15 mg Period 2 (Weeks 56 to 260)"}],
    )
    payload["resultsSection"]["participantFlowModule"]["periods"] = [
        {"title": "Period 1", "milestones": [{"type": "STARTED", "achievements": [
            {"groupId": "FG000", "numSubjects": "211"},
            {"groupId": "FG001", "numSubjects": "429"},
            {"groupId": "FG002", "numSubjects": "0"},
            {"groupId": "FG003", "numSubjects": "0"},
        ]}]},
        {"title": "Period 2", "milestones": [{"type": "STARTED", "achievements": [
            {"groupId": "FG002", "numSubjects": "180"},
            {"groupId": "FG003", "numSubjects": "390"},
        ]}]},
    ]
    parsed = _parse_payload("NCT55555560", payload)

    assert {a.arm_id for a in parsed.arms if ":FG" in a.arm_id} == {
        "NCT55555560:FG000", "NCT55555560:FG001"
    }
    assert any("later period" in w for w in parsed.warnings)


def test_a_single_period_record_keeps_every_group():
    """Exclusion needs positive evidence. With one period there is nothing to compare to."""
    payload = _one_measure_payload(
        "NCT55555561", "Placebo",
        flow=[{"id": "FG000", "title": "Placebo"},
              {"id": "FG001", "title": "Rinvoq Period 2"}],
    )
    payload["resultsSection"]["participantFlowModule"]["periods"] = [
        {"title": "Period 1", "milestones": [{"type": "STARTED", "achievements": [
            {"groupId": "FG000", "numSubjects": "211"},
        ]}]},
    ]
    parsed = _parse_payload("NCT55555561", payload)

    assert {a.arm_id for a in parsed.arms if ":FG" in a.arm_id} == {
        "NCT55555561:FG000", "NCT55555561:FG001"
    }


def _multi_class_payload(nct_id: str, titles_and_values: list[tuple[str, str]]) -> dict:
    """One measure whose classes are *titles_and_values*, all against one group."""
    payload = _one_measure_payload(nct_id, "Placebo")
    measure = payload["resultsSection"]["outcomeMeasuresModule"]["outcomeMeasures"][0]
    measure["denoms"] = [
        {"units": "Participants", "counts": [{"groupId": "OG009", "value": "100"}]}
    ]
    measure["classes"] = [
        {"title": title,
         "categories": [{"measurements": [{"groupId": "OG009", "value": value}]}]}
        for title, value in titles_and_values
    ]
    return payload


def test_a_class_naming_another_member_of_the_family_does_not_inherit_the_endpoint():
    """The worst of the four: PASI 50, 75 and 100 numbers stored as ``PSA_PASI90_W16``.

    A live PsA measure titled "…Achieved a PASI 50, PASI 75, PASI 90 and PASI 100 Response"
    posts 20 classes. Only PASI 90 is modelled for this indication, so the measure title
    matched it **unambiguously** and every class inherited it. The matcher's refusal to guess
    was not defeated — it was bypassed by asking about the family instead of the member.
    """
    payload = _multi_class_payload("NCT55555562", [
        ("PASI 75 responders", "40.0"),
        ("PASI 90 responders", "25.0"),
        ("PASI 100 responders", "10.0"),
    ])
    measure = payload["resultsSection"]["outcomeMeasuresModule"]["outcomeMeasures"][0]
    measure["title"] = (
        "Percentage of Participants Who Achieved a PASI 75, PASI 90 and PASI 100 Response"
    )
    measure["timeFrame"] = "Week 16"
    parsed = _parse_payload("NCT55555562", payload)

    by_class = {o.result_id.split(":")[2]: o for o in parsed.outcomes}
    assert by_class["1"].canonical_outcome_id == "PSA_PASI90_W16"  # the class that names it
    assert by_class["0"].canonical_outcome_id is None              # PASI 75 is not PASI 90
    assert by_class["2"].canonical_outcome_id is None
    assert ctg.FLAG_CLASS_NAMES_ANOTHER_ENDPOINT in _flags(by_class["0"])
    assert ctg.FLAG_CLASS_NAMES_ANOTHER_ENDPOINT not in _flags(by_class["1"])


def test_two_classes_claiming_one_endpoint_at_one_week_are_both_left_unidentified(select_psa):
    """The fixture's BSA strata are the genuine subgroup shape, and they collide.

    Both rows are the same arm, endpoint and week with different numbers, so at most one can
    be that endpoint's value. ``gather_evidence`` keys arm data by treatment, so leaving both
    identified means whichever row is read last silently becomes the arm's number — which is
    how a subgroup result reaches an engine dressed as the randomised one.
    """
    pasi = [o for o in select_psa.outcomes if "PASI90" in o.endpoint]
    assert len(pasi) == 4                    # still one row per class; nothing collapsed
    assert all(o.canonical_outcome_id is None for o in pasi)
    assert all(ctg.FLAG_ENDPOINT_NOT_DISTINGUISHED in _flags(o) for o in pasi)
    # The stratum itself is never discarded, so curation can still act on the row.
    assert any("BSA" in (o.endpoint_definition or "") for o in pasi)


def test_a_visit_series_is_not_labelled_a_subgroup():
    """``STRATIFIED_RESULT`` covered 5528 of 6342 rows and almost none were subgroups.

    311 of 327 multi-class measures in the harvest are visit series and 16 are endpoint
    families. Labelling those as strata is what made the population filter look as though it
    were carrying the corpus.
    """
    payload = _multi_class_payload("NCT55555565", [
        ("Week 12", "30.0"), ("Week 16", "35.0"), ("Week 24", "40.0"),
    ])
    measure = payload["resultsSection"]["outcomeMeasuresModule"]["outcomeMeasures"][0]
    measure["title"] = "Percentage of Participants Achieving ACR20 Response"
    measure["timeFrame"] = "Weeks 12, 16 and 24"
    parsed = _parse_payload("NCT55555565", payload)

    assert {o.timepoint_week for o in parsed.outcomes} == {12.0, 16.0, 24.0}
    assert all(o.canonical_outcome_id == "PSA_ACR20_W16" for o in parsed.outcomes)
    assert not any(ctg.FLAG_STRATIFIED in _flags(o) for o in parsed.outcomes)


def test_no_count_is_derived_when_a_class_disputes_the_measure_denominator():
    """185 of 597 posted measures post class-level ``denoms``, and they cannot be read.

    In NCT01695239 the measure states 38 participants for a group while its ``PASI 75`` class
    states 7 for the same group. Neither reading survives arithmetic: only 1 of the 185 is
    consistent with the class figure being a numerator, and dividing 10.9% by 7 yields one
    participant. So the N is unknown, and a count derived from the wrong one is not a lossy
    number — it is a wrong one.
    """
    payload = _one_measure_payload("NCT55555563", "Placebo")
    measure = payload["resultsSection"]["outcomeMeasuresModule"]["outcomeMeasures"][0]
    measure["denoms"] = [
        {"units": "Participants", "counts": [{"groupId": "OG009", "value": "38"}]}
    ]
    measure["classes"][0]["denoms"] = [
        {"units": "Participants", "counts": [{"groupId": "OG009", "value": "7"}]}
    ]
    parsed = _parse_payload("NCT55555563", payload)

    row = parsed.outcomes[0]
    assert row.events is None, "a fabricated numerator is worse than a missing one"
    assert ctg.FLAG_DISPUTED_DENOMINATOR in _flags(row)
    assert ctg.FLAG_DERIVED_EVENTS not in _flags(row)


def test_an_agreeing_class_denominator_is_not_a_dispute():
    """The guard: a class restating the measure's N must not cost the row its count."""
    payload = _one_measure_payload("NCT55555564", "Placebo")
    measure = payload["resultsSection"]["outcomeMeasuresModule"]["outcomeMeasures"][0]
    measure["denoms"] = [
        {"units": "Participants", "counts": [{"groupId": "OG009", "value": "50"}]}
    ]
    measure["classes"][0]["denoms"] = [
        {"units": "Participants", "counts": [{"groupId": "OG009", "value": "50"}]}
    ]
    parsed = _parse_payload("NCT55555564", payload)

    row = parsed.outcomes[0]
    assert row.events == round(40.0 / 100 * 50)
    assert ctg.FLAG_DERIVED_EVENTS in _flags(row)
    assert ctg.FLAG_DISPUTED_DENOMINATOR not in _flags(row)


def test_one_result_reported_by_two_measures_is_disclosed_rather_than_dropped():
    """A registry posts the same number twice and both readings are faithful.

    Once as its own measure ("ACR 20 Response at Week 16") and again inside a combined one
    ("ACR 20, ACR 50 and ACR 70 Response" by visit). 18 of 44 such identities in the harvest
    carry identical numbers, so withholding either would discard correct evidence. The
    consumer is what has the problem, so the count is reported and the rows are left alone.
    """
    payload = _one_measure_payload("NCT55555566", "Placebo")
    measures = payload["resultsSection"]["outcomeMeasuresModule"]["outcomeMeasures"]
    measures.append(json.loads(json.dumps(measures[0])))
    measures[1]["title"] = "Percentage Achieving ACR20 Response by Visit"
    parsed = _parse_payload("NCT55555566", payload)

    assert len(parsed.outcomes) == 2, "both postings are kept"
    assert {o.canonical_outcome_id for o in parsed.outcomes} == {"PSA_ACR20_W16"}
    assert any("whichever it reads last" in w for w in parsed.warnings)


def test_the_flag_census_counts_how_much_of_a_study_each_caveat_covers(select_psa):
    """A flag nobody counts is not disclosure.

    ``EVENTS_DERIVED_FROM_PERCENTAGE`` covered 2468 of 6342 rows in one harvest and no report
    said so, which is how a number back-derived from a rounded percentage reaches a reviewer
    looking exactly like one the registry posted.
    """
    census = select_psa.flag_counts
    assert census[ctg.FLAG_DERIVED_EVENTS] == sum(
        1 for o in select_psa.outcomes if ctg.FLAG_DERIVED_EVENTS in _flags(o)
    )
    # Most frequent first, so the dominant caveat is the one a reader sees.
    assert list(census.values()) == sorted(census.values(), reverse=True)


# =====================================================================================
# Degradation
# =====================================================================================
def test_parse_returns_none_for_a_failed_fetch():
    failed = FetchResult.failure(ctg.SOURCE_TYPE, "NCT00000000", "not found", status_code=404)
    assert ctg.parse(failed) is None


def test_parse_survives_an_empty_payload():
    empty = FetchResult(ok=True, source_type=ctg.SOURCE_TYPE, source_identifier="NCT1", payload={})
    assert ctg.parse(empty) is None


def test_a_registration_without_results_still_yields_arms():
    """Worth holding: the arms exist, they simply have no measurements yet."""
    payload = {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT99999999", "briefTitle": "Ongoing trial"},
            "conditionsModule": {"conditions": ["Atopic Dermatitis"]},
            "armsInterventionsModule": {
                "armGroups": [
                    {"label": "Upadacitinib 30 mg QD"},
                    {"label": "Placebo"},
                ]
            },
        }
    }
    parsed = ctg.parse(
        FetchResult(ok=True, source_type=ctg.SOURCE_TYPE, source_identifier="NCT99999999", payload=payload),
        indication="Atopic Dermatitis",
    )
    assert parsed is not None
    assert not parsed.has_results
    assert {a.treatment for a in parsed.arms} == {"Rinvoq", "Placebo"}


def test_registry_condition_text_is_used_but_warned_about():
    """Registry conditions are free text and will not match the overlay reliably."""
    payload = {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT88888888"},
            "conditionsModule": {"conditions": ["Arthritis, Psoriatic"]},
        }
    }
    parsed = ctg.parse(
        FetchResult(ok=True, source_type=ctg.SOURCE_TYPE, source_identifier="NCT88888888", payload=payload)
    )
    assert parsed.study.indication == "Arthritis, Psoriatic"
    assert any("registry condition text" in w for w in parsed.warnings)
