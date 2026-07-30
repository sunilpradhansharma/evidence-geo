"""Add a brand to the taxonomy from the UI: resolve the name, draft a record, write it.

Separate from ``brand_taxonomy_service`` on purpose. That module is the persistence layer —
it imports a whole document and renders it back. This one is a single use case with a
different risk profile: it takes a name typed by an analyst and a record drafted by a model,
and turns them into rows that scoring, coverage and corpus-wide attribution will read.

**This reverses an explicit design stance.** ``competitor_discovery_service`` says a queue
that edits the taxonomy "would turn it into the inferred kind, which is explicitly out of
scope", and only ever renders YAML for a human to commit. Three things are what keep writing
here defensible, and dropping any one of them loses it:

* the model only ever *suggests* — a competitor is saved only if the analyst ticked it;
* an indication whose endpoints the model drafted is stored ``DRAFT`` and fenced out of the
  evidence programme until a human verifies it;
* every write is audited and re-exportable as YAML, so it stays reviewable after the fact.

**A write cannot introduce a configuration error.** The rows are inserted, the snapshot is
rebuilt, and ``validate_config()`` runs against it; anything that was not already broken
causes a rollback. That reuses the startup checks rather than restating them, so the two can
never disagree about what "valid" means.
"""
from __future__ import annotations

import difflib

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import taxonomy
from app.models.brand_taxonomy import (
    ROLE_BRAND,
    ROLE_COMPETITOR,
    SIDE_FOCUS,
    STATUS_DRAFT,
    TaxonomyAreaBlock,
    TaxonomyDrug,
    TaxonomyIndication,
    TaxonomyIndicationDrug,
    _dumps,
)
from app.services import brand_taxonomy_service as store
from app.utils.audit import write_audit
from app.utils.logging import get_logger

logger = get_logger("services.brand_authoring")

# Close enough to be worth showing as "did you mean", loose enough to catch a real typo.
# Matches the ratio ``app.scoring.differ`` already uses for the same job.
_NEAR_MATCH_CUTOFF = 0.82
_MAX_NEAR_MATCHES = 5


