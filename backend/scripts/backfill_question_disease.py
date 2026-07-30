"""Backfill ``disease`` on existing questions (and their responses) from question text.

Context
-------
``scripts/seed_questions.py`` never set ``indication`` or ``disease``, so roughly 175
seeded questions carry NULL for both. Nothing downstream could tell a Rinvoq
Rheumatoid Arthritis question from a Rinvoq Atopic Dermatitis question, which is why
therapeutic-area derivation fell back to the brand and landed the RA/PsA workshop set
under "Immunology" (see ``scripts/hotfix_rhem_therapeutic_area.py``, a one-off patch
for exactly that symptom).

Resolution uses the same disease index the live mapper uses
(``prompt_volume.mapping.map_query`` -> ``config.taxonomy.disease_index``), so a row
backfilled today matches what an identical question imported tomorrow would get. Only
NULL values are ever written: a disease already recorded by a human is never
overwritten.

Two deliberate defaults
-----------------------
* **Responses are included.** Responses denormalise ``disease`` from their question at
  run time, so backfilling questions alone would leave every historical response
  unlabelled and still incomparable across indications. Pass ``--no-responses`` to
  restrict the run to the question bank.
* **``therapeutic_area`` is NOT touched.** A stored therapeutic area is a live filter
  key on dashboards, and silently moving historical rows between filters is a bigger
  decision than filling a blank field. Pass ``--fix-therapeutic-area`` to opt in; it
  then rewrites the area only where the resolved disease disagrees with what is stored
  AND the disease is unambiguous.

Run locally (repo-root .venv, cwd backend):
    python -m scripts.backfill_question_disease                        # dry run
    python -m scripts.backfill_question_disease --commit
    python -m scripts.backfill_question_disease --fix-therapeutic-area --commit

Inside the prod container:
    docker exec -i evidence-monitoring-agent python -m scripts.backfill_question_disease
    docker exec -i evidence-monitoring-agent python -m scripts.backfill_question_disease --commit
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import taxonomy  # noqa: E402
from app.models.database import AsyncSessionLocal  # noqa: E402
from app.models.question import Question, utcnow  # noqa: E402
from app.models.response import Response  # noqa: E402
from app.prompt_volume.mapping import resolve_disease  # noqa: E402
from app.utils.audit import write_audit  # noqa: E402


def _clip(text: str, n: int = 68) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "\u2026"


def _resolve(question: Question) -> str | None:
    """Disease named by a question, or ``None``.

    Reads the question text only. ``brand_focus`` is deliberately NOT used as a
    fallback: inferring an indication from a multi-indication brand is precisely the
    assumption this backfill exists to remove.
    """
    return resolve_disease(question.question_text or "")


async def main() -> None:
    ap = argparse.ArgumentParser(
        description="Backfill question.disease (and response.disease) from question text."
    )
    ap.add_argument("--commit", action="store_true", help="Write changes (default: dry run).")
    ap.add_argument(
        "--no-responses", action="store_true",
        help="Only touch the question bank; leave historical responses unlabelled.",
    )
    ap.add_argument(
        "--fix-therapeutic-area", action="store_true",
        help="Also correct therapeutic_area where the resolved disease disagrees with it.",
    )
    args = ap.parse_args()

    async with AsyncSessionLocal() as db:
        questions = list((await db.execute(
            select(Question).where(
                Question.deleted_at.is_(None), Question.superseded_by.is_(None)
            )
        )).scalars().all())

        resolved: dict[str, str] = {}          # question_id -> disease (all matches)
        disease_changes: list[tuple[Question, str]] = []
        area_changes: list[tuple[Question, str, str]] = []   # (q, disease, new_area)
        unresolved: list[Question] = []

        for q in questions:
            disease = _resolve(q)
            if not disease:
                if not q.disease:
                    unresolved.append(q)
                continue
            resolved[q.question_id] = disease

            if not q.disease:
                disease_changes.append((q, disease))

            if args.fix_therapeutic_area:
                correct_area = taxonomy.therapeutic_area_key_for_disease(disease)
                if correct_area and correct_area != q.therapeutic_area:
                    area_changes.append((q, disease, correct_area))

        mode = "COMMIT" if args.commit else "DRY RUN"
        print(f"Question disease backfill  [{mode}]")
        print(f"active questions scanned : {len(questions)}")
        print(f"disease resolvable       : {len(resolved)}")
        print(f"already labelled         : {sum(1 for q in questions if q.disease)}")
        print(f"blank -> will be set     : {len(disease_changes)}")
        print(f"no disease in text       : {len(unresolved)}  (left NULL, never guessed)")

        by_disease: dict[str, int] = {}
        for _q, disease in disease_changes:
            by_disease[disease] = by_disease.get(disease, 0) + 1
        if by_disease:
            print("\n--- new labels by indication ---")
            for disease, count in sorted(by_disease.items(), key=lambda kv: (-kv[1], kv[0])):
                area = taxonomy.therapeutic_area_key_for_disease(disease) or "?"
                print(f"  {count:5}  {disease}  [{area}]")

        if disease_changes:
            print(f"\n--- questions to label ({len(disease_changes)}) ---")
            for q, disease in disease_changes[:40]:
                print(f"  {q.question_id:14} -> {disease:42} | {_clip(q.question_text)}")
            if len(disease_changes) > 40:
                print(f"  ... and {len(disease_changes) - 40} more")

        if args.fix_therapeutic_area:
            print(f"\n--- therapeutic_area corrections ({len(area_changes)}) ---")
            for q, disease, area in area_changes[:40]:
                print(f"  {q.question_id:14} {q.therapeutic_area!r} -> {area!r}  ({disease})")
            if len(area_changes) > 40:
                print(f"  ... and {len(area_changes) - 40} more")

        # Responses denormalise disease from their question; fill only the blanks.
        response_changes: list[tuple[Response, str]] = []
        if not args.no_responses and resolved:
            responses = list((await db.execute(
                select(Response).where(
                    Response.disease.is_(None),
                    Response.question_id.in_(list(resolved)),
                )
            )).scalars().all())
            response_changes = [
                (r, resolved[r.question_id]) for r in responses if r.question_id in resolved
            ]
            print(f"\nresponses to label       : {len(response_changes)}")

        if not args.commit:
            print("\nDRY RUN - nothing written. Re-run with --commit to apply.")
            return
        if not (disease_changes or area_changes or response_changes):
            print("\nNothing to change - already correct.")
            return

        for q, disease in disease_changes:
            q.disease = disease
            q.updated_at = utcnow()
            try:
                await write_audit(
                    db, role="OPERATOR", event="QUESTION_DISEASE_BACKFILL",
                    question_id=q.question_id,
                    context={"disease": disease, "source": "question_text"},
                    commit=False,
                )
            except Exception as e:  # noqa: BLE001 — audit is best-effort, never block the fix
                print(f"warn: audit skipped for {q.question_id} - {e}")

        for q, disease, area in area_changes:
            before = q.therapeutic_area
            q.therapeutic_area = area
            q.updated_at = utcnow()
            try:
                await write_audit(
                    db, role="OPERATOR", event="THERAPEUTIC_AREA_BACKFILL",
                    question_id=q.question_id,
                    context={"from": before, "to": area, "disease": disease},
                    commit=False,
                )
            except Exception as e:  # noqa: BLE001
                print(f"warn: audit skipped for {q.question_id} - {e}")

        for r, disease in response_changes:
            r.disease = disease

        await db.commit()
        print(
            f"\nAPPLIED - {len(disease_changes)} question label(s), "
            f"{len(area_changes)} area correction(s), "
            f"{len(response_changes)} response label(s)."
        )


if __name__ == "__main__":
    asyncio.run(main())
