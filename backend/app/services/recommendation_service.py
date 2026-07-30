"""Service layer for GEO Intervention Recommendations (BR-012).

Wraps the remediation engine (generate) and provides read helpers for the ranked list +
CSV export consumed by the Insights dashboard. Reads default to the **latest generated
batch** so the dashboard shows a stable, non-duplicated ranking; pass ``batch_id`` to pin
a specific batch.
"""
import csv
import io
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.taxonomy import keys_for_area
from app.models.recommendation import Recommendation
from app.models.recommendation_review import RecommendationReview
from app.remediation import citations, engine

_CSV_FIELDS = [
    "rec_id", "created_at", "impact_score", "content_type", "recommended_action",
    "rationale", "competitive_position", "outperforming_competitor", "competitor_domain",
    "missing_citations", "therapeutic_area", "indication", "persona", "brand_focus",
    "llm_name", "search_volume", "domain_authority", "metrics_source", "volume_multiplier",
    "citation_gap_score", "citation_multiplier", "mlr_status",
    # Phase 9 — exported so a spreadsheet reader can tell an alignment finding from a
    # positioning one, and can see which rows are not content work at all.
    "source_type", "strategic_implication", "implication_owner", "externally_actionable",
    "confidence", "classification", "certainty_verdict", "gap_attribution",
    "evidence_action", "claim_text",
]


def _serialize(r: Recommendation) -> dict:
    return {
        "rec_id": r.rec_id,
        "batch_id": r.batch_id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "source_response_id": r.source_response_id,
        "question_id": r.question_id,
        "run_id": r.run_id,
        "persona": r.persona,
        "therapeutic_area": r.therapeutic_area,
        "indication": r.indication,
        "brand_focus": r.brand_focus,
        "llm_name": r.llm_name,
        "competitive_position": r.competitive_position,
        "gap_severity": r.gap_severity,
        "outperforming_competitor": r.outperforming_competitor,
        "competitor_domain": r.competitor_domain,
        "missing_citations": json.loads(r.missing_citations) if r.missing_citations else [],
        "search_volume": r.search_volume,
        "domain_authority": r.domain_authority,
        "metrics_source": r.metrics_source,
        "volume_multiplier": r.volume_multiplier,
        "citation_gap_score": r.citation_gap_score,
        "citation_multiplier": r.citation_multiplier,
        "content_type": r.content_type,
        "recommended_action": r.recommended_action,
        "rationale": r.rationale,
        "content_brief": json.loads(r.content_brief) if r.content_brief else [],
        "suggested_questions": json.loads(r.suggested_questions) if r.suggested_questions else [],
        "impact_score": r.impact_score,
        "mlr_status": r.mlr_status,
        # --- Phase 9 -------------------------------------------------------------------
        "source_type": r.source_type,
        "confidence": r.confidence,
        "strategic_implication": r.strategic_implication,
        "implication_owner": r.implication_owner,
        "externally_actionable": r.externally_actionable,
        "evidence_action": r.evidence_action,
        "claim_id": r.claim_id,
        "claim_text": r.claim_text,
        "classification": r.classification,
        "certainty_verdict": r.certainty_verdict,
        "finding_reason": r.finding_reason,
        "gap_attribution": r.gap_attribution,
    }


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
    """Generate + persist a new batch of recommendations (BR-012.1, extended in Phase 9)."""
    return await engine.generate(
        db,
        persona=persona,
        therapeutic_area=therapeutic_area,
        indication=indication,
        brand=brand,
        llm_name=llm_name,
        response_ids=response_ids,
        limit=limit,
        include_evidence_gaps=include_evidence_gaps,
    )


async def _latest_batch_id(db: AsyncSession) -> str | None:
    row = await db.execute(
        select(Recommendation.batch_id).order_by(Recommendation.created_at.desc()).limit(1)
    )
    return row.scalars().first()


async def list_recommendations(
    db: AsyncSession,
    *,
    persona: str | None = None,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    brand: str | None = None,
    llm_name: str | None = None,
    batch_id: str | None = None,
    source_type: str | None = None,
    strategic_implication: str | None = None,
) -> dict:
    """Return the ranked recommendation list (impact_score desc) for a batch + filters (BR-012.3/.6)."""
    if batch_id is None:
        batch_id = await _latest_batch_id(db)
    if batch_id is None:
        return {"batch_id": None, "count": 0, "generated_at": None, "items": []}

    stmt = select(Recommendation).where(Recommendation.batch_id == batch_id)
    if persona:
        stmt = stmt.where(Recommendation.persona == persona)
    if therapeutic_area:
        child_keys = keys_for_area(therapeutic_area)
        if child_keys:
            stmt = stmt.where(Recommendation.therapeutic_area.in_(child_keys))
        else:
            stmt = stmt.where(Recommendation.therapeutic_area == therapeutic_area)
    if indication:
        stmt = stmt.where(Recommendation.indication == indication)
    if brand:
        stmt = stmt.where(Recommendation.brand_focus == brand)
    if llm_name:
        stmt = stmt.where(Recommendation.llm_name == llm_name)
    if source_type:
        stmt = stmt.where(Recommendation.source_type == source_type)
    if strategic_implication:
        stmt = stmt.where(Recommendation.strategic_implication == strategic_implication)

    stmt = stmt.order_by(Recommendation.impact_score.desc(), Recommendation.created_at.desc())
    rows = list((await db.execute(stmt)).scalars().all())
    items = [_serialize(r) for r in rows]
    await _attach_placement(db, items)
    generated_at = items[0]["created_at"] if items else None
    return {"batch_id": batch_id, "count": len(items), "generated_at": generated_at, "items": items}


