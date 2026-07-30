"""Hotfix: set therapeutic_area (+ RA/PsA disease) for the Rhem.csv workshop set.

Context
-------
The RA/PsA question set from ``Rhem.csv`` was bulk-imported through "Import
prompts", which derives ``therapeutic_area`` from the focus brand when it is not
pinned (``question_service._derive_ta`` -> ``map_query``). Rinvoq/Humira/Skyrizi
map to Immunology by default, so most workshop rows landed as
``therapeutic_area="Immunology"`` instead of "Rheumatology". The "Therapeutic
Area & Indication" filter is an EXACT match on ``therapeutic_area``, so only the
few rows imported as Rheumatology appeared under that filter.

This one-off:
  * sets ``therapeutic_area="Rheumatology"`` on every workshop question (base
    rows matched by NORMALIZED text, plus their variations via
    ``variation_group_id``), and
  * sets ``disease`` from each row's designation:
        Patient/HCP RA  -> "Rheumatoid Arthritis"
        Patient/HCP PsA -> "Psoriatic Arthritis"
        HCP RA & PsA    -> None  (both indications; left blank)

Matching mirrors ``question_service.attach_designation`` exactly
(``pv_gap.normalize``, full-cell), so it targets precisely the workshop set and
nothing else. It is idempotent (only rows whose values actually differ are
written) and DRY-RUN by default.

Run locally (repo-root .venv, cwd backend):
    python -m scripts.hotfix_rhem_therapeutic_area            # dry run, writes nothing
    python -m scripts.hotfix_rhem_therapeutic_area --commit   # apply

Inside the prod container:
    docker exec -i evidence-monitoring-agent python -m scripts.hotfix_rhem_therapeutic_area
    docker exec -i evidence-monitoring-agent python -m scripts.hotfix_rhem_therapeutic_area --commit
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config.analyst_questions import ANALYST_QUESTION_DESIGNATIONS  # noqa: E402
from app.models.database import AsyncSessionLocal  # noqa: E402
from app.models.question import Question, utcnow  # noqa: E402
from app.prompt_volume.gap import normalize  # noqa: E402
from app.utils.audit import write_audit  # noqa: E402

THERAPEUTIC_AREA = "Rheumatology"

# designation -> disease (indication). The single "both" row is left blank (None).
DESIGNATION_DISEASE: dict[str, str | None] = {
    "Patient RA": "Rheumatoid Arthritis",
    "HCP RA": "Rheumatoid Arthritis",
    "Patient PsA": "Psoriatic Arthritis",
    "HCP PsA": "Psoriatic Arthritis",
    "HCP RA & PsA": None,
}


def _clip(text: str, n: int = 62) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "\u2026"


def _designations_by_norm() -> dict[str, str]:
    return {normalize(prompt): desig for prompt, desig in ANALYST_QUESTION_DESIGNATIONS}


async def main() -> None:
    ap = argparse.ArgumentParser(description="Set therapeutic_area + RA/PsA disease for the Rhem.csv workshop set.")
    ap.add_argument("--commit", action="store_true", help="Write changes (default: dry run).")
    args = ap.parse_args()

    desig_by_norm = _designations_by_norm()

    async with AsyncSessionLocal() as db:
        questions = list((await db.execute(
            select(Question).where(
                Question.deleted_at.is_(None), Question.superseded_by.is_(None)
            )
        )).scalars().all())

        # Base workshop rows: question_id -> designation (matched by normalized text).
        base_desig: dict[str, str] = {}
        for q in questions:
            if not q.is_variation:
                d = desig_by_norm.get(normalize(q.question_text))
                if d:
                    base_desig[q.question_id] = d

        def designation_for(q: Question) -> str | None:
            """Base rows match by normalized text; variations inherit via group link."""
            if not q.is_variation:
                return desig_by_norm.get(normalize(q.question_text))
            return base_desig.get(q.variation_group_id) or base_desig.get(q.variation_of)

        planned = []  # (q, designation, new_ta, new_disease)
        for q in questions:
            d = designation_for(q)
            if d:
                planned.append((q, d, THERAPEUTIC_AREA, DESIGNATION_DISEASE[d]))

        changes = [
            (q, d, ta, dis) for (q, d, ta, dis) in planned
            if q.therapeutic_area != ta or q.disease != dis
        ]

        mode = "COMMIT" if args.commit else "DRY RUN"
        print(f"Rhem.csv therapeutic_area / disease hotfix  [{mode}]")
        print(f"workshop rows matched: {len(planned)}  "
              f"(base {len(base_desig)} + variations {len(planned) - len(base_desig)})")

        by_desig: dict[str, int] = {}
        for d in base_desig.values():
            by_desig[d] = by_desig.get(d, 0) + 1
        print("base questions by designation (expected: Patient RA 5, HCP RA 5, "
              "HCP RA & PsA 1, HCP PsA 5, Patient PsA 5):")
        for d in ("Patient RA", "HCP RA", "HCP RA & PsA", "HCP PsA", "Patient PsA"):
            print(f"  {d:14} {by_desig.get(d, 0)}")

        base_changes = [c for c in changes if not c[0].is_variation]
        var_changes = [c for c in changes if c[0].is_variation]
        print(f"\nrows needing a change: {len(changes)}  (base {len(base_changes)}, variations {len(var_changes)})")
        print(f"\n--- base questions ({len(base_changes)}) ---")
        for q, d, ta, dis in base_changes:
            print(f"  {q.question_id:14} {d:13} ta[{q.therapeutic_area!r}->{ta!r}] "
                  f"disease[{q.disease!r}->{dis!r}] | {_clip(q.question_text)}")
        if var_changes:
            print(f"\n--- variations ({len(var_changes)}) ---")
            for q, d, ta, dis in var_changes:
                print(f"  {q.question_id:14} {d:13} ta[{q.therapeutic_area!r}->{ta!r}] "
                      f"disease[{q.disease!r}->{dis!r}] | {_clip(q.question_text)}")

        if not args.commit:
            print("\nDRY RUN - nothing written. Re-run with --commit to apply.")
            return
        if not changes:
            print("\nNothing to change - already correct.")
            return

        for q, d, ta, dis in changes:
            before = {"therapeutic_area": q.therapeutic_area, "disease": q.disease}
            q.therapeutic_area = ta
            q.disease = dis
            q.updated_at = utcnow()
            try:
                await write_audit(
                    db, role="OPERATOR", event="THERAPEUTIC_AREA_HOTFIX",
                    question_id=q.question_id,
                    context={"from": before, "to": {"therapeutic_area": ta, "disease": dis},
                             "designation": d, "source": "Rhem.csv"},
                    commit=False,
                )
            except Exception as e:  # audit is best-effort; never block the fix
                print(f"warn: audit skipped for {q.question_id} - {e}")
        await db.commit()
        print(f"\nAPPLIED - updated {len(changes)} row(s).")


if __name__ == "__main__":
    asyncio.run(main())
