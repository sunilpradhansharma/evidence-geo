"""The extraction pipeline's three stages, and the single-call baseline they must beat.

``extraction.py`` defines the *contracts* — ``ExtractedValue``, ``HarmonisationProposal``,
``ValidationOutcome``, ``StageProvenance`` — and enforces the proposal-only rule
structurally. This module is what actually produces them. Its docstring referred to
``run_baseline`` as though it existed; it did not, so the descope path the plan's risk
register depends on had no artefact behind it.

**Both runners satisfy one interface**, which is the whole point:

    async def runner(task: ExtractionTask) -> PipelineResult

``run_baseline`` is one ``chat_json`` call. ``run_pipeline`` is extraction, then
harmonisation, then validation. They are interchangeable at every call site, so if the
harness cannot show the pipeline is better on the same corpus, deleting the agents is a
one-line change rather than a refactor. **Agent count is not a quality metric.**

**Sequential stages, not a council.** These are three responsibilities in order, not peers
debating. Extraction reads values; harmonisation *proposes* alignments and can never apply
one; validation re-derives against the retained source and a disagreement blocks promotion
outright rather than shaving a confidence score.

**Every stage records who produced what.** Model id, prompt version and pipeline version
travel on each output through ``StageProvenance``. Without that a non-deterministic
pipeline is unauditable, which is disqualifying in a system with a medical review gate.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.evidence import licensing
from app.evidence.extraction import (
    PIPELINE_VERSION,
    STAGE_BASELINE,
    STAGE_EXTRACTION,
    STAGE_HARMONISATION,
    STAGE_VALIDATION,
    ExtractedValue,
    HarmonisationProposal,
    PipelineResult,
    StageProvenance,
    ValidationOutcome,
    screen_proposal,
)

logger = logging.getLogger(__name__)

# Bumped whenever the wording changes. Recorded on every value, because "the model said so"
# is not reproducible unless you also know which prompt it was answering.
BASELINE_PROMPT_VERSION = "baseline-1"
EXTRACTION_PROMPT_VERSION = "extraction-1"
HARMONISATION_PROMPT_VERSION = "harmonisation-1"
VALIDATION_PROMPT_VERSION = "validation-1"

_NO_VALUE = object()

_RULES = (
    "Read ONLY the document supplied. Never infer, complete or correct a value from "
    "background knowledge — an invented number is worse than a missing one here, because "
    "a missing one is visible.\n"
    "If the document does not state a field, return null for it and say so in the notes.\n"
    "Return STRICT JSON and nothing else."
)

_BASELINE_SYSTEM = (
    "You extract structured clinical-trial facts from a source document.\n"
    + _RULES
    + "\nReturn {\"values\": {field: value}, \"confidence\": {field: 0..1}, "
    "\"source_text\": {field: \"the exact span you read it from\"}}."
)

_EXTRACTION_SYSTEM = (
    "You extract arm-level clinical-trial results from a source document: endpoints, "
    "event counts, denominators, placebo response rates and baseline variance.\n"
    + _RULES
    + "\nReturn {\"values\": {field: value}, \"confidence\": {field: 0..1}, "
    "\"source_text\": {field: \"the exact span\"}, \"rationale\": {field: \"why\"}}."
)

_HARMONISATION_SYSTEM = (
    "You review extracted trial results and PROPOSE whether two timepoints or population "
    "strata could be aligned for pooling. You are proposing to a human reviewer.\n"
    "You may NOT apply an alignment, edit a value, or clear a mismatch flag. Your output "
    "is a suggestion that a statistician will accept or reject.\n"
    "Cite the ISPOR or Cochrane basis where one applies. A citation is context, not "
    "authority.\n"
    "Return STRICT JSON: {\"proposals\": [{\"kind\": \"TIMEPOINT\"|\"STRATA\", "
    "\"from_value\": ..., \"to_value\": ..., \"rationale\": \"...\", "
    "\"confidence\": 0..1, \"guideline_citation\": \"...\"}]}."
)

_VALIDATION_SYSTEM = (
    "You re-derive previously extracted values from the source document, independently. "
    "You are checking, not extracting: for each field, read the document and report what "
    "it says, then whether that agrees with the value you were given.\n"
    "Disagreeing is the useful outcome. Do not reconcile, round, or explain away a "
    "difference.\n"
    "Return STRICT JSON: {\"checks\": [{\"field\": \"...\", \"observed\": ..., "
    "\"agrees\": true|false, \"note\": \"...\"}]}."
)


@dataclass(frozen=True)
class ExtractionTask:
    """One document to extract from, and the bounds the extractor works inside.

    ``document`` is whatever retention permits — the full payload for a public-domain
    source, only the retained fragment for a restricted one. The licence class travels with
    it so validation coverage can be reported per tier rather than as one flattering
    figure.
    """

    source_id: str
    document: str
    fields: tuple[str, ...]
    license_class: str = licensing.PUBLIC_DOMAIN
    approved_time_window: tuple[float, float] | None = None
    # Values already on record, for the validation stage to check against. Empty for a
    # first pass, in which case validation checks the extraction stage's own output.
    known_values: dict[str, Any] = field(default_factory=dict)

    @property
    def validation_reach(self) -> str:
        return licensing.validation_reach(self.license_class)


async def _ask(system: str, user: str, *, max_tokens: int = 2000) -> tuple[dict, str | None]:
    """One model call. Returns ``({}, error)`` rather than raising.

    Mirrors the never-raises boundary every source adapter keeps: a provider outage during
    extraction must degrade to "nothing extracted", never take down the caller. The error
    is returned so it lands in ``PipelineResult.errors`` and is visible in the report,
    instead of being logged and forgotten.
    """
    from app.insights.llm import chat_json  # late import: keeps this module import-cheap

    try:
        payload = await chat_json(system, user, max_tokens=max_tokens)
    except Exception as exc:  # provider errors are a taxonomy of their own; all degrade
        logger.warning("extraction model call failed: %s", exc)
        return {}, f"model call failed: {exc}"
    if not isinstance(payload, dict):
        return {}, "model returned a non-object body"
    return payload, None


def _model_id() -> str | None:
    """The scoring model's id, recorded on every stage output."""
    try:
        from app.insights.llm import get_scoring_config

        return getattr(get_scoring_config(), "model_id", None)
    except Exception:  # pragma: no cover — provenance must never break a run
        return None


