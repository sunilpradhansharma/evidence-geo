"""Suggest curation candidates for config/source_authority.yaml (FR-706a).

An engineering aid for keeping the curated taxonomy rich. Lists the domains AI actually CITED
that the curated lists do NOT cover (so today they're classified only by the LLM — a guess — or
not at all), ranked by how often they were cited. The most-cited rows are the best candidates to
fold into source_authority.yaml. Also prints a paste-ready YAML snippet grouped by the LLM's
suggested category — REVIEW before adding, then bump rules_version and reclassify.

    python scripts/suggest_curation_candidates.py
    python scripts/suggest_curation_candidates.py --min-citations 3 --limit 40
    python scripts/suggest_curation_candidates.py --ta dermatology --llm gpt-4o
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.models.database import AsyncSessionLocal, init_db  # noqa: E402
from app.source_authority import service as svc  # noqa: E402

# authority_type -> the YAML list key a domain should be added under.
_AUTH_TO_YAML_KEY = {
    "REGULATORY": "regulatory",
    "GUIDELINE": "guideline",
    "PEER_REVIEWED": "peer_reviewed",
    "MEDICAL_REFERENCE": "medical_reference",
    "HEALTH_MEDIA": "health_media",
    "SOCIAL_UGC": "social_ugc",
}
_UNCATEGORIZED = "uncategorized (LLM returned OTHER / low confidence — decide manually)"


def _conf(c) -> str:
    return f"{c:.2f}" if isinstance(c, (int, float)) else "-"


async def main() -> None:
    ap = argparse.ArgumentParser(description="Suggest domains to add to source_authority.yaml.")
    ap.add_argument("--min-citations", type=int, default=2,
                    help="only show domains cited at least this many times (default 2)")
    ap.add_argument("--limit", type=int, default=50, help="max candidates to show (default 50)")
    ap.add_argument("--ta", default=None, help="restrict to a therapeutic area")
    ap.add_argument("--llm", default=None, help="restrict to a single model")
    args = ap.parse_args()

    await init_db()
    async with AsyncSessionLocal() as db:
        result = await svc.curation_candidates(
            db, therapeutic_area=args.ta, llm_name=args.llm,
            min_citations=args.min_citations, limit=args.limit,
        )

    items = result["items"]
    if not items:
        print(f"No uncurated cited domains at min_citations={args.min_citations}. "
              "The taxonomy already covers what's being cited.")
        return

    print(f"\nCuration candidates — cited domains missing from source_authority.yaml "
          f"(min citations {args.min_citations}; showing {len(items)} of {result['total']})\n")
    print(f"  {'cited':>6} {'resp':>5}  {'suggested':<18} {'conf':>5}  {'review':<6}  domain")
    print(f"  {'-' * 6} {'-' * 5}  {'-' * 18} {'-' * 5}  {'-' * 6}  {'-' * 30}")
    for it in items:
        sugg = it["suggested_authority"] or "(unclassified)"
        review = "yes" if it["requires_review"] else "no"
        print(f"  {it['citation_count']:>6} {it['response_count']:>5}  {sugg:<18} "
              f"{_conf(it['confidence']):>5}  {review:<6}  {it['authority_domain']}")

    # Paste-ready YAML, grouped by the LLM's suggested category. Real category keys print as
    # valid YAML; the uncategorized bucket prints commented-out so it can't be pasted blindly.
    groups: dict[str, list[str]] = {}
    for it in items:
        key = _AUTH_TO_YAML_KEY.get(it["suggested_authority"] or "", _UNCATEGORIZED)
        note = f"  # cited {it['citation_count']}x"
        if it["confidence"] is not None:
            note += f", LLM {_conf(it['confidence'])}"
        groups.setdefault(key, []).append(f"  - {it['authority_domain']}{note}")

    print("\n# ---- paste-ready YAML (REVIEW; then bump rules_version + run "
          "reclassify_source_domains.py) ----")
    for key in _AUTH_TO_YAML_KEY.values():
        if key in groups:
            print(f"{key}:")
            for line in groups[key]:
                print(line)
    if _UNCATEGORIZED in groups:
        print(f"# {_UNCATEGORIZED}:")
        for line in groups[_UNCATEGORIZED]:
            print(f"#{line}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
