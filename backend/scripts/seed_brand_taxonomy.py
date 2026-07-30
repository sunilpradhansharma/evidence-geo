"""Load the brand taxonomy into SQLite from the reviewed baseline.

The taxonomy used to be read straight from ``brands.yaml`` on every request. It now lives in
four tables so it can be edited at runtime and survive a redeploy — the container is rebuilt
from the working tree on every deploy and only ``data/`` is host-mounted, so a file the
application wrote would be destroyed by the next one.

``backend/app/config/seed/brands_seed.yaml`` remains in git as the reviewed baseline. This
script is what puts it in the database.

Idempotent by default, like every other seeder in ``ec2_deploy.sh``: an already-populated
taxonomy is left completely alone, so a redeploy never reverts an edit someone made through
the UI. ``--force`` is the deliberate opposite — it REPLACES the stored taxonomy with the
baseline and discards those edits, which is a recovery tool, not part of a deploy.

``--export`` writes the stored taxonomy to stdout as YAML and touches nothing. It is how
``ec2_deploy.sh`` keeps a rolling snapshot on the host: with the file retired from git, that
snapshot is the only record of what production believes, and the only thing to compare
against after a mistaken ``--force`` or a bad edit made through the UI.

Run:  python -m scripts.seed_brand_taxonomy
      python -m scripts.seed_brand_taxonomy --dry-run
      python -m scripts.seed_brand_taxonomy --force
      python -m scripts.seed_brand_taxonomy --export > taxonomy.yaml
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import taxonomy  # noqa: E402
from app.models.database import AsyncSessionLocal, init_db  # noqa: E402
from app.services import brand_taxonomy_service as store  # noqa: E402


def _describe(document: dict) -> str:
    areas = document.get("therapeutic_areas") or {}
    indications = document.get("indications") or {}
    catalog = document.get("drug_catalog") or {}
    drugs = sum(
        len((block or {}).get(kind) or [])
        for block in areas.values()
        for kind in ("focus_brands", "competitors")
    )
    return (
        f"{len(areas)} therapeutic area(s), {drugs} drug entr(ies), "
        f"{len(indications)} indication(s), {len(catalog)} catalog drug(s)"
    )


async def main(*, dry_run: bool, force: bool, export: bool) -> int:
    await init_db()

    if export:
        # Nothing else may reach stdout on this path: the deploy redirects it straight into a
        # file, so a stray progress line would corrupt the snapshot it is trying to take.
        async with AsyncSessionLocal() as db:
            if await store.is_empty(db):
                print("No taxonomy stored yet.", file=sys.stderr)
                return 1
            rendered = await store.export_yaml(db)
        # Written as explicit UTF-8 bytes rather than through the text layer, whose encoding
        # follows the environment's locale. The document carries non-ASCII (Waldenström, the
        # dashes in the header comments), so a container running under a non-UTF-8 locale
        # would otherwise emit a snapshot that will not parse — and the whole point of it is
        # to be readable later, on a different machine, after something has gone wrong.
        sys.stdout.buffer.write(rendered.encode("utf-8"))
        return 0

    document = store.seed_document()
    print(f"Baseline {taxonomy.SEED_FILENAME}: {_describe(document)}")

    async with AsyncSessionLocal() as db:
        empty = await store.is_empty(db)

        if dry_run:
            if empty:
                print("Database is EMPTY. Would import the baseline.")
            elif force:
                current = await store.build_snapshot(db)
                print(f"Database holds: {_describe(current)}")
                print("Would REPLACE it with the baseline (--force), discarding any edits.")
            else:
                current = await store.build_snapshot(db)
                print(f"Database holds: {_describe(current)}. Nothing to do.")
            return 0

        if not empty and not force:
            print("Taxonomy already seeded — leaving it untouched.")
            return 0

        if not empty:
            print("--force: replacing the stored taxonomy, discarding any runtime edits.")
        await store.import_document(db, document)
        # Verify rather than assume. A silently partial import would leave the application
        # serving a taxonomy that is missing brands, which reads as "no gaps" rather than
        # as an error.
        rebuilt = await store.build_snapshot(db)
        if rebuilt != document:
            print("ERROR: the stored taxonomy does not match the baseline it was built from.")
            return 1
        print(f"Seeded: {_describe(rebuilt)}")

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would happen; write nothing"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="replace an existing taxonomy with the baseline, discarding runtime edits",
    )
    parser.add_argument(
        "--export", action="store_true",
        help="write the STORED taxonomy to stdout as YAML and exit; changes nothing",
    )
    args = parser.parse_args()
    raise SystemExit(
        asyncio.run(main(dry_run=args.dry_run, force=args.force, export=args.export))
    )
