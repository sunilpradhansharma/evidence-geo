"""Verify every study and drug label in one pass, so the evidence surfaces have numbers.

Why this exists
---------------
``gather_evidence`` skips an unverified study **even in EXPLORATORY mode**, and question
generation plus every approval, safety and mechanism claim read **verified labels only**. So
a freshly ingested corpus renders as a complete UI with nothing in it: networks, comparisons
and synthesis are all wired and all empty, and nothing on screen says the cause is a curation
queue nobody has walked.

This walks it. It is the bulk, mechanical half of review — *does this extraction reproduce
from the document we retained?* — and deliberately **not** the judgement half. Protocol
approval and network ratification are decisions about method and about whether an assembled
evidence set is fit to compute on; they belong to a person clicking a button on the Governance
page, not to a loop in a script. This script will not touch them.

What a name here is, and is not
-------------------------------
``verified_by`` is **recorded, not authenticated** — there is no RBAC in this tree. It is a
governance record rather than an access control, and a bulk pass is exactly when that is worth
saying out loud: one command is about to sign for the whole corpus.

The default name is therefore the dev pilot marker rather than yours, for two reasons:

* ``reparse_dev_pilot.py`` refuses to run once **any** study carries a verifier other than
  that marker — it reads a real name as a real signature and stops, which is correct. Keeping
  the marker keeps that re-parse hatch open; ``--as "Your Name"`` closes it for good.
* ``VERIFIED`` has no outbound edge in the verification lifecycle, so there is no undo beyond
  that dev reset. The honest default is the name that says nobody actually looked.

Refusals are the interesting output, not errors
-----------------------------------------------
``record_curator_check`` refuses a study whose stored rows no longer match a fresh parse of
its retained payload, because a ``VERIFIED`` row is skipped by ``ingest_study`` and certifying
a stale one puts it beyond the reach of the ordinary re-parse. Each refusal is printed
individually: it names a study that needs ``reparse_stored_payloads`` first.

A dry run runs the full check and writes nothing
------------------------------------------------
Both curation services accept ``commit=False``, so the reproducibility comparison genuinely
executes and the session is rolled back at the end. Unlike ``ingest_evidence --verify-as``,
a dry run here is not a guess about what would happen — it is what happened, discarded.

Run (repo-root .venv, cwd backend)::

    python -m scripts.verify_all_evidence --indication "Psoriatic Arthritis"
    python -m scripts.verify_all_evidence --indication "Psoriatic Arthritis" --commit

Then open Clinical Evidence -> Governance to record the two protocol approvals and walk the
network through medical and statistical review.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.evidence import lifecycles  # noqa: E402
from app.models.database import AsyncSessionLocal, init_db  # noqa: E402
from app.models.evidence_network import EvidenceNetwork  # noqa: E402
from app.services import drug_fact_curation_service as fact_curation  # noqa: E402
from app.services import evidence_review_service as review  # noqa: E402
from app.services import study_curation_service as curation  # noqa: E402

# Imported rather than restated: this is the exact string reparse_dev_pilot's guard compares
# against, and a second copy of it would let the two drift apart silently — which would break
# the one escape hatch this script's default exists to preserve.
from scripts.reparse_dev_pilot import DEFAULT_MARKER  # noqa: E402

# A decided row is not backlog. VERIFIED has no outbound edge and a REJECTED study was
# deliberately refused, so neither is something a bulk pass should reconsider.
DECIDED = (lifecycles.VERIFIED, lifecycles.REJECTED)


def _clip(text: str, n: int = 96) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "\u2026"


async def _verify_studies(
    db: AsyncSession, *, indication: str | None, name: str, note: str | None, limit: int
) -> tuple[int, int]:
    """Attempt a curator check on every undecided study. Returns (verified, refused)."""
    queue = await curation.curation_queue(db, indication=indication, limit=limit)
    studies = queue["studies"]
    pending = [s for s in studies if s["verification_status"] not in DECIDED]

    print("=== studies ===")
    print(f"in scope             : {len(studies)}")
    for status, count in queue["by_status"].items():
        print(f"  {status:18} {count}")
    print(f"already decided      : {len(studies) - len(pending)}")
    print(f"to attempt           : {len(pending)}")

    if not pending:
        print("\nNothing to verify. Every study in scope already carries a decision.")
        return 0, 0

    verified: list[str] = []
    refused: list[tuple[str, str]] = []
    for study in pending:
        try:
            await curation.record_curator_check(
                db, study_id=study["study_id"], verified_by=name, note=note, commit=False,
            )
            verified.append(study["study_id"])
        except curation.CurationError as e:
            refused.append((study["study_id"], str(e)))

    print(f"\nverified             : {len(verified)}")
    print(f"refused              : {len(refused)}")

    if refused:
        # Named individually and never truncated to a count. Each of these is a specific
        # study whose stored rows are stale, and the remedy is per-study.
        print(f"\n--- refused, and why ({len(refused)}) ---")
        for study_id, reason in refused:
            print(f"  {study_id:16} {_clip(reason)}")
        print(
            "\n  These are not failures of this script. A stale extraction must be "
            "re-parsed\n  before it can be certified:\n"
            "    python -m scripts.reparse_stored_payloads --study <ID> --commit"
        )
    return len(verified), len(refused)


async def _verify_drug_facts(
    db: AsyncSession, *, brand: str | None, name: str, note: str | None, limit: int
) -> tuple[int, int]:
    """Attempt a curator check on every undecided drug label. Returns (verified, refused)."""
    queue = await fact_curation.curation_queue(db, brand=brand, limit=limit)
    facts = queue["facts"]
    pending = [f for f in facts if f["verification_status"] not in DECIDED]

    print("\n=== drug labels ===")
    print(f"in scope             : {len(facts)}")
    for status, count in queue["by_verification_status"].items():
        print(f"  {status:18} {count}")
    print(f"to attempt           : {len(pending)}")

    # Not curator backlog: a label whose indication list was never structured cannot answer
    # an approval claim however carefully it is checked, so verifying it changes nothing.
    if queue["approval_blocked"]:
        print(
            f"approval_blocked     : {len(queue['approval_blocked'])} "
            "(no structured indications — verifying these answers no approval claim)"
        )

    if not pending:
        print("\nNothing to verify. Every label in scope already carries a decision.")
        return 0, 0

    verified: list[str] = []
    refused: list[tuple[str, str]] = []
    for fact in pending:
        try:
            await fact_curation.record_curator_check(
                db, fact_id=fact["fact_id"], verified_by=name, note=note, commit=False,
            )
            verified.append(fact["brand"])
        except curation.CurationError as e:
            refused.append((fact["brand"], str(e)))

    print(f"\nverified             : {len(verified)}")
    print(f"refused              : {len(refused)}")
    if refused:
        print(f"\n--- refused, and why ({len(refused)}) ---")
        for brand_name, reason in refused:
            print(f"  {brand_name:16} {_clip(reason)}")
    return len(verified), len(refused)


async def _print_gates(db: AsyncSession) -> None:
    """What still stands between each network and GOVERNED execution.

    Printed after the pass because verification is only one of three gates, and a run that
    verified everything can still leave every network EXPLORATORY. Saying so here is what
    stops that being discovered later from an empty synthesis page.
    """
    network_ids = sorted(
        (await db.execute(select(EvidenceNetwork.network_id))).scalars().all()
    )
    if not network_ids:
        print("\n=== governance gate ===\nNo networks built yet.")
        return

    print("\n=== governance gate (per network) ===")
    for network_id in network_ids:
        gate = await review.governance_gate(db, network_id=network_id)
        verdict = "OPEN" if gate["may_compute_governed"] else gate["blocking_status"]
        print(f"  {network_id:22} {verdict}")
        print(f"    protocol   : {gate['protocol_id']} ({gate['protocol_status']})")
        print(f"    ratified   : {gate['ratification_status']}")
        if not gate["may_compute_governed"]:
            print(f"    reason     : {_clip(gate['reason'], 84)}")


async def main() -> None:
    ap = argparse.ArgumentParser(
        description="Verify every study and drug label so the evidence surfaces render data."
    )
    ap.add_argument("--indication", default=None, help='Scope studies, e.g. "Psoriatic Arthritis".')
    ap.add_argument("--brand", default=None, help="Scope drug labels to one brand.")
    ap.add_argument(
        "--as", dest="verified_by", default=DEFAULT_MARKER, metavar="NAME",
        help=("Name recorded against every check. Recorded, NOT authenticated. Defaults to "
              "the dev pilot marker, which keeps reparse_dev_pilot.py usable afterwards."),
    )
    ap.add_argument("--note", default=None, help="What the check consisted of, in your words.")
    ap.add_argument("--skip-studies", action="store_true")
    ap.add_argument("--skip-drug-facts", action="store_true")
    ap.add_argument("--limit", type=int, default=500, help="Cap rows examined per queue.")
    ap.add_argument("--commit", action="store_true", help="Write changes (default: dry run).")
    args = ap.parse_args()

    name = (args.verified_by or "").strip()
    if not name:
        print("ERROR: --as cannot be blank; an anonymous check is not an audit trail.",
              file=sys.stderr)
        raise SystemExit(2)

    mode = "COMMIT" if args.commit else "DRY RUN"
    print(f"Bulk evidence verification  [{mode}]")
    print(f"indication : {args.indication or '(all)'}")
    print(f"recorded as: {name}")
    print("network    : none — every byte is read from the database\n")

    print("NOTE: this name is RECORDED, not authenticated. There is no RBAC in this tree, so")
    print("      the audit trail will say who claimed to check, not who provably did.")
    if name != DEFAULT_MARKER:
        # Loud, because it is irreversible and the consequence lands in a different script
        # months later, where it reads as an unexplained refusal.
        print(
            f"\nWARNING: {name!r} is not the dev pilot marker. Once committed, "
            "reparse_dev_pilot.py\n         will refuse to run — it reads a real name as a "
            "real signature. VERIFIED has no\n         outbound edge, so this is not "
            f"undoable. Use --as {DEFAULT_MARKER!r} to keep\n         that hatch open."
        )
    print()

    await init_db()
    async with AsyncSessionLocal() as db:
        try:
            studies_ok = facts_ok = 0
            if not args.skip_studies:
                studies_ok, _ = await _verify_studies(
                    db, indication=args.indication, name=name, note=args.note,
                    limit=args.limit,
                )
            if not args.skip_drug_facts:
                facts_ok, _ = await _verify_drug_facts(
                    db, brand=args.brand, name=name, note=args.note, limit=args.limit,
                )

            await db.flush()
            await _print_gates(db)

            print(
                "\nVerification is one of three gates. The other two are human judgements "
                "this\nscript deliberately does not make — record them on Clinical Evidence "
                "-> Governance:\n"
                "  1. protocol approval, MEDICAL and STATISTICAL, independently\n"
                "  2. network ratification, submit -> medical review -> statistical review"
            )
            if args.commit:
                await db.commit()
                print(
                    f"\nCommitted. {studies_ok} study/studies and {facts_ok} label(s) are "
                    "now VERIFIED."
                )
            else:
                await db.rollback()
                print(
                    f"\nDRY RUN - rolled back, nothing written. {studies_ok} study/studies "
                    f"and {facts_ok} label(s)\nwould be verified. The reproducibility check "
                    "above really ran; only the result was\ndiscarded. Re-run with --commit "
                    "to apply.\n(init_db did create any missing tables; that is schema, not "
                    "data, and is not rolled back.)"
                )
        except Exception:
            await db.rollback()
            raise


if __name__ == "__main__":
    asyncio.run(main())
