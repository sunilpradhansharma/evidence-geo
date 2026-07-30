"""Read and write the brand taxonomy in SQLite, and install it into the config layer.

This is the only module that knows the taxonomy lives in a database. ``app.config.taxonomy``
receives a finished nested dict and never imports a model, keeping the config layer free of
``app.models`` exactly as protocol validation was kept out of it to avoid an import cycle.

**Fidelity is the contract.** ``build_snapshot`` reconstructs the document shape
``brands.yaml`` had, key for key, so that a snapshot built from seeded rows compares *equal*
to the parsed seed file. That equality is the whole proof that moving the store changed no
behaviour, and ``tests/test_brand_taxonomy_store.py`` asserts it. Two rules follow from it:

* An unset field is OMITTED rather than nulled, because ``drug_index()`` distinguishes the
  two through ``.get(key, default)``.
* Every read is ordered by ``display_order``. The YAML expressed its curated tiering purely
  as line order and ``curation.coverage.rank()`` still reads it that way, so unordered rows
  would silently rerank the gap queue.
"""
from __future__ import annotations

import yaml
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import taxonomy
from app.config.settings import load_yaml_config
from app.models.brand_taxonomy import (
    ROLE_BRAND,
    ROLE_COMPETITOR,
    SIDE_CATALOG,
    SIDE_COMPETITOR,
    SIDE_FOCUS,
    STATUS_DRAFT,
    TaxonomyAreaBlock,
    TaxonomyDrug,
    TaxonomyIndication,
    TaxonomyIndicationDrug,
    _dumps,
)
from app.utils.logging import get_logger

logger = get_logger("services.brand_taxonomy")

_TABLES = (TaxonomyAreaBlock, TaxonomyDrug, TaxonomyIndication, TaxonomyIndicationDrug)


# --- reading --------------------------------------------------------------------------
async def is_empty(db: AsyncSession) -> bool:
    """True when no taxonomy has been seeded yet."""
    count = (await db.execute(
        select(func.count()).select_from(TaxonomyAreaBlock)
    )).scalar_one()
    return not count


async def build_snapshot(db: AsyncSession) -> dict:
    """Rebuild the whole taxonomy document from rows, in ``brands.yaml`` shape."""
    areas = list((await db.execute(
        select(TaxonomyAreaBlock).order_by(TaxonomyAreaBlock.display_order)
    )).scalars().all())
    drugs = list((await db.execute(
        select(TaxonomyDrug).order_by(TaxonomyDrug.display_order)
    )).scalars().all())
    indications = list((await db.execute(
        select(TaxonomyIndication).order_by(TaxonomyIndication.display_order)
    )).scalars().all())
    memberships = list((await db.execute(
        select(TaxonomyIndicationDrug).order_by(TaxonomyIndicationDrug.display_order)
    )).scalars().all())

    therapeutic_areas: dict[str, dict] = {}
    for block in areas:
        # All three keys always present: every block in the reviewed baseline declares them,
        # and a block with an empty competitor list is a real statement (nothing to compare
        # against) rather than an omission.
        therapeutic_areas[block.ta_key] = {
            "area": block.area,
            "focus_brands": [
                d.as_entry() for d in drugs
                if d.ta_key == block.ta_key and d.side == SIDE_FOCUS
            ],
            "competitors": [
                d.as_entry() for d in drugs
                if d.ta_key == block.ta_key and d.side == SIDE_COMPETITOR
            ],
        }

    drug_catalog: dict[str, dict] = {
        d.name: d.as_catalog_entry() for d in drugs if d.side == SIDE_CATALOG
    }

    by_disease: dict[str, list[TaxonomyIndicationDrug]] = {}
    for row in memberships:
        by_disease.setdefault(row.disease, []).append(row)

    overlay: dict[str, dict] = {}
    for indication in indications:
        rows = by_disease.get(indication.disease, [])
        block = {
            "area": indication.area,
            "therapeutic_area_key": indication.therapeutic_area_key,
            "aliases": indication.aliases,
            "brands": [r.drug_name for r in rows if r.role == ROLE_BRAND],
            "competitors": [r.drug_name for r in rows if r.role == ROLE_COMPETITOR],
            "canonical_outcomes": indication.canonical_outcomes,
        }
        # Only ever written for a UI-created indication. Its absence is what keeps a snapshot
        # of the reviewed baseline equal to the seed file.
        if indication.verification_status == STATUS_DRAFT:
            block["verification_status"] = STATUS_DRAFT
        overlay[indication.disease] = block

    return {
        "therapeutic_areas": therapeutic_areas,
        "indications": overlay,
        "drug_catalog": drug_catalog,
    }


# --- writing --------------------------------------------------------------------------
def _drug_rows(cfg: dict) -> list[TaxonomyDrug]:
    """Flatten every drug in the document into rows, numbering declaration order."""
    rows: list[TaxonomyDrug] = []
    order = 0

    # Scalar fields a document may state as an explicit null. Recorded so the distinction
    # between "declared as none" and "not declared" survives, which is what keeps a rebuilt
    # snapshot equal to the source rather than merely equivalent to it.
    nullable_fields = (
        "generic", "company", "drug_class", "administration_route",
        "evidence_depth", "biosimilar_of",
    )

    def _add(entry: dict, *, ta_key: str | None, side: str, name: str) -> None:
        nonlocal order
        declared_null = [f for f in nullable_fields if f in entry and entry[f] is None]
        rows.append(TaxonomyDrug(
            ta_key=ta_key,
            side=side,
            name=name,
            generic=entry.get("generic"),
            company=entry.get("company"),
            drug_class=entry.get("drug_class"),
            administration_route=entry.get("administration_route"),
            evidence_depth=entry.get("evidence_depth"),
            biosimilar_of=entry.get("biosimilar_of"),
            background_therapy=bool(entry.get("background_therapy", False)),
            aliases_json=_dumps(entry.get("aliases")),
            indications_json=_dumps(entry.get("indications")),
            null_fields_json=_dumps(declared_null),
            display_order=order,
        ))
        order += 1

    for ta_key, block in (cfg.get("therapeutic_areas") or {}).items():
        block = block or {}
        for side, key in ((SIDE_FOCUS, "focus_brands"), (SIDE_COMPETITOR, "competitors")):
            for entry in block.get(key) or []:
                name = (entry or {}).get("name")
                if name:
                    _add(entry, ta_key=ta_key, side=side, name=name)

    for name, entry in (cfg.get("drug_catalog") or {}).items():
        _add(entry or {}, ta_key=None, side=SIDE_CATALOG, name=name)

    return rows


