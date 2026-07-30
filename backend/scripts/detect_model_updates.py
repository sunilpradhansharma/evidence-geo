"""Backfill auto-detected model updates (FR-707a) across existing response drifts.

Model updates are normally auto-logged after every scoring run. This one-off script
runs the same detection over ALL historical drifts, so older data populates the
AI Update Impact tab without any manual logging. Idempotent — safe to re-run.

Run:  python -m scripts.detect_model_updates
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.models.database import AsyncSessionLocal, init_db  # noqa: E402
from app.services import model_release_service as svc  # noqa: E402


async def main() -> None:
    await init_db()
    async with AsyncSessionLocal() as db:
        result = await svc.detect_model_updates(db)
    print(
        f"Detection complete: {result['events_created']} update event(s) created, "
        f"{result['diffs_linked']} drift(s) linked to a release."
    )


if __name__ == "__main__":
    asyncio.run(main())
