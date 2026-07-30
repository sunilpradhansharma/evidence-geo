"""Therapeutic-area to indication taxonomy (SE-007).

The taxonomy stores each monitored entry under a granular key (the indication for
Lupron, or the broad area itself for Oncology) and declares its parent therapeutic
area via an ``area:`` attribute. This module exposes that two-level grouping for
analytics rollups and any taxonomy-aware surface, WITHOUT changing the stored join
key used by scoring (scorer._context_for) or harvest classify.

It also exposes the **disease overlay** (``indications:``), which exists because a
focus brand does not map to exactly one therapeutic area: Rinvoq spans Rheumatology
(RA, PsA, AS, nr-axSpA) and Immunology (AD, UC, CD), as does Humira. Reading
competitors at therapeutic-area-block level therefore scores a Rinvoq Atopic
Dermatitis question against RA competitors. The overlay is purely additive — stored
``therapeutic_area`` values are untouched and nothing reads it unless it asks for a
disease.

**Where the data comes from.** SQLite, via a snapshot the application installs at
startup (``app.services.brand_taxonomy_service.hydrate``). Every accessor here stays
SYNCHRONOUS because they are called from pure sync code — ``curation.coverage.build_matrix``,
``curation.generator.postprocess``, the ClinicalTrials parser — which cannot await. Reading
the database per call is therefore not an option; the snapshot is swapped whole and every
``lru_cache`` in this module is cleared with it.

With no snapshot installed the module falls back to ``config/seed/brands_seed.yaml``. That
file is a **bootstrap fixture, not runtime config**: it seeds an empty database and backs
the tests, which read the real taxonomy with no database at all. Nothing in a running
application reads it once ``hydrate`` has run.
"""
import importlib
from functools import lru_cache

from app.config import outcomes
from app.config.settings import load_yaml_config

# The reviewed baseline. Read by the seeder and by tests; never by a running request.
SEED_FILENAME = "seed/brands_seed.yaml"

# Caches OUTSIDE this module that are derived from the taxonomy and would otherwise survive
# a reload. Cleared by ``reload()`` through a late import so the config layer keeps no
# import-time dependency on scoring, social or the copilot.
_DEPENDENT_CACHES = (
    ("app.scoring.scorer", "_brand_context"),
    ("app.social.community", "_brand_lookup"),
    ("app.copilot.nodes.tool_executor", "_valid_therapeutic_areas"),
)

# The installed snapshot, shaped exactly like the parsed YAML:
# ``{"therapeutic_areas": {...}, "indications": {...}, "drug_catalog": {...}}``.
_SNAPSHOT: dict | None = None

# Curated vocabularies. Deliberately closed sets: an unrecognised value is a config
# error surfaced at startup, not a silently accepted free-text label.
ADMINISTRATION_ROUTES = ("ORAL", "SC", "IV", "IM", "TOPICAL", "OTHER")
EVIDENCE_DEPTHS = ("standard", "full")

# `full` is always an explicit opt-in, for focus brands as much as for competitors.
# Defaulting focus brands to full would silently enrol every monitored brand in the
# evidence programme — Lupron, Vraylar and the GLP-1 set included — and start ingesting
# trials nobody asked for. Scope is a decision, not a side effect of being a focus brand.
_DEFAULT_DEPTH = "standard"


def config() -> dict:
    """The whole taxonomy document, in the shape ``brands.yaml`` had.

    The single read point for the handful of callers that want the raw nested dict rather
    than a resolved accessor (harvest query expansion, the scorer's competitive field, the
    social brand gate). They used to call ``load_yaml_config("brands.yaml")`` directly, which
    is what made the file a second source of truth.
    """
    if _SNAPSHOT is not None:
        return _SNAPSHOT
    return load_yaml_config(SEED_FILENAME)


def install_snapshot(data: dict | None) -> None:
    """Swap the taxonomy the whole module reads, then invalidate every derived cache.

    Passing ``None`` restores the seed-file fallback, which is how a test undoes itself.
    """
    global _SNAPSHOT
    _SNAPSHOT = data
    reload()


def clear_caches() -> None:
    """Clear every ``lru_cache`` in this module.

    Discovered by walking the module namespace rather than hand-listed. There are twenty-odd
    of them and they are added routinely; a list would be correct on the day it was written
    and quietly wrong afterwards, leaving one accessor serving the previous taxonomy.
    """
    for value in list(globals().values()):
        cache_clear = getattr(value, "cache_clear", None)
        if callable(cache_clear):
            cache_clear()


