"""GEO Intervention Recommendations API (BR-012).

Generates plain-language, ranked, evidence-backed content recommendations for
competitive-position gaps (focus brand scored SECOND_LINE / NOT_RECOMMENDED), enriched
with SEMrush SEO metrics. Results are persisted and served as a ranked, filterable,
CSV-exportable list for the Insights dashboard. Every recommendation is a STRATEGIC
SUGGESTION ONLY — never MLR-approved content (surfaced to the UI as a hardcoded banner).
"""
from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.models.recommendation import RECOMMENDATION_SOURCES
from app.remediation import implications as impl
from app.remediation import semrush
from app.remediation.prompts import APPROVED_CONTENT_TYPES
from app.schemas import GenerateRecommendationsRequest, RecommendationReviewUpdate
from app.services import recommendation_service as svc
from app.utils.audit import write_audit

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


@router.get("/content-types")
async def content_types():
    """The approved content_type enum the LLM must choose from (BR-012.2)."""
    return {"content_types": APPROVED_CONTENT_TYPES, "semrush_configured": semrush.is_configured()}


@router.get("/implications")
async def implications():
    """The Phase-9 strategic-implication vocabulary, with owner and actionability.

    ``externally_actionable: false`` is the important half: those findings are real and are
    reported, but no publishable asset closes them, so a UI must not offer "create content
    action" against one.
    """
    return {
        "sources": list(RECOMMENDATION_SOURCES),
        "implications": [
            {
                "implication": name,
                "owner": impl.OWNER_OF[name],
                "severity": impl.SEVERITY_OF[name],
                "externally_actionable": name in impl.EXTERNALLY_ACTIONABLE,
            }
            for name in impl.IMPLICATIONS
        ],
    }


@router.post("/generate", status_code=201)
async def generate(req: GenerateRecommendationsRequest, db: AsyncSession = Depends(get_db)):
    """Run the pipeline (find gaps -> SEMrush -> LLM -> rank) and persist a batch (BR-012.1).

    When ``response_ids`` is supplied the batch is scoped to exactly those answers (e.g. a
    source's not-mentioned cohort from the Influence Graph) and the limit widens to fit them.
    """
    # Cover the whole requested cohort; otherwise honour the requested ranking limit.
    limit = len(req.response_ids) if req.response_ids else req.limit
    return await svc.generate(
        db,
        persona=req.persona,
        therapeutic_area=req.therapeutic_area,
        indication=req.indication,
        brand=req.brand,
        llm_name=req.llm_name,
        response_ids=req.response_ids,
        limit=limit,
    )


@router.get("")
async def list_recommendations(
    persona: str | None = Query(None),
    therapeutic_area: str | None = Query(None),
    indication: str | None = Query(None),
    brand: str | None = Query(None),
    llm_name: str | None = Query(None),
    batch_id: str | None = Query(None),
    source_type: str | None = Query(
        None, description="POSITIONING_GAP | EVIDENCE_GAP — which finder produced the row."
    ),
    strategic_implication: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Ranked (impact_score desc) recommendations for the latest batch + filters (BR-012.3/.6)."""
    return await svc.list_recommendations(
        db,
        persona=persona,
        therapeutic_area=therapeutic_area,
        indication=indication,
        brand=brand,
        llm_name=llm_name,
        batch_id=batch_id,
        source_type=source_type,
        strategic_implication=strategic_implication,
    )


@router.get("/export.csv")
async def export_csv(
    persona: str | None = Query(None),
    therapeutic_area: str | None = Query(None),
    indication: str | None = Query(None),
    brand: str | None = Query(None),
    llm_name: str | None = Query(None),
    batch_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """CSV export of the ranked/filtered recommendation list (BR-012.3)."""
    result = await svc.list_recommendations(
        db,
        persona=persona,
        therapeutic_area=therapeutic_area,
        indication=indication,
        brand=brand,
        llm_name=llm_name,
        batch_id=batch_id,
    )
    return PlainTextResponse(
        svc.to_csv(result["items"]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=recommendations.csv"},
    )


@router.get("/citation-opportunities")
async def citation_opportunities(
    persona: str | None = Query(None),
    therapeutic_area: str | None = Query(None),
    indication: str | None = Query(None),
    brand: str | None = Query(None),
    llm_name: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Authoritative domains where the focus brand is MISSING from AI citations (A / BR-005).

    "Where credible sources for a therapy are missing in grounded answers" — the domains
    worth earning a presence/citation on, ranked by frequency + competitive-gap weight.
    """
    return await svc.citation_opportunities(
        db,
        persona=persona,
        therapeutic_area=therapeutic_area,
        indication=indication,
        brand=brand,
        llm_name=llm_name,
        limit=limit,
    )


@router.get("/share-of-citation")
async def share_of_citation(
    persona: str | None = Query(None),
    therapeutic_area: str | None = Query(None),
    indication: str | None = Query(None),
    brand: str | None = Query(None),
    llm_name: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Share of cited sources that reference the focus brand vs each competitor (C / BR-005)."""
    return await svc.share_of_citation(
        db,
        persona=persona,
        therapeutic_area=therapeutic_area,
        indication=indication,
        brand=brand,
        llm_name=llm_name,
    )


@router.get("/preferred-source-gaps")
async def preferred_source_gaps(
    therapeutic_area: str | None = Query(None),
    llm_name: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Medical-Affairs preferred domains and how often AI OMITS them (top-priority gaps).

    Reads durable presence/absence observations (FR-706a.7); the domains MA most wants cited
    but AI ignores rank first.
    """
    return await svc.preferred_source_gaps(
        db, therapeutic_area=therapeutic_area, llm_name=llm_name
    )


@router.get("/query-fanouts")
async def query_fanouts(
    persona: str | None = Query(None),
    therapeutic_area: str | None = Query(None),
    indication: str | None = Query(None),
    brand: str | None = Query(None),
    llm_name: str | None = Query(None),
    limit: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """The real search terms grounded models ran — the phrasings a content team should target."""
    return await svc.query_fanouts(
        db,
        persona=persona,
        therapeutic_area=therapeutic_area,
        indication=indication,
        brand=brand,
        llm_name=llm_name,
        limit=limit,
    )


@router.get("/citation-trend")
async def citation_trend(
    persona: str | None = Query(None),
    therapeutic_area: str | None = Query(None),
    indication: str | None = Query(None),
    brand: str | None = Query(None),
    llm_name: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """AbbVie/competitor/independent citation share over time — is the AI position improving?"""
    return await svc.citation_trend(
        db,
        persona=persona,
        therapeutic_area=therapeutic_area,
        indication=indication,
        brand=brand,
        llm_name=llm_name,
    )


@router.get("/reviews")
async def list_reviews(
    batch_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Persisted triage state (status/owner/note) for recommendations, by batch (BR-010)."""
    return await svc.list_reviews(db, batch_id=batch_id)


@router.put("/{rec_id}/review")
async def set_review(
    rec_id: str,
    data: RecommendationReviewUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Set/clear a recommendation's triage status/owner/note (persisted + audited, BR-010)."""
    result = await svc.set_review(
        db,
        rec_id=rec_id,
        status=data.status,
        owner=data.owner,
        note=data.note,
        updated_by=data.updated_by,
    )
    await write_audit(
        db,
        role="MEDICAL_AFFAIRS",
        event="RECOMMENDATION_REVIEW_SET",
        context={"rec_id": rec_id, "status": data.status,
                 "owner": data.owner, "by": data.updated_by},
    )
    return result
