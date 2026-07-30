"""Backfill FR-707a correlation on EXISTING material drifts.

Correlation (`ResponseDiff.correlated_release_id`) is normally computed once, at
drift-detection time (see scoring/scorer._compute_response_diff). Drifts detected
before any model release was logged stay "unexplained" forever. This maintenance
script re-applies the *real* correlation rule (services.model_release_service
.find_correlated_release) to existing material diffs so newly-logged releases are
reflected without re-running the pipeline.

Optionally (--ensure-demo-releases) it first logs one release per drifting platform,
dated a few days before that platform's earliest material drift, so the correlation
has something to match in demo databases.

Usage:
  python -m scripts.backfill_release_correlation --ensure-demo-releases
  python -m scripts.backfill_release_correlation            # recompute only
"""
import argparse
import asyncio
from datetime import timedelta

from sqlalchemy import select

from app.models.database import AsyncSessionLocal
from app.models.model_release import ModelReleaseLog
from app.models.response import Response
from app.models.response_diff import ResponseDiff
from app.services.model_release_service import find_correlated_release


async def _material_diffs_with_dates(db):
    """Return (diff, observed_date) for every material-change diff."""
    stmt = (
        select(ResponseDiff, Response)
        .join(Response, ResponseDiff.current_response_id == Response.response_id)
        .where(ResponseDiff.material_change.is_(True))
    )
    rows = (await db.execute(stmt)).all()
    out = []
    for diff, resp in rows:
        observed = resp.created_at or resp.timestamp_utc
        observed = observed.date() if hasattr(observed, "date") else observed
        out.append((diff, resp.llm_name, observed))
    return out


async def ensure_demo_releases(db, pairs) -> int:
    """Log one release per drifting platform, 7 days before its earliest drift."""
    earliest: dict[str, object] = {}
    for _diff, llm, observed in pairs:
        if observed is None:
            continue
        if llm not in earliest or observed < earliest[llm]:
            earliest[llm] = observed

    inserted = 0
    for llm, first_drift in earliest.items():
        release_date = first_drift - timedelta(days=7)
        exists = (await db.execute(
            select(ModelReleaseLog).where(
                ModelReleaseLog.target_platform == llm,
                ModelReleaseLog.release_date == release_date,
            )
        )).scalars().first()
        if exists is not None:
            continue
        db.add(ModelReleaseLog(
            target_platform=llm,
            release_date=release_date,
            version=f"{llm}-demo-{release_date.isoformat()}",
            release_notes="Demo release logged to correlate existing material drift (backfill).",
            url="https://example.com/release-notes",
        ))
        inserted += 1
    if inserted:
        await db.commit()
    return inserted


async def backfill(db, pairs) -> tuple[int, int]:
    """Recompute correlated_release_id for each material diff. Returns (correlated, total)."""
    correlated = 0
    for diff, llm, observed in pairs:
        if observed is None:
            continue
        release = await find_correlated_release(db, llm_name=llm, observed_on=observed)
        diff.correlated_release_id = release.id if release else None
        if release is not None:
            correlated += 1
    await db.commit()
    return correlated, len(pairs)


async def main(ensure_demo: bool) -> None:
    async with AsyncSessionLocal() as db:
        pairs = await _material_diffs_with_dates(db)
        if ensure_demo:
            n = await ensure_demo_releases(db, pairs)
            print(f"Ensured demo releases: {n} inserted.")
        correlated, total = await backfill(db, pairs)
        print(f"Backfill complete: {correlated}/{total} material drifts correlated with a release.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ensure-demo-releases", action="store_true",
                    help="Log a matching release per drifting platform before backfilling.")
    args = ap.parse_args()
    asyncio.run(main(args.ensure_demo_releases))
