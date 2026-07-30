"""Build the GEO corpus (JSON-LD schema/*.json + llms.txt) from curated YAML source.

Curated values in ``config/geo/source/*.yaml`` are the source of truth; label-derived
fields are SEEDED from openFDA only where curated values are absent (curated always wins).
Every generated record is validated against ``DrugSchema`` before it is written, and
``llms.txt`` is rendered deterministically from the schema docs so it can never drift.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.config import taxonomy
from app.geo.schema_model import DrugSchema
from app.geo.sources import openfda
from app.geo.sources.openfda import LabelSeed

logger = logging.getLogger(__name__)

GEO_DIR = Path(__file__).parent.parent / "config" / "geo"
SOURCE_DIR = GEO_DIR / "source"
SCHEMA_DIR = GEO_DIR / "schema"
LLMS_TXT_PATH = GEO_DIR / "llms.txt"

_PLACEHOLDER_NOTE = (
    "Curated YAML overrides openFDA-seeded fields. Replace placeholder clinical values "
    "with Medical-Affairs-approved data."
)


@dataclass
class BrandReport:
    brand: str
    file: str
    valid: bool
    seeded_fields: list[str] = field(default_factory=list)  # fields actually applied from the label
    label_source: str | None = None
    label_matched: bool = False
    clinical_values_verified: bool = False
    error: str | None = None


@dataclass
class GenerateReport:
    brands: list[BrandReport] = field(default_factory=list)
    llms_txt_written: bool = False
    wrote: bool = False

    @property
    def ok(self) -> bool:
        return all(b.valid for b in self.brands)

    @property
    def unverified(self) -> list[str]:
        """Brands whose curated clinical values are still placeholders (not MA-verified)."""
        return [b.brand for b in self.brands if not b.clinical_values_verified]


def _slug(brand: str) -> str:
    return brand.strip().lower().replace(" ", "-")


def _default_data_source(*, seeded: bool, verified: bool) -> str:
    if verified:
        base = "Curated, Medical-Affairs-verified clinical values"
    else:
        base = "Curated (POC placeholder; clinical values pending Medical-Affairs verification)"
    return base + "; label fields seeded from openFDA (open.fda.gov)" if seeded else base


def _truncate(text: str, limit: int = 4000) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def _curated_fields(src: dict) -> list[str]:
    skip = {"openfda", "data_source", "aliases", "clinical_values_verified"}
    return sorted(
        k for k, v in src.items() if k not in skip and v not in (None, "", [], {})
    )


def _known_ta_keys() -> set[str]:
    """Every stored ``therapeutic_area`` key declared in brands.yaml."""
    return {key for group in taxonomy.grouped() for key in group["indications"]}


def declared_areas(src: dict) -> list[str]:
    """The therapeutic areas a brand is indicated under.

    Reads the ``therapeutic_areas:`` list. Falls back to the legacy singular field,
    including the slash-joined form that briefly stood in for a list, so an
    un-migrated source file still generates.
    """
    raw = src.get("therapeutic_areas")
    if raw is None:
        legacy = src.get("therapeutic_area")
        raw = str(legacy).split("/") if legacy else []
    return [a for a in (str(x).strip() for x in raw) if a]


def competitors_by_indication(src: dict) -> dict[str, list[str]]:
    """Indication -> competitive field, read from the brands.yaml disease overlay.

    Derived rather than curated so there is ONE opinion about who competes with whom:
    this is the same accessor the scorer uses, so the GEO fallback and the score can
    never disagree. Indications outside the overlay (the oncology and neuroscience
    sets) fall back to their area block, and only when the brand is single-area —
    with three areas in play there is no non-arbitrary choice, so nothing is emitted.
    """
    brand = (src.get("brand") or "").strip().lower()
    areas = declared_areas(src)
    out: dict[str, list[str]] = {}
    for entry in src.get("indications") or []:
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        disease = taxonomy.canonical_disease(name)
        names = list(taxonomy.competitors_for_disease(disease)) if disease else []
        if not names and len(areas) == 1:
            names = list(taxonomy.competitors_for_key(areas[0]))
        names = [n for n in names if n.strip().lower() != brand]
        if names:
            out[name] = names
    return out


def validate_source(src: dict) -> list[str]:
    """Problems with a curated source record; empty means valid.

    An area that is not a brands.yaml key is fatal rather than cosmetic: it is the
    label the corpus is grouped and served by, and an orphan value would be invisible
    to every taxonomy-aware surface.
    """
    errors: list[str] = []
    known = _known_ta_keys()
    for area in declared_areas(src):
        if area not in known:
            errors.append(
                f"therapeutic_areas entry {area!r} is not a key of `therapeutic_areas:` "
                f"in brands.yaml (known: {', '.join(sorted(known))})"
            )
    return errors


def build_jsonld(src: dict, seed: LabelSeed | None, *, generated_at: str) -> dict:
    """Map one curated YAML record (+ optional openFDA seed) into a JSON-LD Drug doc."""
    applied: list[str] = []

    def _pick(field_name: str, curated: Any, seeded: Any) -> Any:
        """Curated value wins; fall back to the label seed and record that it was applied."""
        if curated:
            return curated
        if seeded:
            applied.append(field_name)
            return seeded
        return None

    brand = src["brand"]
    generic = src.get("generic")
    active = _pick("active_ingredient", src.get("active_ingredient"), (seed.active_ingredient.lower() if seed and seed.active_ingredient else None))
    manufacturer = _pick("manufacturer", src.get("manufacturer"), seed.manufacturer if seed else None)
    drug_class = _pick("drug_class", src.get("drug_class"), seed.drug_class if seed else None)
    route = _pick("administration_route", src.get("administration_route"), seed.administration_route if seed else None)
    pi = _pick("prescribing_information", src.get("prescribing_information"), seed.prescribing_information if seed else None)

    doc: dict[str, Any] = {"@context": "https://schema.org", "@type": "Drug", "name": brand}
    if generic:
        doc["nonProprietaryName"] = generic
    if manufacturer:
        doc["manufacturer"] = {"@type": "Organization", "name": manufacturer}
    if drug_class:
        doc["drugClass"] = drug_class
    if route:
        doc["administrationRoute"] = route
    if src.get("dosage_form"):
        doc["dosageForm"] = src["dosage_form"]
    if active:
        doc["activeIngredient"] = active
    if src.get("aliases"):
        doc["alternateName"] = list(src["aliases"])

    strengths = src.get("strengths") or []
    if strengths:
        doc["availableStrength"] = [
            {
                "@type": "DrugStrength",
                "activeIngredient": active or generic or brand,
                "strengthValue": str(s["value"]),
                "strengthUnit": s["unit"],
            }
            for s in strengths
        ]

    indications = src.get("indications") or []
    if indications:
        doc["indication"] = [
            {"@type": "MedicalIndication", "name": i["name"], **({"description": i["description"]} if i.get("description") else {})}
            for i in indications
        ]

    aos = list(src.get("adverse_outcomes") or [])
    if not aos and seed and seed.has_boxed_warning:
        aos = [{"name": "Boxed Warning (see label)", "severity": "Boxed Warning"}]
        applied.append("adverse_outcomes(boxed_warning)")
    if aos:
        doc["adverseOutcome"] = [
            {"@type": "MedicalCondition", "name": a["name"], "severity": a.get("severity", "Common"), **({"incidence": a["incidence"]} if a.get("incidence") else {})}
            for a in aos
        ]

    if src.get("efficacy"):
        doc["clinicalEfficacy"] = src["efficacy"]

    for key, value in (src.get("extra") or {}).items():
        doc[key] = value

    areas = declared_areas(src)
    comp = src.get("competitors") or {}
    by_indication = competitors_by_indication(src)
    if comp or by_indication:
        cc: dict[str, Any] = {}
        if areas:
            cc["therapeuticAreas"] = areas
        cc["biosimilarsAvailable"] = bool(comp.get("biosimilars_available", False))
        # A curated list wins only where it exists (it carries generic names the
        # taxonomy does not); otherwise the union of the derived per-indication
        # fields stands in, so the flat key is never hand-maintained twice.
        curated = [c for c in (comp.get("key_competitors") or []) if c]
        union: list[str] = []
        for names in by_indication.values():
            for name in names:
                if name not in union:
                    union.append(name)
        cc["keyCompetitors"] = curated or union
        if by_indication:
            cc["competitorsByIndication"] = by_indication
        if comp.get("differentiators"):
            cc["differentiators"] = comp["differentiators"]
        doc["competitorContext"] = cc

    if pi:
        doc["prescribingInformation"] = pi

    reference: dict[str, str] = {}
    if seed:
        reference = {
            k: _truncate(v)
            for k, v in {
                "boxedWarning": seed.boxed_warning_text,
                "indicationsAndUsage": seed.indications_text,
                "adverseReactions": seed.adverse_reactions_text,
                "dosageAndAdministration": seed.dosage_text,
            }.items()
            if v
        }
        if reference:
            doc["labelReference"] = reference

    matched = bool(seed and seed.set_id)
    verified = bool(src.get("clinical_values_verified", False))
    doc["dataSource"] = src.get("data_source") or _default_data_source(seeded=matched, verified=verified)
    doc["lastUpdated"] = generated_at
    doc["provenance"] = {
        "generator": "scripts.generate_geo_schema",
        "generatedAt": generated_at,
        "therapeuticAreas": areas,
        "clinicalValuesVerified": verified,
        "curatedFields": _curated_fields(src),
        "appliedFromLabel": applied,
        "labelProvided": (seed.seeded_fields if seed else []),
        "labelReferenceIncluded": sorted(reference.keys()),
        "labelSource": (f"openFDA/DailyMed SPL {seed.set_id}" if matched else None),
        "labelEffectiveTime": seed.effective_time if seed else None,
        "note": _PLACEHOLDER_NOTE,
    }
    return doc


def _efficacy_line(clinical_efficacy: dict) -> str | None:
    parts = []
    for entry in list(clinical_efficacy.values())[:2]:
        if isinstance(entry, dict):
            measure, value, tp = entry.get("measure"), entry.get("value"), entry.get("timepoint")
            if measure and value:
                parts.append(f"{measure} {value}" + (f" ({tp})" if tp else ""))
    return "; ".join(parts) or None


def render_llms_txt(docs: list[dict]) -> str:
    """Deterministically render llms.txt from the JSON-LD schema docs (grouped by TA)."""
    last_updated = max((d.get("lastUpdated") or "" for d in docs), default="")
    lines = [
        "# AbbVie Therapy Portfolio — Verified Clinical & Commercial Data",
        "# Machine-readable content summary for AI systems (GEO governance layer).",
        f"# Last updated: {last_updated}" if last_updated else "# Last updated: (unknown)",
        "",
        "> This file provides structured references to verified pharmaceutical data",
        "> for use by AI orchestration systems. It serves as the ground-truth fallback",
        "> when generative models diverge on clinical facts.",
        "",
    ]

    def _areas_of(doc: dict) -> list[str]:
        """Every area a doc belongs under, tolerating the legacy single-value form."""
        cc = doc.get("competitorContext") or {}
        prov = doc.get("provenance") or {}
        areas = cc.get("therapeuticAreas") or prov.get("therapeuticAreas") or []
        if areas:
            return [str(a).strip() for a in areas if str(a).strip()]
        legacy = cc.get("therapeuticArea") or prov.get("therapeuticArea")
        if legacy:
            return [p.strip() for p in str(legacy).split("/") if p.strip()]
        return ["Other"]

    # A multi-area brand is listed under EACH of its areas. This file is read by models
    # looking for "what treats X" — omitting Humira from the dermatology section because
    # it also treats Crohn's is precisely the failure the corpus exists to prevent.
    grouped: dict[str, list[dict]] = {}
    for doc in docs:
        for area in _areas_of(doc):
            grouped.setdefault(area, []).append(doc)

    for ta in sorted(grouped):
        lines.append(f"## {ta}")
        lines.append("")
        for doc in sorted(grouped[ta], key=lambda d: d.get("name", "")):
            name = doc.get("name", "")
            generic = doc.get("nonProprietaryName")
            manufacturer = (doc.get("manufacturer") or {}).get("name", "")
            heading = f"### {name}" + (f" ({generic})" if generic else "")
            if manufacturer:
                heading += f" — {manufacturer}"
            lines.append(heading)
            if doc.get("drugClass"):
                lines.append(f"- **Type**: {doc['drugClass']}")
            if doc.get("administrationRoute"):
                lines.append(f"- **Route**: {doc['administrationRoute']}")
            indications = [i.get("name") for i in doc.get("indication", []) if i.get("name")]
            if indications:
                lines.append(f"- **Approved Indications**: {', '.join(indications)}")
            eff = _efficacy_line(doc.get("clinicalEfficacy", {}))
            if eff:
                lines.append(f"- **Key Efficacy**: {eff}")
            safety = [a.get("name") for a in doc.get("adverseOutcome", []) if a.get("name")]
            if safety:
                lines.append(f"- **Safety Profile**: {', '.join(safety)}")
            cc = doc.get("competitorContext") or {}
            if "biosimilarsAvailable" in cc:
                lines.append(f"- **Biosimilars Available**: {'Yes' if cc['biosimilarsAvailable'] else 'No'}")
            lines.append(f"- **Schema**: /geo/schema/{_slug(name)}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def load_sources(only_brand: str | None = None) -> list[dict]:
    """Load curated YAML source records (optionally a single brand, case-insensitive)."""
    sources: list[dict] = []
    if not SOURCE_DIR.is_dir():
        return sources
    for path in sorted(SOURCE_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not data.get("brand"):
            logger.warning("GEO source %s has no 'brand' — skipping", path.name)
            continue
        if only_brand and data["brand"].lower() != only_brand.lower():
            continue
        sources.append(data)
    return sources


def _write_schema(brand: str, doc: dict) -> Path:
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    path = SCHEMA_DIR / f"{_slug(brand)}.json"
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


async def generate(*, seed: bool = True, only_brand: str | None = None, write: bool = True, generated_at: str | None = None) -> tuple[GenerateReport, list[dict]]:
    """Generate the corpus. Returns (report, docs). Only rewrites llms.txt on a full run."""
    generated_at = generated_at or date.today().isoformat()
    report = GenerateReport(wrote=write)
    docs: list[dict] = []

    for src in load_sources(only_brand):
        brand = src["brand"]
        source_errors = validate_source(src)
        label_seed: LabelSeed | None = None
        if seed:
            of = src.get("openfda") or {}
            label_seed = await openfda.fetch_label(of.get("brand_name") or brand, of.get("generic_name") or src.get("generic"))
        doc = build_jsonld(src, label_seed, generated_at=generated_at)
        try:
            DrugSchema.model_validate(doc)
            valid, err = True, None
        except ValidationError as e:  # pragma: no cover - defensive
            valid, err = False, str(e)
        if source_errors:
            valid = False
            err = "; ".join([*source_errors, *([err] if err else [])])
        docs.append(doc)
        report.brands.append(
            BrandReport(
                brand=brand,
                file=f"{_slug(brand)}.json",
                valid=valid,
                seeded_fields=list(doc.get("provenance", {}).get("appliedFromLabel", [])),
                label_source=(f"SPL {label_seed.set_id}" if label_seed and label_seed.set_id else None),
                label_matched=bool(label_seed and label_seed.set_id),
                clinical_values_verified=bool(doc.get("provenance", {}).get("clinicalValuesVerified")),
                error=err,
            )
        )
        if write and valid:
            _write_schema(brand, doc)

    if write and report.ok and only_brand is None and docs:
        LLMS_TXT_PATH.write_text(render_llms_txt(docs), encoding="utf-8")
        report.llms_txt_written = True
    return report, docs
