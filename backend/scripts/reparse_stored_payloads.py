"""Re-extract stored studies from their own retained payloads. No network call.

A **stale parse is a defect in our code, not in the source.** Re-harvesting to pick up a
parser fix changes both variables at once — afterwards a moved estimate could be the fix or
an updated registry record, and nobody can tell which. This re-reads the bytes already on
record, so the delta is attributable to the code change and nothing else. Provenance is
preserved rather than re-created: the payload row is reused because its checksum still
matches, so re-parsing does not mint a second document for the same fetch.

Use this after any change to ``sources/clinicaltrials.py``, ``evidence/treatments.py`` or
``evidence/endpoints.py``. ``canonical_outcome_id`` is assigned at parse time, so improving
the endpoint matcher has no effect on stored rows until they are re-parsed.

Before and after counts are printed for the four things a parse can silently get wrong:
orphaned outcome rows, arms with no randomised denominator, rows carrying a canonical
endpoint, and the arm count. **A re-parse that changes nothing is a result worth seeing**,
and so is one that moves a number nobody expected to move.

It has no privileged access to a decided row: a VERIFIED or REJECTED study is reported
SKIPPED, exactly as a re-harvest would be. Freeing one is a separate, audited act — see
``scripts/reset_dev_verification.py``.

Run (repo-root .venv, cwd backend):
    python -m scripts.reparse_stored_payloads --indication "Psoriatic Arthritis"
    python -m scripts.reparse_stored_payloads --indication "Psoriatic Arthritis" --commit \
        --verify-as "DEV PILOT - extractions not reviewed" \
        --rebuild-network PSA_ACR50_W16 --protocol PSA_ACR50_W16_PRIMARY
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.models.clinical_study import ClinicalStudy, OutcomeResult, StudyArm  # noqa: E402
from app.models.database import AsyncSessionLocal, init_db  # noqa: E402
from app.models.source_payload import SourcePayload  # noqa: E402
from app.services import evidence_ingestion_service as ingest  # noqa: E402
from app.services import network_builder_service as builder  # noqa: E402


def _clip(text: str, n: int = 88) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "\u2026"


async def _snapshot(db: AsyncSession, study_ids: list[str]) -> dict[str, int]:
    """The counts a parser fix is supposed to move.

    ``arm_id IS NULL`` is legal for a contrast-level row, so orphans are counted only among
    arm-level rows — otherwise a legitimate contrast would read as a defect and mask the
    real number.
    """
    async def one(stmt) -> int:
        return int((await db.execute(stmt)).scalar() or 0)

    in_scope = OutcomeResult.study_id.in_(study_ids)
    return {
        "studies": len(study_ids),
        "arms": await one(
            select(func.count()).select_from(StudyArm)
            .where(StudyArm.study_id.in_(study_ids))
        ),
        "arms_without_n": await one(
            select(func.count()).select_from(StudyArm)
            .where(StudyArm.study_id.in_(study_ids), StudyArm.sample_size.is_(None))
        ),
        "outcome_rows": await one(
            select(func.count()).select_from(OutcomeResult).where(in_scope)
        ),
        "orphan_rows": await one(
            select(func.count()).select_from(OutcomeResult).where(
                in_scope,
                OutcomeResult.arm_id.is_(None),
                OutcomeResult.outcome_type != "contrast",
            )
        ),
        "rows_with_canonical_endpoint": await one(
            select(func.count()).select_from(OutcomeResult).where(
                in_scope, OutcomeResult.canonical_outcome_id.is_not(None)
            )
        ),
        "payload_rows": await one(
            select(func.count()).select_from(SourcePayload)
        ),
    }


def _print_delta(before: dict[str, int], after: dict[str, int]) -> None:
    print("\n--- corpus before / after ---")
    for key in before:
        was, now = before[key], after[key]
        arrow = "  (unchanged)" if was == now else f"   {now - was:+d}"
        print(f"  {key:30} {was:8} -> {now:8}{arrow}")


async def main() -> None:
    ap = argparse.ArgumentParser(
        description="Re-parse stored studies from their retained payloads, offline."
    )
    ap.add_argument("--indication", default=None, help='Scope to one indication.')
    ap.add_argument("--study", nargs="*", default=None, help="Scope to specific study ids.")
    ap.add_argument(
        "--verify-as", default=None, metavar="NAME",
        help="Re-verify successfully re-parsed studies, recording NAME. Recorded, NOT "
             "authenticated. Requires --commit.",
    )
    ap.add_argument(
        "--rebuild-network", default=None, metavar="OUTCOME",
        help="Rebuild the network for this canonical outcome afterwards. A network's stored "
             "topology is computed from the arms, so it is stale until this runs.",
    )
    ap.add_argument("--protocol", default=None, help="Analysis protocol id for the network.")
    ap.add_argument("--phase", default="PRIMARY", help="PRIMARY | INDUCTION | MAINTENANCE.")
    ap.add_argument("--stratum", default=None, help="Population stratum, e.g. BIO_NAIVE.")
    ap.add_argument("--commit", action="store_true", help="Write changes (default: dry run).")
    args = ap.parse_args()

    if args.rebuild_network and not args.indication:
        print("ERROR: --rebuild-network needs --indication.", file=sys.stderr)
        raise SystemExit(2)

    mode = "COMMIT" if args.commit else "DRY RUN"
    print(f"Re-parse stored payloads  [{mode}]")
    print(f"indication : {args.indication or '(all)'}")
    print("network    : none — every byte is read from the database\n")

    await init_db()
    async with AsyncSessionLocal() as db:
        query = select(ClinicalStudy.study_id)
        if args.indication:
            query = query.where(ClinicalStudy.indication == args.indication)
        if args.study:
            query = query.where(ClinicalStudy.study_id.in_(list(args.study)))
        study_ids = sorted((await db.execute(query)).scalars().all())

        if not study_ids:
            print("No stored studies match that scope.")
            return

        before = await _snapshot(db, study_ids)
        results = await ingest.reparse_studies(
            db, indication=args.indication, study_ids=args.study, commit=False
        )
        await db.flush()
        after = await _snapshot(db, study_ids)

        by_action: dict[str, int] = {}
        for r in results:
            by_action[r.action] = by_action.get(r.action, 0) + 1
        print(f"studies in scope     : {len(study_ids)}")
        for action, count in sorted(by_action.items()):
            print(f"  {action:20} {count}")

        skipped = [r for r in results if r.action == "SKIPPED"]
        if skipped:
            print(f"\n--- skipped ({len(skipped)}) ---")
            for r in skipped:
                print(f"  {r.study_id:16} {_clip(r.reason or '')}")

        flagged = [r for r in results if r.warnings]
        if flagged:
            print(f"\n--- re-parse warnings ({len(flagged)} studies) ---")
            for r in flagged[:15]:
                print(f"  {r.study_id}")
                for warning in r.warnings[:3]:
                    print(f"      {_clip(warning)}")
                if len(r.warnings) > 3:
                    print(f"      ... and {len(r.warnings) - 3} more for this study")
            # A count in the header and a shorter list beneath it reads as the whole list.
            # It is not, and a reader who counts the lines draws a wrong conclusion.
            if len(flagged) > 15:
                print(f"  ... and {len(flagged) - 15} more study(ies) not shown")

        _print_delta(before, after)
        if before == after and not skipped:
            print("\n  Nothing moved. Either the parse was already current, or the change "
                  "does not affect this corpus.")

        # Verification is an assertion, so a dry run does not simulate one — the same stance
        # ingest_evidence.py takes. verify_study commits, which would also defeat the
        # rollback below.
        if args.verify_as and not args.commit:
            print(f"\nDRY RUN: not verifying as {args.verify_as!r}. Recording a reviewer's "
                  "name is an assertion, not a simulation.")
        elif args.verify_as:
            eligible = [r for r in results if r.action in ("INGESTED", "UPDATED")]
            print(f"\nVerifying {len(eligible)} study/studies as {args.verify_as!r}.")
            print("NOTE: this name is RECORDED, not authenticated — there is no RBAC here.")
            for r in eligible:
                try:
                    await ingest.verify_study(db, r.study_id, verified_by=args.verify_as)
                except ingest.IngestionError as e:
                    print(f"  warn: {r.study_id} not verified - {e}")

        if args.rebuild_network:
            build = await builder.build_network(
                db,
                indication=args.indication,
                canonical_outcome_id=args.rebuild_network,
                treatment_phase=args.phase,
                population_stratum=args.stratum,
                protocol_id=args.protocol,
                commit=bool(args.commit),
            )
            graph = build.topology_summary
            print(f"\n=== network {build.network_id} ===")
            print(f"studies proposed     : {len(build.proposed_studies)}")
            print(f"nodes                : {graph.get('node_count')}  {graph.get('nodes')}")
            print(f"edges                : {graph.get('edge_count')}")
            print(f"connected            : {graph.get('is_connected')}")
            print(f"loops (independent)  : {graph.get('independent_loop_count')}")
            scope = build.protocol_scope
            if scope is not None:
                scoped = scope.topology_summary
                print(f"under {scope.protocol_id} (weeks "
                      f"{scope.approved_time_window[0]:g}-{scope.approved_time_window[1]:g})"
                      f" : {scoped.get('node_count')} nodes  {scoped.get('nodes')}")
                if scope.nodes_lost:
                    print(f"  nodes the protocol excludes: {', '.join(scope.nodes_lost)}")

        if args.commit:
            await db.commit()
            print("\nCommitted.")
        else:
            await db.rollback()
            print("\nDRY RUN - rolled back, nothing written. Re-run with --commit to apply.")


if __name__ == "__main__":
    asyncio.run(main())
