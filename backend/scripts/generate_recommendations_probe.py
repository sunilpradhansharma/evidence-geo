"""End-to-end probe for the GEO Intervention Recommendations pipeline (BR-012).

Runs the REAL pipeline against the local DB in its own process (so it reads .env fresh and
is immune to a running server's lru_cached settings):

    find gaps (latest score = SECOND_LINE / NOT_RECOMMENDED)
      -> SEMrush enrich (LIVE search volume + domain authority)
      -> LLM reasoning (Bedrock scoring model — BILLED, one call per gap)
      -> impact ranking (gap_severity x log-damped volume multiplier)
      -> persist a new batch

Then prints the batch summary + top ranked recommendations, and how many rows used LIVE
vs STUB SEMrush metrics. The persisted batch is exactly what the dashboard shows at
/dashboard/recommendations (it reads the latest batch).

WARNING: makes BILLED Bedrock + SEMrush calls. ``limit`` bounds the number of gaps, i.e.
the number of LLM calls.

Run (cwd = backend/):
    python -m scripts.generate_recommendations_probe             # limit 8, all filters open
    python -m scripts.generate_recommendations_probe 5           # limit 5
    python -m scripts.generate_recommendations_probe 5 Provider  # limit 5, persona=Provider
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.models.database import AsyncSessionLocal, init_db  # noqa: E402
from app.remediation import engine, semrush  # noqa: E402
from app.remediation import gaps as gaps_mod  # noqa: E402
from app.services import recommendation_service as svc  # noqa: E402


async def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    persona = sys.argv[2] if len(sys.argv) > 2 else None

    print("=" * 84)
    print("GEO Intervention Recommendations — full pipeline probe (BR-012)")
    print("=" * 84)
    print(f"semrush configured : {semrush.is_configured()}")
    print(f"limit (max gaps)   : {limit}")
    print(f"persona filter     : {persona or '(all)'}")

    await init_db()

    async with AsyncSessionLocal() as db:
        # 1) Preview how many gaps exist BEFORE spending any LLM budget.
        preview = await gaps_mod.find_gaps(db, persona=persona, limit=limit)
        print(f"\ngaps available (<= limit): {len(preview)}")
        for g in preview:
            topic = g.get("indication") or g.get("therapeutic_area") or "?"
            print(f"  - {g.get('brand_focus')!r} [{g.get('competitive_position')}] "
                  f"vs {g.get('outperforming_competitor') or '?'} "
                  f"| {g.get('llm_name')} | {g.get('persona')} | {topic}")
        if not preview:
            print("\nNo gaps found (no scored responses positioned SECOND_LINE / NOT_RECOMMENDED).")
            print("Nothing for the engine to enrich, so no SEMrush/LLM calls would run.")
            return

        # 2) Run the REAL pipeline (this is where SEMrush + Bedrock are called + billed).
        print("\nrunning engine.generate() ... (live SEMrush + billed Bedrock per gap)")
        summary = await engine.generate(db, persona=persona, limit=limit)
        print("\nsummary:", summary)

        # 3) Read back the ranked batch (exactly what the dashboard renders).
        listing = await svc.list_recommendations(db, batch_id=summary["batch_id"])
        items = listing["items"]
        live = sum(1 for it in items if it["metrics_source"] == "live")
        print(f"\nbatch_id  : {listing['batch_id']}")
        print(f"generated : {listing['count']}")
        print(f"metrics   : {live} live / {listing['count'] - live} stub")

        print("\nTop ranked recommendations:")
        for i, it in enumerate(items[:10], 1):
            comp = it["outperforming_competitor"] or "?"
            dom = f" @ {it['competitor_domain']}" if it["competitor_domain"] else ""
            print(f"\n  #{i}  impact={it['impact_score']}  [{it['metrics_source']}]  {it['content_type']}")
            print(f"      {it['brand_focus']} ({it['competitive_position']}) vs {comp}{dom}")
            print(f"      vol={it['search_volume']} authority={it['domain_authority']} "
                  f"mult={it['volume_multiplier']} | {it['llm_name']} | {it['persona']}")
            print(f"      action: {(it['recommended_action'] or '')[:170]}")

    print("\n" + "=" * 84)
    print("Done. The dashboard shows this latest batch at /dashboard/recommendations.")
    print("=" * 84)


if __name__ == "__main__":
    asyncio.run(main())