def reload() -> None:
    """Re-derive everything from the current snapshot, here and in dependent modules."""
    clear_caches()
    for module_name, attribute in _DEPENDENT_CACHES:
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 — an unimportable dependant has no cache to clear
            continue
        cache_clear = getattr(getattr(module, attribute, None), "cache_clear", None)
        if callable(cache_clear):
            cache_clear()


@lru_cache
def _areas() -> dict:
    return config().get("therapeutic_areas", {}) or {}


@lru_cache
def _indications() -> dict:
    """The additive ``indications:`` disease overlay."""
    return config().get("indications", {}) or {}


@lru_cache
def _drug_catalog() -> dict:
    """Curated class/route for agents that have no ``therapeutic_areas:`` entry."""
    return config().get("drug_catalog", {}) or {}


@lru_cache
def keys_for_area(area: str | None) -> tuple[str, ...]:
    """Stored ``therapeutic_area`` keys belonging to a broad area display name.

    Returns an empty tuple when *area* is not a known area display name (e.g. it
    is itself already a stored key like "Endometriosis"), letting callers fall
    back to an exact-match filter.
    """
    if not area:
        return ()
    return tuple(
        key for key, block in _areas().items()
        if ((block or {}).get("area") or key) == area
    )


@lru_cache
def area_for(key: str | None) -> str:
    """Parent therapeutic area for a stored therapeutic_area key.

    Falls back to the key itself when unmapped (e.g. legacy/unknown values) so
    callers can group safely without losing rows.
    """
    if not key:
        return key or ""
    block = _areas().get(key) or {}
    return block.get("area") or key


def grouped() -> list[dict]:
    """Ordered ``[{area, indications: [keys]}]`` for grouped pickers/dashboards."""
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for key, block in _areas().items():
        area = (block or {}).get("area") or key
        if area not in groups:
            groups[area] = []
            order.append(area)
        groups[area].append(key)
    return [{"area": area, "indications": groups[area]} for area in order]


@lru_cache
def alias_index() -> tuple[dict, ...]:
    """Flattened alias lookup over brands.yaml for mapping free text to the taxonomy (FR-116).

    Every focus brand, competitor, generic, indication, and area name becomes a lowercased
    alias pointing at its granular therapeutic_area key (the value stored on questions) plus
    its parent ``area`` and a ``kind`` (brand | competitor | indication | area). Sorted
    longest-alias first so a specific multi-word alias ("leuprolide acetate") wins over a
    short one ("lupron"). This is the single source of truth for taxonomy mapping — the
    Prompt Volume mapper reads *this*, never a forked copy of the data.
    """
    entries: list[dict] = []

    def _add(alias: str | None, ta_key: str, area: str, kind: str, canonical: str, is_comp: bool):
        alias = (alias or "").strip().lower()
        if len(alias) < 2:
            return
        entries.append({
            "alias": alias, "ta_key": ta_key, "area": area,
            "kind": kind, "canonical": canonical, "is_competitor": is_comp,
        })

    for ta_key, block in _areas().items():
        block = block or {}
        area = block.get("area") or ta_key
        _add(ta_key, ta_key, area, "area", area, False)
        _add(area, ta_key, area, "area", area, False)
        for b in block.get("focus_brands", []) or []:
            name = b.get("name")
            _add(name, ta_key, area, "brand", name or "", False)
            _add(b.get("generic"), ta_key, area, "brand", name or b.get("generic") or "", False)
            for ind in b.get("indications", []) or []:
                _add(ind, ta_key, area, "indication", ind, False)
        for c in block.get("competitors", []) or []:
            name = c.get("name")
            _add(name, ta_key, area, "competitor", name or "", True)
            _add(c.get("generic"), ta_key, area, "competitor", name or c.get("generic") or "", True)

    entries.sort(key=lambda e: len(e["alias"]), reverse=True)
    return tuple(entries)


