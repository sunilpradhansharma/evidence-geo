"""Source Authority Mapping API (FR-706a).

Read endpoints (distribution / top-domains / coverage / preferred / observations) are strictly
side-effect-free. Mutations (preferred add/remove, classify sweep) are audited. Role-gating is
deferred until app-wide RBAC returns (it was reverted); every mutation is audit-logged now.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.schemas import PreferredSourceCreate
from app.source_authority import enrichment, service as svc
from app.utils.audit import write_audit

router = APIRouter(prefix="/source-authority", tags=["source-authority"])


@router.get("/distribution")
async def distribution(
    llm_name: str | None = Query(None),
    therapeutic_area: str | None = Query(None),
    indication: str | None = Query(None),
    brand: str | None = Query(None),
    persona: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Source Authority Distribution by display_category (+ coverage context) — FR-706a.3."""
    return await svc.distribution(
        db, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona,
    )


@router.get("/top-domains")
async def top_domains(
    llm_name: str | None = Query(None),
    therapeutic_area: str | None = Query(None),
    indication: str | None = Query(None),
    brand: str | None = Query(None),
    persona: str | None = Query(None),
    group_by: str | None = Query(None, pattern="^(llm_name)$"),
    limit: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Top-cited authority domains, optionally the top-N PER model — FR-706a.5/.6."""
    return await svc.top_domains(
        db, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona,
        group_by=group_by, limit=limit,
    )


@router.get("/coverage")
async def coverage(
    llm_name: str | None = Query(None),
    therapeutic_area: str | None = Query(None),
    indication: str | None = Query(None),
    brand: str | None = Query(None),
    persona: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Share of citation-capable responses that carried classified citations (4 states)."""
    return await svc.coverage(
        db, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona,
    )


@router.get("/status")
async def status():
    """Which optional enrichment layers are active (RDAP is always on; LLM/WhoisXML optional)."""
    return enrichment.enrichment_status()


# --- Enhancements: trends / domain drill-down / sentiment x source ----------------
@router.get("/trends")
async def trends(
    llm_name: str | None = Query(None),
    therapeutic_area: str | None = Query(None),
    indication: str | None = Query(None),
    brand: str | None = Query(None),
    persona: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Citation-category volume over time (day buckets) for the trust-trend timeline."""
    return await svc.citation_trends(
        db, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona,
    )


@router.get("/domain")
async def domain_detail(
    authority_domain: str = Query(..., min_length=1),
    llm_name: str | None = Query(None),
    therapeutic_area: str | None = Query(None),
    indication: str | None = Query(None),
    brand: str | None = Query(None),
    persona: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Every response that cited a given authority domain, with real cited URLs + scores."""
    return await svc.domain_detail(
        db, authority_domain=authority_domain, llm_name=llm_name,
        therapeutic_area=therapeutic_area, indication=indication, brand=brand,
        persona=persona, limit=limit,
    )


@router.get("/sentiment-correlation")
async def sentiment_correlation(
    llm_name: str | None = Query(None),
    therapeutic_area: str | None = Query(None),
    indication: str | None = Query(None),
    brand: str | None = Query(None),
    persona: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Brand sentiment/positioning bucketed by the control of each answer's top-cited source."""
    return await svc.sentiment_by_source(
        db, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona,
    )


@router.get("/share-of-voice")
async def share_of_voice(
    llm_name: str | None = Query(None),
    therapeutic_area: str | None = Query(None),
    indication: str | None = Query(None),
    brand: str | None = Query(None),
    persona: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Citation share of voice (AbbVie vs competitor vs independent) + top competitor domains."""
    return await svc.share_of_voice(
        db, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona,
    )


@router.get("/pages")
async def top_pages(
    llm_name: str | None = Query(None),
    therapeutic_area: str | None = Query(None),
    indication: str | None = Query(None),
    brand: str | None = Query(None),
    persona: str | None = Query(None),
    control: str | None = Query(None, pattern="^(ABBVIE|COMPETITOR|INDEPENDENT|UNKNOWN)$"),
    limit: int = Query(25, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Most-cited individual pages (URLs), aggregated across responses; optional control filter."""
    return await svc.top_pages(
        db, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona, control=control, limit=limit,
    )


@router.get("/influence-graph")
async def influence_graph(
    llm_name: str | None = Query(None),
    therapeutic_area: str | None = Query(None),
    indication: str | None = Query(None),
    brand: str | None = Query(None),
    persona: str | None = Query(None),
    theme: str | None = Query(None),
    focus_domain: str | None = Query(None),
    top_n: int = Query(60, ge=10, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Corpus-wide Source -> Claim -> Theme -> Position influence graph (force-directed web).

    Returns nodes/links plus per-narrative source drivers and a grounded-coverage denominator.
    Optionally focus on one theme label or one authority domain's subgraph.
    """
    return await svc.influence_graph(
        db, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona,
        theme=theme, focus_domain=focus_domain, top_n=top_n,
    )


@router.get("/influence-graph/node-evidence")
async def influence_graph_node_evidence(
    node_type: str = Query(..., description="theme | position"),
    key: str = Query(..., description="theme label, or competitive_position value"),
    llm_name: str | None = Query(None),
    therapeutic_area: str | None = Query(None),
    indication: str | None = Query(None),
    brand: str | None = Query(None),
    persona: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Top real answers behind a narrative or brand-position node in the influence graph.

    Complements the source drill-down (``/source-authority/domain/...``) so every node type
    in the web can show its underlying evidence, using the same item shape.
    """
    return await svc.node_evidence(
        db, node_type=node_type, key=key,
        llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona, limit=limit,
    )


@router.get("/response/{response_id}/provenance")
async def response_provenance(response_id: str, db: AsyncSession = Depends(get_db)):
    """Per-claim source trust for one response (which claims rest on trusted vs risky sources)."""
    return await svc.response_provenance(db, response_id)


# --- Preferred sources (FR-706a.7) ------------------------------------------------
@router.get("/preferred/observations")
async def preferred_observations(
    therapeutic_area: str | None = Query(None),
    llm_name: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """READ stored preferred-source presence/absence observations (never writes)."""
    return await svc.preferred_observations(
        db, therapeutic_area=therapeutic_area, llm_name=llm_name
    )


@router.get("/preferred")
async def list_preferred(
    therapeutic_area: str | None = Query(None),
    include_inactive: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    return {"items": await svc.list_preferred(
        db, therapeutic_area=therapeutic_area, include_inactive=include_inactive
    )}


@router.post("/preferred", status_code=201)
async def add_preferred(data: PreferredSourceCreate, db: AsyncSession = Depends(get_db)):
    """Designate a preferred authority domain for a TA (Medical Affairs)."""
    try:
        pref = await svc.add_preferred(
            db,
            therapeutic_area=data.therapeutic_area,
            domain=data.domain,
            note=data.note,
            created_by=data.created_by,
            change_reason=data.change_reason,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    await write_audit(
        db, role="MEDICAL_AFFAIRS", event="PREFERRED_SOURCE_ADD",
        context={"therapeutic_area": data.therapeutic_area, "domain": pref["authority_domain"],
                 "by": data.created_by, "reason": data.change_reason},
    )
    return pref


@router.delete("/preferred/{pref_id}")
async def delete_preferred(
    pref_id: str,
    updated_by: str = Query("Medical Affairs"),
    change_reason: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete (deactivate) a preferred source, preserving the audit trail."""
    ok = await svc.delete_preferred(
        db, pref_id, updated_by=updated_by, change_reason=change_reason
    )
    if not ok:
        raise HTTPException(404, "Preferred source not found or already inactive")
    await write_audit(
        db, role="MEDICAL_AFFAIRS", event="PREFERRED_SOURCE_REMOVE",
        context={"pref_id": pref_id, "by": updated_by, "reason": change_reason},
    )
    return {"status": "removed", "pref_id": pref_id}


# --- Backfill sweep ---------------------------------------------------------------
@router.post("/classify/sweep", status_code=202)
async def classify_sweep(
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Classify sources for historical responses that have none yet (paginated, idempotent)."""
    result = await svc.classify_unclassified_sweep(db, limit=limit, offset=offset)
    await write_audit(
        db, role="SYSTEM", event="SOURCE_AUTHORITY_SWEEP", context=result,
    )
    return result