async def _attach_placement(db: AsyncSession, items: list[dict]) -> None:
    """Attach compact "where to publish / earn a citation" guidance to each recommendation.

    The recommendation tells you *what* asset to publish; this adds *where* — the authoritative
    domains the AI already trusts for the topic + the search phrasings to target. Memoised per
    (persona, TA, brand) so a full ranked list adds only a handful of aggregation queries, and
    best-effort so the list still renders if the citation graph is empty.
    """
    cache: dict[tuple, dict] = {}
    for it in items:
        key = (it.get("persona"), it.get("therapeutic_area"), it.get("brand_focus"))
        if key not in cache:
            try:
                cache[key] = await citations.placement_guidance(
                    db, persona=key[0], therapeutic_area=key[1], brand=key[2]
                )
            except Exception:  # noqa: BLE001 — additive context; never break the ranked list
                cache[key] = {"earn_citations": [], "preferred_gaps": [], "target_queries": []}
        it["placement"] = cache[key]


def to_csv(items: list[dict]) -> str:
    """Render recommendation dicts to CSV (BR-012.3 exportable)."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for item in items:
        row = {k: item.get(k) for k in _CSV_FIELDS}
        row["missing_citations"] = " | ".join(item.get("missing_citations") or [])
        writer.writerow(row)
    return buf.getvalue()


async def citation_opportunities(
    db: AsyncSession,
    *,
    persona: str | None = None,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    brand: str | None = None,
    llm_name: str | None = None,
    limit: int = 20,
) -> dict:
    """Ranked 'where to earn citations' domains (A). See ``remediation.citations``."""
    return await citations.citation_opportunities(
        db,
        persona=persona,
        therapeutic_area=therapeutic_area,
        indication=indication,
        brand=brand,
        llm_name=llm_name,
        limit=limit,
    )


async def share_of_citation(
    db: AsyncSession,
    *,
    persona: str | None = None,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    brand: str | None = None,
    llm_name: str | None = None,
) -> dict:
    """Share-of-citation benchmark: brand vs competitor citation shares (C)."""
    return await citations.share_of_citation(
        db,
        persona=persona,
        therapeutic_area=therapeutic_area,
        indication=indication,
        brand=brand,
        llm_name=llm_name,
    )


async def preferred_source_gaps(
    db: AsyncSession,
    *,
    therapeutic_area: str | None = None,
    llm_name: str | None = None,
) -> dict:
    """Medical-Affairs preferred domains AI most often OMITS (top-priority gaps)."""
    return await citations.preferred_source_gaps(
        db, therapeutic_area=therapeutic_area, llm_name=llm_name
    )


async def query_fanouts(
    db: AsyncSession,
    *,
    persona: str | None = None,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    brand: str | None = None,
    llm_name: str | None = None,
    limit: int = 25,
) -> dict:
    """Search terms grounded models actually ran — phrasings a content team should target."""
    return await citations.query_fanouts(
        db,
        persona=persona,
        therapeutic_area=therapeutic_area,
        indication=indication,
        brand=brand,
        llm_name=llm_name,
        limit=limit,
    )


async def citation_trend(
    db: AsyncSession,
    *,
    persona: str | None = None,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    brand: str | None = None,
    llm_name: str | None = None,
) -> dict:
    """AbbVie/competitor/independent citation share over time (day buckets)."""
    return await citations.citation_trend(
        db,
        persona=persona,
        therapeutic_area=therapeutic_area,
        indication=indication,
        brand=brand,
        llm_name=llm_name,
    )


# --- Recommendation triage workflow (persisted + audited, BR-010) -----------------
def _serialize_review(r: RecommendationReview) -> dict:
    return {
        "rec_id": r.rec_id,
        "status": r.status,
        "owner": r.owner,
        "note": r.note,
        "updated_by": r.updated_by,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


async def list_reviews(db: AsyncSession, *, batch_id: str | None = None) -> dict:
    """Triage state for recommendations, scoped to a batch's rec_ids when batch_id is given."""
    stmt = select(RecommendationReview)
    if batch_id:
        rec_ids = list(
            (await db.execute(
                select(Recommendation.rec_id).where(Recommendation.batch_id == batch_id)
            )).scalars().all()
        )
        if not rec_ids:
            return {"count": 0, "items": []}
        stmt = stmt.where(RecommendationReview.rec_id.in_(rec_ids))
    rows = list((await db.execute(stmt)).scalars().all())
    return {"count": len(rows), "items": [_serialize_review(r) for r in rows]}


async def set_review(
    db: AsyncSession,
    *,
    rec_id: str,
    status: str,
    owner: str | None = None,
    note: str | None = None,
    updated_by: str | None = None,
) -> dict:
    """Upsert the triage state for one recommendation (shared across users, audited)."""
    review = await db.get(RecommendationReview, rec_id)
    if review is None:
        review = RecommendationReview(rec_id=rec_id)
        db.add(review)
    review.status = status
    review.owner = owner
    review.note = note
    review.updated_by = updated_by
    await db.commit()
    await db.refresh(review)
    return _serialize_review(review)
