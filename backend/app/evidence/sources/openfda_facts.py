"""openFDA label -> ``DrugFact`` (Phase 3B).

The fetcher already exists and is proven: ``app.geo.sources.openfda.fetch_label`` is the
never-raises contract every other adapter in this codebase is modelled on. This module
adds only the mapping onto the canonical schema, so there is one HTTP boundary rather
than two implementations of the same call drifting apart.

Two judgements are encoded here, both about **not overwriting curated knowledge**:

* **The curated table wins on class and route.** ``brands.yaml`` is hand-authored and
  reviewed; openFDA's ``pharm_class_epc`` is a regulatory classification that often
  disagrees with how a drug is discussed clinically ("Janus Kinase Inhibitor" vs "JAK
  inhibitor"). The label value is still recorded when it conflicts, as a review signal.
* **Nothing here reaches VERIFIED.** A parsed label is ``EXTRACTED``. Adverse events and
  contraindications are prose that needs an LLM pass and a human before they are facts,
  and a label parsed into structured fields is not the same as a label understood.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime

from app.config import taxonomy
from app.evidence.lifecycles import EXTRACTED
from app.geo.sources.openfda import LabelSeed, fetch_label
from app.models.drug_fact import DrugFact

SOURCE_TYPE = "OPENFDA"

# Flags surfaced to curation. A disagreement is information, not an error.
FLAG_CLASS_CONFLICT = "DRUG_CLASS_CONFLICTS_WITH_CURATED"
FLAG_ROUTE_CONFLICT = "ROUTE_CONFLICTS_WITH_CURATED"
FLAG_NO_CURATED_ENTRY = "DRUG_NOT_IN_CURATED_CATALOG"
FLAG_INDICATIONS_UNPARSED = "INDICATIONS_TEXT_NOT_STRUCTURED"

# openFDA route strings -> the closed vocabulary in taxonomy.ADMINISTRATION_ROUTES.
_ROUTE_MAP = {
    "oral": "ORAL",
    "subcutaneous": "SC",
    "intravenous": "IV",
    "intramuscular": "IM",
    "topical": "TOPICAL",
}

# Boxed warnings are separated on the bullet/sentence boundaries openFDA actually uses.
_WARNING_SPLIT = re.compile(r"(?:\r?\n|\u2022|\s{2,}\u2013\s|(?<=[a-z])\.\s+(?=[A-Z]{2,}))")


def _normalise_route(raw: str | None) -> str | None:
    """Map a label route onto the closed vocabulary, or ``None`` when unrecognised.

    Returning ``None`` rather than passing the raw string through keeps the closed set
    closed — an unmapped route would otherwise reach the Phase 6 route-mixing check as an
    unrecognised value and be treated as "different from everything".
    """
    lowered = (raw or "").strip().lower()
    for needle, canonical in _ROUTE_MAP.items():
        if needle in lowered:
            return canonical
    return None


def _split_warnings(text: str | None, *, limit: int = 12) -> list[str]:
    """Break boxed-warning prose into discrete statements, conservatively."""
    if not text:
        return []
    parts = [p.strip(" .;\u2022") for p in _WARNING_SPLIT.split(text)]
    return [p for p in parts if len(p) > 12][:limit]


def parse_label(
    seed: LabelSeed,
    *,
    brand: str,
    fact_id: str,
    generic: str | None = None,
) -> DrugFact:
    """Map an openFDA ``LabelSeed`` onto an unsaved ``DrugFact``. Pure; no I/O.

    ``fact_id`` is supplied by the caller so ingestion can make it deterministic (and
    therefore idempotent) or versioned by label date, as it prefers.
    """
    flags: list[str] = []

    curated_class = taxonomy.drug_class_for(brand) or taxonomy.drug_class_for(generic)
    curated_route = taxonomy.administration_route_for(brand) or taxonomy.administration_route_for(generic)
    label_route = _normalise_route(seed.administration_route)

    if curated_class is None and curated_route is None:
        flags.append(FLAG_NO_CURATED_ENTRY)
    if curated_class and seed.drug_class and not _classes_agree(curated_class, seed.drug_class):
        flags.append(FLAG_CLASS_CONFLICT)
    if curated_route and label_route and curated_route != label_route:
        flags.append(FLAG_ROUTE_CONFLICT)
    if seed.indications_text:
        # Structuring indication prose is an LLM job under the Phase 3A pipeline, not a
        # regex job here. Recording that it is unstructured is more honest than a
        # half-parsed list a curator would have to un-trust.
        flags.append(FLAG_INDICATIONS_UNPARSED)

    boxed = _split_warnings(seed.boxed_warning_text)

    return DrugFact(
        fact_id=fact_id,
        brand=brand,
        generic=generic or seed.generic_name,
        molecule=seed.active_ingredient,
        manufacturer=seed.manufacturer,
        # Curated values win; the label's own reading is preserved in the rationale.
        drug_class=curated_class or seed.drug_class,
        administration_route=curated_route or label_route,
        dosage_form=None,
        approved_indications=None,
        label_updated_at=_iso_date(seed.effective_time),
        boxed_warnings=json.dumps(boxed) if boxed else None,
        has_boxed_warning=bool(seed.has_boxed_warning),
        regulatory_source="FDA",
        prescribing_information=seed.prescribing_information,
        extraction_confidence=1.0,  # structured label fields, read not inferred
        extraction_rationale=_rationale(seed, curated_class, curated_route, label_route),
        mismatch_flags=json.dumps(flags) if flags else None,
        # Structured does not mean understood. A human verifies.
        verification_status=EXTRACTED,
        source_is_citable=True,
        claim_is_approved_for_external_use=False,
    )


def label_date(seed: LabelSeed) -> date | None:
    """The SPL effective date this seed carries, or ``None``.

    Public because a drug fact is **versioned by label date**, so ingestion has to key an
    id on it. Reading ``effective_time`` a second time in the service would be a second
    opinion about when a label changed, and the two would eventually disagree about
    whether a row needs superseding.
    """
    return _iso_date(seed.effective_time)


def _classes_agree(curated: str, label: str) -> bool:
    """Loose comparison — "JAK inhibitor" and "Janus Kinase Inhibitor" are the same thing.

    Deliberately generous: the point of the conflict flag is to catch a genuinely
    different classification, not to make a curator adjudicate wording.
    """
    def _key(value: str) -> set[str]:
        return {w for w in re.split(r"[^a-z0-9]+", value.lower()) if len(w) > 2}

    curated_words, label_words = _key(curated), _key(label)
    if not curated_words or not label_words:
        return True
    if curated_words & label_words:
        return True
    # "JAK" vs "Janus Kinase" — compare initialisms both ways.
    initials = "".join(w[0] for w in sorted(label_words))
    return any(len(w) <= 4 and set(w) <= set(initials) for w in curated_words)


def _rationale(
    seed: LabelSeed, curated_class: str | None, curated_route: str | None, label_route: str | None
) -> str:
    parts = [f"openFDA SPL set_id={seed.set_id or 'unknown'}"]
    if curated_class and seed.drug_class:
        parts.append(f"class: curated {curated_class!r} kept over label {seed.drug_class!r}")
    if curated_route and label_route:
        parts.append(f"route: curated {curated_route!r} kept over label {label_route!r}")
    return "; ".join(parts)


def _iso_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


async def ingest(brand: str, *, generic: str | None = None, fact_id: str | None = None) -> DrugFact | None:
    """Fetch and map one brand's label. Never raises; ``None`` when openFDA has nothing."""
    seed = await fetch_label(brand, generic)
    if seed is None:
        return None
    return parse_label(
        seed, brand=brand, generic=generic, fact_id=fact_id or f"openfda:{brand.lower()}"
    )
