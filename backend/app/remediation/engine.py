"""GEO Intervention engine (BR-012): find gaps -> SEMrush enrich -> LLM reason -> score -> persist.

Network-bound work (SEMrush + LLM) runs concurrently (bounded) per gap; the resulting rows
are then written to the DB in a single commit (the AsyncSession is never touched
concurrently). The LLM is constrained to an approved content-type enum; if it is
unavailable, a deterministic fallback keeps the batch non-empty so the feature degrades
gracefully (matching the SEMrush stub philosophy).
"""
import asyncio
import math
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.insights.llm import chat_json
from app.models.recommendation import (
    MLR_UNAPPROVED,
    SOURCE_EVIDENCE_GAP,
    SOURCE_POSITIONING_GAP,
    Recommendation,
)
from app.remediation import evidence_gaps as evidence_gaps_mod
from app.remediation import gaps as gaps_mod
from app.remediation import implications as impl
from app.remediation import prompts, semrush
from app.utils.audit import write_audit
from app.utils.logging import get_logger

logger = get_logger("remediation.engine")

_MAX_CONCURRENCY = 5


def _impact(
    gap_severity: float, search_volume: int | None, citation_gap_score: float = 0.0
) -> tuple[float, float, float]:
    """impact_score = gap_severity × volume_multiplier × citation_multiplier (BR-012.3).

    volume_multiplier = 1 + log10(search_volume) — log-damped so one very-high-volume
    keyword can't dominate the ranking. citation_multiplier = 1 + log10(1 + citation_gap_score)
    — a transparent "citeability" boost (BR-005): the more trusted sources omit the brand
    while citing the competitor, the more valuable closing that evidence gap is. Both default
    to a neutral 1.0 (no volume / no citation gap), so ranking is unchanged when a signal is
    absent.
    """
    vol = max(int(search_volume or 1), 1)
    vol_mult = 1.0 + math.log10(vol)
    cite_mult = 1.0 + math.log10(1.0 + max(citation_gap_score, 0.0))
    impact = gap_severity * vol_mult * cite_mult
    return round(impact, 3), round(vol_mult, 3), round(cite_mult, 3)


def _fallback(
    gap: dict, content_type: str = "FAQ"
) -> tuple[str, str, str, list[str], list[str]]:
    """Deterministic recommendation used when the LLM is unavailable/invalid."""
    brand = gap.get("brand_focus") or "the brand"
    competitor = gap.get("outperforming_competitor") or "the leading competitor"
    topic = gap.get("indication") or gap.get("therapeutic_area") or "this indication"
    action = (
        f"Publish an {content_type} that directly addresses how {brand} compares to "
        f"{competitor} for {topic}, with clear efficacy, safety, and access information."
    )
    if gap.get("competitive_position") == "NOT_MENTIONED":
        rationale = (
            f"AI did not mention {brand} for {topic} while favouring {competitor}. "
            f"Authoritative, brand-owned content creates a citable presence to earn a mention."
        )
    else:
        rationale = (
            f"AI positioned {brand} as {gap.get('competitive_position')} while favouring "
            f"{competitor}. Authoritative, brand-owned content closes the citation gap."
        )
    brief = [
        f"Overview of {brand} for {topic} (indication, place in therapy)",
        f"Comparison vs {competitor}: efficacy, safety, dosing, access",
        "Evidence summary citing peer-reviewed / guideline sources",
        "Plain-language FAQ answering the exact question the AI handled poorly",
    ]
    questions = [
        f"How does {brand} compare to {competitor} for {topic}?",
        f"What is the safety profile of {brand} in {topic}?",
    ]
    return content_type, action, rationale, brief, questions