def _provenance(stage: str, prompt_version: str) -> StageProvenance:
    return StageProvenance(
        stage=stage,
        model_id=_model_id(),
        prompt_version=prompt_version,
        pipeline_version=PIPELINE_VERSION,
    )


def _user_prompt(task: ExtractionTask) -> str:
    return (
        f"Source id: {task.source_id}\n"
        f"Fields to extract: {', '.join(task.fields)}\n\n"
        f"DOCUMENT:\n{task.document}"
    )


def _values_from(
    payload: dict, task: ExtractionTask, provenance: StageProvenance
) -> list[ExtractedValue]:
    """Map a model response onto ``ExtractedValue`` rows for the requested fields only.

    A field the model volunteered but nobody asked for is dropped. Accepting it would let
    the prompt's field list stop being the contract, and the harness would then be scoring
    against a moving target.
    """
    values = payload.get("values")
    if not isinstance(values, dict):
        return []
    confidences = payload.get("confidence") if isinstance(payload.get("confidence"), dict) else {}
    spans = payload.get("source_text") if isinstance(payload.get("source_text"), dict) else {}
    rationales = payload.get("rationale") if isinstance(payload.get("rationale"), dict) else {}

    out: list[ExtractedValue] = []
    for name in task.fields:
        value = values.get(name, _NO_VALUE)
        # A field the model declined to answer is left absent rather than recorded as null.
        # "Not extracted" and "extracted as nothing" are different facts, and the harness
        # scores them differently on purpose.
        if value is _NO_VALUE or value is None:
            continue
        raw_confidence = confidences.get(name)
        try:
            confidence = min(1.0, max(0.0, float(raw_confidence)))
        except (TypeError, ValueError):
            confidence = 0.5
        out.append(ExtractedValue(
            field_name=name,
            value=value,
            source_text=spans.get(name) if isinstance(spans.get(name), str) else None,
            confidence=confidence,
            provenance=provenance,
            rationale=rationales.get(name) if isinstance(rationales.get(name), str) else None,
        ))
    return out


