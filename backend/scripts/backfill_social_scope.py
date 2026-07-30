"""Re-tag captured social posts to the area whose seed term captured them (scope isolation).

After the switch to scope-isolated social ingests (a captured post is tagged with the area you
ingested, not the classifier's guessed indication), pre-existing posts may still carry a
cross-routed ``therapeutic_area`` -- e.g. a Lupron post captured under an Endometriosis run that
the classifier tagged "Uterine Fibroids". This one-off backfill re-tags each post by its stored
``search_term``, but ONLY for seed terms that belong to exactly one configured area.

Left untouched (by design):
  - posts whose ``therapeutic_area`` is already NULL (deemed off-topic at capture time);
  - posts whose ``search_term`` is shared across areas (e.g. "Lupron Depot", "Myfembree",
    "Humira", "Rinvoq") -- ambiguous, cannot be attributed to a single area;
  - posts whose ``search_term`` is empty or is not present in the current seed-term config.

Dry-run by default (nothing is written); pass --commit to persist.

    python -m scripts.backfill_social_scope            # preview
    python -m scripts.backfill_social_scope --commit   # apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config.settings import load_yaml_config  # noqa: E402
from app.models.database import AsyncSessionLocal, init_db  # noqa: E402
from app.models.social_post import SocialPost  # noqa: E402


def _seed_term_maps() -> tuple[dict[str, str], set[str], set[str]]:
    """Return (unambiguous term -> area, set of all seed terms, set of area names), lowercased terms.

    A term is unambiguous when it appears in exactly one area's list in
    ``config/social_sources.yaml`` under ``seed_terms_by_area``.
    """
    cfg = load_yaml_config("social_sources.yaml")
    by_area = cfg.get("seed_terms_by_area") or {}
    areas_for: dict[str, set[str]] = defaultdict(set)
    for area, terms in by_area.items():
        for t in (terms or []):
            key = str(t).strip().lower()
            if key:
                areas_for[key].add(area)
    unambiguous = {term: next(iter(areas)) for term, areas in areas_for.items() if len(areas) == 1}
    return unambiguous, set(areas_for), set(by_area)


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-tag captured social posts to the area whose (unambiguous) seed term captured them.")
    parser.add_argument("--commit", action="store_true",
                        help="Persist changes. Without this flag the script only previews.")
    parser.add_argument("--reassign-term", default=None,
                        help="A shared/ambiguous search_term to force-reassign (e.g. 'Lupron Depot').")
    parser.add_argument("--reassign-to", default=None,
                        help="Target therapeutic_area for --reassign-term (e.g. 'Endometriosis').")
    args = parser.parse_args()

    term_to_area, all_terms, area_names = _seed_term_maps()

    reassign_term = (args.reassign_term or "").strip()
    reassign_to = (args.reassign_to or "").strip()
    if bool(reassign_term) != bool(reassign_to):
        parser.error("--reassign-term and --reassign-to must be used together.")
    if reassign_to and reassign_to not in area_names:
        parser.error(f"--reassign-to must be one of the configured areas: {sorted(area_names)}")
    reassign_key = reassign_term.lower()

    await init_db()
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(SocialPost))).scalars().all()
        moves: Counter = Counter()  # (old_ta -> new_ta) transitions actually retagged
        shared_by_ta: Counter = Counter()    # ambiguous posts, keyed by current therapeutic_area
        shared_by_term: Counter = Counter()  # ambiguous posts, keyed by the shared search_term
        retagged = reassigned = left_null = left_shared = left_unmatched = 0
        for sp in rows:
            if sp.therapeutic_area is None:
                left_null += 1  # off-topic at capture; do not resurrect
                continue
            term = (sp.search_term or "").strip().lower()
            area = term_to_area.get(term)
            if area is None:
                if term and term in all_terms:
                    left_shared += 1   # term used by >1 area -> ambiguous
                    shared_by_ta[sp.therapeutic_area] += 1
                    shared_by_term[sp.search_term] += 1
                    # Optional explicit override: force a chosen shared term to a chosen area.
                    if reassign_key and term == reassign_key and sp.therapeutic_area != reassign_to:
                        moves[(sp.therapeutic_area, reassign_to)] += 1
                        reassigned += 1
                        if args.commit:
                            sp.therapeutic_area = reassign_to
                else:
                    left_unmatched += 1  # empty / legacy / unknown term -> leave as-is
                continue
            if sp.therapeutic_area != area:
                moves[(sp.therapeutic_area, area)] += 1
                retagged += 1
                if args.commit:
                    sp.therapeutic_area = area
        if args.commit:
            await db.commit()

    mode = "APPLIED" if args.commit else "DRY-RUN (no changes written; pass --commit to apply)"
    print(f"[{mode}] posts={len(rows)}  retagged={retagged}  reassigned={reassigned}  "
          f"left_null={left_null}  left_shared={left_shared}  left_unmatched={left_unmatched}")
    if moves:
        print("  transitions (old therapeutic_area -> new):")
        for (old, new), n in sorted(moves.items(), key=lambda kv: -kv[1]):
            print(f"    {old!r:<28} -> {new!r:<28} {n:>4}")
    if shared_by_ta:
        print("  shared/ambiguous posts, by current therapeutic_area (pre-reassignment):")
        for ta, n in sorted(shared_by_ta.items(), key=lambda kv: -kv[1]):
            print(f"    {ta!r:<28} {n:>4}")
        print("  shared/ambiguous posts, by search_term:")
        for term, n in sorted(shared_by_term.items(), key=lambda kv: -kv[1]):
            print(f"    {term!r:<28} {n:>4}")


if __name__ == "__main__":
    asyncio.run(main())
