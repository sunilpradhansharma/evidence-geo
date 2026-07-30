"""Tests for the OpenEvidence reference-list -> sources parser (app.source_authority.references)."""
from app.source_authority.references import parse_reference_sources

# A realistic OpenEvidence answer tail (matches the shape captured in prod).
SAMPLE = """
Venetoclax dose interruption is recommended for grade 4 neutropenia.[1][4]

---

Would you like to explore the specific CYP3A inhibitor dose adjustments?

### References

1. Venclexta. Food and Drug Administration. Updated date: 2026-05-20.
2. Acute Myeloid Leukemia. National Comprehensive Cancer Network. Updated 2025-11-24.
4. All-Oral Treatment of Newly Diagnosed Acute Myeloid Leukemia. Roboz GJ, Zeidan AM, Mannis GN, et al. The New England Journal of Medicine. 2026;394(21):2107-2116. doi:10.1056/NEJMoa2510223.
6. Venetoclax Combinations in Untreated CLL. Furstenau M, Niemann CU, et al. Blood. 2026;:blood.2025032160. doi:10.1182/blood.2025032160.
"""


def _by_title(srcs, needle):
    return next(s for s in srcs if needle.lower() in s["title"].lower())


def test_maps_named_publishers_to_domains():
    srcs = parse_reference_sources(SAMPLE)
    domains = [s["domain"] for s in srcs]
    assert "fda.gov" in domains
    assert "nccn.org" in domains
    assert "nejm.org" in domains
    assert "ashpublications.org" in domains


def test_specific_publisher_beats_title_disease_word():
    # The NEJM entry's title contains "Leukemia"; the full journal name must still win
    # over the short "leukemia" -> nature.com mapping.
    srcs = parse_reference_sources(SAMPLE)
    nejm = _by_title(srcs, "All-Oral Treatment")
    assert nejm["domain"] == "nejm.org"
    assert nejm["url"] is None  # named publisher -> classify by domain, not the DOI link


def test_all_entries_have_capture_shape():
    for s in parse_reference_sources(SAMPLE):
        assert set(s) == {"url", "title", "domain", "redirect_url", "snippet", "origin"}
        assert s["origin"] == "GROUNDED"
        assert s["title"]


def test_doi_fallback_for_unknown_publisher():
    txt = "### References\n\n1. A Case Report. Obscure Regional Bulletin. 2020. doi:10.9999/xyz.123."
    srcs = parse_reference_sources(txt)
    assert len(srcs) == 1
    assert srcs[0]["domain"] is None
    assert srcs[0]["url"] == "https://doi.org/10.9999/xyz.123"


def test_reference_without_publisher_or_doi_is_skipped():
    txt = "### References\n\n1. An untraceable personal communication, 2021."
    assert parse_reference_sources(txt) == []


def test_no_references_section_returns_empty():
    assert parse_reference_sources("A plain answer with no reference list.") == []
    assert parse_reference_sources("") == []


def test_wrapped_multiline_reference_is_joined():
    txt = (
        "### References\n\n"
        "1. A Long Title That Wraps\n"
        "   Across Two Lines. The Lancet. 2023. doi:10.1016/S0140-6736(23)00001-0.\n"
    )
    srcs = parse_reference_sources(txt)
    assert len(srcs) == 1
    assert srcs[0]["domain"] == "thelancet.com"
    assert "Across Two Lines" in srcs[0]["title"]
