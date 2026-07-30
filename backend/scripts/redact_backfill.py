"""Re-redact all stored harvest + social free text with the current PHI/PII detector.

Run once after upgrading the detector (``app.compliance.phi``) to clean rows that were
captured under a weaker layer. Because only redacted text is ever persisted, re-running the
stronger detector now strips identifiers (names, US locations) the old layer missed.
Idempotent and safe to re-run.

Run:  python -m scripts.redact_backfill [--ta "Obesity"] [--brief]
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.compliance import backfill  # noqa: E402
from app.models.database import AsyncSessionLocal, init_db  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(description="Re-redact stored harvest + social text.")
    parser.add_argument("--ta", "--therapeutic-area", dest="ta", default=None,
                        help="Optional: scope the social sweep to one therapeutic area.")
    parser.add_argument("--brief", action="store_true",
                        help="Also regenerate the social narrative brief afterward.")
    args = parser.parse_args()

    await init_db()
    async with AsyncSessionLocal() as db:
        summary = await backfill.redact_backfill(db, therapeutic_area=args.ta)
        print("Redact backfill summary:")
        for key, value in summary.items():
            print(f"  {key}: {value}")
        if args.brief:
            from app.social import narrative

            res = await narrative.generate_social_brief(db, therapeutic_area=args.ta or "Obesity")
            print(f"  brief: {res.get('status')}")


if __name__ == "__main__":
    asyncio.run(main())
