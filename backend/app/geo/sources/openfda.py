"""openFDA drug-label fetcher — seeds the GEO corpus with REAL, public FDA data.

openFDA (https://open.fda.gov) exposes structured FDA drug labeling (SPL) with NO API key
required (an optional key only raises rate limits). We use it to SEED label-derived fields
of the GEO schema — manufacturer, route, pharmacologic class, whether a boxed warning
exists, the raw indications / adverse-reaction / dosing label text, and the SPL effective
date — plus a real DailyMed prescribing-information link.

Design contract (mirrors app.remediation.semrush.enrich): ``fetch_label`` NEVER raises. On a
missing key, timeout, 404 (openFDA returns 404 when nothing matches), non-2xx, or malformed
payload it returns ``None`` so the generator simply skips seeding and uses curated data only.
Curated YAML values ALWAYS override anything seeded here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from app.config.settings import get_settings

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.fda.gov"
_TIMEOUT = 20.0


@dataclass
class LabelSeed:
    """Label-derived fields pulled from openFDA (all best-effort / may be None)."""

    brand_name: str | None = None
    generic_name: str | None = None
    manufacturer: str | None = None
    administration_route: str | None = None
    drug_class: str | None = None
    active_ingredient: str | None = None
    has_boxed_warning: bool = False
    indications_text: str | None = None
    adverse_reactions_text: str | None = None
    dosage_text: str | None = None
    boxed_warning_text: str | None = None
    effective_time: str | None = None  # ISO YYYY-MM-DD
    set_id: str | None = None
    prescribing_information: str | None = None
    seeded_fields: list[str] = field(default_factory=list)


def is_configured() -> bool:
    """openFDA needs no key, so seeding is available whenever a base URL is set."""
    return bool(get_settings().openfda_base_url or _DEFAULT_BASE_URL)


def _base_url() -> str:
    # NB: settings copies blank fields into os.environ, so guard against "" (memory 5d95d26e).
    return (get_settings().openfda_base_url or _DEFAULT_BASE_URL).rstrip("/")


def _first(value: object) -> str | None:
    """openFDA returns most fields as single-element lists; take the first string."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _iso_date(raw: str | None) -> str | None:
    """Convert an SPL effective_time (YYYYMMDD) into ISO YYYY-MM-DD."""
    if raw and len(raw) == 8 and raw.isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw or None


def _clean_class(raw: str | None) -> str | None:
    """Strip the trailing ``[EPC]`` / ``[MoA]`` marker openFDA appends to pharm classes."""
    if not raw:
        return None
    return raw.split("[")[0].strip() or None


async def _query(search: str) -> dict | None:
    """Run one openFDA drug/label query; return the first result dict or None."""
    settings = get_settings()
    params: dict[str, str | int] = {"search": search, "limit": 1}
    if settings.openfda_api_key:
        params["api_key"] = settings.openfda_api_key
    url = f"{_base_url()}/drug/label.json"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, params=params)
        if resp.status_code == 404:  # openFDA's "no matches" response
            return None
        resp.raise_for_status()
        results = resp.json().get("results") or []
        return results[0] if results else None
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
        logger.debug("openFDA query failed for %s: %s", search, e)
        return None


async def fetch_label(brand: str, generic: str | None = None) -> LabelSeed | None:
    """Fetch + parse the FDA label for a brand (falling back to its generic name).

    Returns a ``LabelSeed`` on success, or ``None`` if openFDA has no usable data (the
    generator then relies purely on curated YAML). Never raises.
    """
    result: dict | None = None
    for search in _search_terms(brand, generic):
        result = await _query(search)
        if result:
            break
    if not result:
        logger.debug("openFDA: no label found for brand=%r generic=%r", brand, generic)
        return None

    openfda = result.get("openfda") or {}
    set_id = _first(result.get("set_id")) or _first(result.get("id"))
    boxed = _first(result.get("boxed_warning"))

    seed = LabelSeed(
        brand_name=_first(openfda.get("brand_name")),
        generic_name=_first(openfda.get("generic_name")),
        manufacturer=_first(openfda.get("manufacturer_name")),
        administration_route=(r.title() if (r := _first(openfda.get("route"))) else None),
        drug_class=_clean_class(_first(openfda.get("pharm_class_epc"))),
        active_ingredient=_first(openfda.get("substance_name")),
        has_boxed_warning=bool(boxed),
        boxed_warning_text=boxed,
        indications_text=_first(result.get("indications_and_usage")),
        adverse_reactions_text=_first(result.get("adverse_reactions")),
        dosage_text=_first(result.get("dosage_and_administration")),
        effective_time=_iso_date(_first(result.get("effective_time"))),
        set_id=set_id,
    )
    if set_id:
        seed.prescribing_information = (
            f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={set_id}"
        )

    seed.seeded_fields = [
        name
        for name, value in (
            ("manufacturer", seed.manufacturer),
            ("administration_route", seed.administration_route),
            ("drug_class", seed.drug_class),
            ("active_ingredient", seed.active_ingredient),
            ("boxed_warning", seed.has_boxed_warning or None),
            ("indications_text", seed.indications_text),
            ("adverse_reactions_text", seed.adverse_reactions_text),
            ("dosage_text", seed.dosage_text),
            ("prescribing_information", seed.prescribing_information),
        )
        if value
    ]
    return seed


def _search_terms(brand: str, generic: str | None) -> list[str]:
    """openFDA search expressions, brand first then generic, most specific first."""
    terms = [f'openfda.brand_name:"{brand}"']
    if generic:
        terms.append(f'openfda.generic_name:"{generic}"')
    return terms
