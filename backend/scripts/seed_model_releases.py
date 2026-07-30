"""Seed the Model Release Log (FR-707a) with REAL vendor release dates.

These are actual, publicly-announced model releases/updates for the platforms the
POC monitors, so drift correlation and the timeline overlay are demonstrable against
realistic dates. Platform names match Response.llm_name (case-insensitive).

Idempotent: skips a (platform, release_date, version) triple already present.

Run:  python -m scripts.seed_model_releases
"""
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.models.database import AsyncSessionLocal, init_db  # noqa: E402
from app.models.model_release import ModelReleaseLog  # noqa: E402
from sqlalchemy import select  # noqa: E402

# (target_platform, release_date, version, url, notes) — real, public releases.
RELEASES: list[tuple[str, date, str, str, str]] = [
    ("Claude", date(2024, 10, 22), "claude-3-5-sonnet-20241022-v2",
     "https://www.anthropic.com/news/3-5-models-and-computer-use",
     "Claude 3.5 Sonnet (upgraded / v2) and computer use released."),
    ("Claude", date(2024, 11, 4), "claude-3-5-haiku-20241022",
     "https://www.anthropic.com/news/3-5-models-and-computer-use",
     "Claude 3.5 Haiku made generally available."),
    ("Nova-Pro", date(2024, 12, 3), "amazon.nova-pro-v1:0",
     "https://aws.amazon.com/blogs/aws/introducing-amazon-nova/",
     "Amazon Nova family (incl. Nova Pro) announced at re:Invent 2024."),
    ("Llama", date(2024, 7, 23), "meta.llama3-1-70b-instruct-v1:0",
     "https://ai.meta.com/blog/meta-llama-3-1/",
     "Meta Llama 3.1 (8B/70B/405B) released."),
    ("Gemini", date(2024, 12, 11), "gemini-2.0-flash-exp",
     "https://blog.google/technology/google-deepmind/google-gemini-ai-update-december-2024/",
     "Gemini 2.0 Flash (experimental) introduced."),
    ("GPT-4o", date(2024, 5, 13), "gpt-4o-2024-05-13",
     "https://openai.com/index/hello-gpt-4o/",
     "GPT-4o launched."),
    ("GPT-4o", date(2024, 11, 20), "gpt-4o-2024-11-20",
     "https://platform.openai.com/docs/models",
     "GPT-4o November 2024 update."),
]


async def seed() -> None:
    await init_db()
    async with AsyncSessionLocal() as db:
        existing = {
            (p, d, v)
            for (p, d, v) in (
                await db.execute(
                    select(
                        ModelReleaseLog.target_platform,
                        ModelReleaseLog.release_date,
                        ModelReleaseLog.version,
                    )
                )
            ).all()
        }

        inserted = 0
        skipped = 0
        for platform, rel_date, version, url, notes in RELEASES:
            if (platform, rel_date, version) in existing:
                skipped += 1
                continue
            db.add(ModelReleaseLog(
                target_platform=platform,
                release_date=rel_date,
                version=version,
                release_notes=notes,
                url=url,
                source="seed",
            ))
            inserted += 1
        await db.commit()

        print(f"Model release seed complete: {inserted} inserted, {skipped} already present "
              f"(of {len(RELEASES)} defined).")


if __name__ == "__main__":
    asyncio.run(seed())
