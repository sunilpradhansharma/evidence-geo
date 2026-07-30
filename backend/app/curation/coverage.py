"""The comparison matrix the question bank is measured against. Pure — no DB, no network.

Built from the ``indications:`` disease overlay, using the SAME accessors the scorer
reads (``brands_for_disease`` / ``competitors_for_disease``). That is deliberate: if the
matrix and the scorer disagreed about who competes with whom, a question generated to
fill a gap would be graded against a different field than the one it was written for.

A cell is one monitorable head-to-head: a focus brand against one comparator, in one
indication, asked by one persona. Cells are ranked, not just listed, because the full
product is far larger than anyone will generate and the interesting end of it is narrow.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from app.config import taxonomy
from app.prompt_volume import mapping

# Comparative is the default and only ranked domain: a head-to-head cell asks how two
# named agents compare, which is a Comparative question by definition. The other four
# domains describe a single agent and are already well covered by the seed banks.
DEFAULT_DOMAIN = "Comparative"
DEFAULT_PERSONAS = ("Patient", "Provider")
ALL_PERSONAS = ("Prospect", "Provider", "Patient")

_WS = re.compile(r"\s+")
_NONWORD = re.compile(r"[^a-z0-9 ]+")


def normalize(text: str) -> str:
    """Dedupe key shared with the harvest pipeline's ``_dedupe_hash`` normalisation."""
    return _NONWORD.sub("", _WS.sub(" ", (text or "").lower()).strip())


@dataclass(frozen=True)
class Cell:
    """One monitorable comparison: brand vs comparator, in an indication, for a persona."""

    disease: str
    brand: str
    competitor: str
    persona: str
    domain: str = DEFAULT_DOMAIN

    @property
    def therapeutic_area(self) -> str | None:
        return taxonomy.therapeutic_area_key_for_disease(self.disease)

    @property
    def area(self) -> str | None:
        return taxonomy.area_for_disease(self.disease)

    @property
    def key(self) -> str:
        return f"{self.disease}|{self.brand}|{self.competitor}|{self.persona}|{self.domain}"

    def dedupe_hash(self) -> str:
        """Stable id for the CELL, so regenerating refreshes rather than duplicates."""
        return hashlib.sha1(f"curation:{self.key}".encode("utf-8")).hexdigest()

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "disease": self.disease,
            "brand": self.brand,
            "competitor": self.competitor,
            "persona": self.persona,
            "domain": self.domain,
            "therapeutic_area": self.therapeutic_area,
            "area": self.area,
        }


@dataclass
class CellCoverage:
    """A cell plus the questions that already cover it."""

    cell: Cell
    covering: list[str] = field(default_factory=list)

    @property
    def covered(self) -> bool:
        return bool(self.covering)


def _diseases_in_scope(
    diseases: list[str] | None, therapeutic_areas: list[str] | None
) -> list[str]:
    """Resolve a scope to declared disease keys. Empty scope means every disease."""
    if diseases:
        resolved = [taxonomy.canonical_disease(d) for d in diseases]
        return [d for d in resolved if d]
    if therapeutic_areas:
        out: list[str] = []
        for key in therapeutic_areas:
            # Accept either a stored TA key (Dermatology) or a broad area display name.
            keys = taxonomy.keys_for_area(key) or (key,)
            for ta_key in keys:
                for disease in taxonomy.diseases_for_key(ta_key):
                    if disease not in out:
                        out.append(disease)
        return out
    return list(taxonomy.diseases())


