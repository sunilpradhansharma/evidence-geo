"""Ingest regulatory drug facts from openFDA labels.

The drug-facts deliverable the plan says ships **independently of the NMA stack** — it
stays valuable even for an indication whose network turns out to be disconnected. It had
never run: ``app/evidence/sources/openfda_facts.py`` was imported by its own test and by
nothing else, so ``drug_facts`` was empty in every environment and three consumers that
filter on ``verification_status == VERIFIED`` returned nothing.

What it does:

  1. fetch the current FDA label for each brand (openFDA, no key required)
  2. map it onto a ``DrugFact`` and persist it against a retained payload
  3. report what would change, including which rows a **new label date** supersedes

**DRY RUN by default.** A dry run still calls openFDA — it has to, to report what it would
store — but writes nothing.

**Nothing here verifies.** Facts land ``EXTRACTED``, or ``MAPPED`` when the brand resolves
onto the curated catalog. Verification is a curator's judgement and lives on
``POST /evidence-review/drug-facts/{fact_id}/curator-check``. There is deliberately no
``--verify-as`` equivalent: bulk-stamping one name across labels nobody opened manufactures
an audit trail that looks real, and unlike the trial corpus there are only a handful of
labels, so the honest path is not the slow one.

Run locally (repo-root .venv, cwd backend):
    python -m scripts.ingest_drug_facts
    python -m scripts.ingest_drug_facts --brands Rinvoq Skyrizi --commit

Inside the prod container:
    docker exec -i evidence-monitoring-agent python -m scripts.ingest_drug_facts --commit
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import taxonomy  # noqa: E402
from app.models.database import AsyncSessionLocal, init_db  # noqa: E402
from app.services import evidence_ingestion_service as ingest  # noqa: E402

_ACTION_NOTE = {
    "INGESTED": "new",
    "UPDATED": "same label date, refreshed",
    "SUPERSEDED": "new label date; the previous version is kept and marked superseded",
    "SKIPPED": "no change made",
    "NOT_FOUND": "openFDA returned no usable label",
}


async def main() -> None:
    ap = argparse.ArgumentParser(
        description="Ingest openFDA labels as DrugFact rows (dry run by default)."
    )
    ap.add_argument(
        "--brands", nargs="*", default=None,
        help="Brands to fetch. Defaults to every full-depth drug in brands.yaml.",
    )
    ap.add_argument("--commit", action="store_true", help="Write changes.")
    args = ap.parse_args()

    brands = args.brands or list(taxonomy.full_depth_drugs())
    if not brands:
        print("No brands to ingest. Pass --brands or set evidence_depth: full in brands.yaml.")
        return

    await init_db()
    async with AsyncSessionLocal() as db:
        print(f"Fetching {len(brands)} label(s): {', '.join(brands)}")
        report = await ingest.ingest_drug_facts(db, brands, commit=False)

        data = report.as_dict()
        print(
            f"\nrequested {data['requested']} | ingested {data['ingested']} | "
            f"updated {data['updated']} | superseded {data['superseded']} | "
            f"skipped {data['skipped']} | not found {data['not_found']}"
        )

        for fact in data["facts"]:
            print(f"\n  {fact['brand']}  [{fact['action']}] — {_ACTION_NOTE[fact['action']]}")
            if fact["fact_id"]:
                print(f"    id            {fact['fact_id']}")
            if fact["label_updated_at"]:
                print(f"    label updated {fact['label_updated_at']}")
            if fact["supersedes"]:
                print(f"    supersedes    {fact['supersedes']}")
            if fact["verification_status"]:
                print(f"    status        {fact['verification_status']}")
            if fact["reason"]:
                print(f"    reason        {fact['reason']}")
            # Flags are the whole point of showing per-brand detail: a curated/label
            # disagreement about class or route is what a reviewer has to adjudicate.
            for flag in fact["flags"]:
                print(f"    flag          {flag}")

        awaiting = data["awaiting_verification"]
        if awaiting:
            print(
                f"\n{awaiting} label(s) await a curator. Question generation and every "
                "approval, safety and mechanism claim read VERIFIED labels only, so "
                "nothing downstream changes until they are checked:"
            )
            print("  GET  /api/evidence-review/drug-facts")
            print("  GET  /api/evidence-review/drug-facts/{fact_id}/source-check")
            print("  POST /api/evidence-review/drug-facts/{fact_id}/curator-check")

        # Stated every run, because it is the limitation most likely to be forgotten:
        # openfda_facts does not structure the indications prose, so an approval claim
        # still has nothing to grade against even after verification.
        print(
            "\nNote: the adapter records INDICATIONS_TEXT_NOT_STRUCTURED rather than "
            "half-parsing the label's indications prose, so an approval question or claim "
            "has no list to check against yet. Verification does not change that — "
            "structuring the indications is Phase 3A pipeline work."
        )

        if args.commit:
            await db.commit()
            print("\nCommitted.")
        else:
            await db.rollback()
            print("\nDRY RUN — nothing written. Re-run with --commit.")


if __name__ == "__main__":
    asyncio.run(main())