# =====================================================================================
# The baseline — one call, and the bar the pipeline has to clear
# =====================================================================================
async def run_baseline(task: ExtractionTask) -> PipelineResult:
    """A single ``chat_json`` extraction. No harmonisation, no validation.

    This is not a strawman. It is the honest default: the plan's documented descope is to
    ship **this plus the validation stage** if the three-stage pipeline cannot demonstrate
    better accuracy on the same corpus.
    """
    payload, error = await _ask(_BASELINE_SYSTEM, _user_prompt(task))
    provenance = _provenance(STAGE_BASELINE, BASELINE_PROMPT_VERSION)
    return PipelineResult(
        values=_values_from(payload, task, provenance),
        license_class=task.license_class,
        errors=[error] if error else [],
    )


# =====================================================================================
# Stage 1 — extraction
# =====================================================================================
async def extract(task: ExtractionTask) -> tuple[list[ExtractedValue], list[str]]:
    """Arm-level endpoints, placebo rates, baseline variance and denominators."""
    payload, error = await _ask(_EXTRACTION_SYSTEM, _user_prompt(task))
    provenance = _provenance(STAGE_EXTRACTION, EXTRACTION_PROMPT_VERSION)
    return _values_from(payload, task, provenance), ([error] if error else [])


# =====================================================================================
# Stage 2 — harmonisation. Proposes; never applies.
# =====================================================================================
async def propose_harmonisation(
    task: ExtractionTask, values: list[ExtractedValue]
) -> tuple[list[HarmonisationProposal], list[str]]:
    """Suggest timepoint or strata alignments, then screen them against the protocol.

    Every proposal goes through ``extraction.screen_proposal`` before it is returned, so a
    suggestion that contradicts the governing ``approved_time_window`` is **auto-rejected
    with no escalation path**. Escalating would invite someone to overrule an approved
    protocol from a review queue.
    """
    if not values:
        return [], []
    summary = json.dumps(
        [{"field": v.field_name, "value": v.value} for v in values], default=str
    )
    window = task.approved_time_window
    user = (
        f"Extracted values:\n{summary}\n\n"
        + (
            f"The governing protocol admits weeks {window[0]} to {window[1]}.\n"
            if window else "No approved time window was supplied.\n"
        )
        + f"DOCUMENT:\n{task.document}"
    )
    payload, error = await _ask(_HARMONISATION_SYSTEM, user)
    provenance = _provenance(STAGE_HARMONISATION, HARMONISATION_PROMPT_VERSION)

    rows = payload.get("proposals")
    if not isinstance(rows, list):
        return [], ([error] if error else [])

    proposals: list[HarmonisationProposal] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("kind", "")).upper()
        if kind not in ("TIMEPOINT", "STRATA"):
            continue
        try:
            confidence = min(1.0, max(0.0, float(row.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
        proposal = HarmonisationProposal(
            kind=kind,
            from_value=row.get("from_value"),
            to_value=row.get("to_value"),
            rationale=str(row.get("rationale") or ""),
            confidence=confidence,
            provenance=provenance,
            guideline_citation=(
                str(row["guideline_citation"]) if row.get("guideline_citation") else None
            ),
        )
        # The protocol wins here, without a human in the loop, by construction.
        proposals.append(screen_proposal(proposal, approved_time_window=window))
    return proposals, ([error] if error else [])


# =====================================================================================
# Stage 3 — validation. A disagreement blocks promotion.
# =====================================================================================
async def validate(
    task: ExtractionTask, values: list[ExtractedValue]
) -> tuple[list[ValidationOutcome], list[str]]:
    """Re-derive each value against the retained source, independently of stage 1.

    **Reach is bounded by licence class.** A restricted source retains only a fragment, so
    validation can only re-check what the fragment contains — reported as ``FRAGMENT``
    rather than claimed as full coverage.
    """
    if not values:
        return [], []
    claimed = json.dumps(
        {v.field_name: v.value for v in values}, default=str
    )
    user = (
        f"Values to check:\n{claimed}\n\n"
        f"DOCUMENT:\n{task.document}"
    )
    payload, error = await _ask(_VALIDATION_SYSTEM, user)
    provenance = _provenance(STAGE_VALIDATION, VALIDATION_PROMPT_VERSION)

    rows = payload.get("checks")
    if not isinstance(rows, list):
        return [], ([error] if error else [])

    expected = {v.field_name: v.value for v in values}
    outcomes: list[ValidationOutcome] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("field", ""))
        if name not in expected:
            continue
        outcomes.append(ValidationOutcome(
            field_name=name,
            agrees=bool(row.get("agrees")),
            expected=expected[name],
            observed=row.get("observed"),
            reach=task.validation_reach,
            provenance=provenance,
            note=str(row["note"]) if row.get("note") else None,
        ))
    return outcomes, ([error] if error else [])


# =====================================================================================
# The pipeline
# =====================================================================================
async def run_pipeline(task: ExtractionTask) -> PipelineResult:
    """Extraction, then harmonisation, then validation. Same interface as the baseline.

    Ordered, not concurrent: harmonisation reasons about what extraction found and
    validation checks it. Running them in parallel would mean validating values the
    harmonisation stage had not yet seen, which is a different and weaker check.
    """
    errors: list[str] = []

    values, stage_errors = await extract(task)
    errors += stage_errors

    proposals, stage_errors = await propose_harmonisation(task, values)
    errors += stage_errors

    validations, stage_errors = await validate(task, values)
    errors += stage_errors

    return PipelineResult(
        values=values,
        proposals=proposals,
        validations=validations,
        license_class=task.license_class,
        errors=errors,
    )


async def run_baseline_with_validation(task: ExtractionTask) -> PipelineResult:
    """The plan's documented descope: one extraction call plus the validation stage.

    Kept as a first-class runner rather than described in a comment, so that shipping it
    is a configuration choice and not a rewrite. If the harness says the pipeline does not
    beat the baseline, **this** is what ships — the validation stage earns its place
    independently, because it is the only stage that can block a wrong value.
    """
    baseline = await run_baseline(task)
    validations, errors = await validate(task, baseline.values)
    return PipelineResult(
        values=baseline.values,
        proposals=[],
        validations=validations,
        license_class=task.license_class,
        errors=baseline.errors + errors,
    )


RUNNERS = {
    "baseline": run_baseline,
    "baseline_with_validation": run_baseline_with_validation,
    "pipeline": run_pipeline,
}

# =====================================================================================
# The justify-or-drop verdict, recorded rather than described
# =====================================================================================
# RUN ON THE REAL CORPUS, 2026-07-27, against live model calls:
#
#     baseline   85.7% (12/14)   2 wrong, 0 missed   PUBLIC_DOMAIN 81.8%  RESTRICTED 100.0%
#     pipeline   71.4% (10/14)   3 wrong, 1 missed   PUBLIC_DOMAIN 72.7%  RESTRICTED  66.7%
#     verdict    SHIP_BASELINE_PLUS_VALIDATION
#
# The three-stage pipeline was both wronger AND abstained more, so the extra stages were
# adding noise rather than signal. The plan's pre-committed rule - the pipeline ships only
# if it is strictly more accurate - therefore selects the descope, and this is that decision
# made once rather than left for whoever wires extraction up to decide again.
#
# HOW MUCH THIS ESTABLISHES: not much, and it should not be quoted as though it did. It is
# 14 graded fields over 3 cases, and the gap is 2 fields. That is nowhere near a significant
# difference, and the per-tier splits (3 RESTRICTED fields) are single observations. What
# makes the verdict safe despite the sample is the DIRECTION of the tie-break: the rule
# already ships the simpler, cheaper runner unless the complex one earns its place, so a
# corpus too small to settle the question resolves the same way as a genuine tie. Widening
# the corpus is curator time, and only that turns this signal into a settlement.
#
# Nothing in ``app/`` calls a runner yet - deterministic parsing in
# ``evidence_ingestion_service`` is what populates rows today. This constant exists so the
# first caller inherits the measured decision instead of picking a runner fresh.
DEFAULT_RUNNER_NAME = "baseline_with_validation"
DEFAULT_RUNNER = run_baseline_with_validation
