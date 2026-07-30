"""Recover the head-to-head a comparison question was written for.

``harvest_service.promote`` used to set ``disease`` only from a Phase-7 evidence proposal
and never set ``competitor_focus`` at all. A curation coverage cell knows both facts, so
every promoted comparison question lost the very pairing it was commissioned to monitor:
79 of 79 Comparative questions carry no comparator. The promote path is fixed; this
recovers what the old path already dropped.

Two sources, best first:

  * the **staged coverage cell** the question was promoted from — authoritative, this is
    the cell the generator was given
  * otherwise **the question's own text**, through the shared resolver, which applies the
    same conditions ``coverage.covers`` uses to decide the question covers that cell

Only NULL fields are filled. A question that already names a comparator is never
re-pointed at a different one, so this cannot move an answer onto a comparison a reviewer
did not approve.

**Columns are written in place, deliberately.** ``update_question`` mints a new version
and re-runs the approval invariant; this changes no question text, so version-bumping 79
rows and disturbing their approval state would be a side effect with no cause. Approval
status, text and version are left exactly as they are.

Idempotent — a second run finds nothing to do.

Run locally (from backend/):
    python -m scripts.backfill_comparative_pairs --dry-run
    python -m scripts.backfill_comparative_pairs
Inside the prod container:
    docker exec -i evidence-monitoring-agent python -m scripts.backfill_comparative_pairs --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402

from app.competitive import pairs as pairs_mod  # noqa: E402
from app.config import taxonomy  # noqa: E402
from app.models.database import AsyncSessionLocal, init_db  # noqa: E402
from app.models.harvested_question import HarvestedQuestion  # noqa: E402
from app.models.question import Question  # noqa: E402


def _cell_for(payload: str | None) -> dict | None:
    """A validated coverage cell out of a staged row's payload, or None.

    Same validation as ``harvest_service._curation_cell``: the comparator must still be a
    declared competitor of the indication. A payload written before a brands.yaml edit can
    name a pairing the taxonomy no longer has, and writing that onto a question would put a
    comparison on the board that the scorer would never grade the same way.
    """
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    disease = taxonomy.canonical_disease(parsed.get("disease"))
    competitor = (parsed.get("competitor") or "").strip()
    if not disease or not competitor:
        return None
    declared = {c.strip().lower() for c in taxonomy.competitors_for_disease(disease)}
    if competitor.lower() not in declared:
        return None
    return {"disease": disease, "competitor": competitor}


async def main() -> None:
    ap = argparse.ArgumentParser(
        description="Backfill disease + competitor_focus on comparison questions."
    )
    ap.add_argument("--dry-run", action="store_true", help="Report only, write nothing.")
    args = ap.parse_args()

    await init_db()
    async with AsyncSessionLocal() as db:
        rows = list((await db.execute(
            select(Question).where(
                Question.domain == "Comparative",
                Question.monitoring_mode == "BRAND",
                Question.competitor_focus.is_(None),
                Question.deleted_at.is_(None),
                Question.superseded_by.is_(None),
            )
        )).scalars().all())

        # The cell the question was promoted from, when it came through Discover at all.
        staged = dict((await db.execute(
            select(HarvestedQuestion.promoted_question_id, HarvestedQuestion.evidence_payload)
            .where(
                HarvestedQuestion.promoted_question_id.is_not(None),
                HarvestedQuestion.source == "curation",
            )
        )).all())

        print(f"comparison questions missing a comparator: {len(rows)}"
              f"{'  (DRY RUN)' if args.dry_run else ''}")

        filled = 0
        disease_filled = 0
        by_origin: collections.Counter = collections.Counter()
        unresolved: collections.Counter = collections.Counter()

        for q in rows:
            cell = _cell_for(staged.get(q.question_id))
            if cell:
                competitor, disease, origin = cell["competitor"], cell["disease"], "staged cell"
            else:
                res = pairs_mod.resolve(
                    q.question_text,
                    brand_focus=q.brand_focus,
                    therapeutic_area=q.therapeutic_area,
                    disease=q.disease,
                )
                if not res.resolved:
                    unresolved[res.reason] += 1
                    continue
                # A question naming several comparators covers several cells; the question
                # ROW can only record one pairing, so the text stays the source of truth
                # for the rest and the read path re-derives them. Recording the first would
                # be a silent, arbitrary narrowing of what the answer actually informs.
                if len(res.pairs) > 1:
                    unresolved["multiple_competitors_named"] += 1
                    continue
                pair = res.pairs[0]
                competitor, disease, origin = pair.competitor, pair.disease, "question text"

            by_origin[origin] += 1
            note = []
            if q.disease is None and disease:
                note.append(f"disease={disease}")
                disease_filled += 1
            note.append(f"competitor={competitor}")
            print(f"  {q.question_id}  [{origin}]  {'  '.join(note)}")
            filled += 1
            if not args.dry_run:
                q.competitor_focus = json.dumps([competitor])
                if q.disease is None and disease:
                    q.disease = disease

        if filled and not args.dry_run:
            await db.commit()

        print(f"\nfilled: {filled}  (indication also recovered on {disease_filled})")
        for origin, n in by_origin.most_common():
            print(f"  from {origin}: {n}")
        if unresolved:
            print("left alone:")
            for reason, n in unresolved.most_common():
                label = pairs_mod.REASON_LABELS.get(reason, reason)
                print(f"  {n:>4}  {reason} — {label}")


if __name__ == "__main__":
    asyncio.run(main())
