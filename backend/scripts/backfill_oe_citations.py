"""Backfill Source Authority citations for OpenEvidence answers captured without sources.

OpenEvidence answers are pasted as text ending in a numbered ``### References`` list, but
operators frequently capture them without filling the Citations field, leaving
``Response.sources`` empty so the answers never appear in the Source Authority dashboards
(the "top cited domains per model" view shows no OpenEvidence column). This one-off recovers
the provenance from each answer's own reference list (see ``app.source_authority.references``)
and runs the standard citation classification, so OpenEvidence shows up alongside the other
targets.

Idempotent: only touches ``open-evidence`` responses whose ``sources`` are still empty.

Run inside the prod container:
    docker exec -i evidence-monitoring-agent python -m scripts.backfill_oe_citations
Preview only (no writes):
    docker exec -i evidence-monitoring-agent python -m scripts.backfill_oe_citations --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402

from app.models.database import AsyncSessionLocal, init_db  # noqa: E402
from app.models.response import Response  # noqa: E402
from app.source_authority import references  # noqa: E402
from app.source_authority import service as sa_svc  # noqa: E402

OE_TARGET = "open-evidence"
_EMPTY = ("", "[]", "null")


def _needs_sources(r: Response) -> bool:
    return not r.sources or str(r.sources).strip() in _EMPTY


async def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill OpenEvidence citation provenance.")
    ap.add_argument("--dry-run", action="store_true", help="Parse + preview, write nothing.")
    ap.add_argument("--limit", type=int, default=0, help="Cap responses processed (0 = all).")
    args = ap.parse_args()

    await init_db()
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Response).where(Response.llm_name == OE_TARGET)
        )).scalars().all()
        targets = [r for r in rows if _needs_sources(r)]
        if args.limit:
            targets = targets[:args.limit]

        print(f"open-evidence responses: {len(rows)}  |  needing sources: {len(targets)}"
              f"{'  (DRY RUN)' if args.dry_run else ''}")

        domains = Counter()
        sources_written = classified = skipped_no_refs = failed = 0
        for r in targets:
            srcs = references.parse_reference_sources(r.response_text or "")
            if not srcs:
                skipped_no_refs += 1
                continue
            for s in srcs:
                domains[s["domain"] or s["url"] or "?"] += 1

            if args.dry_run:
                sources_written += 1
                preview = ", ".join(sorted({s["domain"] or "doi.org" for s in srcs}))
                print(f"  {r.response_id[:8]}  refs={len(srcs):2d}  {preview}")
                continue

            r.sources = json.dumps(srcs)
            await db.flush()
            try:
                await sa_svc.classify_response(db, r, commit=True)
                classified += 1
                sources_written += 1
            except Exception as e:  # noqa: BLE001
                await db.rollback()
                failed += 1
                print(f"  ! classify failed for {r.response_id[:8]}: {e}")

        if not args.dry_run:
            await db.commit()

    print("-" * 60)
    print(f"sources written: {sources_written}  classified: {classified}  "
          f"skipped (no refs): {skipped_no_refs}  failed: {failed}")
    print("top mapped domains:")
    for dom, n in domains.most_common(15):
        print(f"  {dom:<32} {n}")


if __name__ == "__main__":
    asyncio.run(main())
