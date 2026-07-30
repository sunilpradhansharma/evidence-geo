"""Generate the GEO ground-truth corpus from curated YAML source.

Reads ``app/config/geo/source/*.yaml`` (the human/Medical-Affairs source of truth),
optionally SEEDS label-derived fields from the free openFDA drug-label API (curated values
always win), validates every record against ``DrugSchema``, and writes the JSON-LD
``schema/*.json`` + regenerates ``llms.txt``.

Because it builds settings fresh from .env, it does NOT need the backend restarted — but a
RUNNING backend caches the loaded corpus, so call ``POST /api/geo/refresh`` (or restart) to
pick up the new files in a live server.

Run (cwd = backend/):
    python -m scripts.generate_geo_schema                 # generate all, seed from openFDA
    python -m scripts.generate_geo_schema --no-seed       # curated YAML only (offline)
    python -m scripts.generate_geo_schema --brand Humira  # one brand (skips llms.txt)
    python -m scripts.generate_geo_schema --dry-run       # report only, write nothing
    python -m scripts.generate_geo_schema --check         # gate: fail if any placeholder/unverified
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.geo import builder  # noqa: E402
from app.geo.sources import openfda  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the GEO schema corpus from curated YAML.")
    parser.add_argument("--no-seed", action="store_true", help="Skip openFDA seeding (curated YAML only).")
    parser.add_argument("--brand", default=None, help="Generate a single brand (skips llms.txt regeneration).")
    parser.add_argument("--dry-run", action="store_true", help="Validate + report without writing any files.")
    parser.add_argument("--check", action="store_true", help="Report-only gate: exit non-zero if any record is invalid or still has placeholder (unverified) clinical values. Implies no writes + no seeding.")
    args = parser.parse_args()

    seed = not args.no_seed and not args.check
    write = not args.dry_run and not args.check
    mode = "CHECK (gate; no writes, no seeding)" if args.check else ("DRY RUN (no writes)" if args.dry_run else "WRITE")
    print("=" * 78)
    print("GEO corpus generator")
    print("=" * 78)
    print(f"source dir : {builder.SOURCE_DIR}")
    print(f"schema dir : {builder.SCHEMA_DIR}")
    print(f"openFDA    : {'seeding ON (' + openfda._base_url() + ')' if seed else 'seeding OFF (--no-seed)'}")
    print(f"mode       : {mode}")
    if args.brand:
        print(f"brand      : {args.brand} (llms.txt not regenerated for single-brand runs)")
    print("-" * 78)

    report, _docs = await builder.generate(seed=seed, only_brand=args.brand, write=write)

    for b in report.brands:
        status = "OK " if b.valid else "FAIL"
        if b.seeded_fields:
            detail = "applied from label: " + ", ".join(b.seeded_fields)
        elif b.label_matched:
            detail = "label linked (curated values kept)"
        else:
            detail = "curated only (no label match)"
        label = f"  [{b.label_source}]" if b.label_source else ""
        vtag = "" if b.clinical_values_verified else "  [clinical values UNVERIFIED]"
        print(f"  [{status}] {b.file}{label}  {detail}{vtag}")
        if b.error:
            print(f"         validation error: {b.error}")

    print("-" * 78)
    if not report.brands:
        print("No source YAML found in", builder.SOURCE_DIR)
        return 1
    if not report.ok:
        print("RESULT: FAILED — one or more records did not validate; files not fully written.")
        return 1
    if report.unverified:
        print(f"NOTE: {len(report.unverified)} brand(s) still carry placeholder clinical values pending MA verification:")
        print(f"      {', '.join(report.unverified)}")
        print("      To verify: edit config/geo/source/<brand>.yaml (efficacy + competitors),")
        print("      set 'clinical_values_verified: true', then rerun the generator.")
    if args.check:
        if report.unverified:
            print("RESULT: CHECK FAILED — placeholder (unverified) clinical values present.")
            print("=" * 78)
            return 1
        print(f"RESULT: CHECK OK — {len(report.brands)} brand(s) valid and MA-verified.")
        print("=" * 78)
        return 0
    if args.dry_run:
        print(f"RESULT: DRY RUN OK — {len(report.brands)} brand(s) validated. No files written.")
    else:
        wrote_llms = "llms.txt regenerated" if report.llms_txt_written else "llms.txt unchanged (single-brand run)"
        print(f"RESULT: OK — wrote {len(report.brands)} schema file(s); {wrote_llms}.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
