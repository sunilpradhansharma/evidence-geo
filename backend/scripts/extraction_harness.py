"""Measure the extraction pipeline against the single-call baseline. Justify-or-drop.

The plan commits to a descope: *"if the pipeline cannot demonstrate measurably better
extraction accuracy than the ``chat_json`` baseline, ship the baseline and keep the
validation stage only."* That commitment is only real if something measures it, and until
this ran the comparison had never been made.

**This costs money.** Each runner makes one model call per case (the pipeline makes three),
so a full run over the committed corpus is a handful of calls. Nothing here is on a
scheduled path — it is a decision tool, run when the prompts or the corpus change.

Run locally (repo-root .venv, cwd backend):
    python -m scripts.extraction_harness
    python -m scripts.extraction_harness --runners baseline pipeline --json report.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.evidence import harness  # noqa: E402
from app.evidence.agents import RUNNERS  # noqa: E402

DEFAULT_CORPUS = Path(__file__).parent.parent / "tests" / "fixtures" / "extraction_corpus.json"


def _print_report(report_dict: dict) -> None:
    overall = report_dict["overall"]
    print(f"\n  {report_dict['runner']}")
    print(f"    cases          {report_dict['cases']}")
    if overall["accuracy"] is None:
        print("    no gradeable fields")
        return
    print(
        f"    accuracy       {overall['accuracy']:.1%} "
        f"({overall['correct']}/{overall['fields']})"
    )
    # Printed apart from accuracy because they are different failures. A wrong value is
    # worse than an absent one, and one number cannot tell a reader which happened.
    print(f"    wrong          {overall['wrong']} ({overall['error_rate']:.1%})")
    print(f"    missed         {overall['missed']} ({overall['miss_rate']:.1%})")
    for tier, rates in report_dict["by_license_class"].items():
        if rates["accuracy"] is None:
            continue
        print(f"    {tier:<14} {rates['accuracy']:.1%} over {rates['fields']} field(s)")
    if report_dict["blocked_by_validation"]:
        print(f"    blocked by validation  {report_dict['blocked_by_validation']} case(s)")
    if report_dict["proposals_auto_rejected"]:
        print(f"    proposals auto-rejected {report_dict['proposals_auto_rejected']}")
    if report_dict["model_errors"]:
        print(f"    model errors   {report_dict['model_errors']}")


async def main() -> None:
    ap = argparse.ArgumentParser(
        description="Compare extraction runners on a labelled corpus."
    )
    ap.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    ap.add_argument(
        "--runners", nargs="*", default=["baseline", "pipeline"],
        choices=sorted(RUNNERS), help="Which runners to measure.",
    )
    ap.add_argument("--json", default=None, help="Write the full report here.")
    args = ap.parse_args()

    corpus = harness.load_corpus(args.corpus)
    print(f"Corpus: {args.corpus}  ({len(corpus)} case(s))")
    tiers = sorted({c.license_class for c in corpus})
    print(f"Licence tiers represented: {', '.join(tiers)}")

    reports: dict[str, harness.HarnessReport] = {}
    for name in args.runners:
        print(f"\nRunning {name}…")
        reports[name] = await harness.evaluate(RUNNERS[name], corpus, name=name)
        _print_report(reports[name].as_dict())

    payload = {name: r.as_dict() for name, r in reports.items()}

    if "baseline" in reports and "pipeline" in reports:
        decision = harness.verdict(reports["baseline"], reports["pipeline"])
        payload["verdict"] = decision
        print("\n" + "=" * 72)
        print(f"VERDICT: {decision['verdict']}")
        print(f"  {decision['reason']}")
        print("=" * 72)
        if decision["verdict"] == harness.SHIP_BASELINE:
            print(
                "\nThe descope is the documented outcome, not a failure. Both runners sit\n"
                "behind one interface, so shipping `baseline_with_validation` is a\n"
                "configuration change rather than a rewrite."
            )
    else:
        print(
            "\nNo verdict: it needs both `baseline` and `pipeline`. Measuring one runner "
            "says nothing about whether the other is worth its cost."
        )

    # The corpus size is printed with the verdict on purpose. A verdict from three cases is
    # a signal, not a settlement, and a number quoted without its denominator invites the
    # opposite reading.
    print(
        f"\nRead this against the corpus size: {len(corpus)} case(s). Widening the corpus "
        "is curator time, and it is what would turn this signal into a settlement."
    )

    if args.json:
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nFull report written to {args.json}")


if __name__ == "__main__":
    asyncio.run(main())