@lru_cache
def brand_owner_index() -> dict:
    """Lowercased brand/generic alias -> owning ``company`` string, from brands.yaml.

    Single source of truth for brand OWNERSHIP (distinct from the focus-vs-competitor split in
    ``alias_index``). Ownership must key off ``company`` because some entries listed under
    ``competitors`` are actually AbbVie (Orilissa/Oriahnn) and some areas (Obesity) have no
    AbbVie focus brand at all. Brand NAMES are canonical and never collide, so they are added
    first; ambiguous generics (e.g. leuprolide acetate spans AbbVie + Tolmar) only fill gaps
    via ``setdefault`` and never override a name mapping. Used by the Social Listening surface.
    """
    idx: dict[str, str] = {}
    for _ta_key, block in _areas().items():
        block = block or {}
        for kind in ("focus_brands", "competitors"):
            for b in block.get(kind, []) or []:
                company = (b.get("company") or "").strip()
                if not company:
                    continue
                name = (b.get("name") or "").strip().lower()
                if name:
                    idx.setdefault(name, company)
                gen = (b.get("generic") or "").strip().lower()
                if gen:
                    idx.setdefault(gen, company)
    return idx


def company_for(brand_name: str | None) -> str | None:
    """Owning company for a brand (or generic) name, or None when unknown."""
    if not brand_name:
        return None
    return brand_owner_index().get(brand_name.strip().lower())


def is_abbvie_brand(brand_name: str | None) -> bool:
    """True when *brand_name* is owned by AbbVie (case-insensitive company match)."""
    company = company_for(brand_name)
    return bool(company) and "abbvie" in company.lower()


# --- therapeutic-area block accessors -------------------------------------------------
@lru_cache
def focus_brands_for_key(ta_key: str | None) -> tuple[str, ...]:
    """Focus-brand names declared under a stored therapeutic_area key."""
    block = _areas().get((ta_key or "").strip()) or {}
    return tuple(b["name"] for b in block.get("focus_brands", []) or [] if b.get("name"))


@lru_cache
def competitors_for_key(ta_key: str | None) -> tuple[str, ...]:
    """Competitor names declared under a stored therapeutic_area key."""
    block = _areas().get((ta_key or "").strip()) or {}
    return tuple(c["name"] for c in block.get("competitors", []) or [] if c.get("name"))


@lru_cache
def area_keys_for_brand(name: str | None) -> tuple[str, ...]:
    """Every stored therapeutic_area key a drug appears under, in declaration order.

    Matches on brand name, generic, or any declared alias. A single-area drug (Vraylar)
    returns one key; a multi-area one (Rinvoq -> Dermatology, Gastroenterology,
    Rheumatology) returns all of them.

    This exists so a caller can tell "the brand names the area" from "the brand CANNOT
    name the area". ``alias_index()`` collapses those two cases into one winner decided
    by whichever block brands.yaml declares first, which is fine for a search-demand
    hint and wrong for a stored classification.
    """
    alias = (name or "").strip().lower()
    if not alias:
        return ()
    keys: list[str] = []
    for ta_key, block in _areas().items():
        block = block or {}
        for kind in ("focus_brands", "competitors"):
            for entry in block.get(kind, []) or []:
                names = {
                    (entry.get("name") or "").strip().lower(),
                    (entry.get("generic") or "").strip().lower(),
                }
                names.update((a or "").strip().lower() for a in entry.get("aliases") or [])
                if alias in names and ta_key not in keys:
                    keys.append(ta_key)
    return tuple(keys)


# --- disease overlay ------------------------------------------------------------------
@lru_cache
def disease_index() -> tuple[dict, ...]:
    """Flattened ``alias -> disease`` lookup over the ``indications:`` overlay.

    Each entry is ``{alias, disease, area, ta_key}`` with a lowercased alias, sorted
    longest-alias-first so "plaque psoriasis" wins over "psoriasis" and
    "non-radiographic axial spondyloarthritis" wins over "axspa".

    This is intentionally SEPARATE from ``alias_index()``: Prompt Volume (FR-116) reads
    ``alias_index()`` raw, so its content and output shape must not move. Disease
    detection is new signal and gets its own index.
    """
    entries: list[dict] = []
    seen: set[str] = set()

    def _add(alias: str | None, disease: str, area: str, ta_key: str) -> None:
        alias = (alias or "").strip().lower()
        if len(alias) < 2 or alias in seen:
            return
        seen.add(alias)
        entries.append({"alias": alias, "disease": disease, "area": area, "ta_key": ta_key})

    for disease, block in _indications().items():
        block = block or {}
        area = block.get("area") or disease
        ta_key = block.get("therapeutic_area_key") or area
        _add(disease, disease, area, ta_key)
        for alias in block.get("aliases", []) or []:
            _add(alias, disease, area, ta_key)

    entries.sort(key=lambda e: len(e["alias"]), reverse=True)
    return tuple(entries)


