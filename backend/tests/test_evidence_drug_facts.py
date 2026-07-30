"""Phase 3B — openFDA label -> DrugFact mapping. No network."""
from __future__ import annotations

import json

import pytest

from app.evidence.lifecycles import EXTRACTED
from app.evidence.sources import openfda_facts as facts
from app.geo.sources.openfda import LabelSeed


def _seed(**overrides) -> LabelSeed:
    defaults = dict(
        brand_name="RINVOQ",
        generic_name="upadacitinib",
        manufacturer="AbbVie Inc.",
        administration_route="Oral",
        drug_class="Janus Kinase Inhibitor",
        active_ingredient="UPADACITINIB",
        has_boxed_warning=True,
        boxed_warning_text=(
            "SERIOUS INFECTIONS. Patients treated with RINVOQ are at increased risk.\n"
            "MORTALITY. A higher rate of all-cause mortality was observed.\n"
            "MALIGNANCIES. Lymphoma and other malignancies have been observed.\n"
            "THROMBOSIS. Thrombosis, including deep venous thrombosis, has occurred."
        ),
        indications_text="RINVOQ is indicated for the treatment of adults with...",
        effective_time="2024-11-01",
        set_id="abc-123",
        prescribing_information="https://dailymed.nlm.nih.gov/x",
    )
    defaults.update(overrides)
    return LabelSeed(**defaults)


def _flags(fact) -> set[str]:
    return set(json.loads(fact.mismatch_flags)) if fact.mismatch_flags else set()


def test_label_maps_onto_a_drug_fact():
    fact = facts.parse_label(_seed(), brand="Rinvoq", generic="upadacitinib", fact_id="DF-1")
    assert fact.brand == "Rinvoq"
    assert fact.generic == "upadacitinib"
    assert fact.manufacturer == "AbbVie Inc."
    assert fact.has_boxed_warning
    assert fact.label_updated_at.isoformat() == "2024-11-01"
    assert fact.regulatory_source == "FDA"


def test_the_curated_table_wins_over_the_label_classification():
    """brands.yaml is reviewed; pharm_class_epc is a regulatory string that often differs."""
    fact = facts.parse_label(_seed(), brand="Rinvoq", generic="upadacitinib", fact_id="DF-1")
    assert fact.drug_class == "JAK inhibitor"          # curated, not "Janus Kinase Inhibitor"
    assert fact.administration_route == "ORAL"
    assert "curated" in fact.extraction_rationale


def test_a_wording_difference_is_not_reported_as_a_conflict():
    """"JAK inhibitor" and "Janus Kinase Inhibitor" are the same classification."""
    fact = facts.parse_label(_seed(), brand="Rinvoq", generic="upadacitinib", fact_id="DF-1")
    assert facts.FLAG_CLASS_CONFLICT not in _flags(fact)


def test_a_genuinely_different_classification_is_flagged_for_review():
    fact = facts.parse_label(
        _seed(drug_class="Interleukin-23 Antagonist"),
        brand="Rinvoq", generic="upadacitinib", fact_id="DF-1",
    )
    assert facts.FLAG_CLASS_CONFLICT in _flags(fact)
    assert fact.drug_class == "JAK inhibitor"  # still curated; the flag is the signal


def test_a_route_disagreement_is_flagged():
    fact = facts.parse_label(
        _seed(administration_route="Subcutaneous"),
        brand="Rinvoq", generic="upadacitinib", fact_id="DF-1",
    )
    assert facts.FLAG_ROUTE_CONFLICT in _flags(fact)
    assert fact.administration_route == "ORAL"


def test_an_uncurated_drug_is_flagged_rather_than_silently_accepted():
    fact = facts.parse_label(
        _seed(brand_name="NEWDRUG", drug_class="Some Novel Class", administration_route="Oral"),
        brand="Newdrug", generic="novelmab", fact_id="DF-2",
    )
    assert facts.FLAG_NO_CURATED_ENTRY in _flags(fact)
    assert fact.drug_class == "Some Novel Class"       # falls back to the label
    assert fact.administration_route == "ORAL"


def test_an_unrecognised_route_becomes_none_not_a_raw_string():
    """Keeping the closed set closed — an unmapped route must not reach the NMA layer."""
    assert facts._normalise_route("Intrathecal") is None
    assert facts._normalise_route("SUBCUTANEOUS; INTRAVENOUS") == "SC"


def test_boxed_warnings_split_into_discrete_statements():
    fact = facts.parse_label(_seed(), brand="Rinvoq", generic="upadacitinib", fact_id="DF-1")
    warnings = json.loads(fact.boxed_warnings)
    assert len(warnings) == 4
    assert any("SERIOUS INFECTIONS" in w for w in warnings)
    assert any("THROMBOSIS" in w for w in warnings)


def test_indication_prose_is_marked_unstructured_rather_than_half_parsed():
    fact = facts.parse_label(_seed(), brand="Rinvoq", generic="upadacitinib", fact_id="DF-1")
    assert facts.FLAG_INDICATIONS_UNPARSED in _flags(fact)
    assert fact.approved_indications is None


def test_a_parsed_label_is_never_verified_and_never_externally_approved():
    """Structured is not the same as understood."""
    fact = facts.parse_label(_seed(), brand="Rinvoq", generic="upadacitinib", fact_id="DF-1")
    assert fact.verification_status == EXTRACTED
    assert fact.source_is_citable
    assert not fact.claim_is_approved_for_external_use


def test_a_malformed_effective_date_is_dropped_not_guessed():
    fact = facts.parse_label(
        _seed(effective_time="not-a-date"), brand="Rinvoq", generic="upadacitinib", fact_id="DF-1"
    )
    assert fact.label_updated_at is None


async def test_ingest_returns_none_when_openfda_has_nothing(monkeypatch):
    async def _nothing(brand, generic=None):  # noqa: ANN001, ARG001
        return None

    monkeypatch.setattr(facts, "fetch_label", _nothing)
    assert await facts.ingest("Nonexistent") is None


async def test_ingest_maps_a_returned_seed(monkeypatch):
    async def _seeded(brand, generic=None):  # noqa: ANN001, ARG001
        return _seed()

    monkeypatch.setattr(facts, "fetch_label", _seeded)
    fact = await facts.ingest("Rinvoq", generic="upadacitinib")
    assert fact is not None
    assert fact.fact_id == "openfda:rinvoq"
    assert fact.drug_class == "JAK inhibitor"
