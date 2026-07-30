"""Clear the EVIDENCE stamp from questions that were never evidence-generated.

``harvest_service.promote`` used to read ANY ``evidence_payload`` as a Phase-7 evidence
proposal. Curation stages the coverage cell it filled into that same column, so every
promoted curation candidate was stamped ``generation_method="EVIDENCE"`` while creating
zero ``QuestionEvidence`` rows — and the Phase-7 approval invariant then refused the
approval forever, with no evidence anyone could verify to unblock it. The Approve button
appeared to do nothing.

The promote path is fixed; this repairs the rows it already wrote. A question is only
touched when all three hold, so nothing that IS evidence-backed can be downgraded:

  * it is stamped ``EVIDENCE``
  * the harvested row it was promoted from was NOT staged by the evidence programme
  * it has no ``QuestionEvidence`` associations at all

Idempotent — a second run finds nothing. Approval status is left exactly as it is: this
unblocks the reviewer's decision, it does not make it.

Run locally (from backend/):
    python -m scripts.repair_curation_question_stamps
Preview only (no writes):
    python -m scripts.repair_curation_question_stamps --dry-run
Inside the prod container:
    docker exec -i evidence-monitoring-agent python -m scripts.repair_curation_question_stamps
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.models.database import AsyncSessionLocal, init_db  # noqa: E402
from app.models.harvested_question import HarvestedQuestion  # noqa: E402
from app.models.question import Question  # noqa: E402
from app.models.question_evidence import QuestionEvidence  # noqa: E402
from app.services import evidence_question_service as eqs  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser(description="Repair mis-stamped evidence questions.")
    ap.add_argument("--dry-run", action="store_true", help="Report only, write nothing.")
    args = ap.parse_args()

    await init_db()
    async with AsyncSessionLocal() as db:
        stamped = (await db.execute(
            select(Question).where(
                Question.generation_method == eqs.GENERATION_METHOD,
                Question.deleted_at.is_(None),
                Question.superseded_by.is_(None),
            )
        )).scalars().all()

        staged_source = dict((await db.execute(
            select(HarvestedQuestion.promoted_question_id, HarvestedQuestion.source).where(
                HarvestedQuestion.promoted_question_id.is_not(None)
            )
        )).all())

        link_counts = dict((await db.execute(
            select(QuestionEvidence.question_id, func.count())
            .group_by(QuestionEvidence.question_id)
        )).all())

        print(f"questions stamped {eqs.GENERATION_METHOD}: {len(stamped)}"
              f"{'  (DRY RUN)' if args.dry_run else ''}")

        repaired, kept = 0, 0
        for q in stamped:
            source = staged_source.get(q.question_id)
            links = link_counts.get(q.question_id, 0)
            if source == eqs.SOURCE or links:
                kept += 1
                continue
            print(f"  clearing stamp: {q.question_id}  staged_by={source or 'unknown'}  "
                  f"status={q.approval_status}  {q.question_text[:60]!r}")
            repaired += 1
            if not args.dry_run:
                q.generation_method = None

        if repaired and not args.dry_run:
            await db.commit()

        print(f"repaired: {repaired}  |  left as evidence-generated: {kept}")


if __name__ == "__main__":
    asyncio.run(main())