@lru_cache
def _disease_by_alias() -> dict:
    return {e["alias"]: e["disease"] for e in disease_index()}


def canonical_disease(name: str | None) -> str | None:
    """Canonical disease key for any declared alias (or the key itself), else ``None``.

    Case-insensitive exact-alias resolution — this does NOT scan free text. Query
    scanning lives in ``prompt_volume.mapping.map_query``, which owns the word-boundary
    matching and already caches its patterns.
    """
    if not name:
        return None
    return _disease_by_alias().get(name.strip().lower())


@lru_cache
def _disease_block(disease: str | None) -> dict:
    key = canonical_disease(disease)
    return (_indications().get(key) or {}) if key else {}


@lru_cache
def competitors_for_disease(disease: str | None) -> tuple[str, ...]:
    """The competitive field for one indication.

    This is the fix for the flattening bug: the parent area's competitor list mixes
    every indication together, so an Atopic Dermatitis question would be scored against
    Xeljanz and Olumiant while missing Cibinqo, Adbry and Ebglyss entirely. Returns an
    empty tuple for an unknown disease so callers can fall back to the area block.
    """
    return tuple(_disease_block(disease).get("competitors", []) or [])


@lru_cache
def brands_for_disease(disease: str | None) -> tuple[str, ...]:
    """Monitored/focus brands in play for one indication."""
    return tuple(_disease_block(disease).get("brands", []) or [])


@lru_cache
def area_for_disease(disease: str | None) -> str | None:
    """Parent therapeutic area for a disease, or ``None`` when it is not declared."""
    return _disease_block(disease).get("area") or None


@lru_cache
def therapeutic_area_key_for_disease(disease: str | None) -> str | None:
    """The stored ``therapeutic_area`` value a question in this disease should carry.

    This is what makes TA derivation correct: a Rinvoq question is Rheumatology or
    Immunology depending on the DISEASE, never on the brand alone.
    """
    block = _disease_block(disease)
    if not block:
        return None
    return block.get("therapeutic_area_key") or block.get("area") or None


@lru_cache
def canonical_outcomes_for_disease(disease: str | None) -> tuple[str, ...]:
    """Canonical outcome IDs referenced by an indication (resolved via outcomes.py)."""
    return tuple(_disease_block(disease).get("canonical_outcomes", []) or [])


@lru_cache
def diseases() -> tuple[str, ...]:
    """Every declared disease key, in declaration order."""
    return tuple(_indications())


@lru_cache
def diseases_for_key(ta_key: str | None) -> tuple[str, ...]:
    """Diseases whose stored therapeutic_area key is *ta_key*."""
    key = (ta_key or "").strip()
    if not key:
        return ()
    return tuple(d for d in _indications() if therapeutic_area_key_for_disease(d) == key)


@lru_cache
def draft_diseases() -> tuple[str, ...]:
    """Indications whose endpoints have not been through medical review.

    An indication created through the UI carries model-drafted ``canonical_outcomes``. A
    wrong ``allowed_window`` silently changes which trial results are eligible for every
    comparison in that disease, so a draft is usable for comparison coverage and question
    generation but is refused by the evidence programme until a human verifies it — the same
    direction as ``require_verified`` on a study.

    Absence of the marker means VERIFIED: everything seeded from the reviewed baseline is.
    """
    return tuple(
        disease for disease, block in _indications().items()
        if (block or {}).get("verification_status") == "DRAFT"
    )


def is_draft_disease(disease: str | None) -> bool:
    """True when this indication's endpoints are model-drafted and not yet verified."""
    resolved = canonical_disease(disease)
    return bool(resolved) and resolved in draft_diseases()


