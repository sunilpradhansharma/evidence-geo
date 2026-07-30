"""Write the question for one uncovered comparison, with the internal Claude model.

Pure helpers (``build_prompt``, ``parse_questions``, ``postprocess``) are split from the
network call so prompt shape and parsing are testable offline, mirroring
``app.variations.generator``. Same orchestrator model, same provider registry, no new client.

Safety by construction: the prompt names the only two agents allowed to appear and forbids
claims, doses and outcomes, so the model is writing a QUESTION, not an answer. Everything
it returns is still PII/injection/AE-screened before staging and still has to clear the
Medical-Affairs gate before any monitoring run.
"""
from __future__ import annotations

import re

from app.curation.coverage import Cell
from app.insights.llm import extract_json
from app.providers.base import ModelParams
from app.providers.registry import get_orchestrator_config, get_provider_client
from app.utils.logging import get_logger

logger = get_logger("curation.generator")

MAX_QUESTION_CHARS = 300

_PERSONA_VOICE = {
    "Patient": (
        "someone already taking one of these treatments, writing in plain first-person "
        "language about their own care"
    ),
    "Prospect": (
        "someone recently diagnosed and choosing between treatments, writing in plain "
        "language without clinical jargon"
    ),
    "Provider": (
        "a prescribing clinician, using accepted clinical terminology and no first-person "
        "patient framing"
    ),
}

_SYSTEM = (
    "You write realistic questions that a real person would type into an AI assistant when "
    "comparing two named prescription treatments. Follow these rules exactly:\n"
    "1. Name ONLY the two treatments given to you. Never mention any other drug, brand or "
    "generic.\n"
    "2. Write a QUESTION, never an answer. Do not state or imply any efficacy, safety, "
    "superiority or outcome claim about either treatment.\n"
    "3. Do not include any dose, strength, frequency or administration schedule.\n"
    "4. Ground the question in the given indication, and keep it answerable on its own.\n"
    "5. No personal data, names, ages, locations or identifiers. No invented case details.\n"
    "6. One sentence where possible, never more than two.\n"
    'Return ONLY compact JSON of the form {"questions": ["..."]} with no prose.'
)


def build_prompt(cells: list[Cell]) -> tuple[str, str]:
    """Return (system, user) prompt strings for a batch of cells. Pure — no network."""
    lines: list[str] = []
    for i, cell in enumerate(cells, start=1):
        voice = _PERSONA_VOICE.get(cell.persona, cell.persona)
        lines.append(
            f"{i}. Treatments: {cell.brand} and {cell.competitor}. "
            f"Indication: {cell.disease}. "
            f"Asked by: {voice}."
        )
    user = (
        "Write exactly one question for each numbered item below, comparing the two named "
        "treatments for that indication in that person's voice.\n\n"
        + "\n".join(lines)
        + f"\n\nReturn {len(cells)} questions in the same order as the items, as JSON: "
        '{"questions": [...]}'
    )
    return _SYSTEM, user


def _coerce_list(parsed) -> list[str]:
    """Pull a list of strings out of whatever JSON shape the model returned."""
    if isinstance(parsed, dict):
        for key in ("questions", "variations", "items", "results"):
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
            for key in ("question", "text", "value"):
                if isinstance(item.get(key), str):
                    out.append(item[key])
                    break
    return out


def parse_questions(raw_text: str) -> list[str]:
    """Parse model output into candidate strings. Tolerant of fenced JSON and light prose."""
    try:
        return _coerce_list(extract_json(raw_text))
    except Exception:  # noqa: BLE001 — fall back to line heuristics
        lines = [ln.strip(" \t-*0123456789.)") for ln in (raw_text or "").splitlines()]
        return [ln for ln in lines if len(ln) > 8 and ln.endswith("?")]


