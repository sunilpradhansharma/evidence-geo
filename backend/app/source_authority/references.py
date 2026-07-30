"""Recover citation provenance from an OpenEvidence answer's ``### References`` list.

OpenEvidence answers are pasted as Markdown that ends with a numbered reference list — each
entry naming the publisher and, for journal articles, a DOI. Operators frequently capture the
answer without filling the separate *Citations* field, which leaves ``Response.sources`` empty
so the answer never appears in the Source Authority dashboards. This module rebuilds that
provenance from the answer's own reference section.

Attribution is deliberately conservative:

* the publisher the reference explicitly names is mapped to its canonical domain via a curated
  list (longest/most-specific name wins, so a disease word in a title — "Leukemia", "Blood" —
  never out-votes a full journal name like "The New England Journal of Medicine");
* if the publisher is not recognised but the reference carries a DOI, the DOI resolver URL
  (``doi.org``) is used;
* otherwise the reference is skipped.

No domain is ever invented beyond the publisher the text names or a DOI the text contains.
"""
from __future__ import annotations

import re

# Curated publisher/issuer name -> canonical domain. Kept explicit (not derived) so attribution
# is auditable. Matched case-insensitively as a substring of the reference text; the list is
# sorted longest-name-first at import time so the most specific publisher wins.
_PUBLISHER_DOMAINS: list[tuple[str, str]] = [
    # Regulatory / guideline / government bodies
    ("u.s. food and drug administration", "fda.gov"),
    ("food and drug administration", "fda.gov"),
    ("european medicines agency", "ema.europa.eu"),
    ("national comprehensive cancer network", "nccn.org"),
    ("national institute for health and care excellence", "nice.org.uk"),
    ("national cancer institute", "cancer.gov"),
    ("centers for disease control and prevention", "cdc.gov"),
    ("world health organization", "who.int"),
    ("american society of clinical oncology", "asco.org"),
    ("american society of hematology", "hematology.org"),
    ("european society for medical oncology", "esmo.org"),
    # Journals (specific titles before generic ones)
    ("new england journal of medicine", "nejm.org"),
    ("journal of clinical oncology", "ascopubs.org"),
    ("annals of oncology", "annalsofoncology.org"),
    ("clinical lymphoma, myeloma", "sciencedirect.com"),
    ("clinical lymphoma", "sciencedirect.com"),
    ("british journal of haematology", "onlinelibrary.wiley.com"),
    ("american journal of hematology", "onlinelibrary.wiley.com"),
    ("journal of hematology & oncology", "jhoonline.biomedcentral.com"),
    ("leukemia & lymphoma", "tandfonline.com"),
    ("nature reviews clinical oncology", "nature.com"),
    ("nature medicine", "nature.com"),
    ("clinical cancer research", "aacrjournals.org"),
    ("cancer discovery", "aacrjournals.org"),
    ("cancer research", "aacrjournals.org"),
    ("cancer cell", "cell.com"),
    ("blood advances", "ashpublications.org"),
    ("haematologica", "haematologica.org"),
    ("jama oncology", "jamanetwork.com"),
    ("jama network", "jamanetwork.com"),
    ("lancet", "thelancet.com"),
    ("blood", "ashpublications.org"),
    ("leukemia", "nature.com"),
    ("nature", "nature.com"),
    ("cochrane", "cochranelibrary.com"),
    ("uptodate", "uptodate.com"),
    ("medscape", "medscape.com"),
    ("pubmed", "pubmed.ncbi.nlm.nih.gov"),
    ("jama", "jamanetwork.com"),
    ("bmj", "bmj.com"),
]

_SORTED_PUBLISHERS = sorted(_PUBLISHER_DOMAINS, key=lambda kv: -len(kv[0]))

# DOI only when the reference explicitly marks it ("doi:" or a doi.org URL), so a bare "10.x"
# inside a title is never mistaken for one.
_DOI_RE = re.compile(
    r"(?:doi:\s*|https?://(?:dx\.)?doi\.org/)(10\.\d{4,9}/[^\s\]);]+)", re.I
)
# A reference-list heading on its own line: "References", "## References", "References:".
_REF_HEADING_RE = re.compile(r"^[ \t]{0,3}#{0,6}[ \t]*references[ \t]*:?[ \t]*$", re.I | re.M)
# The start of a numbered entry: "1. ", "12) ", "(3) ".
_ENTRY_RE = re.compile(r"^\(?(\d{1,3})[.)]\s+(.*)$")

_MAX_REFS = 60


def _match_publisher(ref_lower: str) -> str | None:
    for needle, domain in _SORTED_PUBLISHERS:
        if needle in ref_lower:
            return domain
    return None


def _extract_doi(ref: str) -> str | None:
    m = _DOI_RE.search(ref)
    if not m:
        return None
    return m.group(1).rstrip(".,;)")


def _extract_reference_entries(text: str) -> list[str]:
    """Return the numbered reference lines that follow the ``References`` heading."""
    if not text:
        return []
    m = _REF_HEADING_RE.search(text)
    if m:
        tail = text[m.end():]
    else:  # fallback: a bare "References" line without markdown hashes
        low = text.lower()
        idx = low.rfind("\nreferences")
        if idx == -1:
            return []
        tail = text[idx + 1:]
        nl = tail.find("\n")
        tail = tail[nl:] if nl != -1 else ""

    entries: list[str] = []
    current: list[str] = []
    for raw in tail.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):  # a new markdown section ends the reference list
            break
        entry = _ENTRY_RE.match(line)
        if entry:
            if current:
                entries.append(" ".join(current))
            current = [entry.group(2).strip()]
        elif current:  # continuation of the previous (wrapped) reference
            current.append(line)
    if current:
        entries.append(" ".join(current))
    return [e.strip() for e in entries if len(e.strip()) >= 5]


def parse_reference_sources(answer_text: str) -> list[dict]:
    """Turn an OpenEvidence answer's reference list into ``sources`` dicts.

    The returned dicts match the shape written by the manual-capture path
    (``url``/``title``/``domain``/``redirect_url``/``snippet``/``origin``) so the standard
    Source Authority classifier can group and classify them unchanged.
    """
    out: list[dict] = []
    for ref in _extract_reference_entries(answer_text)[:_MAX_REFS]:
        domain = _match_publisher(ref.lower())
        doi = _extract_doi(ref)
        if domain:
            url = None
        elif doi:
            url, domain = f"https://doi.org/{doi}", None
        else:
            continue  # no recognisable publisher and no DOI -> not attributable
        out.append({
            "url": url,
            "title": ref[:500],
            "domain": domain,
            "redirect_url": None,
            "snippet": None,
            "origin": "GROUNDED",
        })
    return out
