"""Live probe for the SEMrush Analytics API integration (GEO Interventions / BR-012).

Why this exists: ``app.remediation.semrush.enrich()`` NEVER raises — on any bad key,
wrong endpoint, timeout, non-2xx, or out-of-units response it SILENTLY falls back to
deterministic *stub* metrics. So the GEO Interventions UI can look perfectly fine while
not actually using your SEMrush key at all. The only real signal is ``source == "live"``.

This script makes a few REAL, BILLED SEMrush Analytics API calls (one Keyword Overview,
one Backlinks Overview, and one ``enrich()``), prints the RAW SEMrush response bodies so
any error string (wrong key / out of units / wrong regional database) is visible, then
reports whether ``enrich()`` returns ``source == "live"``.

Because it constructs settings fresh from .env, it does NOT need the backend restarted
(``get_settings()`` is lru_cached inside a running server, but this is its own process).

Run (cwd = backend/):
    python -m scripts.semrush_probe                       # defaults: Skyrizi / drugs.com
    python -m scripts.semrush_probe "Humira" "drugs.com"  # custom keyword + domain
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx  # noqa: E402

from app.config.settings import get_settings  # noqa: E402
from app.remediation import semrush  # noqa: E402


def _mask(key: str) -> str:
    """Show enough to confirm the key loaded without leaking it to the console."""
    if not key:
        return "(empty)"
    if len(key) <= 8:
        return "*" * len(key) + f" (len={len(key)})"
    return f"{key[:4]}...{key[-4:]} (len={len(key)})"


async def _raw_keyword(keyword: str) -> None:
    """Raw Keyword Overview call — reveals the exact SEMrush response for search volume."""
    s = get_settings()
    params = {
        "type": "phrase_this",
        "key": s.semrush_api_key,
        "phrase": keyword,
        "database": s.semrush_database or "us",
        "export_columns": "Ph,Nq,Cp,Co,Nr",
    }
    url = s.semrush_base_url.rstrip("/") + "/"
    print(f"\n[1] Keyword Overview (search volume)  phrase={keyword!r} database={params['database']!r}")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params)
        print(f"    HTTP {resp.status_code}")
        print("    body:", ((resp.text or "").strip()[:400] or "(empty)"))
    except Exception as e:  # noqa: BLE001
        print("    request FAILED:", repr(e))


async def _raw_backlinks(domain: str) -> None:
    """Raw Backlinks Overview call — reveals the exact SEMrush response for Authority Score."""
    s = get_settings()
    params = {
        "type": "backlinks_overview",
        "key": s.semrush_api_key,
        "target": domain,
        "target_type": "root_domain",
        "export_columns": "ascore,total,domains_num",
    }
    url = s.semrush_base_url.rstrip("/") + "/analytics/v1/"
    print(f"\n[2] Backlinks Overview (domain authority)  target={domain!r}")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params)
        print(f"    HTTP {resp.status_code}")
        print("    body:", ((resp.text or "").strip()[:400] or "(empty)"))
    except Exception as e:  # noqa: BLE001
        print("    request FAILED:", repr(e))


async def main() -> None:
    keyword = sys.argv[1] if len(sys.argv) > 1 else "Skyrizi"
    domain = sys.argv[2] if len(sys.argv) > 2 else "drugs.com"

    s = get_settings()
    print("=" * 82)
    print("SEMrush integration probe  (GEO Interventions / BR-012)")
    print("=" * 82)
    print("configured :", semrush.is_configured())
    print("base_url   :", s.semrush_base_url)
    print("database   :", s.semrush_database)
    print("api_key    :", _mask(s.semrush_api_key))

    if not semrush.is_configured():
        print("\nSEMRUSH_API_KEY is not set -> the engine uses STUB metrics. Nothing to probe.")
        return

    await _raw_keyword(keyword)
    await _raw_backlinks(domain)

    print(f"\n[3] semrush.enrich(domain={domain!r}, keyword={keyword!r})  <- what the engine calls")
    result = await semrush.enrich(domain, keyword=keyword)
    print("    result:", result)

    print("\n" + "=" * 82)
    if result.get("source") == "live":
        print("RESULT: LIVE  ✓  The SEMrush key works — GEO Interventions will rank on REAL metrics.")
    else:
        print("RESULT: STUB  ✗  enrich() fell back to SIMULATED metrics — your key was NOT used.")
        print("Check the raw [1]/[2] bodies above for the cause, e.g.:")
        print("  - 'ERROR 120 :: WRONG KEY'        -> the key is invalid / not an Analytics API key")
        print("  - 'ERROR :: API units balance ...'-> out of API units on the plan")
        print("  - 'ERROR 50 :: NOTHING FOUND'     -> no data for that keyword/database (try another)")
        print("  - a UI/Trends login key           -> only the *Analytics API* key works here")
    print("=" * 82)


if __name__ == "__main__":
    asyncio.run(main())
