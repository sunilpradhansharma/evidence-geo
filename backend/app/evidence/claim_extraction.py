"""The LLM boundary for Phase 8 — decompose a response into structured claims.

**The only place in the phase where a model is asked anything, and it is never asked for a
verdict.** It reports what the response said: which drug, which comparator, which direction,
which hedging words. Whether that was right is decided by ``evidence.claims``, in pure
Python, against our evidence.

That split is not stylistic. A model asked to grade itself against evidence produces a
finding nobody can reproduce, appeal, or put in front of a medical reviewer — and the same
model that wrote a wrong answer is a poor choice to mark it. Keeping extraction here and
judgement there means the verdict is a function of stored data, so re-running it a year
later on the same rows gives the same answer.

``parse_claims`` is pure and exported separately from the call that fetches them, so every
prompt-response shape this module has to survive is testable without a network.
"""
from __future__ import annotations

import asyncio
import json

from app.evidence import claims as cl
from app.providers.base import ModelParams
from app.providers.registry import get_provider_client, get_scoring_config
from app.config.settings import get_settings
from app.insights.llm import extract_json
from app.utils.logging import get_logger

logger = get_logger("claim_extraction")

EXTRACTION_VERSION = "v1"

# Bounded so one rambling answer cannot turn into fifty findings and a large bill. Responses
# that genuinely make more claims than this are truncated at the model's own ordering, which
# puts the load-bearing claims first.
MAX_CLAIMS = 12

SYSTEM_PROMPT = (
    "You are a claim extraction tool for pharmaceutical evidence monitoring.\n"
    "\n"
    "Decompose the assistant response into ATOMIC, INDEPENDENTLY CHECKABLE claims about "
    "medicines. Report ONLY what the response says. You are NOT evaluating whether any "
    "claim is correct — another system does that against a clinical evidence database. "
    "Never add a claim the response did not make, and never omit one because you believe "
    "it is wrong.\n"
    "\n"
    "Return ONLY a JSON object, no prose:\n"
    '{"claims": [{\n'
    '  "claim_text": verbatim sentence or clause from the response,\n'
    '  "claim_type": one of APPROVAL_CLAIM|SAFETY_WARNING_CLAIM|TRIAL_RESULT_CLAIM|'
    'DIRECT_COMPARISON_CLAIM|RANKING_CLAIM|PIPELINE_CLAIM|MECHANISM_CLAIM|CERTAINTY_CLAIM,\n'
    '  "subject": the drug or brand the claim is about,\n'
    '  "comparator": the other drug when the claim compares two, else null,\n'
    '  "indication": the disease named, else null,\n'
    '  "outcome": the endpoint named (e.g. ACR50, PASI90), else null,\n'
    '  "direction": SUPERIOR|INFERIOR|SIMILAR|NO_DIRECTION — is the SUBJECT better, worse '
    'or the same as the comparator,\n'
    '  "polarity": ASSERTED or NEGATED — NEGATED when the response denies the claim '
    '("is NOT approved", "does NOT carry a boxed warning"),\n'
    '  "certainty": DEFINITIVE|PROBABLE|HEDGED|UNCERTAIN — how strongly the response '
    'worded it. DEFINITIVE: "is more effective", "the best option". PROBABLE: "generally", '
    '"tends to". HEDGED: "may", "some evidence suggests". UNCERTAIN: "unclear", '
    '"evidence is limited". Judge the WORDING ONLY, never whether the confidence is '
    'warranted,\n'
    '  "magnitude": a number the response stated (45 for "45% of patients"), else null,\n'
    '  "magnitude_unit": the unit of that number ("%", "weeks"), else null,\n'
    '  "cited_identifiers": trial or publication identifiers the response named '
    '(["NCT03104400"]), else []\n'
    "}]}\n"
    "\n"
    "Rules:\n"
    "- Split compound sentences. \"Rinvoq is approved for PsA and works better than "
    "Humira\" is TWO claims with different claim_types.\n"
    "- DIRECT_COMPARISON_CLAIM when the response implies a head-to-head trial; "
    "RANKING_CLAIM when it ranks among several or cites a synthesis.\n"
    "- A comparative claim MUST have a comparator and a direction other than NO_DIRECTION. "
    "If the response names no comparator, it is not a comparative claim.\n"
    "- Skip general advice, disclaimers and 'consult your doctor' — they assert nothing "
    "checkable against evidence.\n"
    f"- Return at most {MAX_CLAIMS} claims, most important first.\n"
    "- Return an empty list when the response makes no checkable claim. An empty list is a "
    "valid and correct answer."
)


