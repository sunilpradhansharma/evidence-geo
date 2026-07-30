"""Read-only audit: which Rhem.csv "Workshop Questions" are (not) in the bank.

Replicates the Workshop Questions filter EXACTLY — a *base* question
(``is_variation`` False, not soft-deleted, not superseded) whose normalized text
(``app.prompt_volume.gap.normalize``) equals a curated prompt's normalized text —
and reports, for each of the 21 curated prompts (``ANALYST_QUESTION_DESIGNATIONS``),
whether a matching base question exists on the connected database.

For any MISSING prompt it explains WHY by checking whether the same normalized
text is instead present as a phrasing variation, a superseded (edited) version,
or a soft-deleted row — or is absent from the bank entirely.

Writes NOTHING. Safe to run against prod.

Run locally (repo-root .venv, cwd backend):
    python -m scripts.audit_workshop_questions

Inside the prod container:
    docker exec -i evidence-monitoring-agent python -m scripts.audit_workshop_questions
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config.analyst_questions import ANALYST_QUESTION_DESIGNATIONS  # noqa: E402
from app.models.database import AsyncSessionLocal  # noqa: E402
from app.models.question import Question  # noqa: E402
from app.prompt_volume.gap import normalize  # noqa: E402


def _clip(text: str, n: int = 92) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "\u2026"


def _diagnose(
    norm: str,
    var_norm: dict[str, list[Question]],
    superseded_norm: dict[str, list[Question]],
    deleted_norm: dict[str, list[Question]],
    current_by_qid: dict[str, Question],
) -> str:
    """Explain why a curated prompt has no matching *base* question."""
    if norm in var_norm:
        qids = ", ".join(q.question_id for q in var_norm[norm])
        return (
            f"present ONLY as a phrasing variation (is_variation=True) [{qids}]. "
            "The bank lists only base rows, so it is hidden. Its base question was "
            "likely dropped as a duplicate at import, edited, or deleted."
        )
    if norm in superseded_norm:
        parts = []
        for q in superseded_norm[norm]:
            cur = current_by_qid.get(q.question_id)
            cur_txt = _clip(cur.question_text, 68) if cur else "(no current version)"
            parts.append(f'{q.question_id} now reads "{cur_txt}"')
        return "the current version was EDITED so its text no longer matches — " + "; ".join(parts)
    if norm in deleted_norm:
        qids = ", ".join(q.question_id for q in deleted_norm[norm])
        return f"soft-deleted (deleted_at set) [{qids}]."
    return (
        "absent from the bank entirely — never imported, or dropped at import as a "
        "duplicate / PII / length skip."
    )


async def main() -> None:
    async with AsyncSessionLocal() as db:
        rows = list((await db.execute(select(Question))).scalars().all())

    base_norm: dict[str, list[Question]] = {}
    var_norm: dict[str, list[Question]] = {}
    superseded_norm: dict[str, list[Question]] = {}
    deleted_norm: dict[str, list[Question]] = {}
    current_by_qid: dict[str, Question] = {}
    for q in rows:
        n = normalize(q.question_text)
        if q.deleted_at is not None:
            deleted_norm.setdefault(n, []).append(q)
        elif q.superseded_by is not None:
            superseded_norm.setdefault(n, []).append(q)
        elif q.is_variation:
            var_norm.setdefault(n, []).append(q)
        else:
            base_norm.setdefault(n, []).append(q)
        if q.deleted_at is None and q.superseded_by is None:
            current_by_qid[q.question_id] = q

    total = len(ANALYST_QUESTION_DESIGNATIONS)
    matched = 0
    missing: list[tuple[str, str, str]] = []  # (designation, prompt, reason)

    print(f"Workshop Questions audit  |  {len(rows)} question rows scanned\n")
    for prompt, desig in ANALYST_QUESTION_DESIGNATIONS:
        n = normalize(prompt)
        hits = base_norm.get(n, [])
        if hits:
            matched += 1
            qids = ", ".join(q.question_id for q in hits)
            dup = "  <-- DUPLICATE base rows" if len(hits) > 1 else ""
            print(f"  ok       {desig:13} {_clip(prompt)}  [{qids}]{dup}")
        else:
            reason = _diagnose(n, var_norm, superseded_norm, deleted_norm, current_by_qid)
            missing.append((desig, prompt, reason))
            print(f"  MISSING  {desig:13} {_clip(prompt)}")

    print("\n" + "=" * 80)
    print(f"matched base questions: {matched} / {total}")
    if not missing:
        print("All curated prompts have a matching base question. Nothing is missing.")
        return
    print(f"MISSING ({len(missing)}):")
    for desig, prompt, reason in missing:
        print(f"  - [{desig}] {prompt}")
        print(f"      why: {reason}")


if __name__ == "__main__":
    asyncio.run(main())
