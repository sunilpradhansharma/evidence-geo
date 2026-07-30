"""Phase 3B — PubMed/PMC adapter. No network.

The licence tests carry the most weight: PMC membership does not imply an open-access
grant, and getting that wrong is the difference between lawfully retaining a full text
and holding a document the platform has no right to.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.evidence import licensing
from app.evidence.sources import pubmed
from app.evidence.sources.base import FetchResult

_ESUMMARY = {
    "result": {
        "uids": ["38000001", "38000002", "38000003"],
        "38000001": {
            "title": "Comparative efficacy of biologics in psoriatic arthritis: a network meta-analysis.",
            "fulljournalname": "Annals of the Rheumatic Diseases",
            "pubdate": "2024 Mar 15",
            "authors": [{"name": "Smith J"}, {"name": "Doe A"}],
            "pubtype": ["Journal Article", "Meta-Analysis"],
            "articleids": [
                {"idtype": "doi", "value": "10.1136/ard-2024-000001"},
                {"idtype": "pmc", "value": "PMC1234567"},
            ],
        },
        "38000002": {
            "title": "Upadacitinib versus adalimumab: a pairwise meta-analysis.",
            "source": "J Rheumatol",
            "pubdate": "2023",
            "authors": [{"name": "Brown K"}],
            "pubtype": ["Journal Article", "Meta-Analysis"],
            "articleids": [],
        },
        "38000003": {
            "title": "",
            "pubdate": "2024 Jan",
            "articleids": [],
        },
    }
}


def _ok(payload) -> FetchResult:
    return FetchResult(
        ok=True, source_type=pubmed.SOURCE_TYPE, source_identifier="test", payload=payload
    )


@pytest.fixture(scope="module")
def citations() -> list[pubmed.Citation]:
    return pubmed.parse_summaries(_ok(_ESUMMARY))


# =====================================================================================
# Parsing
# =====================================================================================
def test_summaries_parse(citations):
    assert len(citations) == 2  # the untitled record is dropped
    first = citations[0]
    assert first.pmid == "38000001"
    assert first.journal == "Annals of the Rheumatic Diseases"
    assert first.publication_date == date(2024, 3, 15)
    assert first.doi == "10.1136/ard-2024-000001"
    assert first.pmcid == "PMC1234567"


def test_a_record_without_a_title_is_dropped(citations):
    assert "38000003" not in {c.pmid for c in citations}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2024 Mar 15", date(2024, 3, 15)),
        ("2024 Nov", date(2024, 11, 1)),
        ("2024", date(2024, 1, 1)),
        ("2024/03/15", date(2024, 3, 15)),
        ("2024 Feb 30", date(2024, 2, 1)),  # invalid day falls back to the month
        ("", None),
        ("Spring 2024", None),
    ],
)
def test_publication_date_parsing(raw, expected):
    assert pubmed._parse_pubdate(raw) == expected


def test_search_results_parse():
    assert pubmed.parse_search(_ok({"esearchresult": {"idlist": ["1", "2"]}})) == ["1", "2"]


def test_parsers_degrade_on_a_failed_fetch():
    failed = FetchResult.failure(pubmed.SOURCE_TYPE, "x", "transport error")
    assert pubmed.parse_search(failed) == []
    assert pubmed.parse_summaries(failed) == []


def test_formatted_citation_is_human_readable(citations):
    assert citations[0].formatted() == (
        "Smith J et al. (2024). Comparative efficacy of biologics in psoriatic arthritis: "
        "a network meta-analysis. Annals of the Rheumatic Diseases. PMID:38000001"
    )


# =====================================================================================
# Synthesis triage
# =====================================================================================
def test_only_true_nmas_reach_the_level_2_queue(citations):
    """A pairwise meta-analysis cannot resolve an indirect comparison."""
    candidates = pubmed.synthesis_candidates(citations)
    assert [c.pmid for c in candidates] == ["38000001"]


def test_a_pairwise_meta_analysis_is_a_synthesis_but_not_an_nma(citations):
    pairwise = next(c for c in citations if c.pmid == "38000002")
    assert pairwise.is_synthesis
    assert not pairwise.is_network_meta_analysis


def test_indirect_treatment_comparison_wording_is_recognised():
    citation = pubmed.Citation(pmid="1", title="An indirect treatment comparison of IL-23 inhibitors")
    assert citation.is_network_meta_analysis


# =====================================================================================
# Licence boundary
# =====================================================================================
def test_pmc_membership_alone_does_not_grant_open_access():
    """The expensive mistake this function exists to prevent."""
    assert pubmed.licence_for_pmc_record(None) == licensing.RESTRICTED
    assert pubmed.licence_for_pmc_record("") == licensing.RESTRICTED
    assert pubmed.licence_for_pmc_record("Copyright the publisher") == licensing.RESTRICTED


def test_an_explicit_open_licence_grants_open_access():
    for text in ("CC BY 4.0", "CC0 1.0 Universal", "Public Domain"):
        assert pubmed.licence_for_pmc_record(text) == licensing.OPEN_ACCESS


@pytest.mark.parametrize(
    "text", ["CC BY-NC-ND 4.0", "CC BY-NC 4.0", "cc-by-nc-nd", "Attribution-NonCommercial 4.0"]
)
def test_nc_and_nd_grants_are_treated_conservatively(text):
    """Permission to read is not permission to store and reprocess.

    Every one of these strings also contains a permissive marker ("cc by"), so this is
    the regression guard for the ordering of the two checks.
    """
    assert pubmed.licence_for_pmc_record(text) == licensing.RESTRICTED


def test_share_alike_remains_open_access():
    """SA constrains redistribution terms, not our right to retain and reprocess."""
    assert pubmed.licence_for_pmc_record("CC BY-SA 4.0") == licensing.OPEN_ACCESS


def test_summary_metadata_is_public_domain_even_for_a_paywalled_article(citations):
    """The abstract record and the full text are two different licence questions."""
    paywalled = citations[0]
    assert paywalled.pmcid  # it is in PMC
    assert paywalled.license_class == licensing.PUBLIC_DOMAIN  # …but this is metadata
    assert pubmed.licence_for_pmc_record(None) == licensing.RESTRICTED  # …the text is not


def test_the_source_type_map_agrees_with_the_adapter():
    assert licensing.license_for_source(pubmed.SOURCE_TYPE) == licensing.PUBLIC_DOMAIN
    assert licensing.license_for_source(pubmed.PMC_OA_SOURCE_TYPE) == licensing.OPEN_ACCESS
    assert licensing.license_for_source(pubmed.PMC_SOURCE_TYPE) == licensing.RESTRICTED
