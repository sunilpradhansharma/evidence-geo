"""GEO Schema Data loader — serves verified ground-truth data for Chairman fallback.

Reads the generated llms.txt + JSON-LD schema/*.json from the config/geo directory.
Brands are AUTO-DISCOVERED from the schema directory (drop a validated file in and it is
available — no code edit) and every record is VALIDATED against
``app.geo.schema_model.DrugSchema`` on load, so a malformed "verified" record is logged +
skipped rather than silently served as ground truth.

Regenerate the corpus from the curated YAML source (optionally seeded from openFDA) with:
    python -m scripts.generate_geo_schema        (cwd = backend/)
"""
import json
import logging
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError

from app.config import taxonomy
from app.geo.schema_model import DrugSchema

logger = logging.getLogger(__name__)

GEO_DIR = Path(__file__).parent.parent / "config" / "geo"
SCHEMA_DIR = GEO_DIR / "schema"


@lru_cache
def get_llms_txt() -> str:
    """Load and return the llms.txt content."""
    path = GEO_DIR / "llms.txt"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


@lru_cache
def _index() -> dict[str, tuple[dict, DrugSchema]]:
    """Discover + validate every brand schema file, indexed by lookup alias.

    Returns a map of ``alias(lower) -> (raw_jsonld_dict, validated DrugSchema)``.
    Unparseable or invalid files are logged and skipped.
    """
    index: dict[str, tuple[dict, DrugSchema]] = {}
    if not SCHEMA_DIR.is_dir():
        logger.warning("GEO schema directory not found: %s", SCHEMA_DIR)
        return index
    for path in sorted(SCHEMA_DIR.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            schema = DrugSchema.model_validate(raw)
        except (json.JSONDecodeError, ValidationError, OSError) as e:
            logger.warning("GEO schema %s invalid — skipping: %s", path.name, e)
            continue
        for alias in schema.lookup_aliases():
            index[alias] = (raw, schema)
    return index


def get_brand_schema(brand: str) -> dict | None:
    """Get the JSON-LD schema for a brand (case-insensitive lookup)."""
    entry = _index().get(brand.lower())
    return entry[0] if entry else None


def _scope_indications(
    schema: DrugSchema, therapeutic_area: str | None, disease: str | None
) -> list[str]:
    """Which of the brand's indications this question is actually about.

    Disease first, therapeutic area second — the same precedence the scorer uses. An
    empty result means "cannot narrow", and the caller must then serve the union:
    showing nothing would read as "this drug has no competitors".
    """
    declared = list(schema.competitor_context.competitors_by_indication)
    if not declared:
        return []

    canonical = taxonomy.canonical_disease(disease) or (disease or "").strip()
    if canonical:
        for name in declared:
            if (taxonomy.canonical_disease(name) or name) == canonical:
                return [name]

    key = (therapeutic_area or "").strip()
    if key:
        wanted = set(taxonomy.diseases_for_key(key))
        hits = [n for n in declared if (taxonomy.canonical_disease(n) or n) in wanted]
        if hits:
            return hits
    return []


def get_geo_context(
    brand: str, therapeutic_area: str, disease: str | None = None
) -> dict | None:
    """Get combined GEO context for a brand, narrowed to the question's indication.

    Returns a dict with the distilled schema view, or None if no verified data is
    available for the brand. *disease* narrows the competitive field to that
    indication; without it the area is used, and failing that the union is served.
    Rinvoq's atopic dermatitis comparators are not its rheumatoid arthritis ones, so
    an un-narrowed fallback actively misinforms the model it is meant to correct.
    """
    entry = _index().get(brand.lower())
    if entry is None:
        return None
    _raw, schema = entry
    scope = _scope_indications(schema, therapeutic_area, disease)
    return {
        "brand": brand,
        "therapeutic_area": therapeutic_area,
        "disease": disease,
        "schema": schema.context_view(indications=scope),
        "source": schema.data_source or "GEO verified schema data",
    }


def list_available_brands() -> list[str]:
    """Return list of brands with available (valid) schema data."""
    names = {schema.name for _raw, schema in _index().values()}
    return sorted(names)


def reload() -> None:
    """Clear cached llms.txt + schema index (call after regenerating the corpus)."""
    get_llms_txt.cache_clear()
    _index.cache_clear()
