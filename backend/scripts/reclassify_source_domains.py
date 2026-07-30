"""Reclassify cached SourceDomain rows against the current curated taxonomy (FR-706a).

Run this AFTER editing config/source_authority.yaml (and bumping rules_version). It recomputes
the curated control/authority/display for every cached domain WITHOUT touching WHOIS metadata,
so domains newly added to the lists move out of "Other / unverified" into their real category.
Curated-only: makes no external (RDAP/WhoisXML) or LLM calls (classify() with no evidence
falls straight through to the curated taxonomy).

    python scripts/reclassify_source_domains.py
"""
from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402

from app.models.database import AsyncSessionLocal, init_db  # noqa: E402
from app.models.source_domain import SourceDomain  # noqa: E402
from app.source_authority import classifier, taxonomy  # noqa: E402

# Only the curated-derived fields are updated; WHOIS/enrichment metadata is preserved.
_FIELDS = (
    "control_type",
    "authority_type",
    "display_category",
    "verification",
    "classification_status",
    "classification_source",
    "classification_confidence",
    "classification_reason",
    "classification_evidence",
    "requires_review",
)


async def main() -> None:
    await init_db()
    rv = taxonomy.rules_version()
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(SourceDomain))).scalars().all()
        before = Counter(r.display_category for r in rows)
        changed = 0
        for sd in rows:
            host = sd.authority_domain
            regdom = sd.registrable_domain or host
            result = classifier.classify(host, regdom, host)
            if any(getattr(sd, f) != result[f] for f in _FIELDS):
                changed += 1
            for f in _FIELDS:
                setattr(sd, f, result[f])
            sd.rules_version = rv
        await db.commit()
        after = Counter(r.display_category for r in rows)

    print(f"rules_version={rv}  domains={len(rows)}  reclassified={changed}")
    for c in sorted(set(before) | set(after)):
        b, a = before.get(c, 0), after.get(c, 0)
        flag = "" if a == b else f"  ({a - b:+d})"
        print(f"  {c:<24} {b:>4} -> {a:>4}{flag}")


if __name__ == "__main__":
    asyncio.run(main())
