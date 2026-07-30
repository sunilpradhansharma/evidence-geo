"""Hotfix: correct persona tags for the Rhem.csv (RA / PsA) question set.

Context
-------
The RA/PsA question set from ``Rhem.csv`` was bulk-imported through the
"Import prompts" flow, which applies ONE persona to the WHOLE file. Every row
therefore landed as ``Patient`` in the Approved Question Bank on prod, including
the rows the sheet marks ``HCP`` (which map to the model's ``Provider`` persona).

This one-off re-tags each affected question to the persona declared in the sheet
and propagates that persona to every variation-group member of an affected
question (variations inherit persona from their base question, so they were
mis-tagged the same way).

Matching is by NORMALIZED question text (case / punctuation / quote / whitespace
insensitive, full-sequence), so it targets exactly these questions and nothing
else. Multi-question cells are matched both as the whole cell and as their split
parts, so the fix still lands if a question was stored split. It is idempotent
(only rows whose persona actually differs are written) and DRY-RUN by default.

Run locally (repo-root .venv, cwd backend):
    python -m scripts.hotfix_rhem_personas            # dry run, writes nothing
    python -m scripts.hotfix_rhem_personas --commit   # apply

Inside the prod container:
    docker exec -i evidence-monitoring-agent python -m scripts.hotfix_rhem_personas
    docker exec -i evidence-monitoring-agent python -m scripts.hotfix_rhem_personas --commit
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402

from app.models.database import AsyncSessionLocal  # noqa: E402
from app.models.question import Question, utcnow  # noqa: E402
from app.utils.audit import write_audit  # noqa: E402

PATIENT = "Patient"
PROVIDER = "Provider"  # the sheet's "HCP" maps to the model's Provider persona

# (target_persona, verbatim prompt from Rhem.csv). 10 Patient + 11 Provider = 21.
RHEM_ROWS: list[tuple[str, str]] = [
    (PATIENT, "How do I know if my RA treatment is working? What do doctors look for when deciding if a treatment is working?"),
    (PATIENT, "Who is a good candidate for RINVOQ?"),
    (PATIENT, "What happens if rheumatoid arthritis is not well controlled?"),
    (PATIENT, "What is the best treatment for rheumatoid arthritis?"),
    (PATIENT, "What should I know about the risks and side effects of RINVOQ?"),
    (PROVIDER, "Based on the black box warning for JAK inhibitors, how should I weigh cardiovascular risk in a 55-year-old RA patient versus switching to a second anti-TNF?"),
    (PROVIDER, "Can I take RINVOQ if I am currently being treated for high blood pressure? / Can I take RINVOQ with controlled hypertension?"),
    (PROVIDER, "Does upadacitinib have data similar to the ORAL Surveillance trial?; Does upadacitinib have the same level of risk of safety as other JAKs?"),
    (PROVIDER, "How should I counsel patients on the boxed warning of upadacitinib? How can I articulate RINVOQ's safety risks to a nervous patient?"),
    (PROVIDER, "What is the absolute risk of DVT or PE with RINVOQ 15mg in an RA patient with a history of controlled hypertension?"),
    (PROVIDER, "Are there particular patient sub-types or biomarkers at higher risk for VTE, CV events, etc.? Beyond known risk factors (diabetes, smoking), are there biomarkers we could measure to identify risk?"),
    (PROVIDER, "Which PsA drug is best by disease domain?"),
    (PROVIDER, "When should I start a patient on a biologic in PsA, and which one do I start them on?"),
    (PROVIDER, "Which IL-23 is better for PsA, Skyrizi or Tremfya?"),
    (PROVIDER, "How does Skyrizi compare to TNFs and IL-17s in the joints for PsA?"),
    (PROVIDER, "What is the best PsA treatment after a TNF fails?"),
    (PATIENT, "What's the difference between RINVOQ and XELJANZ?"),
    (PATIENT, "What's the difference between SKYRIZI and TREMFYA?"),
    (PATIENT, "Why does RINVOQ have a boxed warning?"),
    (PATIENT, "What are RINVOQ's/SKYRIZI's side effects?"),
    (PATIENT, "How do I know when to change PsA medications? or How do I know my PsA medication isn't working?"),
]

# Split multi-question cells on '/', ';', or a sentence-ending '?'. Bare "or" is
# NOT a delimiter (many clinical questions contain "or", e.g. "DVT or PE").
_SPLIT = re.compile(r"\s*/\s*|\s*;\s*|\?")


def _norm(text: str) -> str:
    """Full-sequence match key: NFKC, lowercase, strip to alphanumeric tokens."""
    s = unicodedata.normalize("NFKC", text or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _candidates(prompt: str) -> set[str]:
    """Normalized keys for a sheet prompt: the whole cell plus its split parts.

    A question is only ever re-tagged when a candidate matches a stored question's
    FULL normalized text, so extra/garbage split fragments are harmless.
    """
    keys = {_norm(prompt)}
    for part in _SPLIT.split(prompt):
        n = re.sub(r"^(?:or|and)\s+", "", _norm(part))
        if len(n) >= 12:
            keys.add(n)
    keys.discard("")
    return keys


def _build_target() -> tuple[dict[str, str], dict[str, int], list[str]]:
    """key -> persona, key -> csv row index, and any ambiguous keys dropped."""
    target: dict[str, str] = {}
    key_row: dict[str, int] = {}
    ambiguous: list[str] = []
    for idx, (persona, prompt) in enumerate(RHEM_ROWS):
        for key in _candidates(prompt):
            if key in target and target[key] != persona:
                ambiguous.append(key)
                target.pop(key, None)
                continue
            target[key] = persona
            key_row.setdefault(key, idx)
    for key in ambiguous:
        target.pop(key, None)
    return target, key_row, ambiguous


def _clip(text: str, n: int = 88) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "\u2026"


async def main() -> None:
    ap = argparse.ArgumentParser(description="Re-tag Rhem.csv question personas (+ variations).")
    ap.add_argument("--commit", action="store_true", help="Write changes (default: dry run).")
    args = ap.parse_args()

    target, key_row, ambiguous = _build_target()

    async with AsyncSessionLocal() as db:
        questions = list((await db.execute(
            select(Question).where(
                Question.deleted_at.is_(None), Question.superseded_by.is_(None)
            )
        )).scalars().all())

        by_norm: dict[str, list[Question]] = {}
        for q in questions:
            by_norm.setdefault(_norm(q.question_text), []).append(q)

        # 1) Direct text matches from the sheet.
        planned: dict[int, tuple[Question, str, str]] = {}  # row_id -> (q, persona, why)
        row_hits: dict[int, int] = {i: 0 for i in range(len(RHEM_ROWS))}
        group_persona: dict[str, str] = {}  # any variation-group key -> persona
        for key, persona in target.items():
            for q in by_norm.get(key, []):
                planned.setdefault(q.id, (q, persona, "direct"))
                row_hits[key_row[key]] += 1
                for gk in (q.variation_group_id, q.question_id, q.variation_of):
                    if gk:
                        group_persona[gk] = persona

        # 2) Propagate to every variation-group member of a matched question.
        for q in questions:
            if q.id in planned:
                continue
            persona = None
            for gk in (q.variation_group_id, q.variation_of, q.question_id):
                if gk and gk in group_persona:
                    persona = group_persona[gk]
                    break
            if persona is not None:
                planned[q.id] = (q, persona, "variation")

        changes = [(q, persona, why) for (q, persona, why) in planned.values() if q.persona != persona]
        already = [q for (q, persona, _) in planned.values() if q.persona == persona]

        mode = "COMMIT" if args.commit else "DRY RUN"
        print(f"Rhem.csv persona hotfix  [{mode}]")
        print(f"current questions scanned: {len(questions)}")
        if ambiguous:
            print(f"WARNING: {len(ambiguous)} ambiguous match key(s) skipped (same text, two personas).")

        print("\n--- CSV row coverage (matched questions on this DB) ---")
        for idx, (persona, prompt) in enumerate(RHEM_ROWS):
            flag = "ok  " if row_hits[idx] else "MISS"
            print(f"  [{flag}] {persona:8} x{row_hits[idx]}  | {_clip(prompt)}")

        direct = [c for c in changes if c[2] == "direct"]
        varc = [c for c in changes if c[2] == "variation"]
        print(f"\n--- planned persona changes: direct ({len(direct)}) ---")
        for q, persona, _ in direct:
            print(f"  {q.question_id:14} {q.persona:8} -> {persona:8} | {_clip(q.question_text)}")
        if varc:
            print(f"\n--- planned persona changes: variation-group members ({len(varc)}) ---")
            for q, persona, _ in varc:
                print(f"  {q.question_id:14} {q.persona:8} -> {persona:8} | {_clip(q.question_text)}")

        missed = [i for i, n in row_hits.items() if not n]
        print(
            f"\nsummary: rows matched {len(RHEM_ROWS) - len(missed)}/{len(RHEM_ROWS)}"
            f"  |  questions to change {len(changes)} (direct {len(direct)}, variations {len(varc)})"
            f"  |  already correct {len(already)}"
        )
        if missed:
            print("  unmatched CSV rows (not present on this DB):")
            for i in missed:
                print(f"    - {RHEM_ROWS[i][0]:8} | {_clip(RHEM_ROWS[i][1])}")

        if not args.commit:
            print("\nDRY RUN — nothing written. Re-run with --commit to apply.")
            return
        if not changes:
            print("\nNothing to change — personas already correct.")
            return

        for q, persona, why in changes:
            old = q.persona
            q.persona = persona
            q.updated_at = utcnow()
            await write_audit(
                db, role="OPERATOR", event="PERSONA_HOTFIX", question_id=q.question_id,
                context={"from": old, "to": persona, "match": why, "source": "Rhem.csv"},
                commit=False,
            )
        await db.commit()
        print(f"\nAPPLIED — updated persona on {len(changes)} question(s).")


if __name__ == "__main__":
    asyncio.run(main())