# --- curated cross-class annotation (bounded, hand-authored) --------------------------
@lru_cache
def drug_index() -> dict:
    """Lowercased brand/generic alias -> curated drug facts.

    Values are ``{canonical, generic, drug_class, administration_route, evidence_depth,
    is_competitor, biosimilar_of, background_therapy}``. Sources, in precedence order:
    the ``therapeutic_areas:`` entries (which also carry the focus/competitor split) then
    ``drug_catalog:`` for agents that appear only as disease-overlay comparators.

    Each entry contributes its ``name``, its ``generic``, and any ``aliases:`` it declares.
    Aliases exist because trial registries label arms with abbreviations — a PsA search
    returns ``UPA``, ``ADA`` and ``IXE`` far more often than the brand or generic — and an
    unmatched abbreviation becomes a junk network node. "UPA means upadacitinib" is a
    curated, reviewable claim, which is why it lives in the YAML beside the drug rather
    than in normalisation code.

    Uncurated drugs are simply absent — callers get ``None``, never a guess. Open-set
    class inference (RxNorm/ATC/ChEMBL) is deliberately out of scope: a curated table is
    a reviewable artefact, an inferred label is an unreviewed assertion.
    """
    idx: dict[str, dict] = {}

    def _put(alias: str | None, record: dict) -> None:
        alias = (alias or "").strip().lower()
        if alias:
            idx.setdefault(alias, record)

    for _key, block in _areas().items():
        block = block or {}
        for kind, is_comp in (("focus_brands", False), ("competitors", True)):
            for entry in block.get(kind, []) or []:
                name = (entry.get("name") or "").strip()
                if not name:
                    continue
                record = {
                    "canonical": name,
                    "generic": entry.get("generic") or None,
                    "drug_class": entry.get("drug_class") or None,
                    "administration_route": entry.get("administration_route") or None,
                    "evidence_depth": entry.get("evidence_depth") or _DEFAULT_DEPTH,
                    "is_competitor": is_comp,
                    "biosimilar_of": entry.get("biosimilar_of") or None,
                    # Never read here: a focus brand or a tracked competitor is the very
                    # thing `comparison_agents()` exists to police, so the flag would be
                    # a contradiction rather than a curated exception.
                    "background_therapy": False,
                }
                _put(name, record)
                _put(entry.get("generic"), record)
                for alias in entry.get("aliases") or []:
                    _put(alias, record)

    for name, entry in _drug_catalog().items():
        entry = entry or {}
        record = {
            "canonical": name,
            "generic": entry.get("generic") or None,
            "drug_class": entry.get("drug_class") or None,
            "administration_route": entry.get("administration_route") or None,
            "evidence_depth": entry.get("evidence_depth") or _DEFAULT_DEPTH,
            "is_competitor": True,
            "biosimilar_of": entry.get("biosimilar_of") or None,
            "background_therapy": bool(entry.get("background_therapy", False)),
        }
        _put(name, record)
        _put(entry.get("generic"), record)
        for alias in entry.get("aliases") or []:
            _put(alias, record)

    return idx


def drug_class_for(name: str | None) -> str | None:
    """Curated pharmacological class, or ``None`` when the drug is not curated."""
    if not name:
        return None
    return (drug_index().get(name.strip().lower()) or {}).get("drug_class")


def administration_route_for(name: str | None) -> str | None:
    """Curated administration route (ORAL/SC/IV/...), or ``None`` when not curated.

    Feeds the route-mixing transitivity check: an oral small molecule and an injectable
    biologic in the same network is a threat to disclose, never a difference to adjust
    away.
    """
    if not name:
        return None
    return (drug_index().get(name.strip().lower()) or {}).get("administration_route")


def generic_for(name: str | None) -> str | None:
    """Curated generic (INN) name, or ``None`` when the drug is not curated.

    Used as a *fallback* search term when fetching a regulatory label: openFDA indexes
    some products under the generic only. Returning ``None`` rather than echoing the
    input keeps the caller able to tell "we know its generic" from "we do not", which is
    the difference between a second search worth running and one that repeats the first.
    """
    if not name:
        return None
    return (drug_index().get(name.strip().lower()) or {}).get("generic")


def biosimilar_of(name: str | None) -> str | None:
    """The originator this agent is a biosimilar of, or ``None``.

    **Recorded, never acted on here.** Whether a biosimilar shares its originator's network
    node is a clinical equivalence claim, so it belongs to ``biosimilar_policy`` in an
    approved analysis protocol — exactly as dose pooling belongs to ``dose_policy``. This
    only makes the relationship known, so that the policy has something to act on.
    """
    if not name:
        return None
    return (drug_index().get(name.strip().lower()) or {}).get("biosimilar_of")


def evidence_depth_for(name: str | None) -> str:
    """``full`` or ``standard``. Unknown drugs are ``standard`` — no trial ingestion.

    Read ONLY by the evidence layer. It never changes monitoring identity: a competitor
    carried at full depth stays a competitor to scoring and alerting.
    """
    if not name:
        return _DEFAULT_DEPTH
    record = drug_index().get(name.strip().lower()) or {}
    return record.get("evidence_depth") or _DEFAULT_DEPTH


