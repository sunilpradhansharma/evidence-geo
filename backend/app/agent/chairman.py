"""Chairman Consensus & Arbitration — multi-LLM response evaluation.

After all target LLMs respond to a question, the Chairman (Claude) evaluates
whether there is clinical consensus. Divergence triggers GEO schema fallback.
"""
import json
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.consensus import ConsensusRecord
from app.models.question import Question
from app.models.response import Response
from app.models.run import Run
from app.providers.base import ModelParams
from app.providers.registry import get_orchestrator_config, get_provider_client
from app.utils.logging import get_logger

logger = get_logger("chairman")


@dataclass
class ConsensusResult:
    consensus_level: Literal["FULL", "PARTIAL", "MISSING"]
    agreed_recommendation: str | None
    divergence_points: list[str]
    confidence: float
    geo_fallback_used: bool
    geo_context: str | None = None
    final_answer: str | None = None  # synthesized single "council" answer


def _build_arbitration_prompt(
    question: Question, responses: list[Response], intent: str
) -> tuple[str, str]:
    """Build the Chairman prompt to evaluate consensus across LLM responses."""
    strictness = "strict" if intent == "CLINICAL" else "general"

    system = (
        "You are the Chairman Arbitrator for a pharmaceutical evidence monitoring system. "
        "You evaluate whether multiple LLMs agree on their clinical and factual content.\n\n"
        "Assess consensus across the provided LLM responses and return ONLY valid JSON:\n"
        "{\n"
        '  "consensus_level": "FULL" | "PARTIAL" | "MISSING",\n'
        '  "final_answer": "a single synthesized answer to the question that merges the LLM responses into the best consensus answer; resolve trivial wording differences and state major caveats inline",\n'
        '  "agreed_recommendation": "string summarizing what LLMs agree on, or null",\n'
        '  "divergence_points": ["list of specific disagreements"],\n'
        '  "confidence": float between 0.0 and 1.0\n'
        "}\n\n"
        "Rules:\n"
        f"- Evaluation mode: {strictness}\n"
        "- final_answer: synthesize ONE best answer to the user's question from the responses; "
        "do not invent facts beyond what the LLMs state; if they conflict materially, present the "
        "most-supported position and flag the disagreement\n"
        "- FULL: All LLMs substantially agree on core facts (treatment, safety, positioning)\n"
        "- PARTIAL: Agreement on some points, divergence on others\n"
        "- MISSING: No meaningful consensus or critical factual contradictions\n"
        "- For CLINICAL mode, check treatment recommendations, dosing, safety data, guidelines\n"
        "- For general mode, check overall direction and key facts (minor wording differences are OK)"
    )

    # Build per-LLM response summaries
    llm_sections = []
    for r in responses:
        if r.status in ("SUCCESS", "TRUNCATED"):
            truncated = r.response_text[:3000]
            llm_sections.append(f"### {r.llm_name}\n{truncated}")

    # FR-108a: disease-state questions have no focus brand — describe the landscape instead.
    brand_label = question.brand_focus or "disease-state landscape (no focus brand)"
    user = (
        f"QUESTION (persona={question.persona}, domain={question.domain}, "
        f"brand={brand_label}):\n{question.question_text}\n\n"
        f"INTENT: {intent}\n\n"
        f"LLM RESPONSES ({len(llm_sections)} models):\n\n"
        + "\n\n---\n\n".join(llm_sections)
        + "\n\nProduce the JSON consensus evaluation now."
    )

    return system, user


async def evaluate_consensus(
    question: Question,
    responses: list[Response],
    intent: str,
) -> tuple[ConsensusResult, dict]:
    """Compute consensus across LLM responses WITHOUT touching the database.

    Split out from `arbitrate` so the (slow, network-bound) Chairman LLM call can run
    concurrently across questions while the DB write stays serialized (NF-003). Returns
    the result plus persistence metadata (model, tokens, responses_evaluated)."""
    # Filter to only successful/truncated responses (skip FAILED/BLOCKED)
    valid_responses = [r for r in responses if r.status in ("SUCCESS", "TRUNCATED")]

    if len(valid_responses) < 2:
        # Can't evaluate consensus with fewer than 2 responses
        result = ConsensusResult(
            consensus_level="MISSING",
            agreed_recommendation=None,
            divergence_points=["Insufficient valid responses for consensus evaluation"],
            confidence=0.0,
            geo_fallback_used=False,
        )
        return result, {"model": "n/a", "tokens": 0, "responses_evaluated": len(valid_responses)}

    # Skip arbitration for SHORTHAND intent
    if intent == "SHORTHAND":
        result = ConsensusResult(
            consensus_level="FULL",
            agreed_recommendation="Shorthand query — arbitration skipped",
            divergence_points=[],
            confidence=1.0,
            geo_fallback_used=False,
        )
        return result, {"model": "skip", "tokens": 0, "responses_evaluated": len(valid_responses)}

    # Call the Chairman (orchestrator model)
    cfg = get_orchestrator_config()
    client = get_provider_client(cfg.provider)
    system, user = _build_arbitration_prompt(question, valid_responses, intent)

    try:
        llm_result = await client.chat(
            cfg.model_id, system, user,
            ModelParams(max_tokens=1200, temperature=0.0),
        )
        parsed = _parse_consensus(llm_result.text or "")
        tokens = (llm_result.prompt_tokens or 0) + (llm_result.completion_tokens or 0)
    except Exception as e:  # noqa: BLE001
        logger.warning("Chairman arbitration failed: %s — defaulting to PARTIAL", e)
        parsed = ConsensusResult(
            consensus_level="PARTIAL",
            agreed_recommendation=None,
            divergence_points=[f"Arbitration error: {e}"],
            confidence=0.3,
            geo_fallback_used=False,
        )
        tokens = 0

    # If consensus is not FULL, load GEO fallback data (brand-scoped; skipped when brand-less)
    if parsed.consensus_level != "FULL" and question.brand_focus:
        geo_context = _load_geo_fallback(
            question.brand_focus,
            question.therapeutic_area,
            getattr(question, "disease", None),
        )
        if geo_context:
            parsed.geo_fallback_used = True
            parsed.geo_context = geo_context

    return parsed, {"model": cfg.model_id, "tokens": tokens, "responses_evaluated": len(valid_responses)}