async def import_document(db: AsyncSession, cfg: dict, *, commit: bool = True) -> dict:
    """Replace the stored taxonomy with the contents of a parsed document.

    Wholesale replacement rather than a merge. The document is the complete statement of the
    taxonomy, and a merge would leave a drug someone deliberately deleted alive in the table
    with no way to tell it apart from one they had not added yet.
    """
    for table in _TABLES:
        await db.execute(delete(table))

    for order, (ta_key, block) in enumerate((cfg.get("therapeutic_areas") or {}).items()):
        block = block or {}
        db.add(TaxonomyAreaBlock(
            ta_key=ta_key, area=block.get("area") or ta_key, display_order=order,
        ))

    for row in _drug_rows(cfg):
        db.add(row)

    membership_order = 0
    for order, (disease, block) in enumerate((cfg.get("indications") or {}).items()):
        block = block or {}
        db.add(TaxonomyIndication(
            disease=disease,
            area=block.get("area"),
            therapeutic_area_key=block.get("therapeutic_area_key"),
            aliases_json=_dumps(block.get("aliases")),
            canonical_outcomes_json=_dumps(block.get("canonical_outcomes")),
            verification_status=block.get("verification_status") or "VERIFIED",
            display_order=order,
        ))
        for role, key in ((ROLE_BRAND, "brands"), (ROLE_COMPETITOR, "competitors")):
            for name in block.get(key) or []:
                db.add(TaxonomyIndicationDrug(
                    disease=disease, drug_name=name, role=role,
                    display_order=membership_order,
                ))
                membership_order += 1

    if commit:
        await db.commit()
    else:
        await db.flush()

    return {
        "therapeutic_areas": len(cfg.get("therapeutic_areas") or {}),
        "indications": len(cfg.get("indications") or {}),
        "drug_catalog": len(cfg.get("drug_catalog") or {}),
    }


def seed_document() -> dict:
    """The reviewed baseline, parsed. A bootstrap fixture — never read to serve a request."""
    return load_yaml_config(taxonomy.SEED_FILENAME)


async def seed_if_empty(db: AsyncSession) -> bool:
    """Import the baseline when nothing is stored yet. Returns True when it seeded."""
    if not await is_empty(db):
        return False
    counts = await import_document(db, seed_document())
    logger.info(
        "Seeded brand taxonomy from %s (%d areas, %d indications, %d catalog drugs)",
        taxonomy.SEED_FILENAME, counts["therapeutic_areas"],
        counts["indications"], counts["drug_catalog"],
    )
    return True


_EXPORT_HEADER = """\
# Brand taxonomy, exported from the live database.
#
# NOT the file the application reads — the taxonomy is stored in SQLite so it can be edited
# at runtime and survive a redeploy. This is a point-in-time rendering, and it exists because
# retiring brands.yaml removed the only reviewable diff of what the taxonomy actually says.
# Diff it against config/seed/brands_seed.yaml to see everything that has changed since the
# reviewed baseline; copy it over that file to make the current state the new baseline.
#
# Comments do NOT survive the round trip. The curation reasoning that used to live in this
# file's comments is stored per membership row and rendered below each competitor it explains.
"""


async def export_yaml(db: AsyncSession) -> str:
    """Render the stored taxonomy as a YAML document in ``brands.yaml`` shape.

    ``sort_keys=False`` is load-bearing, not cosmetic: it keeps declaration order, which is
    what ``curation.coverage.rank()`` reads to order the gap queue. An alphabetised export
    would silently describe a different ranking from the one the application applies.
    """
    snapshot = await build_snapshot(db)
    notes = {
        (disease, drug): note
        for disease, drug, note in (await db.execute(
            select(
                TaxonomyIndicationDrug.disease,
                TaxonomyIndicationDrug.drug_name,
                TaxonomyIndicationDrug.note,
            )
            .where(TaxonomyIndicationDrug.note.is_not(None))
            .order_by(TaxonomyIndicationDrug.display_order)
        )).all()
    }

    body = yaml.safe_dump(
        snapshot, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100,
    )
    if not notes:
        return _EXPORT_HEADER + "\n" + body

    lines = [
        "",
        "# --- curation notes ------------------------------------------------------------",
        "# Why a drug is on (or off) an indication's list. Recorded, never read by any rule.",
    ]
    for (disease, drug), note in sorted(notes.items()):
        lines.append(f"#   {disease} / {drug}: {note}")
    return _EXPORT_HEADER + "\n" + body + "\n".join(lines) + "\n"


async def hydrate(db: AsyncSession) -> dict:
    """Seed if needed, then install the stored taxonomy into the config layer.

    Called once at startup, before anything reads the taxonomy, and again after any write.
    """
    await seed_if_empty(db)
    snapshot = await build_snapshot(db)
    taxonomy.install_snapshot(snapshot)
    return snapshot