@lru_cache
def full_depth_drugs() -> tuple[str, ...]:
    """Canonical names of every drug the evidence layer should ingest trials for."""
    names: list[str] = []
    for record in drug_index().values():
        name = record.get("canonical")
        if record.get("evidence_depth") == "full" and name and name not in names:
            names.append(name)
    return tuple(names)


@lru_cache
def comparison_agents() -> tuple[str, ...]:
    """Curated agents whose presence in a question changes WHICH comparison it asks.

    A generated head-to-head question may name the two agents of its own cell and nothing
    else: a third named drug is a different comparison from the one that was requested,
    and a bank whose questions do not match their own coverage cells cannot be reasoned
    about. This is the vocabulary `curation/generator.py` checks a candidate against.

    The exception is a declared ``background_therapy``. It is EXPLICIT config, never an
    inference from which YAML block a drug sits in: ``drug_catalog:`` carries genuine
    rivals (Taltz, Bimzelx, Entyvio) alongside annotation-only agents, so "catalog means
    harmless" would let a real competitor into a question. Anything not curated for it is
    policed, which is the safe direction when the comparator lists are incomplete.
    """
    names: list[str] = []
    for record in drug_index().values():
        name = record.get("canonical")
        if not name or record.get("background_therapy") or name in names:
            continue
        names.append(name)
    return tuple(names)


# --- startup validation ---------------------------------------------------------------
def validate_config() -> list[str]:
    """Configuration problems as human-readable strings; empty list means valid.

    Single entry point for config validation, called at startup. The load-bearing check
    is referential: a ``canonical_outcomes`` ID in the disease overlay that does not
    exist in canonical_outcomes.yaml must fail loudly, because silently dropping an
    endpoint reference would produce a network with no defined outcome.
    """
    errors: list[str] = outcomes.validate()
    area_keys = set(_areas())

    # A biosimilar pointing at an originator that is not curated would leave
    # `biosimilar_policy` unable to act: POOL_WITH_ORIGINATOR needs a node to pool onto.
    # Deduped by canonical name because one record is registered under several aliases.
    index = drug_index()
    checked: set[str] = set()
    for record in index.values():
        canonical = record.get("canonical") or ""
        originator = record.get("biosimilar_of")
        if not originator or canonical in checked:
            continue
        checked.add(canonical)
        if originator.strip().lower() not in index:
            errors.append(
                f"brands.yaml {canonical!r}: biosimilar_of {originator!r} is not a curated "
                "drug, so biosimilar_policy would have no node to pool onto"
            )

    for disease, block in _indications().items():
        block = block or {}
        where = f"brands.yaml indications[{disease!r}]"

        if not block.get("area"):
            errors.append(f"{where}: missing `area`")

        ta_key = block.get("therapeutic_area_key") or block.get("area")
        if ta_key and ta_key not in area_keys:
            errors.append(
                f"{where}: therapeutic_area_key {ta_key!r} is not a key of "
                f"`therapeutic_areas:` — questions would store an orphan area"
            )

        referenced = block.get("canonical_outcomes", []) or []
        # A DRAFT indication is allowed to have none yet. It is fenced out of the evidence
        # programme by ``draft_diseases()``, so there is no network for a missing endpoint to
        # silently corrupt — which is the reason this is an error for a verified one.
        if not referenced and block.get("verification_status") != "DRAFT":
            errors.append(f"{where}: declares no canonical_outcomes")
        for outcome_id in referenced:
            if not outcomes.is_defined(outcome_id):
                errors.append(
                    f"{where}: canonical outcome {outcome_id!r} is not defined in "
                    f"canonical_outcomes.yaml"
                )

    for alias, record in drug_index().items():
        route = record.get("administration_route")
        if route and route not in ADMINISTRATION_ROUTES:
            errors.append(
                f"brands.yaml drug {alias!r}: administration_route {route!r} is not one "
                f"of {', '.join(ADMINISTRATION_ROUTES)}"
            )
        depth = record.get("evidence_depth")
        if depth not in EVIDENCE_DEPTHS:
            errors.append(
                f"brands.yaml drug {alias!r}: evidence_depth {depth!r} is not one of "
                f"{', '.join(EVIDENCE_DEPTHS)}"
            )

    return errors
