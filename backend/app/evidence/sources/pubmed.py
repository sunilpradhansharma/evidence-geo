"""PubMed / PMC adapter (Phase 3B).

Retrieves citations and abstracts via E-utilities. Its job in this programme is narrower
than it looks: PubMed is where a **published synthesis** is discovered (Phase 4's Level-2
input) and where a registry record is linked to its publication. It is not a source of
arm-level trial results — those come from ClinicalTrials.gov, which posts them
structured.

The licence boundary is the thing to get right, and it is not uniform across "PubMed":

    PUBMED      E-utilities metadata + abstract        public domain
    PMC_OA      the Open Access subset only            open access, expires
    PMC         everything else in PMC                 RESTRICTED

Membership of PMC does **not** imply an open-access licence, and that is an easy and
expensive mistake — it is the difference between lawfully retaining a full text and
retaining only a fragment. ``licence_for_pmc_record`` makes the check explicit rather
than letting a caller infer it from the source name.

E-utilities asks for <=3 requests/second without an API key.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date

from app.evidence import licensing
from app.evidence.sources.base import FetchResult, get_json

logger = logging.getLogger(__name__)

SOURCE_TYPE = "PUBMED"
PMC_OA_SOURCE_TYPE = "PMC_OA"
PMC_SOURCE_TYPE = "PMC"
BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

_MIN_INTERVAL_SECONDS = 1.0 / 3.0
_rate_lock = asyncio.Lock()
_last_request_at = 0.0

# Publication types that indicate a synthesis worth routing to the Phase 4 adapter.
_SYNTHESIS_HINTS = (
    "network meta-analysis",
    "meta-analysis",
    "systematic review",
    "indirect treatment comparison",
    "indirect comparison",
    "mixed treatment comparison",
)
# Narrower: an actual NMA rather than a pairwise meta-analysis. The distinction matters
# because only the former can supply a Level-2 answer for an indirect comparison.
_NMA_HINTS = (
    "network meta-analysis",
    "indirect treatment comparison",
    "mixed treatment comparison",
)

_OA_LICENCES = ("cc0", "cc-by", "cc by", "public domain")
# Creative Commons components that withdraw the rights this platform needs. Checked as
# whole tokens, because every one of these strings also CONTAINS a permissive marker:
# "CC BY-NC-ND 4.0" starts with "cc by". Substring order would classify it open access.
_RESTRICTIVE_CC_COMPONENTS = frozenset({"nc", "nd"})

_MONTHS = {
    m: i for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}


@dataclass
class Citation:
    """One publication record, with the licence class that governs its retention."""

    pmid: str
    title: str
    journal: str | None = None
    publication_date: date | None = None
    authors: list[str] = field(default_factory=list)
    abstract: str | None = None
    doi: str | None = None
    pmcid: str | None = None
    publication_types: list[str] = field(default_factory=list)
    license_class: str = licensing.PUBLIC_DOMAIN

    @property
    def is_synthesis(self) -> bool:
        """True for any evidence synthesis — meta-analysis or systematic review."""
        return _mentions(self._searchable, _SYNTHESIS_HINTS)

    @property
    def is_network_meta_analysis(self) -> bool:
        """True only for an NMA/ITC. A pairwise meta-analysis cannot answer Level 2."""
        return _mentions(self._searchable, _NMA_HINTS)

    @property
    def _searchable(self) -> str:
        return " ".join([self.title or "", " ".join(self.publication_types)]).lower()

    def formatted(self) -> str:
        """A human-readable citation — required on every retained fragment."""
        lead = self.authors[0] if self.authors else "Anon"
        suffix = " et al." if len(self.authors) > 1 else ""
        year = self.publication_date.year if self.publication_date else "n.d."
        journal = f" {self.journal}." if self.journal else ""
        return f"{lead}{suffix} ({year}). {self.title}.{journal} PMID:{self.pmid}"


def _mentions(haystack: str, needles: tuple[str, ...]) -> bool:
    return any(n in haystack for n in needles)


def licence_for_pmc_record(license_text: str | None) -> str:
    """Licence class for a PMC article, from its stated licence.

    Defaults to RESTRICTED. Membership of PMC is not an open-access grant, and treating
    it as one is the difference between lawfully retaining a full text and retaining a
    document the platform has no right to hold.
    """
    lowered = (license_text or "").strip().lower()
    if not lowered:
        return licensing.RESTRICTED

    # Restrictions are evaluated FIRST and as whole tokens. Order is load-bearing:
    # "CC BY-NC-ND 4.0" contains "cc by", so a permissive-first substring check would
    # class a no-derivatives licence as open access and authorise retaining the full
    # text. The failure direction matters more than the recall here.
    tokens = {t for t in re.split(r"[^a-z0-9]+", lowered) if t}
    if tokens & _RESTRICTIVE_CC_COMPONENTS or "noncommercial" in lowered:
        # Non-commercial and no-derivatives grants permit reading, not necessarily
        # storage and reprocessing. A curator may override per article.
        return licensing.RESTRICTED

    if any(marker in lowered for marker in _OA_LICENCES):
        return licensing.OPEN_ACCESS
    return licensing.RESTRICTED


async def _throttle() -> None:
    global _last_request_at
    async with _rate_lock:
        elapsed = time.monotonic() - _last_request_at
        if elapsed < _MIN_INTERVAL_SECONDS:
            await asyncio.sleep(_MIN_INTERVAL_SECONDS - elapsed)
        _last_request_at = time.monotonic()


async def search(query: str, *, retmax: int = 50) -> FetchResult:
    """ESearch for PMIDs. Never raises."""
    await _throttle()
    return await get_json(
        f"{BASE_URL}/esearch.fcgi",
        params={"db": "pubmed", "term": query, "retmode": "json", "retmax": retmax},
        source_type=SOURCE_TYPE,
        source_identifier=f"search:{query}",
    )


async def summaries(pmids: list[str]) -> FetchResult:
    """ESummary for a batch of PMIDs. Never raises."""
    await _throttle()
    joined = ",".join(pmids)
    return await get_json(
        f"{BASE_URL}/esummary.fcgi",
        params={"db": "pubmed", "id": joined, "retmode": "json"},
        source_type=SOURCE_TYPE,
        source_identifier=f"summary:{joined[:64]}",
    )


def parse_search(result: FetchResult) -> list[str]:
    """PMIDs from an ESearch response. Pure; ``[]`` on any failure."""
    if not result.ok or not isinstance(result.payload, dict):
        return []
    return list((result.payload.get("esearchresult") or {}).get("idlist") or [])


def parse_summaries(result: FetchResult) -> list[Citation]:
    """Citations from an ESummary response. Pure; ``[]`` on any failure."""
    if not result.ok or not isinstance(result.payload, dict):
        return []
    body = result.payload.get("result") or {}
    return [
        citation
        for pmid in (body.get("uids") or [])
        if (citation := _parse_one(pmid, body.get(pmid) or {})) is not None
    ]


def _parse_one(pmid: str, record: dict) -> Citation | None:
    title = (record.get("title") or "").strip().rstrip(".")
    if not title:
        return None

    ids = {a.get("idtype"): a.get("value") for a in record.get("articleids") or []}
    return Citation(
        pmid=str(pmid),
        title=title,
        journal=(record.get("fulljournalname") or record.get("source") or "").strip() or None,
        publication_date=_parse_pubdate(record.get("pubdate") or record.get("sortpubdate")),
        authors=[a.get("name") for a in record.get("authors") or [] if a.get("name")],
        doi=ids.get("doi"),
        pmcid=ids.get("pmc") or ids.get("pmcid"),
        publication_types=[t for t in record.get("pubtype") or [] if t],
        # An ESummary record is metadata, which is public domain. The FULL TEXT of the
        # same article is a separate question answered by licence_for_pmc_record.
        license_class=licensing.PUBLIC_DOMAIN,
    )


def _parse_pubdate(raw: str | None) -> date | None:
    """PubMed dates are "2024 Nov 12", "2024 Nov", "2024" or an ISO-ish sort date."""
    if not raw:
        return None
    text = str(raw).strip()
    iso = re.match(r"^(\d{4})[-/](\d{2})[-/](\d{2})", text)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None

    parts = text.replace(",", " ").split()
    if not parts or not parts[0].isdigit():
        return None
    year = int(parts[0])
    month = _MONTHS.get(parts[1][:3].lower()) if len(parts) > 1 else None
    day = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    try:
        return date(year, month or 1, day or 1)
    except ValueError:
        return date(year, month or 1, 1)


def synthesis_candidates(citations: list[Citation]) -> list[Citation]:
    """Publications worth routing to the Phase 4 published-synthesis adapter.

    Only true NMAs/ITCs. A pairwise meta-analysis cannot resolve an indirect comparison,
    so admitting one here would put an unusable record into the Level-2 queue and cost a
    curator time to reject.
    """
    return [c for c in citations if c.is_network_meta_analysis]