async def persist_consensus(
    db: AsyncSession, run_id: str, question: Question, result: ConsensusResult, meta: dict,
) -> None:
    """Write a previously-computed ConsensusResult (DB-only; pair with evaluate_consensus)."""
    await _persist_consensus(
        db, run_id, question, result,
        model=meta.get("model", "n/a"), tokens=meta.get("tokens", 0),
        responses_evaluated=meta.get("responses_evaluated", 0),
    )


async def arbitrate(
    db: AsyncSession,
    run_id: str,
    question: Question,
    responses: list[Response],
    intent: str,
) -> ConsensusResult:
    """Evaluate consensus across LLM responses for a single question.

    Returns ConsensusResult and persists a ConsensusRecord.
    """
    result, meta = await evaluate_consensus(question, responses, intent)
    await persist_consensus(db, run_id, question, result, meta)
    return result


def _parse_consensus(text: str) -> ConsensusResult:
    """Parse the Chairman's JSON response into a ConsensusResult."""
    try:
        # Extract JSON from potentially wrapped response
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(text[start:end])
        else:
            raise ValueError("No JSON object found")

        level = data.get("consensus_level", "PARTIAL").upper()
        if level not in ("FULL", "PARTIAL", "MISSING"):
            level = "PARTIAL"

        return ConsensusResult(
            consensus_level=level,
            agreed_recommendation=data.get("agreed_recommendation"),
            divergence_points=data.get("divergence_points", []),
            confidence=min(max(float(data.get("confidence", 0.5)), 0.0), 1.0),
            geo_fallback_used=False,
            final_answer=data.get("final_answer"),
        )
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning("Failed to parse Chairman response: %s", e)
        return ConsensusResult(
            consensus_level="PARTIAL",
            agreed_recommendation=None,
            divergence_points=[f"Parse error: {e}"],
            confidence=0.3,
            geo_fallback_used=False,
        )


def _load_geo_fallback(
    brand: str, therapeutic_area: str, disease: str | None = None
) -> str | None:
    """Load GEO schema fallback data for a brand. Returns JSON string or None.

    *disease* narrows the competitive field the model is shown. A multi-indication
    brand has a different comparator set in each indication, so answering an atopic
    dermatitis question with rheumatoid arthritis comparators would introduce the
    error this fallback exists to correct.
    """
    try:
        from app.geo.loader import get_geo_context
        ctx = get_geo_context(brand, therapeutic_area, disease)
        if ctx:
            return json.dumps(ctx) if isinstance(ctx, dict) else str(ctx)
    except Exception as e:  # noqa: BLE001
        logger.debug("GEO fallback not available for %s: %s", brand, e)
    return None


async def _persist_consensus(
    db: AsyncSession,
    run_id: str,
    question: Question,
    result: ConsensusResult,
    *,
    model: str,
    tokens: int,
    responses_evaluated: int = 0,
) -> None:
    """Write consensus record to the database."""
    existing_result = await db.execute(
        select(ConsensusRecord).where(
            ConsensusRecord.run_id == run_id,
            ConsensusRecord.question_id == question.question_id,
        )
    )
    record = existing_result.scalars().first()
    if record is None:
        record = ConsensusRecord(
            consensus_id=str(uuid.uuid4()),
            run_id=run_id,
            question_id=question.question_id,
        )
        db.add(record)

    record.consensus_level = result.consensus_level
    record.agreed_recommendation = result.agreed_recommendation
    record.divergence_points = json.dumps(result.divergence_points) if result.divergence_points else None
    record.confidence = result.confidence
    record.final_answer = result.final_answer
    record.geo_fallback_used = result.geo_fallback_used
    record.geo_context = result.geo_context
    record.responses_evaluated = responses_evaluated
    record.arbitration_model = model
    record.arbitration_tokens = tokens


async def refresh_run_consensus_counters(db: AsyncSession, run_id: str) -> None:
    """Recompute Run.consensus_{full,partial,missing} from the current ConsensusRecords.

    Rebuilt from source rather than incremented, because a question can be arbitrated more
    than once in the same run: a resume or a failed-response retry re-arbitrates a question
    that already has a record, and _persist_consensus UPSERTS on (run_id, question_id). An
    incrementing tally counts that second arbitration as a second question, so the run's
    Full/Partial/Missing add up to more than the number of questions it actually holds.

    The single owner of these three numbers: the orchestrator calls it when a run ends and
    openevidence_service calls it after a late capture flips a level.
    """
    rows = (await db.execute(
        select(ConsensusRecord.consensus_level, func.count())
        .where(ConsensusRecord.run_id == run_id)
        .group_by(ConsensusRecord.consensus_level)
    )).all()
    counts = {lvl: n for lvl, n in rows}
    run = await db.get(Run, run_id)
    if run is None:
        return
    run.consensus_full = counts.get("FULL", 0)
    run.consensus_partial = counts.get("PARTIAL", 0)
    run.consensus_missing = counts.get("MISSING", 0)
    await db.commit()
