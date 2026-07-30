"""Phase-0 spike: verify OpenEvidence login + one question against the LIVE site.

Drives the real browser (headful by default so you can watch and solve any first-time
challenge), logs in with OE_EMAIL/OE_PASSWORD (seeding the reusable session in
OE_USER_DATA_DIR), asks one question, and prints the scraped answer + sources. Use this
to confirm whether your IP is blocked and to tune the OE_*_SELECTOR settings against the
real DOM before turning the unattended worker on.

Prereq:  pip install playwright && playwright install chromium

Run from the backend/ directory:
    python -m scripts.oe_spike "What is the mechanism of action of Vraylar?"
    python -m scripts.oe_spike --headless "..."     # no visible window
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config.settings import get_settings  # noqa: E402

DEFAULT_QUESTION = "What is the mechanism of action of cariprazine (Vraylar)?"


async def main(question: str, headless: bool) -> int:
    from app.openevidence_auto.browser import OpenEvidenceBrowser, OpenEvidenceError

    settings = get_settings()
    settings.oe_headless = headless  # CLI overrides .env for the spike

    if not (settings.oe_email and settings.oe_password):
        print("NOTE: OE_EMAIL/OE_PASSWORD not set. If no saved session exists, log in "
              "manually in the opened window when prompted.")

    try:
        async with OpenEvidenceBrowser(settings) as browser:
            print(f"Session profile: {browser.user_data_dir}")
            print("Ensuring logged in ...")
            try:
                await browser.ensure_logged_in()
            except OpenEvidenceError as e:
                if headless:
                    print(f"LOGIN FAILED: {e}\nSee exports/oe_debug for screenshots.")
                    return 2
                print(f"Auto-login did not complete ({e}).")
                input("Finish logging in in the browser window, then press Enter to continue...")

            print(f"\nAsking: {question}\n")
            answer, sources = await browser.ask(question)

            print("=" * 72)
            print("ANSWER:\n")
            print(answer)
            print("\n" + "=" * 72)
            print(f"SOURCES ({len(sources)}):")
            for i, s in enumerate(sources, 1):
                print(f"  [{i}] {s.get('title') or '(no title)'} -> {s['url']}")
            print("=" * 72)
            return 0
    except OpenEvidenceError as e:
        print(f"ERROR: {e}")
        print("Check exports/oe_debug for screenshots to triage selectors/blocking.")
        return 2


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="OpenEvidence login + ask spike")
    ap.add_argument("question", nargs="?", default=DEFAULT_QUESTION)
    ap.add_argument("--headless", action="store_true", help="run without a visible browser window")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.question, headless=args.headless)))