def violates_scope(text: str, cell: Cell, *, comparison_agents: set[str]) -> str | None:
    """Why this candidate is unusable for this cell, or ``None`` when it is fine.

    Rejecting is the point: a comparison question that quietly names a third agent is not
    the cell that was requested, and a bank whose questions do not match their own cells
    cannot be reasoned about. Checked against a curated drug vocabulary rather than a
    generic word list so it cannot fire on ordinary prose.

    That vocabulary is ``taxonomy.comparison_agents()``, NOT every drug we can name. A
    declared background therapy is excluded, because "after methotrexate stopped working,
    is Rinvoq or Cosentyx the better next step?" is the requested comparison with its
    clinical context attached — the framing Rinvoq's own RA indication is written in —
    rather than a three-way. Policing the whole vocabulary threw those away and left the
    cell an unfilled gap.
    """
    from app.prompt_volume import mapping

    if not text or len(text) < 12:
        return "empty or too short"
    if len(text) > MAX_QUESTION_CHARS:
        return f"longer than {MAX_QUESTION_CHARS} characters"
    if "?" not in text:
        return "not phrased as a question"
    if not mapping.mentions(text, cell.brand):
        return f"does not name {cell.brand}"
    if not mapping.mentions(text, cell.competitor):
        return f"does not name {cell.competitor}"

    allowed = {cell.brand.strip().lower(), cell.competitor.strip().lower()}
    for drug in comparison_agents:
        if drug in allowed:
            continue
        if mapping.mentions(text, drug):
            return f"names an out-of-scope drug: {drug}"
    return None


def postprocess(
    cells: list[Cell], candidates: list[str]
) -> tuple[list[tuple[Cell, str]], list[dict]]:
    """Pair candidates back to their cells positionally, dropping anything unusable.

    Returns ``(accepted, rejected)``. The model is asked for answers in item order; a
    short or misaligned reply loses the tail rather than silently mis-attaching a
    question to the wrong comparison.
    """
    from app.config import taxonomy

    agents = {
        name.strip().lower() for name in taxonomy.comparison_agents() if name.strip()
    }

    accepted: list[tuple[Cell, str]] = []
    rejected: list[dict] = []
    seen: set[str] = set()

    for cell, raw in zip(cells, candidates):
        clean = re.sub(r"\s+", " ", (raw or "").strip()).strip('"')
        reason = violates_scope(clean, cell, comparison_agents=agents)
        if reason:
            rejected.append({"cell": cell.key, "question_text": clean, "reason": reason})
            continue
        key = clean.lower()
        if key in seen:
            rejected.append({"cell": cell.key, "question_text": clean,
                             "reason": "duplicate of another generated question"})
            continue
        seen.add(key)
        accepted.append((cell, clean))

    for cell in cells[len(candidates):]:
        rejected.append({"cell": cell.key, "question_text": None,
                         "reason": "model returned fewer questions than cells"})
    return accepted, rejected


async def generate_for_cells(
    cells: list[Cell],
) -> tuple[list[tuple[Cell, str]], list[dict], str]:
    """Call the model for a batch of cells. Returns ``(accepted, rejected, model_id)``.

    Raises RuntimeError if the call fails on both attempts, so a caller staging results
    never mistakes a transport failure for "no gaps worth filling".
    """
    if not cells:
        return [], [], ""
    system, user = build_prompt(cells)
    cfg = get_orchestrator_config()
    client = get_provider_client(cfg.provider)
    # Enough headroom for one question per cell, with mild diversity in phrasing.
    params = ModelParams(max_tokens=200 * len(cells) + 200, temperature=0.6)

    last_err: Exception | None = None
    for attempt in range(2):
        try:
            result = await client.chat(cfg.model_id, system, user, params)
            accepted, rejected = postprocess(cells, parse_questions(result.text))
            if accepted:
                return accepted, rejected, cfg.model_id
            last_err = RuntimeError("model returned no usable questions")
        except Exception as e:  # noqa: BLE001 — transport/parse failure, retry once
            last_err = e
            logger.warning("curation generation attempt %d failed: %s", attempt + 1, e)
    raise RuntimeError(f"curation generation failed: {last_err}")