class BrandRejected(ValueError):
    """A proposed addition was refused. ``reasons`` is shown to the analyst verbatim."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


# --- step 1: is this actually a new drug? ----------------------------------------------
def _known_names() -> list[str]:
    """Every canonical drug name currently curated, deduped, in declaration order."""
    seen: list[str] = []
    for record in taxonomy.drug_index().values():
        name = record.get("canonical")
        if name and name not in seen:
            seen.append(name)
    return seen


def resolve(name: str) -> dict:
    """Classify a typed brand name against what is already curated.

    Deterministic only. An exact hit or a near miss is decided by string comparison against
    ``drug_index()``, which is also what a collision would be measured against — so a typo of
    a known brand is caught without a model call and without depending on model judgement.
    The LLM "did you mean" in the UI runs only when this returns ``novel``.
    """
    typed = (name or "").strip()
    if not typed:
        return {"status": "invalid", "reason": "Enter a brand name."}

    record = taxonomy.drug_index().get(typed.lower())
    if record:
        canonical = record.get("canonical") or typed
        return {
            "status": "exact_match",
            "typed": typed,
            "canonical": canonical,
            # Says which of the two it was: typing an alias of a curated brand is a different
            # situation from typing its name, and only one of them looks like a duplicate.
            "matched_alias": typed.lower() != canonical.lower(),
            "areas": list(taxonomy.area_keys_for_brand(canonical)),
            "company": taxonomy.company_for(canonical),
        }

    near = difflib.get_close_matches(
        typed, _known_names(), n=_MAX_NEAR_MATCHES, cutoff=_NEAR_MATCH_CUTOFF
    )
    return {
        "status": "near_matches" if near else "novel",
        "typed": typed,
        "near_matches": [
            {"name": n, "company": taxonomy.company_for(n),
             "areas": list(taxonomy.area_keys_for_brand(n))}
            for n in near
        ],
    }


# --- write-time validation --------------------------------------------------------------
def _alias_collisions(payload: dict) -> list[str]:
    """Aliases the proposed brand claims that a curated drug already answers to.

    ``drug_index()`` is built with ``setdefault`` — first declaration wins — so a colliding
    alias does not raise, it silently loses. Since the mentions rollup resolves every scored
    answer through that index, the damage is not a bad row: it is a brand quietly being
    attributed to a different agent across the whole existing corpus. Rejecting at write time
    is the only point where this is visible.
    """
    index = taxonomy.drug_index()
    name = (payload.get("name") or "").strip()
    claimed = [name, payload.get("generic") or "", *(payload.get("aliases") or [])]

    collisions: list[str] = []
    for alias in claimed:
        key = (alias or "").strip().lower()
        if not key:
            continue
        owner = index.get(key)
        if owner and owner.get("canonical", "").lower() != name.lower():
            collisions.append(
                f"{alias!r} is already an alias of {owner.get('canonical')!r} — saving this "
                f"would silently reattribute existing answers to the wrong agent"
            )
    return collisions


def _declared_area(payload: dict) -> tuple[str, str] | None:
    """The new therapeutic area this addition creates, as ``(ta_key, area)``, or None."""
    block = payload.get("new_therapeutic_area") or None
    if not block:
        return None
    return ((block.get("ta_key") or "").strip(), (block.get("area") or "").strip())


def _area_keys_after(payload: dict) -> set[str]:
    """Therapeutic-area keys that will exist once this addition is written."""
    keys = set(taxonomy.config().get("therapeutic_areas") or {})
    declared = _declared_area(payload)
    if declared and declared[0]:
        keys.add(declared[0])
    return keys


def validate_addition(payload: dict) -> list[str]:
    """Reasons this addition must be refused, checked before anything is written."""
    errors: list[str] = []
    name = (payload.get("name") or "").strip()

    if not name:
        errors.append("A brand name is required.")
    elif taxonomy.drug_index().get(name.lower()):
        errors.append(f"{name!r} is already curated. Edit the existing entry instead.")

    errors.extend(_alias_collisions(payload))

    existing_blocks = taxonomy.config().get("therapeutic_areas") or {}
    ta_key = (payload.get("therapeutic_area_key") or "").strip()
    declared = _declared_area(payload)

    if declared:
        new_key, new_area = declared
        if not new_key:
            errors.append("A new therapeutic area needs a key.")
        if not new_area:
            errors.append("A new therapeutic area needs an area name.")
        if new_key and new_key in existing_blocks:
            errors.append(
                f"Therapeutic area {new_key!r} already exists. Select it rather than "
                "creating a second block under the same key."
            )
        # The brand must be filed under the block it is creating. Anything else means the
        # form disagreed with itself, and guessing which half was meant would be worse than
        # refusing.
        if new_key and ta_key and ta_key != new_key:
            errors.append(
                f"The brand is filed under {ta_key!r} but a new area {new_key!r} was "
                "declared. They must match."
            )
    elif not ta_key:
        errors.append("A therapeutic area is required.")
    elif ta_key not in existing_blocks:
        errors.append(f"Therapeutic area {ta_key!r} does not exist.")

    route = payload.get("administration_route")
    if route and route not in taxonomy.ADMINISTRATION_ROUTES:
        errors.append(
            f"administration_route {route!r} is not one of "
            f"{', '.join(taxonomy.ADMINISTRATION_ROUTES)}."
        )

    # Not merely "must be a valid value". `full` enrols the drug in trial ingestion, and a
    # brand added through a modal has had none of the review that decision assumes.
    depth = payload.get("evidence_depth")
    if depth and depth != "standard":
        errors.append(
            "evidence_depth must be 'standard'. Enrolling a brand in the evidence programme "
            "is a separate, deliberate decision."
        )

    diseases = payload.get("diseases") or []
    if not diseases:
        errors.append("Select at least one indication, or the brand produces no comparisons.")

    known = set(taxonomy.diseases())
    # Resolved against the post-write world, so an indication may sit in the area this same
    # addition creates. Checking against the current one would make the two halves of a
    # single submission contradict each other.
    valid_keys = _area_keys_after(payload)

    for entry in diseases:
        disease = ((entry or {}).get("disease") or "").strip()
        if not disease:
            errors.append("An indication with no name was submitted.")
            continue
        if disease in known:
            continue
        # A new indication has to say where it sits, or questions written for it would carry
        # a therapeutic area no filter can select.
        new_key = (entry.get("therapeutic_area_key") or "").strip()
        if not entry.get("area"):
            errors.append(f"New indication {disease!r} needs an area.")
        if not new_key:
            errors.append(f"New indication {disease!r} needs a therapeutic area key.")
        elif new_key not in valid_keys:
            errors.append(
                f"New indication {disease!r}: therapeutic area {new_key!r} does not exist."
            )

    return errors


# --- the write --------------------------------------------------------------------------
async def _next_order(db: AsyncSession, column) -> int:
    """One past the current maximum, so an insert never reorders what is already there.

    ``coverage.rank()`` reads declaration order as the curated tiering. Appending is the only
    insert position that leaves the existing gap queue exactly as it was.
    """
    current = (await db.execute(select(func.max(column)))).scalar()
    return (current or 0) + 1


async def add_brand(db: AsyncSession, payload: dict, *, reviewer: str) -> dict:
    """Insert a focus brand, its indications and its ticked competitors, then reload.

    Raises ``BrandRejected`` and leaves the taxonomy untouched if the addition is invalid —
    either by the checks above, or because the resulting configuration would fail the same
    validation the application runs at startup.
    """
    problems = validate_addition(payload)
    if problems:
        raise BrandRejected(problems)

    # Compared against the post-write result so a pre-existing problem elsewhere in the
    # taxonomy cannot block an unrelated addition.
    errors_before = set(taxonomy.validate_config())

    name = payload["name"].strip()
    diseases = payload.get("diseases") or []
    disease_names = [d["disease"].strip() for d in diseases if (d or {}).get("disease")]

    # A therapeutic area is only ever created as part of filing a brand into it, which is
    # what stops an empty block appearing as a selectable option in every TA filter in the
    # application with nothing behind it.
    declared = _declared_area(payload)
    if declared:
        new_key, new_area = declared
        db.add(TaxonomyAreaBlock(
            ta_key=new_key,
            area=new_area,
            # Appended, never inserted: `keys_for_area` and the grouped pickers render in
            # declaration order, so renumbering would reshuffle every existing area.
            display_order=await _next_order(db, TaxonomyAreaBlock.display_order),
        ))
        await db.flush()
        ta_key = new_key
    else:
        ta_key = payload["therapeutic_area_key"].strip()

    db.add(TaxonomyDrug(
        ta_key=ta_key,
        side=SIDE_FOCUS,
        name=name,
        generic=(payload.get("generic") or None),
        company=(payload.get("company") or None),
        drug_class=(payload.get("drug_class") or None),
        administration_route=(payload.get("administration_route") or None),
        # Left unset rather than written as "standard": the read path already defaults, and
        # storing it would assert a scope decision nobody made.
        evidence_depth=None,
        aliases_json=_dumps(payload.get("aliases")),
        indications_json=_dumps(disease_names),
        display_order=await _next_order(db, TaxonomyDrug.display_order),
    ))

    known = set(taxonomy.diseases())
    indication_order = await _next_order(db, TaxonomyIndication.display_order)
    membership_order = await _next_order(db, TaxonomyIndicationDrug.display_order)

    for entry in diseases:
        disease = entry["disease"].strip()

        if disease not in known:
            db.add(TaxonomyIndication(
                disease=disease,
                area=entry.get("area"),
                therapeutic_area_key=entry.get("therapeutic_area_key"),
                canonical_outcomes_json=_dumps(entry.get("canonical_outcomes")),
                # The endpoints came from a model. DRAFT is what keeps this indication out of
                # network build, NMA and claim grading until a human has checked them, while
                # leaving it usable for coverage and question generation today.
                verification_status=STATUS_DRAFT,
                created_by=reviewer,
                display_order=indication_order,
            ))
            indication_order += 1
            known.add(disease)

        db.add(TaxonomyIndicationDrug(
            disease=disease, drug_name=name, role=ROLE_BRAND,
            display_order=membership_order,
        ))
        membership_order += 1

        existing = set(taxonomy.competitors_for_disease(disease))
        for competitor in entry.get("competitors") or []:
            competitor_name = ((competitor or {}).get("name") or "").strip()
            if not competitor_name or competitor_name in existing:
                continue
            db.add(TaxonomyIndicationDrug(
                disease=disease,
                drug_name=competitor_name,
                role=ROLE_COMPETITOR,
                # The model's stated reason, kept because a competitive field curated by
                # inclusion is unreviewable without knowing why each entry is on it.
                note=(competitor.get("note") or None),
                display_order=membership_order,
            ))
            existing.add(competitor_name)
            membership_order += 1

    await db.flush()

    # Install the would-be taxonomy and run the startup checks against it. Reusing
    # validate_config here is what stops this path and boot disagreeing about validity.
    snapshot = await store.build_snapshot(db)
    taxonomy.install_snapshot(snapshot)
    introduced = [e for e in taxonomy.validate_config() if e not in errors_before]

    if introduced:
        await db.rollback()
        await store.hydrate(db)  # put the previous taxonomy back
        raise BrandRejected(introduced)

    await write_audit(
        db,
        role="ANALYST",
        event="TAXONOMY_BRAND_ADDED",
        context={
            "brand": name,
            "therapeutic_area_key": ta_key,
            # Recorded distinctly: creating an area is a structural change to the taxonomy,
            # not the same kind of event as filing a brand into an existing one.
            "new_therapeutic_area": (
                {"ta_key": declared[0], "area": declared[1]} if declared else None
            ),
            "diseases": disease_names,
            "new_indications": [
                d["disease"] for d in diseases
                if (d or {}).get("disease") and d.get("canonical_outcomes")
            ],
            "competitors_added": {
                d["disease"]: [c.get("name") for c in (d.get("competitors") or [])]
                for d in diseases if d.get("competitors")
            },
            # No RBAC in this tree, so this is a recorded claim rather than an authenticated
            # identity — the same caveat protocol approval carries.
            "reviewer": reviewer,
        },
        commit=False,
    )
    await db.commit()

    logger.info(
        "Added brand %s under %s (%d indication(s)) by %s",
        name, ta_key, len(disease_names), reviewer,
    )
    return {
        "brand": name,
        "therapeutic_area_key": ta_key,
        "created_therapeutic_area": declared[0] if declared else None,
        "diseases": disease_names,
        "draft_indications": [d for d in disease_names if taxonomy.is_draft_disease(d)],
    }


async def area_choices(db: AsyncSession) -> list[dict]:
    """Therapeutic-area blocks a brand can be filed under, in declaration order."""
    rows = (await db.execute(
        select(TaxonomyAreaBlock).order_by(TaxonomyAreaBlock.display_order)
    )).scalars().all()
    return [
        {
            "ta_key": r.ta_key,
            "area": r.area,
            "diseases": list(taxonomy.diseases_for_key(r.ta_key)),
        }
        for r in rows
    ]
