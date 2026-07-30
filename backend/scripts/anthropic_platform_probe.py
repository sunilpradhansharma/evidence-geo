"""Readiness probe for the direct-Anthropic Claude target (web-search citations).

Run this the moment ANTHROPIC_API_KEY lands to confirm the switch works end to end: it loads
the real target config, verifies the `claude` target has swapped from Bedrock to the Anthropic
API, sends one grounded clinical question, and prints the answer + the parsed sources /
search queries / grounded claims (the exact provenance that feeds Source Authority).

Run (cwd = backend, repo-root .venv):
    python -m scripts.anthropic_platform_probe
    python -m scripts.anthropic_platform_probe "What is the first-line treatment for CLL?"

Needs: ANTHROPIC_API_KEY set + `pip install anthropic`. Makes ONE billed Anthropic call.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config.settings import get_settings  # noqa: E402
from app.providers.base import ModelParams  # noqa: E402
from app.providers.registry import get_provider_client, load_targets  # noqa: E402

_DEFAULT_QUESTION = "What is the current first-line treatment for chronic lymphocytic leukemia?"


async def main() -> None:
    question = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_QUESTION
    settings = get_settings()

    if not settings.anthropic_api_key:
        print("ANTHROPIC_API_KEY is not set -> the `claude` target is still on AWS Bedrock.")
        print("Set ANTHROPIC_API_KEY in .env (and restart) to activate the Anthropic path.")
        return

    claude = next((t for t in load_targets() if t.name == "claude"), None)
    if claude is None:
        print("No `claude` target found in targets.yaml.")
        return

    print(f"claude target -> provider={claude.provider}  model_id={claude.model_id}  "
          f"grounding={bool(claude.params.extra.get('grounding'))}  "
          f"force_search={bool(claude.params.extra.get('force_search'))}")
    if claude.provider != "anthropic":
        print("WARNING: claude did not swap to the Anthropic API. Check ANTHROPIC_API_KEY / restart.")
        return

    client = get_provider_client("anthropic")
    params = ModelParams(
        max_tokens=claude.params.max_tokens,
        temperature=claude.params.temperature,
        extra=dict(claude.params.extra),
    )
    print(f"\nAsking: {question}\n" + "-" * 72)
    result = await client.chat(claude.model_id, system="", user=question, params=params)

    print(result.text[:1500] + ("..." if len(result.text) > 1500 else ""))
    print("-" * 72)
    print(f"model_version={result.model_version}  finish={result.finish_reason}  "
          f"tokens in/out={result.prompt_tokens}/{result.completion_tokens}")
    print(f"search_queries ({len(result.search_queries)}): {result.search_queries}")
    print(f"grounded claims: {len(result.grounding_supports)}")
    print(f"\nsources ({len(result.sources)}):")
    for i, s in enumerate(result.sources):
        print(f"  [{i}] {s.domain or '?':<28} {s.title or ''}")
        print(f"       {s.url}")
    if not result.sources:
        print("  (none — the model answered without searching; try a more factual question)")


if __name__ == "__main__":
    asyncio.run(main())
