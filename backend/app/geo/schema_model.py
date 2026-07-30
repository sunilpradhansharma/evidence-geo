"""Pydantic validation models for the GEO schema data layer.

Single source of truth for what a valid brand JSON-LD record must contain. Used BOTH
by the generator (validate output before writing) and the loader (validate on read, so
a malformed "verified" record is logged + skipped rather than silently served as
ground truth). ``extra="allow"`` preserves optional JSON-LD fields we don't model
explicitly (e.g. ``availableStrength``, ``dosingProtocol``, ``drugInteractions``).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Indication(_Base):
    name: str
    description: str | None = None


class AdverseOutcome(_Base):
    name: str
    severity: str = "Common"
    incidence: str | None = None


class Manufacturer(_Base):
    name: str


class CompetitorContext(_Base):
    """Who this drug competes with, per indication.

    A brand is not single-area and its competitors are not one flat set: Humira is
    indicated in dermatology, gastroenterology and rheumatology, and the agents it is
    compared against differ in each. ``competitorsByIndication`` is the real answer;
    ``keyCompetitors`` is retained as the union so historical documents and readers
    that never narrow keep working.
    """

    # Legacy single-value label. Still READ so documents written before the
    # Dermatology/Gastroenterology split (and rows persisted in consensus.geo_context)
    # continue to validate; new records are written with `therapeuticAreas`.
    therapeutic_area: str | None = Field(default=None, alias="therapeuticArea")
    therapeutic_areas: list[str] = Field(default_factory=list, alias="therapeuticAreas")
    biosimilars_available: bool = Field(default=False, alias="biosimilarsAvailable")
    key_competitors: list[str] = Field(default_factory=list, alias="keyCompetitors")
    competitors_by_indication: dict[str, list[str]] = Field(
        default_factory=dict, alias="competitorsByIndication"
    )
    differentiators: str | None = None

    def areas(self) -> list[str]:
        """Declared areas, tolerating the legacy single / slash-joined string form."""
        if self.therapeutic_areas:
            return [a for a in self.therapeutic_areas if a]
        if self.therapeutic_area:
            return [p.strip() for p in self.therapeutic_area.split("/") if p.strip()]
        return []

    def field_for(self, indications: list[str]) -> list[str]:
        """Union of the competitive fields for *indications*, in declaration order.

        Empty when none of them are known, which the caller must read as "cannot
        narrow" and fall back to the union — never as "this drug has no competitors".
        """
        out: list[str] = []
        for indication in indications:
            for name in self.competitors_by_indication.get(indication, []):
                if name not in out:
                    out.append(name)
        return out


class DrugSchema(_Base):
    """A schema.org/Drug JSON-LD record — the on-disk + API-served shape."""

    context: str = Field(default="https://schema.org", alias="@context")
    type_: str = Field(default="Drug", alias="@type")
    name: str
    non_proprietary_name: str | None = Field(default=None, alias="nonProprietaryName")
    manufacturer: Manufacturer | None = None
    drug_class: str | None = Field(default=None, alias="drugClass")
    administration_route: str | None = Field(default=None, alias="administrationRoute")
    active_ingredient: str | None = Field(default=None, alias="activeIngredient")
    alternate_name: list[str] = Field(default_factory=list, alias="alternateName")
    indication: list[Indication] = Field(default_factory=list)
    adverse_outcome: list[AdverseOutcome] = Field(default_factory=list, alias="adverseOutcome")
    clinical_efficacy: dict[str, Any] = Field(default_factory=dict, alias="clinicalEfficacy")
    competitor_context: CompetitorContext = Field(
        default_factory=CompetitorContext, alias="competitorContext"
    )
    prescribing_information: str | None = Field(default=None, alias="prescribingInformation")
    data_source: str | None = Field(default=None, alias="dataSource")
    last_updated: str | None = Field(default=None, alias="lastUpdated")

    def lookup_aliases(self) -> list[str]:
        """Case-insensitive lookup keys a brand can be resolved by."""
        candidates = [
            self.name,
            self.non_proprietary_name,
            self.active_ingredient,
            *self.alternate_name,
        ]
        seen: set[str] = set()
        out: list[str] = []
        for value in candidates:
            if not value:
                continue
            key = value.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(key)
        return out

    def context_view(self, *, indications: list[str] | None = None) -> dict[str, Any]:
        """The distilled dict the Chairman fallback consumes (stable contract).

        When *indications* is given, ``keyCompetitors`` is narrowed to the field for
        those indications and ``competitorScope`` names them, so the model is not shown
        a psoriasis comparator set for a Crohn's question. The key set is otherwise
        unchanged; the per-indication map itself is dropped because the caller has
        already resolved it and the prompt should not carry the whole matrix.
        """
        competitors = self.competitor_context.model_dump(by_alias=True, exclude_none=True)
        competitors.pop("competitorsByIndication", None)
        if indications:
            narrowed = self.competitor_context.field_for(indications)
            if narrowed:
                competitors["keyCompetitors"] = narrowed
                competitors["competitorScope"] = list(indications)
        return {
            "name": self.name,
            "genericName": self.non_proprietary_name,
            "drugClass": self.drug_class,
            "indications": [i.name for i in self.indication],
            "efficacy": self.clinical_efficacy,
            "safety": [
                {"name": s.name, "severity": s.severity} for s in self.adverse_outcome
            ],
            "competitors": competitors,
        }
