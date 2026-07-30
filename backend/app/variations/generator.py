"""Generate intent-preserving question variations with the internal Claude model.

The pure helpers (``build_prompt``, ``parse_variations``, ``postprocess``) are separated from
the network call (``generate_variations``) so the prompt shape, parsing, and dedupe/clean
logic are unit-testable offline. Reuses the configured orchestrator model (Claude on Bedrock)
via the same provider registry that powers scoring/arbitration — no new client.

Safety by construction: the prompt forbids new claims/brands/doses, and every candidate is
PII-linted downstream before it can be approved and sent to a monitored model.
"""
from __future__ import annotations

import re

from app.insights.llm import extract_json
from app.providers.base import ModelParams
from app.providers.registry import get_orchestrator_config, get_provider_client
from app.utils.logging import get_logger

logger = get_logger("variations.generator")

MAX_VARIATIONS = 6
DEFAULT_VARIATIONS = 4

_SYSTEM = (
    "You rewrite a single medical/brand monitoring question into alternative phrasings that a "
    "real person might type into an AI assistant. Follow these rules exactly:\n"
    "1. PRESERVE the underlying intent and clinical meaning of the original question.\n"
    "2. Do NOT introduce any new facts, claims, drug names, brands, doses, indications, or "
    "comparisons that are not already present in the original.\n"
    "3. Vary ONLY the wording, structure, specificity, and register (e.g. layperson vs "
    "clinical phrasing) — never the meaning.\n"
    "4. Keep each variation self-contained and answerable on its own.\n"
    "5. Do NOT include personal data, names, or identifiers.\n"
    'Return ONLY compact JSON of the form {"variations": ["...", "..."]} with no prose.'
)


def build_prompt(
    *,
    question_text: str,
    persona: str | None = None,
    therapeutic_area: str | None = None,
    brand_focus: str | None = None,
    domain: str | None = None,
    monitoring_mode: str = "BRAND",
    competitor_focus: list[str] | None = None,
    n: int = DEFAULT_VARIATIONS,
) -> tuple[str, str]:
    """Return (system, user) prompt strings. Pure — no network."""
    ctx: list[str] = []
    if persona:
        ctx.append(f"Audience/persona: {persona}")
    if therapeutic_area:
        ctx.append(f"Therapeutic area: {therapeutic_area}")
    if monitoring_mode == "DISEASE_STATE":
        comps = ", ".join(competitor_focus or []) or "unspecified competitors"
        ctx.append(f"Brand-less landscape question (competitors in scope: {comps})")
    elif brand_focus:
        ctx.append(f"Brand focus: {brand_focus}")
    if domain:
        ctx.append(f"Domain: {domain}")
    context_block = ("\n".join(ctx) + "\n\n") if ctx else ""

    user = (
        f"{context_block}"
        f"Original question:\n{question_text.strip()}\n\n"
        f"Produce {n} distinct variations that keep the exact same intent. "
        f'Return JSON: {{"variations": [...]}}'
    )
    return _SYSTEM, user


def _coerce_list(parsed) -> list[str]:
    """Pull a list of strings out of whatever JSON shape the model returned."""
    if isinstance(parsed, dict):
        for key in ("variations", "questions", "items", "results"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
        else:
            parsed = list(parsed.values())
    if not isinstance(parsed, list):
        return []
    out: list[str] = []
    for item in parsed:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            # Tolerate [{"question": "..."}] / [{"text": "..."}] shapes.
            for key in ("variation", "question", "text", "value"):
                if isinstance(item.get(key), str):
                    out.append(item[key])
                    break
    return out


def parse_variations(raw_text: str) -> list[str]:
    """Parse model output into a list of candidate strings. Pure — tolerant of fenced JSON,
    bare arrays, and light prose. Falls back to line-splitting if no JSON is present."""
    try:
        return _coerce_list(extract_json(raw_text))
    except Exception:  # noqa: BLE001 — fall back to line heuristics
        lines = [ln.strip(" \t-*0123456789.)") for ln in (raw_text or "").splitlines()]
        return [ln for ln in lines if len(ln) > 8 and ln.endswith("?")]


def normalize(text: str) -> str:
    """Normalized dedupe key: lowercased, punctuation/whitespace-collapsed."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def postprocess(base_text: str, candidates: list[str], *, n: int) -> list[str]:
    """Trim, drop blanks / duplicates / echoes of the base, and cap to ``n``. Pure."""
    seen = {normalize(base_text)}
    out: list[str] = []
    for cand in candidates:
        clean = re.sub(r"\s+", " ", (cand or "").strip()).strip('"')
        key = normalize(clean)
        if len(key) < 6 or key in seen:
            continue
        seen.add(key)
        out.append(clean)
        if len(out) >= n:
            break
    return out


async def generate_variations(
    *,
    question_text: str,
    persona: str | None = None,
    therapeutic_area: str | None = None,
    brand_focus: str | None = None,
    domain: str | None = None,
    monitoring_mode: str = "BRAND",
    competitor_focus: list[str] | None = None,
    n: int = DEFAULT_VARIATIONS,
) -> tuple[list[str], str]:
    """Call the internal model and return ``(variations, model_id)``.

    Raises RuntimeError if the model call fails on both attempts. A modest temperature gives
    phrasing diversity (unlike scoring, which is deterministic)."""
    n = max(1, min(int(n or DEFAULT_VARIATIONS), MAX_VARIATIONS))
    system, user = build_prompt(
        question_text=question_text, persona=persona, therapeutic_area=therapeutic_area,
        brand_focus=brand_focus, domain=domain, monitoring_mode=monitoring_mode,
        competitor_focus=competitor_focus, n=n,
    )
    cfg = get_orchestrator_config()
    client = get_provider_client(cfg.provider)
    # Ask for a few extra so postprocessing can drop near-duplicates and still hit n.
    params = ModelParams(max_tokens=800, temperature=0.7)

    last_err: Exception | None = None
    for attempt in range(2):
        try:
            result = await client.chat(cfg.model_id, system, user, params)
            variations = postprocess(question_text, parse_variations(result.text), n=n)
            if variations:
                return variations, cfg.model_id
            last_err = RuntimeError("model returned no usable variations")
        except Exception as e:  # noqa: BLE001 — transport/parse failure, retry once
            last_err = e
            logger.warning("variation generation attempt %d failed: %s", attempt + 1, e)
    raise RuntimeError(f"variation generation failed: {last_err}")
