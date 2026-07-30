"""Reasoning prompt template + approved content-type enum for the GEO engine (BR-012.3).

The LLM is constrained to output a strict JSON object whose ``content_type`` MUST be one
of ``APPROVED_CONTENT_TYPES`` (BR-012.2). SEMrush metrics + the competitive gap are
injected into the user prompt so the recommendation is data-backed (BR-012.5).
"""
import json

# BR-012.2 — the LLM may ONLY choose from this predefined list. Keep as a plain list so
# the API can expose it and tests can assert membership.
APPROVED_CONTENT_TYPES = [
    "FAQ",
    "Clinical Abstract",
    "Comparison Table",
    "Dosing & Administration Guide",
    "Mechanism of Action Explainer",
    "Patient Education Page",
    "HCP Resource Hub",
]

SYSTEM = (
    "You are a pharmaceutical GEO (Generative Engine Optimization) strategist. AI "
    "assistants are under-representing a focus brand relative to a competitor for a "
    "specific question. Propose ONE concrete, publishable content asset that would "
    "improve how AI assistants represent the focus brand for this topic.\n\n"
    "Return ONLY a single JSON object, no prose, matching exactly this schema:\n"
    "{\n"
    '  "content_type": one of ' + " | ".join(APPROVED_CONTENT_TYPES) + ",\n"
    '  "recommended_action": a specific, plain-language instruction describing exactly '
    "what to publish (one or two sentences),\n"
    '  "rationale": a brief plain-text explanation referencing the competitive gap and '
    "the SEO evidence (search volume / domain authority),\n"
    '  "content_brief": an array of 3 to 5 short strings — the outline sections / key points '
    "the asset should cover,\n"
    '  "suggested_questions": an array of 2 to 3 GENERIC, non-promotional questions worth '
    "adding to ongoing monitoring for this topic\n"
    "}\n\n"
    "Rules:\n"
    "- content_type MUST be exactly one of the listed values. Do NOT invent new types.\n"
    "- These are STRATEGIC SUGGESTIONS, not medical/legal/regulatory-approved content. "
    "Never imply approval and never assert clinical claims as established fact.\n"
    "- Keep the action actionable for a brand/content team (what asset, what angle).\n"
    "- content_brief items are short outline points (a few words each), not paragraphs.\n"
    "- suggested_questions must be generic disease/therapy questions (no patient data, no "
    "PII, no off-label solicitation), suitable for Medical-Affairs review before any use."
)


def build_user_prompt(gap: dict, metrics: dict) -> str:
    """Assemble the per-gap user prompt from the gap record + SEMrush metrics."""
    position = gap.get("competitive_position", "")
    position_label = "not recommended" if position == "NOT_RECOMMENDED" else "second-line"
    missing = gap.get("missing_citations") or []
    missing_block = "\n".join(f"- {c}" for c in missing[:8]) or "- (none captured)"

    return (
        f"THERAPEUTIC AREA: {gap.get('therapeutic_area') or 'n/a'}\n"
        f"INDICATION: {gap.get('indication') or 'n/a'}\n"
        f"PERSONA (audience): {gap.get('persona') or 'n/a'}\n"
        f"FOCUS BRAND: {gap.get('brand_focus') or 'n/a'}\n"
        f"AI PLATFORM THAT SHOWED THE GAP: {gap.get('llm_name') or 'n/a'}\n\n"
        f"QUESTION ASKED:\n{gap.get('question_text') or 'n/a'}\n\n"
        f"THE GAP: the AI response positioned the focus brand as "
        f"**{position_label}** ({position}).\n"
        f"OUTPERFORMING COMPETITOR: {gap.get('outperforming_competitor') or 'unknown'}"
        + (f" ({gap['competitor_domain']})" if gap.get("competitor_domain") else "")
        + "\n\n"
        "SEMrush SEO evidence for the outperforming competitor:\n"
        f"- search_volume: {metrics.get('search_volume')}\n"
        f"- domain_authority: {metrics.get('domain_authority')}\n"
        f"- metrics_source: {metrics.get('source')}\n\n"
        "Citations the AI relied on that the focus brand is absent from:\n"
        f"{missing_block}\n\n"
        "Produce the JSON recommendation now."
    )


