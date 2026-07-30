"""LLM tagging of harvested questions — persona/TA/brand/domain + adverse-event detection.

Reuses the configured scoring model via insights.llm.chat_json (same provider/creds that
power response scoring). Batched to bound cost. The brand/TA vocabulary is pulled from
the taxonomy at runtime so this module stays content-agnostic (SE-007).
"""
from app.config import taxonomy
from app.insights.llm import chat_json
from app.utils.logging import get_logger

logger = get_logger("harvest.classify")

VALID_PERSONAS = {"Prospect", "Provider", "Patient"}
VALID_DOMAINS = {"Efficacy", "Safety", "Access", "Comparative", "General"}


def build_vocab() -> tuple[str, set[str]]:
    """Build the brand/TA vocabulary string (+ valid TA set) from the taxonomy."""
    cfg = taxonomy.config()
    lines: list[str] = []
    tas: set[str] = set()
    for ta, block in (cfg.get("therapeutic_areas") or {}).items():
        tas.add(ta)
        focus = [b.get("name") for b in block.get("focus_brands", []) if b.get("name")]
        comps = [c.get("name") for c in block.get("competitors", []) if c.get("name")]
        inds = sorted({i for b in block.get("focus_brands", []) for i in b.get("indications", [])})
        lines.append(
            f"- {ta}: focus brands = {', '.join(focus)}; "
            f"competitors = {', '.join(comps)}; indications = {', '.join(inds)}"
        )
    return "\n".join(lines), tas


_SYSTEM = (
    "You are a medical query classifier for a pharmaceutical LLM-monitoring system. "
    "You receive REAL questions scraped from public health forums. For EACH question, "
    "decide whether it concerns one of the monitored therapeutic areas/brands below, and "
    "tag it. Return STRICT JSON only — no prose.\n\n"
    "Field rules:\n"
    "- persona: Prospect (exploring / newly diagnosed / not yet on therapy), "
    "Patient (already taking a therapy), or Provider (clinician / technical phrasing).\n"
    "- domain: one of Efficacy, Safety, Access, Comparative, General.\n"
    "- brand_focus: the monitored brand the question most concerns. Prefer the AbbVie "
    "focus brand; use a competitor name only when no focus brand applies.\n"
    "- therapeutic_area: exactly one of the listed area names.\n"
    "- relevant: false if the question is NOT about any monitored brand or area.\n"
    "- adverse_event: true ONLY if the asker describes experiencing a specific harm or "
    "side effect from a named drug (a pharmacovigilance signal), e.g. 'I started X and "
    "now I have Y'. General safety questions are NOT adverse events."
)


async def classify_batch(questions: list[str], vocab: str,
                         hints: list[str | None] | None = None) -> list[dict]:
    """Tag a batch of questions. Returns a list aligned by index (best-effort).

    `hints` carries the persona lens of the search that surfaced each question; it is
    passed to the model as a prior and used as a fallback in normalize_tags.
    """
    if not questions:
        return []
    hints = hints or [None] * len(questions)
    numbered = "\n".join(
        f"{i}.{f' [likely {h}]' if h else ''} {q}"
        for i, (q, h) in enumerate(zip(questions, hints))
    )
    user = (
        f"Monitored therapeutic areas and brands:\n{vocab}\n\n"
        f"Questions:\n{numbered}\n\n"
        "A bracketed [likely <persona>] hint may precede a question, reflecting the "
        "search lens that surfaced it; treat it as a prior but override it when the "
        "wording clearly indicates a different persona.\n"
        "Return a JSON array with one object per question, each having keys: "
        "index (int, matching the number above), relevant (bool), persona, "
        "therapeutic_area, brand_focus, domain, adverse_event (bool). "
        "Use null for unknown string fields."
    )
    try:
        data = await chat_json(_SYSTEM, user, max_tokens=2000)
    except Exception as e:  # noqa: BLE001 — degrade to untagged rather than fail the run
        logger.warning("classify_batch failed: %s", e)
        return [{} for _ in questions]

    if isinstance(data, dict):
        data = data.get("results") or data.get("questions") or data.get("items") or []
    if not isinstance(data, list):
        return [{} for _ in questions]

    by_index: dict[int, dict] = {}
    for obj in data:
        if isinstance(obj, dict) and isinstance(obj.get("index"), int):
            by_index[obj["index"]] = obj
    if not by_index:  # model omitted indices — fall back to positional
        for i, obj in enumerate(data):
            if isinstance(obj, dict):
                by_index[i] = obj
    return [by_index.get(i, {}) for i in range(len(questions))]


def normalize_tags(obj: dict, valid_tas: set[str], hint: str | None = None) -> dict:
    """Clamp raw LLM output to valid enums and compute a relevance score.

    `hint` (the persona lens of the surfacing search) is a fallback when the model does
    not return a valid persona.
    """
    persona = obj.get("persona")
    persona = persona if persona in VALID_PERSONAS else None
    if persona is None and hint in VALID_PERSONAS:
        persona = hint
    domain = obj.get("domain")
    domain = domain if domain in VALID_DOMAINS else None
    ta = obj.get("therapeutic_area")
    ta = ta if ta in valid_tas else None
    brand = obj.get("brand_focus") or None
    relevant = bool(obj.get("relevant", False))
    ae = bool(obj.get("adverse_event", False))

    if not relevant:
        score = 0.2
    elif brand and ta:
        score = 0.9
    else:
        score = 0.6

    return {
        "persona": persona,
        "therapeutic_area": ta,
        "brand_focus": brand,
        "domain": domain,
        "relevance_score": score,
        "ae_flag": ae,
        "relevant": relevant,
    }
