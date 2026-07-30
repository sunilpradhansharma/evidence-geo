"""Re-parse a dev pilot's studies from their retained payloads. **DEV DATABASES ONLY.**

Why this exists, and why it is uncomfortable
--------------------------------------------
The verification lifecycle gives ``VERIFIED`` no outbound edge on purpose: re-opening a
decided row rewrites something a person signed for. ``ingest_study`` enforces the same rule
by reporting a decided study as SKIPPED. Both are correct, and both mean a **parser fix
cannot reach an already-verified study** — a correction is supposed to create a new version.

That production-correct answer is the wrong one for a dev pilot whose 12 verified studies
carry ``verified_by = "DEV PILOT - extractions not reviewed"``. There is no curation record
to protect: that marker exists precisely to say nobody checked anything. It was set so a
resolve could run at all, and it is now the only thing standing between a real parser fix
(664 orphaned outcome rows, 116 arms with no N) and the only network in the database.

So this rewrites history, deliberately, in one narrow case, loudly, with an audit trail.
The versioned ``version`` / ``superseded_by`` path is still the right thing to build before
any *real* curator verifies anything — this does not replace it, it unblocks a dev corpus.

Why re-parse rather than re-harvest
-----------------------------------
Every study's raw registry JSON is retained (ClinicalTrials.gov is PUBLIC_DOMAIN), so the
whole corpus can be rebuilt with **no network call**. That is the point, not a convenience:
re-harvesting would change the parser *and* the source data in one step, and any estimate
that then moved would be unattributable. Re-parsing changes exactly one variable. Payload
checksums are unchanged, so ``_identical_payload`` reuses the existing rows and the
retained-document table does not grow.

Guards, all fatal
-----------------
* a network ``RATIFIED`` **or in either review stage** — a review in progress is as
  untouchable as a finished one, since this would change what the reviewer is mid-way
  through looking at
* any membership decided away from ``PROPOSED`` — ``INCLUDED``, ``EXCLUDED`` or the
  ``REQUIRES_REVIEW`` a human raised — all three are judgements about these studies
* a stored **computed** NMA result, because rewriting the inputs of a persisted number
  invalidates it silently and the result would still read as current
* a ``VERIFIED`` study whose ``verified_by`` is **not** the pilot marker — a real signature

Usage (dry run first, always)::

    python -m scripts.reparse_dev_pilot
    python -m scripts.reparse_dev_pilot --commit

Then rebuild the network, whose stored topology is stale until you do. Use the **offline**
rebuild — ``ingest_evidence`` re-harvests, which would undo the whole point by changing the
source data in the same step::

    python -m scripts.reparse_stored_payloads --indication "Psoriatic Arthritis" \
        --rebuild-network PSA_ACR50_W16 --protocol PSA_ACR50_W16_PRIMARY --commit
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
from app.models.clinical_study import ClinicalStudy, OutcomeResult, StudyArm  # noqa: E402
from app.models.database import AsyncSessionLocal, init_db  # noqa: E402
from app.models.evidence_network import EvidenceNetwork, NetworkMembership  # noqa: E402
from app.models.nma_result import NMAResult  # noqa: E402
from app.models.source_payload import SourcePayload  # noqa: E402
from app.services import evidence_ingestion_service as ingest  # noqa: E402
from app.utils.audit import write_audit  # noqa: E402

DEFAULT_MARKER = "DEV PILOT - extractions not reviewed"

# A network part-way through review is as untouchable as a ratified one: rewriting the rows
# underneath a review in progress changes what the reviewer is looking at, mid-look.
#
# This script had that right first, and privately. `lifecycles.FROZEN_FOR_EDIT` is now the
# one owner of the rule, so widening it reaches the builder and the membership surface too
# rather than only whoever remembers this file exists.
BLOCKING_RATIFICATION = lifecycles.FROZEN_FOR_EDIT


class GuardFailure(RuntimeError):
    """A precondition that makes rewriting history unsafe."""


async def _assert_safe(db: AsyncSession, marker: str) -> list[ClinicalStudy]:
    """Every reason not to proceed, checked before anything is written.

    Returns the studies eligible for reset. Raises on anything that would make this a
    rewrite of someone's actual decision rather than of a placeholder.
    """
    in_review = (await db.execute(
        select(EvidenceNetwork.network_id, EvidenceNetwork.ratification_status).where(
            EvidenceNetwork.ratification_status.in_(BLOCKING_RATIFICATION)
        )
    )).all()
    if in_review:
        shown = ", ".join(f"{n}={st}" for n, st in in_review[:3])
        raise GuardFailure(
            f"{len(in_review)} network(s) are ratified or under review ({shown}). A reviewer "
            "approved — or is currently reading — that evidence set; re-parsing under it "
            "would change what they signed for while it still reads as approved. Reopen the "
            "network to DRAFT first (POST /evidence-review/networks/{id}/reopen, or the "
            "Reopen button on the governance page), recording why."
        )

    # Anything other than PROPOSED is a human's mark, including REQUIRES_REVIEW: somebody
    # looked and said this one needs a decision, which is information a reset would erase.
    decided = (await db.execute(
        select(NetworkMembership.study_id, NetworkMembership.membership_status).where(
            NetworkMembership.membership_status != lifecycles.PROPOSED
        )
    )).all()
    if decided:
        shown = ", ".join(f"{s}={st}" for s, st in decided[:3])
        raise GuardFailure(
            f"{len(decided)} membership(s) carry a human decision ({shown}). "
            "Re-parsing would move the evidence under a judgement someone already made."
        )

    # A published synthesis carries no network_id, so this selects only results WE computed
    # — the ones whose inputs are the rows about to be rewritten.
    computed = (await db.execute(
        select(NMAResult.result_id).where(NMAResult.network_id.is_not(None))
    )).scalars().all()
    if computed:
        raise GuardFailure(
            f"{len(computed)} stored NMA result(s) were computed from these rows "
            f"({', '.join(computed[:3])}). Rewriting a number's inputs invalidates it while "
            "it still reads as current. Delete or supersede those results first, "
            "deliberately."
        )

    verified = (await db.execute(
        select(ClinicalStudy).where(
            ClinicalStudy.verification_status == lifecycles.VERIFIED
        )
    )).scalars().all()
    foreign = sorted({
        (s.verified_by or "(null)") for s in verified if (s.verified_by or "") != marker
    })
    if foreign:
        raise GuardFailure(
            f"{len(foreign)} verifier(s) other than the pilot marker are present: "
            f"{foreign[:3]}. At least one study carries a real signature, so this is not a "
            "dev placeholder reset. Use the versioned correction path instead."
        )

    rejected = (await db.execute(
        select(ClinicalStudy.study_id).where(
            ClinicalStudy.verification_status == lifecycles.REJECTED
        )
    )).scalars().all()
    if rejected:
        raise GuardFailure(
            f"{len(rejected)} study(ies) are REJECTED ({', '.join(rejected[:3])}). A "
            "rejection is a decision too; re-ingesting one would erase it."
        )

    return list(verified)


async def _census(db: AsyncSession) -> dict:
    """The figures this fix is supposed to move."""
    arms = (await db.execute(select(StudyArm))).scalars().all()
    rows = (await db.execute(select(OutcomeResult))).scalars().all()
    payloads = (await db.execute(select(SourcePayload.payload_id))).scalars().all()
    return {
        "studies": len((await db.execute(select(ClinicalStudy.study_id))).scalars().all()),
        "arms": len(arms),
        "arms_with_n": sum(1 for a in arms if a.sample_size),
        "outcome_rows": len(rows),
        "unattached_rows": sum(1 for r in rows if not r.arm_id),
        "payloads": len(payloads),
    }


def _show(label: str, before: dict, after: dict | None = None) -> None:
    print(f"\n--- {label} ---")
    for key in before:
        if after is None:
            print(f"  {key:16} {before[key]}")
        else:
            arrow = "" if before[key] == after[key] else "   <-- changed"
            print(f"  {key:16} {before[key]:>6} -> {after[key]:>6}{arrow}")


async def main() -> None:
    ap = argparse.ArgumentParser(
        description="Re-parse a dev pilot's studies from retained payloads (DEV ONLY)."
    )
    ap.add_argument(
        "--commit", action="store_true",
        help="Actually write. Without it this reports what it would do and rolls back.",
    )
    ap.add_argument(
        "--marker", default=DEFAULT_MARKER,
        help=f"Only reset studies verified by exactly this string. Default: {DEFAULT_MARKER!r}",
    )
    args = ap.parse_args()

    mode = "COMMIT" if args.commit else "DRY RUN (nothing will be written)"
    print("=" * 78)
    print(f"Re-parse dev pilot from retained payloads — {mode}")
    print("=" * 78)
    print(
        "This rewrites VERIFIED rows, which the lifecycle forbids for a reason.\n"
        "It is safe ONLY because the marker below says the extractions were never reviewed."
    )
    print(f"marker: {args.marker!r}")

    await init_db()
    async with AsyncSessionLocal() as db:
        try:
            eligible = await _assert_safe(db, args.marker)
        except GuardFailure as exc:
            print(f"\nABORTED — {exc}")
            sys.exit(1)

        before = await _census(db)
        _show("before", before)
        print(f"\neligible for reset: {len(eligible)} studies")
        print(f"  {', '.join(sorted(s.study_id for s in eligible))}")

        # --- 1. reset the placeholder verifications --------------------------------------
        reset_ids = sorted(s.study_id for s in eligible)
        for study in eligible:
            study.verification_status = lifecycles.EXTRACTED
            study.verified_by = None
            study.verified_at = None
            for row in study.outcomes:
                row.verification_status = lifecycles.EXTRACTED
                row.verified_by = None
                row.verified_at = None

        if reset_ids:
            await write_audit(
                db, role="OPERATOR", event="DEV_PILOT_VERIFICATION_RESET",
                context={
                    "study_ids": reset_ids,
                    "from": lifecycles.VERIFIED,
                    "to": lifecycles.EXTRACTED,
                    "marker": args.marker,
                    "reason": (
                        "dev pilot placeholder verification reset so a parser fix could reach "
                        "the corpus; no curation record existed to protect"
                    ),
                    "lifecycle_exception": "VERIFIED has no outbound edge; this bypassed it",
                },
                commit=False,
            )
        await db.flush()

        # --- 2. re-parse every study from its retained payload ---------------------------
        # Delegated rather than reimplemented. ``reparse_studies`` already handles what a
        # hand-rolled loop here got wrong: a FRAGMENT_ONLY licence that retains no document,
        # a payload that no longer parses, carrying the original ``source_type``/``url``
        # through instead of assuming ClinicalTrials.gov, and warning when current screening
        # rules would now reject a study that is already in the corpus.
        print("\nre-parsing from retained payloads (no network call)")
        outcomes = await ingest.reparse_studies(db, commit=False)
        actions: dict[str, int] = {}
        for outcome in outcomes:
            actions[outcome.action] = actions.get(outcome.action, 0) + 1
            if outcome.action == "SKIPPED":
                print(f"  SKIPPED {outcome.study_id}: {outcome.reason}")
            for warning in outcome.warnings:
                if "current screening rules" in warning:
                    print(f"  NEEDS A DECISION {outcome.study_id}: {warning}")
        print(f"  {', '.join(f'{k}={v}' for k, v in sorted(actions.items()))}")

        # --- 3. restore the placeholder verification -------------------------------------
        reverified = 0
        for study_id in reset_ids:
            try:
                # commit=False or a dry run would persist at the first call and make the
                # rollback below a no-op. That is how the first version of this script
                # wrote to the database while printing "nothing will be written".
                await ingest.verify_study(
                    db, study_id, verified_by=args.marker, commit=False
                )
                reverified += 1
            except ingest.IngestionError as exc:
                print(f"  could not re-verify {study_id}: {exc}")
        print(f"  re-verified {reverified} of {len(reset_ids)} with the same marker")

        await db.flush()
        after = await _census(db)
        _show("before -> after", before, after)

        if args.commit:
            await db.commit()
            print("\nCOMMITTED.")
            print(
                "The network's stored topology is now stale — it was built from the old "
                "parse. Rebuild it OFFLINE before trusting any resolve; ingest_evidence "
                "re-harvests, which would change the source data in the same step and make "
                "any moved estimate unattributable:\n"
                '  python -m scripts.reparse_stored_payloads --indication "Psoriatic '
                'Arthritis" --rebuild-network PSA_ACR50_W16 '
                "--protocol PSA_ACR50_W16_PRIMARY --commit"
            )
        else:
            await db.rollback()
            print("\nROLLED BACK — dry run. Re-run with --commit to apply.")


if __name__ == "__main__":
    asyncio.run(main())