EVIDENCE_SYSTEM = (
    "You are a pharmaceutical medical-communications strategist. An AI assistant made a "
    "specific claim that has been checked against a curated clinical evidence base, and the "
    "check found a problem. Propose ONE concrete, publishable asset that would correct or "
    "close it.\n\n"
    "Return ONLY a single JSON object, no prose, matching exactly this schema:\n"
    "{\n"
    '  "content_type": one of ' + " | ".join(APPROVED_CONTENT_TYPES) + ",\n"
    '  "recommended_action": a specific, plain-language instruction describing exactly '
    "what to publish (one or two sentences),\n"
    '  "rationale": a brief plain-text explanation referencing the specific claim and what '
    "the evidence actually shows,\n"
    '  "content_brief": an array of 3 to 5 short strings — the outline sections the asset '
    "should cover,\n"
    '  "suggested_questions": an array of 2 to 3 GENERIC, non-promotional questions worth '
    "adding to ongoing monitoring for this topic\n"
    "}\n\n"
    "Rules:\n"
    "- content_type MUST be exactly one of the listed values. Do NOT invent new types.\n"
    "- **Never propose content that asserts more than the evidence shown below supports.** "
    "The finding you are answering is that a model over-stated something; answering it with "
    "an asset that over-states in our favour reproduces the fault under our own name.\n"
    "- When the finding is UNSUPPORTED, the correct asset states plainly that the "
    "comparison is not established. An honest 'no head-to-head data exists' page is the "
    "remedy. Do NOT propose content that implies the comparison has been made.\n"
    "- When the finding is a SAFETY contradiction, the asset is a factual safety "
    "communication. No promotional framing of any kind.\n"
    "- These are STRATEGIC SUGGESTIONS, not medical/legal/regulatory-approved content. "
    "Never imply approval.\n"
    "- suggested_questions must be generic disease/therapy questions (no patient data, no "
    "PII, no off-label solicitation)."
)


def build_evidence_user_prompt(gap: dict) -> str:
    """The per-claim prompt for an alignment gap.

    Carries the resolver's own numbers and the grader's own reason verbatim. The model is
    drafting *prose around a finding it is not allowed to revisit* — every fact it needs is
    supplied, so it never has to reach for a clinical claim of its own.
    """
    evidence_action = gap.get("evidence_action")
    return (
        f"THERAPEUTIC AREA: {gap.get('therapeutic_area') or 'n/a'}\n"
        f"INDICATION: {gap.get('indication') or 'n/a'}\n"
        f"PERSONA (audience): {gap.get('persona') or 'n/a'}\n"
        f"SUBJECT OF THE CLAIM: {gap.get('brand_focus') or 'n/a'}\n"
        f"COMPARATOR: {gap.get('outperforming_competitor') or 'none named'}\n"
        f"AI PLATFORM THAT MADE THE CLAIM: {gap.get('llm_name') or 'n/a'}\n\n"
        f"QUESTION ASKED:\n{gap.get('question_text') or 'n/a'}\n\n"
        f"THE CLAIM THE AI MADE:\n{gap.get('claim_text') or 'n/a'}\n\n"
        f"WHAT OUR EVIDENCE SHOWS ({gap.get('classification') or 'n/a'}):\n"
        f"{gap.get('finding_reason') or 'n/a'}\n\n"
        f"CERTAINTY ASSESSMENT: {gap.get('certainty_verdict') or 'not assessed'}\n"
        f"STRATEGIC IMPLICATION: {gap.get('strategic_implication') or 'n/a'} — "
        f"{gap.get('implication_reason') or ''}\n"
        + (f"NON-CONTENT REMEDY ALREADY IDENTIFIED: {evidence_action}\n" if evidence_action else "")
        + "\nProduce the JSON recommendation now."
    )


def coerce_content_type(value: object) -> str:
    """Map a model-returned content_type to an approved value (BR-012.2 fallback = FAQ)."""
    if isinstance(value, str):
        for allowed in APPROVED_CONTENT_TYPES:
            if value.strip().lower() == allowed.lower():
                return allowed
    return "FAQ"


def _coerce_str_list(value: object, *, cap: int, max_len: int = 240) -> list[str]:
    """Normalise a model value into a clean list[str] (drops empties, strips bullets, caps)."""
    if isinstance(value, str):
        parts = value.splitlines() if "\n" in value else [value]
    elif isinstance(value, list):
        parts = value
    else:
        return []
    out: list[str] = []
    for p in parts:
        s = str(p or "").strip().lstrip("-•*").strip()
        if s:
            out.append(s[:max_len])
        if len(out) >= cap:
            break
    return out


def coerce_brief(value: object) -> list[str]:
    """Content-brief outline points, capped at 5 (D/E)."""
    return _coerce_str_list(value, cap=5)


def coerce_questions(value: object) -> list[str]:
    """Suggested generic monitoring questions, capped at 3 (D)."""
    return _coerce_str_list(value, cap=3)


def _example_payload() -> str:
    """A tiny example used only in logs/debugging."""
    return json.dumps(
        {
            "content_type": "FAQ",
            "recommended_action": "Publish an FAQ ...",
            "rationale": "...",
        }
    )