async def _reason(
    gap: dict, metrics: dict
) -> tuple[str, str, str, list[str], list[str]]:
    """Call the LLM for one gap; return (content_type, action, rationale, brief, questions).

    An evidence gap gets a different prompt, because the two findings need different assets:
    a positioning gap asks *"how do we get mentioned?"*, while an alignment gap asks *"how do
    we correct what was said?"* — and a content brief written for the first, handed to a
    contradicted boxed warning, would propose marketing copy in answer to a safety error.
    """
    is_evidence = gap.get("source_type") == SOURCE_EVIDENCE_GAP
    system = prompts.EVIDENCE_SYSTEM if is_evidence else prompts.SYSTEM
    user = (
        prompts.build_evidence_user_prompt(gap)
        if is_evidence else prompts.build_user_prompt(gap, metrics)
    )
    try:
        parsed = await chat_json(system, user, max_tokens=900)
    except Exception as e:  # noqa: BLE001 — degrade to a deterministic recommendation
        logger.warning("recommendation LLM failed for %s: %s", gap.get("source_response_id"), e)
        return _fallback(gap)

    if not isinstance(parsed, dict):
        return _fallback(gap)
    content_type = prompts.coerce_content_type(parsed.get("content_type"))
    action = str(parsed.get("recommended_action") or "").strip()
    rationale = str(parsed.get("rationale") or "").strip()
    if not action:
        return _fallback(gap, content_type=content_type)
    brief = prompts.coerce_brief(parsed.get("content_brief"))
    questions = prompts.coerce_questions(parsed.get("suggested_questions"))
    # If the model omitted the new fields, borrow the deterministic defaults so the UI is
    # never empty (still a suggestion; MLR review is unchanged).
    if not brief or not questions:
        _, _, _, fb_brief, fb_questions = _fallback(gap, content_type=content_type)
        brief = brief or fb_brief
        questions = questions or fb_questions
    return content_type, action, rationale, brief, questions


async def _build_row(gap: dict, batch_id: str, sem: asyncio.Semaphore) -> Recommendation:
    """Enrich + reason for a single gap and return an unsaved Recommendation row."""
    async with sem:
        keyword = gap.get("outperforming_competitor") or gap.get("brand_focus") or ""
        metrics = await semrush.enrich(gap.get("competitor_domain"), keyword=keyword)
        content_type, action, rationale, brief, questions = await _reason(gap, metrics)

    # Transparent citation-gap signal (BR-005): trusted sources that omit the brand plus
    # citations that reference the outperforming competitor.
    citation_gap_score = float(
        len(gap.get("missing_citations") or [])
        + int(gap.get("competitor_citation_count") or 0)
    )
    impact_score, vol_mult, cite_mult = _impact(
        gap["gap_severity"], metrics.get("search_volume"), citation_gap_score
    )
    return Recommendation(
        rec_id=str(uuid.uuid4()),
        batch_id=batch_id,
        source_type=gap.get("source_type") or SOURCE_POSITIONING_GAP,
        confidence=gap.get("confidence"),
        strategic_implication=gap.get("strategic_implication"),
        implication_owner=gap.get("implication_owner"),
        externally_actionable=bool(gap.get("externally_actionable", True)),
        evidence_action=gap.get("evidence_action"),
        claim_id=gap.get("claim_id"),
        claim_text=gap.get("claim_text"),
        classification=gap.get("classification"),
        certainty_verdict=gap.get("certainty_verdict"),
        finding_reason=gap.get("finding_reason"),
        gap_attribution=gap.get("gap_attribution"),
        source_response_id=gap.get("source_response_id"),
        question_id=gap.get("question_id"),
        run_id=gap.get("run_id"),
        persona=gap.get("persona"),
        therapeutic_area=gap.get("therapeutic_area"),
        indication=gap.get("indication"),
        brand_focus=gap.get("brand_focus"),
        llm_name=gap.get("llm_name"),
        competitive_position=gap["competitive_position"],
        gap_severity=gap["gap_severity"],
        outperforming_competitor=gap.get("outperforming_competitor"),
        competitor_domain=gap.get("competitor_domain"),
        missing_citations=_dump(gap.get("missing_citations")),
        search_volume=metrics.get("search_volume"),
        domain_authority=metrics.get("domain_authority"),
        metrics_source=metrics.get("source", "stub"),
        volume_multiplier=vol_mult,
        citation_gap_score=citation_gap_score,
        citation_multiplier=cite_mult,
        content_type=content_type,
        recommended_action=action,
        rationale=rationale,
        content_brief=_dump(brief),
        suggested_questions=_dump(questions),
        impact_score=impact_score,
        mlr_status=MLR_UNAPPROVED,
    )