def build_prompt(question_text: str, response_text: str, *, indication: str | None) -> str:
    """The user half of the extraction call.

    The indication is supplied as *context for reading the text*, never as an instruction to
    assume it: a response can answer a PsA question by talking about psoriasis, and an
    extractor told to label everything PsA would send the claim to the wrong network and
    produce a mismatch finding out of our own prompt.
    """
    scope = f"Indication under monitoring (context only — use what the response says): {indication}\n" if indication else ""
    return (
        f"{scope}"
        f"USER QUESTION:\n{question_text}\n\n"
        f"ASSISTANT RESPONSE:\n{response_text[:8000]}\n\n"
        "Extract the claims now."
    )


def parse_claims(payload: object) -> tuple[list[cl.ExtractedClaim], list[dict]]:
    """``(claims, rejected)`` from a parsed extraction payload. Pure.

    Rejections are returned, not dropped. A model that keeps emitting
    ``DIRECT_COMPARISON_CLAIM`` with no comparator is a prompt defect, and it is only
    visible if the count surfaces somewhere — silently discarding malformed claims makes an
    extractor that has quietly stopped working look like a corpus with nothing to say.
    """
    if isinstance(payload, dict):
        raw = payload.get("claims")
    elif isinstance(payload, list):
        raw = payload
    else:
        raw = None
    if not isinstance(raw, list):
        return [], [{"reason": "extraction payload has no `claims` list", "raw": repr(payload)[:200]}]

    parsed: list[cl.ExtractedClaim] = []
    rejected: list[dict] = []
    for entry in raw[:MAX_CLAIMS]:
        if not isinstance(entry, dict):
            rejected.append({"reason": "claim is not an object", "raw": repr(entry)[:200]})
            continue
        identifiers = entry.get("cited_identifiers") or []
        try:
            parsed.append(cl.ExtractedClaim(
                claim_text=str(entry.get("claim_text") or "").strip(),
                claim_type=str(entry.get("claim_type") or "").strip().upper(),
                subject=str(entry.get("subject") or "").strip(),
                comparator=_optional(entry.get("comparator")),
                indication=_optional(entry.get("indication")),
                outcome=_optional(entry.get("outcome")),
                direction=str(entry.get("direction") or cl.NO_DIRECTION).strip().upper(),
                polarity=str(entry.get("polarity") or cl.ASSERTED).strip().upper(),
                certainty=str(entry.get("certainty") or cl.HEDGED).strip().upper(),
                magnitude=_number(entry.get("magnitude")),
                magnitude_unit=_optional(entry.get("magnitude_unit")),
                cited_identifiers=tuple(
                    str(x).strip() for x in identifiers if str(x).strip()
                ) if isinstance(identifiers, list) else (),
            ))
        except cl.ClaimError as exc:
            rejected.append({"reason": str(exc), "raw": json.dumps(entry)[:300]})
    return parsed, rejected


def _optional(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None if text.lower() not in ("none", "null", "n/a") else None


def _number(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


async def extract(
    question_text: str, response_text: str, *, indication: str | None = None
) -> dict:
    """Run the extraction model for one response. Network-only, no DB.

    Returns ``{claims, rejected, model_id, ok}``. A failure returns ``ok=False`` with an
    empty claim list rather than raising: claim evaluation is an analysis pass over a run
    that has already completed, and a transport hiccup must not fail the run — the same rule
    scoring and insights tagging already follow.
    """
    if not (response_text or "").strip():
        return {"claims": [], "rejected": [], "model_id": None, "ok": True}

    cfg = get_scoring_config()
    client = get_provider_client(cfg.provider)
    user = build_prompt(question_text, response_text, indication=indication)
    timeout = get_settings().target_call_timeout_seconds + 30

    last_err: Exception | None = None
    for _ in range(2):  # one retry, matching the scorer
        try:
            result = await asyncio.wait_for(
                client.chat(
                    cfg.model_id, SYSTEM_PROMPT, user,
                    ModelParams(max_tokens=2500, temperature=0.0),
                ),
                timeout=timeout,
            )
            parsed, rejected = parse_claims(extract_json(result.text))
            return {
                "claims": parsed,
                "rejected": rejected,
                "model_id": cfg.model_id,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "ok": True,
            }
        except Exception as e:  # noqa: BLE001 — transport/parse/timeout; retry once
            last_err = e
    logger.warning("claim extraction failed: %s", last_err)
    return {"claims": [], "rejected": [], "model_id": cfg.model_id, "ok": False,
            "error": str(last_err)}
