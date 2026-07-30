"""One-off: a small Provider-scoped monitoring run to validate EvidenceMD end-to-end.

Runs a handful of current, approved, BRAND-mode Provider questions through the normal
pipeline (execute_run -> scoring -> Source Authority classification -> insights), so the
Provider-only clinical target EvidenceMD produces real answers + peer-reviewed citations.
Then runs the Source Authority straggler sweep so any older unclassified citations (e.g.
the pre-existing EvidenceMD responses) get classified too.

Small by construction: it selects a limited set of question_ids (default 3) rather than the
whole Provider slice. Persona routing still gates targets per question, so only Provider
questions ever reach EvidenceMD.

Usage (cwd = backend, project .venv):
    python -m scripts.provider_test_run                  # 3 Provider questions + sweep
    python -m scripts.provider_test_run --limit 2        # fewer questions
    python -m scripts.provider_test_run --dry-run        # connectivity health-check only
    python -m scripts.provider_test_run --no-sweep       # skip the straggler sweep
    python -m scripts.provider_test_run --question-ids Q-0027 Q-0028
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.models.database import AsyncSessionLocal, init_db  # noqa: E402
from app.models.question import Question  # noqa: E402
from app.models.response import Response  # noqa: E402
from app.schemas import RunCreate  # noqa: E402
from app.services import run_service  # noqa: E402

_EMPTY = ("", "[]", "null")


async def _pick_provider_question_ids(db, limit: int) -> list[str]:
    """The first `limit` current, approved, BRAND-mode Provider questions."""
    stmt = (
        select(Question.question_id)
        .where(
            Question.persona == "Provider",
            Question.approval_status == "APPROVED",
            Question.active.is_(True),
            Question.deleted_at.is_(None),
            Question.superseded_by.is_(None),
            Question.monitoring_mode == "BRAND",
        )
        .order_by(Question.question_id)
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


async def _print_run_summary(db, run_id: str) -> None:
    counts = dict(
        (await db.execute(
            select(Response.llm_name, func.count())
            .where(Response.run_id == run_id)
            .group_by(Response.llm_name)
        )).all()
    )
    print(f"  responses by target : {counts}")

    rows = (await db.execute(
        select(Response.llm_name, Response.sources).where(Response.run_id == run_id)
    )).all()
    with_sources: Counter = Counter()
    for llm, sources in rows:
        if (sources or "").strip() not in _EMPTY:
            with_sources[llm] += 1
    print(f"  with citations      : {dict(with_sources)}")

    ev = (await db.execute(
        select(Response.sources).where(
            Response.run_id == run_id, Response.llm_name == "evidencemd"
        )
    )).scalars().all()
    for i, raw in enumerate(ev):
        try:
            urls = [c.get("url") for c in json.loads(raw or "[]") if isinstance(c, dict)]
        except Exception:  # noqa: BLE001
            urls = []
        print(f"  evidencemd[{i}]      : {len(urls)} citation(s) {urls[:5]}")


async def main() -> None:
    ap = argparse.ArgumentParser(description="Small Provider-scoped run to validate EvidenceMD.")
    ap.add_argument("--limit", type=int, default=3, help="Number of Provider questions (default 3).")
    ap.add_argument("--question-ids", nargs="*", default=None, help="Explicit question_ids (overrides --limit).")
    ap.add_argument("--dry-run", action="store_true", help="Connectivity health-check only, no writes.")
    ap.add_argument("--no-sweep", action="store_true", help="Skip the Source Authority straggler sweep.")
    args = ap.parse_args()

    await init_db()

    async with AsyncSessionLocal() as db:
        ids = args.question_ids or await _pick_provider_question_ids(db, args.limit)
    if not ids:
        print("No matching Provider questions found — nothing to run.")
        return

    print(f"Provider test run over {len(ids)} question(s): {ids}  (dry_run={args.dry_run})")
    data = RunCreate(
        trigger="ADHOC", monitoring_mode="BRAND", question_ids=list(ids), dry_run=args.dry_run
    )

    async with AsyncSessionLocal() as db:
        run = await run_service.create_run(db, data)
        run_id = run.run_id
    print(f"Created run {run_id} — executing (targets fire concurrently; may take a minute)...")

    await run_service.run_in_background(run_id, data)

    async with AsyncSessionLocal() as db:
        run = await run_service.get_run(db, run_id)
        status = getattr(run, "status", "?")
        tokens = getattr(run, "total_tokens", "?")
        print(f"Run {run_id} finished: status={status} tokens={tokens}")
        if not args.dry_run:
            await _print_run_summary(db, run_id)

    if args.dry_run or args.no_sweep:
        return

    from app.source_authority import service as sa_svc

    async with AsyncSessionLocal() as db:
        result = await sa_svc.classify_unclassified_sweep(db, limit=500)
    print(f"Source Authority sweep: {result}")


if __name__ == "__main__":
    asyncio.run(main())
