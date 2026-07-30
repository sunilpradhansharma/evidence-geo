"""Why do harvested outcome rows carry no canonical endpoint? (Issue 2)

Read-only. Re-runs ``evidence.endpoints.match_endpoint`` over every outcome row already in
the database and groups the failures **by the matcher's own reason code**, then by candidate
set. That grouping is the whole point: it separates the three explanations for an unmapped
row, which the stored flags do not distinguish.

  * ``NO_CANONICAL_WORDING`` — the title names no endpoint we model. Registries post 20-40
    outcome measures per trial (PK, adverse events, DAS28, SF-36); most are legitimately
    outside a 20-endpoint canonical set. **Not a defect.**
  * ``AMBIGUOUS_WORDING_AND_TIMEPOINT`` — the title names two of our endpoints and the
    windows overlap. This is the real ambiguity, and the candidate set says whether it is a
    tokenisation problem (one token substring-matching another) or a genuine collision.
  * ``TIMEPOINT_OUTSIDE_ALL_WINDOWS`` — we recognise the endpoint and reject the week. A
    window judgement, owned by the statistical reviewer, not a matcher fix.

Two honest limits of reading this out of the database rather than the payloads:

  * ``OutcomeResult.endpoint`` is stored as ``title[:128]``. The full title is recovered
    from ``source_text`` where possible and the truncation count is reported, because a
    title cut mid-token would make the matcher look worse than it is.
  * ``timepoint_week`` is read as stored, so this audit reproduces the parse the row was
    ingested with rather than re-deriving it. This diagnoses the corpus as ingested; whether
    a parser fix moved anything is ``scripts/reparse_stored_payloads.py``, which owns that
    question. Two tools answering it would eventually disagree.

Run (repo-root .venv, cwd backend):
    python -m scripts.endpoint_mapping_audit
    python -m scripts.endpoint_mapping_audit --indication "Psoriatic Arthritis" --show 60
    python -m scripts.endpoint_mapping_audit --contains ACR      # probe the vocabulary
    python -m scripts.endpoint_mapping_audit --detail "ACR 20 Response"   # raw timeFrames
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402

from app.evidence import endpoints as ep  # noqa: E402
from app.models.clinical_study import ClinicalStudy, OutcomeResult  # noqa: E402
from app.models.database import AsyncSessionLocal  # noqa: E402

# Stored flags, duplicated as literals rather than imported from the adapter: this script
# audits what is *on the rows*, which may predate the current adapter constants.
STORED_AMBIGUOUS = "ENDPOINT_AMBIGUOUS"
STORED_NOT_CANONICAL = "ENDPOINT_NOT_CANONICAL"

_TRUNCATION_LENGTH = 128


def _clip(text: str, n: int = 88) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "\u2026"


def _full_title(row: OutcomeResult) -> tuple[str, bool]:
    """``(best-known title, was_truncated)``.

    ``source_text`` is ``f"{title} | {timeFrame}"``, so the title is everything before the
    final separator — but only when that reconstruction still starts with the stored
    (truncated) endpoint. Otherwise the split cut into the title itself and the stored
    value is the safer answer.
    """
    stored = row.endpoint or ""
    truncated = len(stored) >= _TRUNCATION_LENGTH
    source = row.source_text or ""
    if not source:
        return stored, truncated
    candidate = source.rsplit(" | ", 1)[0]
    for option in (candidate, source):
        if option.startswith(stored):
            return option, truncated
    return stored, truncated


def _flags(row: OutcomeResult) -> list[str]:
    try:
        parsed = json.loads(row.mismatch_flags or "[]")
    except (TypeError, ValueError):
        return []
    return [str(f) for f in parsed] if isinstance(parsed, list) else []


def _percent(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):5.1f}%" if whole else "    -"


def _section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def _detail(rows: list[OutcomeResult], needle: str, show: int) -> None:
    """Raw ``timeFrame`` and per-row qualifiers for the measures matching *needle*.

    ``source_text`` is ``title | timeFrame``, and ``result_id`` encodes
    ``OM{measure}:{class}:{category}:{group}`` — so the class count reveals whether a
    measure's rows are one value per *visit*. That is the only way to tell "the trial
    measured week 2" from "the timeFrame listed seven visits and only the first was
    parsed, and every visit's row now claims that one week".
    """
    grouped: dict[tuple[str, float | None], list[OutcomeResult]] = defaultdict(list)
    for row in rows:
        title, _ = _full_title(row)
        if needle in title.lower():
            grouped[(row.source_text or title, row.timepoint_week)].append(row)

    if not grouped:
        print(f"No measures whose title contains {needle!r}.")
        return

    for (source_text, week) in sorted(grouped, key=lambda k: -len(grouped[k]))[:show]:
        members = grouped[(source_text, week)]
        parsed = f"{week:g}" if week is not None else "None"
        parts = [r.result_id.split(":") for r in members]
        classes = sorted({p[2] for p in parts if len(p) > 2})
        groups = sorted({p[4] for p in parts if len(p) > 4})
        print(f"\n  parsed week = {parsed}   ({len(members)} rows, study {members[0].study_id})")
        print(f"    title | timeFrame: {_clip(source_text, 140)}")
        print(f"    {len(classes)} classes x {len(groups)} groups"
              f"   mapped_to={members[0].canonical_outcome_id or '-'}")
        print(f"    definition: {_clip(members[0].endpoint_definition or '', 140)}")


async def main() -> None:
    ap = argparse.ArgumentParser(
        description="Group unmapped endpoint titles by matcher reason and candidate set."
    )
    ap.add_argument("--indication", default=None, help='Restrict to one indication.')
    ap.add_argument(
        "--contains", default=None,
        help="Only report titles containing this text (case-insensitive vocabulary probe).",
    )
    ap.add_argument("--show", type=int, default=25, help="Distinct titles to print per group.")
    ap.add_argument(
        "--detail", default=None,
        help="Dump the raw timeFrame and per-row qualifiers for measures whose title "
             "contains this text. Use it to tell a mis-parsed timepoint from a real one.",
    )
    args = ap.parse_args()

    async with AsyncSessionLocal() as db:
        studies = {s.study_id: s for s in (await db.execute(select(ClinicalStudy))).scalars()}
        rows = list((await db.execute(select(OutcomeResult))).scalars())

    if not rows:
        print("No outcome_results rows. Ingest first: python -m scripts.ingest_evidence --help")
        return

    if args.detail:
        _detail(rows, args.detail.lower(), args.show)
        return

    needle = (args.contains or "").lower()

    # One "measure" = one distinct (indication, title, week). Rows fan out per arm, class and
    # category, so row counts overstate how many *decisions* a curator faces by ~20x.
    measures: dict[tuple[str, str, float | None], dict] = {}
    stored_flags: Counter[str] = Counter()
    stored_mapped_rows = 0
    truncated_rows = 0
    total_rows = 0
    skipped_no_study = 0

    for row in rows:
        study = studies.get(row.study_id)
        if study is None:
            skipped_no_study += 1
            continue
        indication = study.indication or ""
        if args.indication and indication != args.indication:
            continue
        title, truncated = _full_title(row)
        if needle and needle not in title.lower():
            continue

        total_rows += 1
        truncated_rows += int(truncated)
        if row.canonical_outcome_id:
            stored_mapped_rows += 1
        for flag in _flags(row):
            if flag in (STORED_AMBIGUOUS, STORED_NOT_CANONICAL):
                stored_flags[flag] += 1

        key = (indication, title, row.timepoint_week)
        entry = measures.get(key)
        if entry is None:
            entry = measures[key] = {
                "indication": indication,
                "title": title,
                "week": row.timepoint_week,
                "rows": 0,
                "studies": set(),
                "truncated": truncated,
            }
        entry["rows"] += 1
        entry["studies"].add(row.study_id)

    if not total_rows:
        print("Nothing matched the filters.")
        return

    print("=== Endpoint mapping audit ===")
    print(f"{total_rows} outcome rows, {len(measures)} distinct (indication, title, week) measures")
    print(f"{len(studies)} studies in the database")
    if skipped_no_study:
        print(f"{skipped_no_study} rows skipped: no parent study row")
    if truncated_rows:
        print(
            f"{truncated_rows} rows carry a title stored at the {_TRUNCATION_LENGTH}-char limit; "
            "full text recovered from source_text where available"
        )

    # --- recompute -------------------------------------------------------------------
    for entry in measures.values():
        entry["match"] = ep.match_endpoint(
            entry["title"], indication=entry["indication"], week=entry["week"]
        )

    by_code: dict[str, list[dict]] = defaultdict(list)
    for entry in measures.values():
        by_code[entry["match"].reason_code].append(entry)

    _section("Stored state")
    print(f"  mapped (canonical_outcome_id set)   {stored_mapped_rows:6}  {_percent(stored_mapped_rows, total_rows)}")
    for flag, count in stored_flags.most_common():
        print(f"  {flag:33} {count:6}  {_percent(count, total_rows)}")

    _section("Recomputed by matcher reason")
    ranked = sorted(by_code.items(), key=lambda kv: -sum(e["rows"] for e in kv[1]))
    for code, entries in ranked:
        rows_here = sum(e["rows"] for e in entries)
        print(
            f"  {code or '(none)':33} {rows_here:6}  {_percent(rows_here, total_rows)}"
            f"   {len(entries):5} measures"
        )

    # --- the ambiguity groups, which is what Issue 2 asked for ------------------------
    for code in (ep.AMBIGUOUS_WORDING_AND_TIMEPOINT, ep.WORDING_AMBIGUOUS_NO_TIMEPOINT):
        entries = by_code.get(code) or []
        if not entries:
            continue
        _section(f"{code}: collisions by candidate set")
        groups: dict[tuple[str, ...], list[dict]] = defaultdict(list)
        for entry in entries:
            groups[entry["match"].candidates].append(entry)
        for candidates, members in sorted(
            groups.items(), key=lambda kv: -sum(e["rows"] for e in kv[1])
        ):
            rows_here = sum(e["rows"] for e in members)
            print(f"\n  {' + '.join(candidates)}  \u2014 {rows_here} rows, {len(members)} measures")
            for entry in sorted(members, key=lambda e: -e["rows"])[: args.show]:
                week = f"w{entry['week']:g}" if entry["week"] is not None else "w?"
                print(f"    {entry['rows']:5}  {week:>5}  {_clip(entry['title'])}")
            if len(members) > args.show:
                print(f"    ... and {len(members) - args.show} more measures")

    # --- the wording failures, grouped by indication ----------------------------------
    for code, heading in (
        (ep.TIMEPOINT_OUTSIDE_ALL_WINDOWS, "recognised endpoint, week rejected"),
        (ep.NO_CANONICAL_WORDING, "no canonical endpoint wording in the title"),
    ):
        entries = by_code.get(code) or []
        if not entries:
            continue
        _section(f"{code} \u2014 {heading}")
        per_indication: dict[str, list[dict]] = defaultdict(list)
        for entry in entries:
            per_indication[entry["indication"]].append(entry)
        for indication, members in sorted(
            per_indication.items(), key=lambda kv: -sum(e["rows"] for e in kv[1])
        ):
            rows_here = sum(e["rows"] for e in members)
            print(f"\n  {indication}  \u2014 {rows_here} rows, {len(members)} measures")
            for entry in sorted(members, key=lambda e: -e["rows"])[: args.show]:
                week = f"w{entry['week']:g}" if entry["week"] is not None else "w?"
                seen = f"({len(entry['studies'])} studies)"
                print(f"    {entry['rows']:5}  {week:>5}  {_clip(entry['title'], 72):72} {seen}")
            if len(members) > args.show:
                print(f"    ... and {len(members) - args.show} more measures")

    # --- what actually resolved --------------------------------------------------------
    resolved = Counter()
    resolved_measures = Counter()
    for entry in measures.values():
        match = entry["match"]
        if match.matched:
            resolved[match.outcome_id] += entry["rows"]
            resolved_measures[match.outcome_id] += 1
    _section("Resolved to a canonical outcome")
    if not resolved:
        print("  nothing")
    for oid, count in resolved.most_common():
        print(f"  {oid:38} {count:6} rows   {resolved_measures[oid]:4} measures")


if __name__ == "__main__":
    asyncio.run(main())