def _dump(value) -> str | None:
    import json

    if not value:
        return None
    return json.dumps(value)


async def generate(
    db: AsyncSession,
    *,
    persona: str | None = None,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    brand: str | None = None,
    llm_name: str | None = None,
    response_ids: list[str] | None = None,
    limit: int = 25,
    include_evidence_gaps: bool = True,
) -> dict:
    """Run the full pipeline and persist a new batch of recommendations. Returns a summary.

    Pass ``response_ids`` to scope generation to a specific cohort of responses (e.g. the
    "not mentioned" answers behind one source in the Influence Graph); ``rec_ids`` in the
    summary lets callers link the freshly created content-action rows.

    Phase 9 adds a **second finder** running beside the positioning one. Both emit the same
    record shape, so the pipeline below is unchanged \u2014 what differs is that an alignment gap
    knows whether it can be answered with content at all.
    """
    gaps = await gaps_mod.find_gaps(
        db,
        persona=persona,
        therapeutic_area=therapeutic_area,
        indication=indication,
        brand=brand,
        llm_name=llm_name,
        response_ids=response_ids,
        limit=limit,
    )
    for gap in gaps:
        gap.setdefault("source_type", SOURCE_POSITIONING_GAP)

    evidence_gaps: list[dict] = []
    internal_only: list[dict] = []
    if include_evidence_gaps:
        found = await evidence_gaps_mod.find_evidence_gaps(
            db,
            persona=persona,
            therapeutic_area=therapeutic_area,
            indication=indication,
            brand=brand,
            llm_name=llm_name,
            response_ids=response_ids,
            limit=limit,
        )
        # The refusal. A finding whose remedy is internal curation or a new trial gets NO
        # content recommendation: proposing a comparison table when the real problem is our
        # own verification backlog would send a brand team to spend money while the fix is
        # an afternoon of curation. They are returned in the summary so the work is still
        # visible to whoever actually owns it.
        for gap in found:
            (evidence_gaps if gap["externally_actionable"] else internal_only).append(gap)

    batch_id = str(uuid.uuid4())
    all_gaps = gaps + evidence_gaps

    rows: list[Recommendation] = []
    if all_gaps:
        sem = asyncio.Semaphore(_MAX_CONCURRENCY)
        rows = await asyncio.gather(*[_build_row(g, batch_id, sem) for g in all_gaps])
        for row in rows:
            db.add(row)

    live = sum(1 for r in rows if r.metrics_source == "live")
    await write_audit(
        db,
        role="ORCHESTRATOR",
        event="RECOMMENDATIONS_GENERATED",
        context={
            "batch_id": batch_id,
            "gaps_found": len(gaps),
            "evidence_gaps_found": len(evidence_gaps),
            "internal_only": len(internal_only),
            "generated": len(rows),
            "semrush_live": live,
            "filters": {
                "persona": persona,
                "therapeutic_area": therapeutic_area,
                "indication": indication,
                "brand": brand,
                "llm_name": llm_name,
            },
        },
        commit=False,
    )
    await db.commit()

    return {
        "batch_id": batch_id,
        "gaps_found": len(gaps),
        "evidence_gaps_found": len(evidence_gaps),
        "generated": len(rows),
        "rec_ids": [r.rec_id for r in rows],
        "semrush_source": "live" if live else "stub",
        "semrush_live": live,
        # Findings that are real but cannot be answered with content. Returned rather than
        # dropped: "3 comparisons are blocked by our own verification backlog" is a piece of
        # work someone owns, and a shorter recommendation list would hide it entirely.
        "internal_only": [
            {
                "claim_id": g.get("claim_id"),
                "claim_text": g.get("claim_text"),
                "strategic_implication": g.get("strategic_implication"),
                "owner": g.get("implication_owner"),
                "evidence_action": g.get("evidence_action"),
                "reason": g.get("implication_reason"),
            }
            for g in internal_only
        ],
        "internal_only_count": len(internal_only),
    }