def build_matrix(
    *,
    brands: list[str] | None = None,
    therapeutic_areas: list[str] | None = None,
    diseases: list[str] | None = None,
    personas: list[str] | None = None,
    domain: str = DEFAULT_DOMAIN,
) -> list[Cell]:
    """Every head-to-head cell in scope, in taxonomy declaration order.

    Scope is intersective: naming both a brand and areas gives that brand's cells within
    those areas only. A brand that is not indicated in a disease contributes no cell
    there, so asking for Rinvoq across three areas cannot invent a psoriasis question it
    has no indication for.
    """
    wanted_brands = {b.strip().lower() for b in (brands or []) if b and b.strip()}
    persona_list = [p for p in (personas or DEFAULT_PERSONAS) if p in ALL_PERSONAS]
    cells: list[Cell] = []
    for disease in _diseases_in_scope(diseases, therapeutic_areas):
        competitors = taxonomy.competitors_for_disease(disease)
        if not competitors:
            continue
        for brand in taxonomy.brands_for_disease(disease):
            if wanted_brands and brand.strip().lower() not in wanted_brands:
                continue
            for competitor in competitors:
                for persona in persona_list:
                    cells.append(
                        Cell(
                            disease=disease,
                            brand=brand,
                            competitor=competitor,
                            persona=persona,
                            domain=domain,
                        )
                    )
    return cells


def covers(cell: Cell, question_text: str, *, persona: str | None = None) -> bool:
    """True when *question_text* already asks this comparison.

    Requires BOTH agents to be named (alias-aware) and the indication to match. The
    disease check is what stops "Rinvoq vs Tremfya for psoriasis" from being counted as
    coverage of the ulcerative colitis cell: same pair, different competitive field,
    different answer.
    """
    if persona is not None and cell.persona != persona:
        return False
    if not mapping.mentions(question_text, cell.brand):
        return False
    if not mapping.mentions(question_text, cell.competitor):
        return False
    named = mapping.resolve_disease(question_text)
    return named == cell.disease


def apply_coverage(
    cells: list[Cell], questions: list[dict], *, match_persona: bool = True
) -> list[CellCoverage]:
    """Mark each cell covered/uncovered against existing questions.

    *questions* are ``{question_text, persona}`` dicts — anything with a question text,
    so the approved bank and the pending review queue can both be counted.
    """
    out = [CellCoverage(cell=cell) for cell in cells]
    for entry in questions:
        text = entry.get("question_text") or ""
        if not text:
            continue
        persona = entry.get("persona") if match_persona else None
        for item in out:
            if covers(item.cell, text, persona=persona):
                item.covering.append(text)
    return out


def rank(items: list[CellCoverage]) -> list[CellCoverage]:
    """Uncovered cells, most worth writing first.

    The full product is thousands of cells and nobody will generate them all, so the
    order is the feature. A comparator carried at full evidence depth ranks first because
    a question about it can later be graded against an actual network rather than only
    sentiment-scored.
    """
    full_depth = {name.strip().lower() for name in taxonomy.full_depth_drugs()}

    def sort_key(item: CellCoverage) -> tuple:
        cell = item.cell
        competitor_depth = 0 if cell.competitor.strip().lower() in full_depth else 1
        brand_depth = 0 if cell.brand.strip().lower() in full_depth else 1
        # Declaration order preserves the curated tiering in brands.yaml.
        disease_order = list(taxonomy.diseases()).index(cell.disease) \
            if cell.disease in taxonomy.diseases() else 99
        competitors = list(taxonomy.competitors_for_disease(cell.disease))
        competitor_order = competitors.index(cell.competitor) \
            if cell.competitor in competitors else 99
        return (competitor_depth, brand_depth, disease_order, competitor_order, cell.persona)

    return sorted((i for i in items if not i.covered), key=sort_key)


def summarize(items: list[CellCoverage]) -> dict:
    """Counts by area, disease and brand — what the UI shows above the gap list."""
    covered = [i for i in items if i.covered]
    gaps = [i for i in items if not i.covered]

    def _tally(entries: list[CellCoverage], attr: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in entries:
            value = getattr(item.cell, attr) or "Unmapped"
            out[value] = out.get(value, 0) + 1
        return out

    return {
        "total_cells": len(items),
        "covered": len(covered),
        "gaps": len(gaps),
        "coverage_pct": round(100.0 * len(covered) / len(items), 1) if items else 0.0,
        "gaps_by_area": _tally(gaps, "area"),
        "gaps_by_disease": _tally(gaps, "disease"),
        "gaps_by_brand": _tally(gaps, "brand"),
    }
