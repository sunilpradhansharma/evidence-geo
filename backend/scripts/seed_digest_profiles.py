"""Seed the three stakeholder digest profiles (BR-008a): PV, Brand, Medical Affairs.

Each profile is role-specific: its rules narrow which alerts that function receives,
so PV sees safety-domain negative signals while Brand sees commercial competitor
metrics — the same underlying alerts, differentiated per audience.

Cadence: weekly, Monday 08:00 America/Chicago. Delivery defaults to in-app (email is
opt-in via SES env vars). Recipients are placeholders — edit them in the UI or here.
Idempotent: skips a role that already has a profile.

Run:  python -m scripts.seed_digest_profiles
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.models.database import AsyncSessionLocal, init_db  # noqa: E402
from app.models.digest import DigestProfile, DigestRule  # noqa: E402
from sqlalchemy import select  # noqa: E402

MONDAY_8AM = "0 8 * * 1"
TZ = "America/Chicago"


def _j(value) -> str | None:
    return json.dumps(value) if value else None


# role -> (description, recipients, delivery_methods, rules[list of filter dicts])
PROFILES = {
    "PV": (
        "Pharmacovigilance — safety-domain signals and any negative/omitted brand positioning.",
        ["pv-team@example.com"],
        ["in_app"],
        [
            {"alert_categories": ["LOW_SENTIMENT", "NOT_RECOMMENDED"], "domains": ["Safety"]},
        ],
    ),
    "Brand": (
        "Brand / Commercial — competitor advantage and non-preferred positioning across "
        "efficacy, comparative, and access questions.",
        ["brand-team@example.com"],
        ["in_app"],
        [
            {"alert_categories": ["COMPETITOR_ADVANTAGE"]},
            {"alert_categories": ["NOT_RECOMMENDED"], "domains": ["Efficacy", "Comparative", "Access"]},
        ],
    ),
    "Medical Affairs": (
        "Medical Affairs — broad clinical view across efficacy, safety, and comparative signals.",
        ["medaffairs-team@example.com"],
        ["in_app"],
        [
            {"domains": ["Efficacy", "Safety", "Comparative"]},
        ],
    ),
}


async def seed() -> None:
    await init_db()
    async with AsyncSessionLocal() as db:
        existing = set((await db.execute(select(DigestProfile.role))).scalars().all())

        inserted = 0
        skipped = 0
        for role, (desc, recipients, methods, rules) in PROFILES.items():
            if role in existing:
                skipped += 1
                continue
            profile = DigestProfile(
                role=role,
                description=desc,
                enabled=True,
                cron=MONDAY_8AM,
                timezone=TZ,
                recipients=_j(recipients),
                delivery_methods=_j(methods),
            )
            for r in rules:
                profile.rules.append(DigestRule(
                    alert_categories=_j(r.get("alert_categories")),
                    domains=_j(r.get("domains")),
                    therapeutic_areas=_j(r.get("therapeutic_areas")),
                    personas=_j(r.get("personas")),
                    llm_names=_j(r.get("llm_names")),
                ))
            db.add(profile)
            inserted += 1
        await db.commit()

        print(f"Digest profile seed complete: {inserted} inserted, {skipped} already present "
              f"(of {len(PROFILES)} defined).")


if __name__ == "__main__":
    asyncio.run(seed())
