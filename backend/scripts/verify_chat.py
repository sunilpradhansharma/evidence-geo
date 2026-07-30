"""Quick verification harness for the Cortex Agent chat (Tier 1 "ask anything").

Asks a handful of representative questions that exercise the newly exposed free-text
columns (alert detail, scoring rationale, consensus answer/recommendation) plus the
general-LLM fallback, printing each plain-English answer so we can confirm the agent now
summarizes and explains instead of only counting rows or refusing.

Run:  python -m scripts.verify_chat
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.snowflake import agent  # noqa: E402

# (label, question) — label documents which Tier 1 capability the question probes.
QUESTIONS = [
    ("alert summary (alerts.detail)", "Summarize the competitor advantage alerts"),
    ("scoring rationale (why)", "Why do models rate Humira negatively?"),
    ("consensus answer", "What was the agreed recommendation in the latest consensus records?"),
    ("run cost / tokens", "What was the total cost and tokens for the most recent runs?"),
    ("general fallback (no SQL)", "What kinds of questions can you help me answer?"),
]


async def main() -> None:
    if not agent.is_enabled():
        print(
            "Cortex Agent is not enabled. Check SNOWFLAKE_ENABLED, "
            "SNOWFLAKE_CORTEX_AGENT_ENABLED and credentials in .env. Aborting."
        )
        return

    for label, question in QUESTIONS:
        print("\n" + "=" * 88)
        print(f"[{label}]")
        print("Q:", question)
        try:
            result = await agent.chat(question)
            print("A:", (result.get("answer") or "").strip())
            if result.get("error"):
                print("   [error]:", result["error"])
        except Exception as exc:  # noqa: BLE001
            print("   [exception]:", exc)


if __name__ == "__main__":
    asyncio.run(main())
