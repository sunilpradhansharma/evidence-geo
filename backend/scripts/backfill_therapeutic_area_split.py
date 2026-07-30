"""Migrate the retired "Immunology" therapeutic_area to Dermatology / Gastroenterology.

Context
-------
``brands.yaml`` used to carry one flat ``Immunology`` block holding Plaque Psoriasis,
Atopic Dermatitis, Ulcerative Colitis and Crohn's Disease together. That put a skin
indication and a gut indication behind the same stored key, so a brand-less psoriasis
question fell back to a competitor list containing Entyvio. The block has been split
into ``Dermatology`` and ``Gastroenterology``, and ``Immunology`` no longer exists as a
key — this script moves the historical rows that still carry it.

How a row is resolved
---------------------
Disease first, brand last, and never a guess:

  1. an explicit ``disease`` column
  2. an ``indication`` column
  3. the first disease named in the row's own text (``resolve_disease`` — the same
     index the live mapper uses, so a row migrated today matches what an identical
     row imported tomorrow would get)
  4. the brand, but ONLY when that brand belongs to exactly one area. Humira, Skyrizi
     and Rinvoq all span three, so for them this step is skipped by design: choosing
     the first block brands.yaml declares is precisely the bug that required
     ``scripts/hotfix_rhem_therapeutic_area.py``.

Anything still unresolved is LEFT AS "Immunology" and listed in the report. A stale but
honest label is reviewable; a plausible wrong one is not. ``snowflake_views.sql`` renders
survivors as "Immunology (legacy)" so they stay visible rather than silently vanishing.

Note that step 3 can also land a row in **Rheumatology** — an old Immunology row whose
text says "rheumatoid arthritis" was always mis-filed, and the correct destination is the
one the disease names, not whichever of the two new areas we happen to be splitting into.

Area-level tables
-----------------
``preferred_sources`` and ``social_briefs`` have no disease dimension at all, so they
cannot be resolved per row and are handled separately:

* **preferred_sources** — a domain Medical Affairs designated for Immunology is
  legitimately preferred for BOTH successor specialties, so ``--fan-out-preferred-sources``
  ADDS a Dermatology and a Gastroenterology copy. The Immunology original is kept, not
  deleted: ``preferred_source_observations`` rows point at its ``pref_id``, and removing
  it would orphan historical measurements. Existing (area, domain) pairs are skipped
  rather than violating ``uq_preferred_ta_domain``.
* **social_briefs** — an LLM narrative keyed by area (the area IS the primary key).
  A narrative written about a mixed skin+gut sample cannot be split into two truthful
  ones, so this script only REPORTS them; regenerate per area from the Social Listening
  page instead.

Run locally (repo-root .venv, cwd backend):
    python -m scripts.backfill_therapeutic_area_split                     # dry run
    python -m scripts.backfill_therapeutic_area_split --commit
    python -m scripts.backfill_therapeutic_area_split --fan-out-preferred-sources --commit

Inside the prod container:
    docker exec -i evidence-monitoring-agent python -m scripts.backfill_therapeutic_area_split
    docker exec -i evidence-monitoring-agent python -m scripts.backfill_therapeutic_area_split --commit
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import taxonomy  # noqa: E402
from app.models.competitor_candidate import CompetitorCandidate  # noqa: E402
from app.models.database import AsyncSessionLocal  # noqa: E402
from app.models.harvested_question import HarvestedQuestion  # noqa: E402
from app.models.intervention import Intervention  # noqa: E402
from app.models.preferred_source import PreferredSource  # noqa: E402
from app.models.prompt_volume import PromptVolumeStaging  # noqa: E402
from app.models.prompt_volume_alert import PromptVolumeGapAlert  # noqa: E402
from app.models.question import Question  # noqa: E402
from app.models.recommendation import Recommendation  # noqa: E402
from app.models.response import Response  # noqa: E402
from app.models.response_citation import ResponseCitation  # noqa: E402
from app.models.social_brief import SocialBrief  # noqa: E402
from app.models.social_post import SocialPost  # noqa: E402
from app.prompt_volume.mapping import map_query, resolve_disease  # noqa: E402
from app.utils.audit import write_audit  # noqa: E402

LEGACY_AREA = "Immunology"
SUCCESSORS = ("Dermatology", "Gastroenterology")


@dataclass(frozen=True)
class TableSpec:
    """How to find, resolve and rewrite the legacy area on one table."""

    label: str
    model: type
    ta_attr: str = "therapeutic_area"
    disease_attr: str | None = None
    indication_attr: str | None = None
    text_attrs: tuple[str, ...] = ()
    brand_attr: str | None = None
    id_attr: str = "id"

    # Inherit the area already resolved for a parent row, keyed by this attribute. Used by
    # response_citations, which denormalise their dimensions from Response and carry no
    # text of their own — so the parent's answer is the row's answer, not an inference.
    parent_attr: str | None = None

    # True when the stored value is a CACHE of ``map_query`` rather than a curated
    # classification (prompt_volume_staging.matched_therapeutic_area). Re-deriving it
    # reproduces exactly what an ingest would write today, so it is a refresh, not a guess.
    remap_with_mapper: bool = False


# Every table carrying a therapeutic_area, in dependency-free order. ``text_attrs`` are
# scanned in order and only ever read — never written.
TABLES: tuple[TableSpec, ...] = (
    TableSpec("questions", Question, disease_attr="disease", indication_attr="indication",
              text_attrs=("question_text",), brand_attr="brand_focus", id_attr="question_id"),
    TableSpec("responses", Response, disease_attr="disease", indication_attr="indication",
              text_attrs=("question_text",), brand_attr="brand_focus", id_attr="response_id"),
    TableSpec("response_citations", ResponseCitation, indication_attr="indication",
              brand_attr="brand_focus", id_attr="citation_id", parent_attr="response_id"),
    TableSpec("recommendations", Recommendation, indication_attr="indication",
              text_attrs=("claim_text", "recommended_action"), brand_attr="brand_focus",
              id_attr="rec_id"),
    TableSpec("interventions", Intervention, indication_attr="indication",
              text_attrs=("title", "description"), brand_attr="brand_focus"),
    TableSpec("harvested_questions", HarvestedQuestion,
              text_attrs=("question_text", "source_title"), brand_attr="brand_focus"),
    TableSpec("competitor_candidates", CompetitorCandidate, indication_attr="indication",
              brand_attr="treatment", id_attr="candidate_id"),
    TableSpec("social_posts", SocialPost, text_attrs=("text",), brand_attr="brand_focus"),
    TableSpec("prompt_volume_staging", PromptVolumeStaging, ta_attr="matched_therapeutic_area",
              text_attrs=("prompt_text", "query_text"), brand_attr="matched_brand",
              remap_with_mapper=True),
    TableSpec("prompt_volume_gap_alerts", PromptVolumeGapAlert,
              text_attrs=("question", "label"), id_attr="alert_id"),
)


@dataclass
class Change:
    row_id: str
    old: str
    new: str
    how: str
    detail: str = ""


@dataclass
class TableResult:
    label: str
    scanned: int = 0
    changes: list[Change] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


def _clip(text: object, n: int = 60) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def _area_from_disease(disease: str | None) -> str | None:
    """Stored area key for a disease name, tolerating any declared alias."""
    canonical = taxonomy.canonical_disease(disease)
    return taxonomy.therapeutic_area_key_for_disease(canonical) if canonical else None


def _resolve(
    row: object, spec: TableSpec, inherited: dict[str, str] | None = None
) -> tuple[str | None, str, str]:
    """``(new_area, how, detail)``; ``new_area`` is None when the row cannot be resolved."""
    if spec.parent_attr and inherited:
        parent_id = str(getattr(row, spec.parent_attr, "") or "")
        area = inherited.get(parent_id)
        if area:
            return area, f"parent:{spec.parent_attr}", parent_id

    if spec.disease_attr:
        value = getattr(row, spec.disease_attr, None)
        area = _area_from_disease(value)
        if area:
            return area, "disease", str(value)

    if spec.indication_attr:
        value = getattr(row, spec.indication_attr, None)
        area = _area_from_disease(value)
        if area:
            return area, "indication", str(value)

    for attr in spec.text_attrs:
        disease = resolve_disease(getattr(row, attr, None) or "")
        if disease:
            area = _area_from_disease(disease)
            if area:
                return area, f"text:{attr}", disease

    # Brand last, and only when it is unambiguous. A multi-area brand tells us nothing.
    if spec.brand_attr:
        brand = getattr(row, spec.brand_attr, None)
        keys = taxonomy.area_keys_for_brand(brand)
        if len(keys) == 1:
            return keys[0], "brand", str(brand)

    # A cached mapper result is refreshed, not inferred: whatever an ingest would store
    # for this query today IS the correct value, ambiguity and all.
    if spec.remap_with_mapper:
        for attr in spec.text_attrs:
            text = getattr(row, attr, None)
            if not text:
                continue
            area = map_query(text).get("therapeutic_area")
            if area and area != LEGACY_AREA:
                return area, "remapped", _clip(text, 40)

    return None, "", ""


async def _scan_table(
    db, spec: TableSpec, inherited: dict[str, str] | None = None
) -> TableResult:
    result = TableResult(label=spec.label)
    column = getattr(spec.model, spec.ta_attr)
    rows = list((await db.execute(select(spec.model).where(column == LEGACY_AREA))).scalars().all())
    result.scanned = len(rows)

    for row in rows:
        new_area, how, detail = _resolve(row, spec, inherited)
        row_id = str(getattr(row, spec.id_attr, "?"))
        if new_area and new_area != LEGACY_AREA:
            result.changes.append(Change(row_id, LEGACY_AREA, new_area, how, detail))
        else:
            preview = next(
                (_clip(getattr(row, a, None)) for a in spec.text_attrs if getattr(row, a, None)),
                "",
            )
            result.unresolved.append(f"{row_id:<38} {preview}")
    return result


def _print_table_result(result: TableResult) -> None:
    if not result.scanned:
        return
    print(f"\n--- {result.label} ---")
    print(f"  rows on {LEGACY_AREA!r}: {result.scanned}   resolved: {len(result.changes)}   "
          f"unresolved: {len(result.unresolved)}")

    by_area: dict[str, int] = {}
    by_how: dict[str, int] = {}
    for change in result.changes:
        by_area[change.new] = by_area.get(change.new, 0) + 1
        by_how[change.how] = by_how.get(change.how, 0) + 1
    if by_area:
        print("  destination : " + ", ".join(f"{a}={n}" for a, n in sorted(by_area.items())))
        print("  resolved by : " + ", ".join(f"{h}={n}" for h, n in sorted(by_how.items())))
    for change in result.changes[:15]:
        print(f"    {change.row_id:<38} -> {change.new:<18} [{change.how}] {change.detail}")
    if len(result.changes) > 15:
        print(f"    ... and {len(result.changes) - 15} more")
    if result.unresolved:
        print(f"  LEFT AS {LEGACY_AREA!r} (no disease could be established):")
        for line in result.unresolved[:10]:
            print(f"    {line}")
        if len(result.unresolved) > 10:
            print(f"    ... and {len(result.unresolved) - 10} more")


async def _apply_table(db, spec: TableSpec, result: TableResult) -> None:
    """Rewrite the resolved rows. Re-reads by primary key so the plan and the write agree."""
    if not result.changes:
        return
    column = getattr(spec.model, spec.ta_attr)
    rows = {
        str(getattr(r, spec.id_attr)): r
        for r in (await db.execute(select(spec.model).where(column == LEGACY_AREA))).scalars().all()
    }
    for change in result.changes:
        row = rows.get(change.row_id)
        if row is None:
            continue
        setattr(row, spec.ta_attr, change.new)
        if spec.label == "questions":
            try:
                await write_audit(
                    db, role="OPERATOR", event="THERAPEUTIC_AREA_SPLIT",
                    question_id=change.row_id,
                    context={"from": change.old, "to": change.new,
                             "resolved_by": change.how, "signal": change.detail},
                    commit=False,
                )
            except Exception as e:  # noqa: BLE001 — audit is best-effort, never block the fix
                print(f"  warn: audit skipped for {change.row_id} - {e}")


async def _preferred_sources(db, *, fan_out: bool, commit: bool) -> int:
    """Report (and optionally fan out) Immunology preferred-source designations."""
    rows = list((await db.execute(
        select(PreferredSource).where(PreferredSource.therapeutic_area == LEGACY_AREA)
    )).scalars().all())
    if not rows:
        return 0

    existing = {
        (r.therapeutic_area, r.authority_domain)
        for r in (await db.execute(select(PreferredSource))).scalars().all()
    }

    print(f"\n--- preferred_sources ---")
    print(f"  rows on {LEGACY_AREA!r}: {len(rows)}")
    if not fan_out:
        print("  no action (pass --fan-out-preferred-sources to copy each into both "
              "Dermatology and Gastroenterology; the Immunology row is always kept so "
              "preferred_source_observations keep their parent).")
        return 0

    added = 0
    for row in rows:
        for area in SUCCESSORS:
            if (area, row.authority_domain) in existing:
                print(f"    skip  {area:<18} {row.authority_domain}  (already designated)")
                continue
            existing.add((area, row.authority_domain))
            added += 1
            print(f"    add   {area:<18} {row.authority_domain}")
            if commit:
                db.add(PreferredSource(
                    pref_id=str(uuid.uuid4()),
                    therapeutic_area=area,
                    authority_domain=row.authority_domain,
                    registrable_domain=row.registrable_domain,
                    note=row.note,
                    active=row.active,
                    created_by=row.created_by,
                    change_reason=f"Fanned out from the retired {LEGACY_AREA} area",
                ))
    return added


async def _social_briefs(db) -> int:
    rows = list((await db.execute(
        select(SocialBrief).where(SocialBrief.therapeutic_area == LEGACY_AREA)
    )).scalars().all())
    if not rows:
        return 0
    print(f"\n--- social_briefs ---")
    print(f"  rows on {LEGACY_AREA!r}: {len(rows)}  (therapeutic_area is the PRIMARY KEY)")
    print("  no action: a narrative written about a mixed skin+gut sample cannot be split")
    print("  into two truthful ones. Regenerate per area from the Social Listening page.")
    return len(rows)


async def main() -> None:
    ap = argparse.ArgumentParser(
        description="Migrate the retired Immunology therapeutic_area to Dermatology / Gastroenterology."
    )
    ap.add_argument("--commit", action="store_true", help="Write changes (default: dry run).")
    ap.add_argument(
        "--fan-out-preferred-sources", action="store_true",
        help="Also copy each Immunology preferred source into both successor areas.",
    )
    args = ap.parse_args()

    if LEGACY_AREA in taxonomy.keys_for_area(LEGACY_AREA):
        print(f"ABORT: {LEGACY_AREA!r} is still a live key in brands.yaml. Split it first.")
        return

    mode = "COMMIT" if args.commit else "DRY RUN"
    print(f"Therapeutic-area split backfill  [{mode}]")
    print(f"{LEGACY_AREA} -> {' / '.join(SUCCESSORS)} (or Rheumatology, when that is what "
          f"the row's disease actually says)")

    async with AsyncSessionLocal() as db:
        # Paired with its spec, so a table skipped above cannot desync the apply phase.
        results: list[tuple[TableSpec, TableResult]] = []
        # response_id -> area, filled in once responses are resolved so response_citations
        # can inherit rather than re-infer. Citations are a denormalised copy of Response,
        # so any other answer for them would be a second, independent guess.
        inherited: dict[str, str] = {}
        for spec in TABLES:
            try:
                result = await _scan_table(db, spec, inherited)
            except Exception as e:  # noqa: BLE001 — a missing table must not abort the rest
                print(f"\n--- {spec.label} ---\n  skipped: {e}")
                continue
            results.append((spec, result))
            if spec.label == "responses":
                inherited = {c.row_id: c.new for c in result.changes}

        for _spec, result in results:
            _print_table_result(result)

        total_changes = sum(len(r.changes) for _s, r in results)
        total_unresolved = sum(len(r.unresolved) for _s, r in results)
        total_scanned = sum(r.scanned for _s, r in results)

        fanned = await _preferred_sources(
            db, fan_out=args.fan_out_preferred_sources, commit=args.commit
        )
        briefs = await _social_briefs(db)

        print("\n=========================== SUMMARY ===========================")
        print(f"  rows carrying {LEGACY_AREA!r} : {total_scanned}")
        print(f"  resolvable                 : {total_changes}")
        print(f"  left as {LEGACY_AREA!r}       : {total_unresolved}")
        print(f"  preferred sources to add   : {fanned}")
        print(f"  social briefs to regenerate: {briefs}")

        if not args.commit:
            print("\nDRY RUN - nothing written. Re-run with --commit to apply.")
            return
        if not (total_changes or fanned):
            print("\nNothing to change - already migrated.")
            return

        for spec, result in results:
            await _apply_table(db, spec, result)
        await db.commit()
        print(f"\nAPPLIED - {total_changes} row(s) re-pointed, {fanned} preferred source(s) added.")
        if total_unresolved:
            print(f"NOTE: {total_unresolved} row(s) still carry {LEGACY_AREA!r}. They render as "
                  f"'Immunology (legacy)' in Snowflake and need a human decision.")


if __name__ == "__main__":
    asyncio.run(main())
