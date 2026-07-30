"""Finalize runs left parked in AWAITING_OPENEVIDENCE.

EvidenceMD (an automated OpenAI-compatible clinical LLM) replaced the manual OpenEvidence
capture step: the orchestrator no longer parks Provider ad-hoc runs at
AWAITING_OPENEVIDENCE, and the OpenEvidence capture UI is hidden. Any runs that were
already parked BEFORE that change would otherwise stay stuck forever (no UI to un-park
them). This one-off closes them through the existing
``openevidence_service.finalize_without_oe`` escape hatch: it computes Provider consensus
from whatever automated-target responses exist (Claude/Nova/Llama/Gemini/GPT-4o +
EvidenceMD, and any OpenEvidence answers already captured) for every still-pending
question, then marks the run COMPLETED.

Idempotent: only touches runs whose status is still AWAITING_OPENEVIDENCE.

NOTE: a real (non --dry-run) execution re-arbitrates consensus, which calls the Chairman
model on Amazon Bedrock — so it needs AWS credentials and incurs a small LLM cost per
still-pending Provider question.

Run locally (repo-root .venv, cwd backend):
    python -m scripts.finalize_awaiting_oe_runs --dry-run
    python -m scripts.finalize_awaiting_oe_runs
Inside the prod container:
    docker exec -i evidence-monitoring-agent python -m scripts.finalize_awaiting_oe_runs --dry-run
    docker exec -i evidence-monitoring-agent python -m scripts.finalize_awaiting_oe_runs
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402

from app.models.database import AsyncSessionLocal, init_db  # noqa: E402
from app.models.run import Run  # noqa: E402
from app.services import openevidence_service as oe_svc  # noqa: E402

AWAITING = oe_svc.AWAITING_STATUS


async def main() -> None:
    ap = argparse.ArgumentParser(description="Finalize runs stuck in AWAITING_OPENEVIDENCE.")
    ap.add_argument("--dry-run", action="store_true", help="List parked runs, write nothing.")
    ap.add_argument("--limit", type=int, default=0, help="Cap runs processed (0 = all).")
    args = ap.parse_args()

    await init_db()
    async with AsyncSessionLocal() as db:
        runs = (await db.execute(
            select(Run).where(Run.status == AWAITING).order_by(Run.started_at)
        )).scalars().all()
        parked = [(r.run_id, r.trigger or "?", str(r.started_at)) for r in runs]
    if args.limit:
        parked = parked[:args.limit]

    print(f"runs awaiting OpenEvidence: {len(parked)}{'  (DRY RUN)' if args.dry_run else ''}")
    for rid, trig, started in parked:
        print(f"  {rid[:8]}  {trig:8}  started {started}")
    if args.dry_run or not parked:
        return

    print("-" * 60)
    finalized = failed = 0
    for rid, _, _ in parked:
        async with AsyncSessionLocal() as db:
            try:
                res = await oe_svc.finalize_without_oe(db, rid)
                finalized += 1
                print(f"  {rid[:8]}  -> {res['status']} ({res['questions_finalized']} question(s) finalized)")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"  ! {rid[:8]} failed: {e}")

    print("-" * 60)
    print(f"finalized: {finalized}  failed: {failed}")


if __name__ == "__main__":
    asyncio.run(main())
