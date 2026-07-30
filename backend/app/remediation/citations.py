"""Citation-gap analytics for the GEO engine (BR-005 "evidence gaps").

Read-only aggregations over the CLASSIFIED citation graph (FR-706a Source Authority:
``response_citations`` joined to ``source_domains``), so the GEO recommendation views and the
Source Authority dashboard read the SAME numbers. Borrowed from AEO/GEO practice (e.g.
Profound's Citations reports) but computed with transparent plain math — no external ML.

Views:
  * :func:`share_of_citation` — delegates to Source Authority ``share_of_voice`` (AbbVie vs
    competitor vs independent by control_type). One consistent share across pages (C).
  * :func:`citation_opportunities` — authoritative NON-AbbVie domains (INDEPENDENT / UNKNOWN
    control) the AI relied on, ranked by frequency + competitive-gap weight; flagged when the
    domain is a Medical-Affairs preferred source. Where to earn a citation (A / BR-005).
  * :func:`preferred_source_gaps` — Medical-Affairs preferred domains and how often AI OMITS
    them (durable presence/absence observations). Highest-priority gaps to close.
  * :func:`query_fanouts` — the real search terms grounded models ran
    (``Response.search_queries``), frequency-ranked: phrasings a content team should target.
  * :func:`citation_trend` — AbbVie/competitor/independent citation share over time (day
    buckets), to see whether the brand's AI position is improving.

Everything is best-effort and never raises on malformed JSON.
"""
from __future__ import annotations

import json

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.taxonomy import area_for, keys_for_area
from app.models.preferred_source import PreferredSource
from app.models.preferred_source_observation import PreferredSourceObservation
from app.models.response import Response
from app.models.response_citation import ResponseCitation
from app.models.scoring import ScoringRecord
from app.models.source_domain import (
    CONTROL_ABBVIE,
    CONTROL_COMPETITOR,
    CONTROL_INDEPENDENT,
    CONTROL_UNKNOWN,
    SourceDomain,
)
from app.remediation.gaps import POSITION_SEVERITY
from app.source_authority import service as sa_service


def _loads(raw: str | None) -> list:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


_WEAK_POSITIONS = ("SECOND_LINE", "NOT_RECOMMENDED")
# Control tiers that represent an *earnable* citation opportunity (we don't own them and they
# aren't a competitor's property). COMPETITOR domains are surfaced via share_of_voice instead.
_OPPORTUNITY_CONTROLS = (CONTROL_INDEPENDENT, CONTROL_UNKNOWN)


def _cite_filters(stmt, *, llm_name, therapeutic_area, indication, brand, persona):
    """Apply the standard dashboard filters to a ResponseCitation query (mirrors FR-706a)."""
    if llm_name:
        stmt = stmt.where(ResponseCitation.llm_name == llm_name)
    if therapeutic_area:
        child = keys_for_area(therapeutic_area)
        if child:
            stmt = stmt.where(ResponseCitation.therapeutic_area.in_(child))
        else:
            stmt = stmt.where(ResponseCitation.therapeutic_area == therapeutic_area)
    if indication:
        stmt = stmt.where(ResponseCitation.indication == indication)
    if brand:
        stmt = stmt.where(ResponseCitation.brand_focus == brand)
    if persona:
        stmt = stmt.where(ResponseCitation.persona == persona)
    return stmt


def _resp_filters(stmt, *, llm_name, therapeutic_area, indication, brand, persona):
    """Apply the standard filters to a Response query (for search-query fanouts)."""
    if llm_name:
        stmt = stmt.where(Response.llm_name == llm_name)
    if therapeutic_area:
        child = keys_for_area(therapeutic_area)
        if child:
            stmt = stmt.where(Response.therapeutic_area.in_(child))
        else:
            stmt = stmt.where(Response.therapeutic_area == therapeutic_area)
    if indication:
        stmt = stmt.where(Response.indication == indication)
    if brand:
        stmt = stmt.where(Response.brand_focus == brand)
    if persona:
        stmt = stmt.where(Response.persona == persona)
    return stmt


async def _latest_positions(db: AsyncSession, response_ids: set[str]) -> dict[str, str | None]:
    """Latest competitive_position per response (max score_version, then newest)."""
    if not response_ids:
        return {}
    rows = (await db.execute(
        select(ScoringRecord.response_id, ScoringRecord.competitive_position)
        .where(ScoringRecord.response_id.in_(list(response_ids)))
        .order_by(
            ScoringRecord.response_id,
            ScoringRecord.score_version.desc(),
            ScoringRecord.created_at.desc(),
        )
    )).all()
    out: dict[str, str | None] = {}
    for rid, pos in rows:
        out.setdefault(rid, pos)
    return out


async def _preferred_domains(db: AsyncSession, therapeutic_area: str | None) -> set[str]:
    """Active Medical-Affairs preferred-source domains for a TA (+ its parent area)."""
    stmt = select(PreferredSource.authority_domain).where(PreferredSource.active.is_(True))
    if therapeutic_area:
        tas = {therapeutic_area, area_for(therapeutic_area)}
        stmt = stmt.where(PreferredSource.therapeutic_area.in_(tas))
    return {d for (d,) in (await db.execute(stmt)).all()}


async def share_of_citation(
    db: AsyncSession,
    *,
    persona: str | None = None,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    brand: str | None = None,
    llm_name: str | None = None,
) -> dict:
    """Citation share of voice (AbbVie vs competitor vs independent) — the SAME classified,
    control-based number the Source Authority page shows (C). Delegates to share_of_voice."""
    return await sa_service.share_of_voice(
        db,
        llm_name=llm_name,
        therapeutic_area=therapeutic_area,
        indication=indication,
        brand=brand,
        persona=persona,
    )


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
    """Non-AbbVie authoritative domains AI relies on, ranked as citation opportunities (A).

    Uses the classified citation graph: INDEPENDENT / UNKNOWN domains (the ones we can earn a
    presence on) grouped with control/authority/verification/publisher, weighted by how many
    distinct responses cite them and how many of those were weak-position gaps. Preferred
    (Medical-Affairs designated) domains are flagged and floated to the top.
    """
    stmt = (
        select(
            ResponseCitation.response_id,
            ResponseCitation.authority_domain,
            ResponseCitation.citation_count,
            ResponseCitation.brand_focus,
            SourceDomain.control_type,
            SourceDomain.authority_type,
            SourceDomain.display_category,
            SourceDomain.verification,
            SourceDomain.publisher_name,
        )
        .select_from(ResponseCitation)
        .join(SourceDomain, SourceDomain.domain_id == ResponseCitation.domain_id)
        .where(SourceDomain.control_type.in_(_OPPORTUNITY_CONTROLS))
    )
    stmt = _cite_filters(
        stmt, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona,
    )
    rows = (await db.execute(stmt)).all()
    if not rows:
        return {"count": 0, "responses_with_citations": 0, "items": []}

    positions = await _latest_positions(db, {r.response_id for r in rows})
    preferred = await _preferred_domains(db, therapeutic_area)

    agg: dict[str, dict] = {}
    for r in rows:
        a = agg.setdefault(
            r.authority_domain,
            {
                "citation_count": 0,
                "responses": set(),
                "weak_responses": set(),
                "gap_weight": 0.0,
                "brands": set(),
                "control_type": r.control_type,
                "authority_type": r.authority_type,
                "display_category": r.display_category,
                "verification": r.verification,
                "publisher_name": r.publisher_name,
            },
        )
        a["citation_count"] += int(r.citation_count or 0)
        a["responses"].add(r.response_id)
        if r.brand_focus:
            a["brands"].add(r.brand_focus)
        pos = positions.get(r.response_id)
        if pos in _WEAK_POSITIONS:
            a["weak_responses"].add(r.response_id)
            a["gap_weight"] += POSITION_SEVERITY.get(pos, 0.0)

    items: list[dict] = []
    for dom, a in agg.items():
        response_count = len(a["responses"])
        items.append(
            {
                "domain": dom,
                "control_type": a["control_type"],
                "authority_type": a["authority_type"],
                "display_category": a["display_category"],
                "verification": a["verification"],
                "publisher_name": a["publisher_name"],
                "citation_count": a["citation_count"],
                "response_count": response_count,
                "weak_position_count": len(a["weak_responses"]),
                "opportunity_score": round(response_count + a["gap_weight"], 3),
                "is_preferred": dom in preferred,
                "brands": sorted(a["brands"]),
            }
        )
    # Preferred domains first, then by opportunity score, then raw citation volume.
    items.sort(
        key=lambda x: (x["is_preferred"], x["opportunity_score"], x["citation_count"]),
        reverse=True,
    )
    responses_with = len({r.response_id for r in rows})
    return {
        "count": len(items),
        "responses_with_citations": responses_with,
        "items": items[: max(limit, 1)],
    }


async def preferred_source_gaps(
    db: AsyncSession,
    *,
    therapeutic_area: str | None = None,
    llm_name: str | None = None,
) -> dict:
    """Medical-Affairs preferred domains and how often AI OMITS them (FR-706a.7 + BR-005).

    Reads durable presence/absence observations; ranks by absence so the domains MA most
    wants cited (but AI ignores) surface as the top-priority interventions.
    """
    ps_stmt = select(PreferredSource).where(PreferredSource.active.is_(True))
    if therapeutic_area:
        tas = {therapeutic_area, area_for(therapeutic_area)}
        ps_stmt = ps_stmt.where(PreferredSource.therapeutic_area.in_(tas))
    prefs = list((await db.execute(ps_stmt)).scalars().all())
    if not prefs:
        return {"count": 0, "configured": 0, "items": []}

    obs_stmt = (
        select(
            PreferredSourceObservation.authority_domain,
            func.count().label("total"),
            func.sum(
                case((PreferredSourceObservation.was_present.is_(True), 1), else_=0)
            ).label("present"),
        )
        .group_by(PreferredSourceObservation.authority_domain)
    )
    if therapeutic_area:
        tas = {therapeutic_area, area_for(therapeutic_area)}
        obs_stmt = obs_stmt.where(PreferredSourceObservation.therapeutic_area.in_(tas))
    if llm_name:
        obs_stmt = obs_stmt.where(PreferredSourceObservation.llm_name == llm_name)
    obs = {
        dom: (int(total or 0), int(present or 0))
        for dom, total, present in (await db.execute(obs_stmt)).all()
    }

    items = []
    for p in prefs:
        total, present = obs.get(p.authority_domain, (0, 0))
        absent = total - present
        items.append(
            {
                "authority_domain": p.authority_domain,
                "therapeutic_area": p.therapeutic_area,
                "note": p.note,
                "observations": total,
                "present": present,
                "absent": absent,
                "absence_pct": round(absent / total * 100, 1) if total else None,
            }
        )
    items.sort(
        key=lambda x: (
            x["absence_pct"] if x["absence_pct"] is not None else -1.0,
            x["absent"],
        ),
        reverse=True,
    )
    return {"count": len(items), "configured": len(prefs), "items": items}


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
    """Frequency-ranked search terms grounded models actually ran (Response.search_queries).

    Borrowed from Profound's "query fanouts": the exact phrasings AI searched when answering
    questions in scope — what a content team should target so their assets get retrieved.
    """
    stmt = select(
        Response.response_id,
        Response.search_queries,
        Response.brand_focus,
    ).where(Response.status.in_(("SUCCESS", "TRUNCATED")))
    stmt = _resp_filters(
        stmt, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona,
    )
    rows = (await db.execute(stmt)).all()

    counts: dict[str, dict] = {}
    responses_with = 0
    for rid, sq, brand_focus in rows:
        queries = _loads(sq)
        if queries:
            responses_with += 1
        for q in queries:
            if not isinstance(q, str):
                continue
            norm = q.strip()
            if not norm:
                continue
            c = counts.setdefault(
                norm.lower(),
                {"query": norm, "count": 0, "responses": set(), "brands": set()},
            )
            c["count"] += 1
            c["responses"].add(rid)
            if brand_focus:
                c["brands"].add(brand_focus)
    items = [
        {
            "query": c["query"],
            "count": c["count"],
            "response_count": len(c["responses"]),
            "brands": sorted(c["brands"]),
        }
        for c in counts.values()
    ]
    items.sort(key=lambda x: (x["count"], x["response_count"]), reverse=True)
    return {
        "count": len(items),
        "responses_with_queries": responses_with,
        "items": items[: max(limit, 1)],
    }


async def citation_trend(
    db: AsyncSession,
    *,
    persona: str | None = None,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    brand: str | None = None,
    llm_name: str | None = None,
) -> dict:
    """AbbVie / competitor / independent citation share over time (day buckets), so you can
    see whether the brand's AI citation position is improving run-over-run (BR-003/BR-005)."""
    day = func.date(ResponseCitation.created_at)
    stmt = (
        select(day, SourceDomain.control_type, func.sum(ResponseCitation.citation_count))
        .select_from(ResponseCitation)
        .join(SourceDomain, SourceDomain.domain_id == ResponseCitation.domain_id)
        .group_by(day, SourceDomain.control_type)
        .order_by(day)
    )
    stmt = _cite_filters(
        stmt, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona,
    )
    rows = (await db.execute(stmt)).all()

    periods: dict[str, dict] = {}
    for day_val, control, cc in rows:
        key = str(day_val)[:10]
        n = int(cc or 0)
        p = periods.setdefault(
            key,
            {
                "period": key,
                "total": 0,
                "abbvie": 0,
                "competitor": 0,
                "independent": 0,
                "unknown": 0,
            },
        )
        p["total"] += n
        if control == CONTROL_ABBVIE:
            p["abbvie"] += n
        elif control == CONTROL_COMPETITOR:
            p["competitor"] += n
        elif control == CONTROL_INDEPENDENT:
            p["independent"] += n
        else:
            p["unknown"] += n

    out = []
    for key in sorted(periods):
        p = periods[key]
        t = p["total"] or 1
        out.append(
            {
                **p,
                "abbvie_share_pct": round(p["abbvie"] / t * 100, 1),
                "competitor_share_pct": round(p["competitor"] / t * 100, 1),
                "independent_share_pct": round(p["independent"] / t * 100, 1),
                "unknown_share_pct": round(p["unknown"] / t * 100, 1),
            }
        )
    return {"granularity": "day", "periods": out}


async def placement_guidance(
    db: AsyncSession,
    *,
    persona: str | None = None,
    therapeutic_area: str | None = None,
    brand: str | None = None,
    top: int = 5,
) -> dict:
    """Compact "where to publish / earn a citation" guidance for a gap's TA/persona/brand.

    Answers the "where" that the GEO recommendation itself doesn't: it folds three existing
    citation views into one small, display-ready bundle scoped to the recommendation's topic:

      * ``earn_citations``  — authoritative NON-AbbVie domains the AI already trusts for this
        topic (Medical-Affairs preferred sources flagged + floated to the top). Getting cited
        here is the most direct lever on the AI answer.
      * ``preferred_gaps``  — Medical-Affairs preferred domains the AI currently OMITS (durable
        gaps worth closing).
      * ``target_queries``  — the search phrasings grounded models actually ran, so a content
        team can shape the asset to be retrieved.

    Scoped to TA + persona + brand (not indication/llm) so the guidance is populated and stable,
    and so a list of recommendations sharing a scope can reuse one computation. Best-effort:
    any sub-view that errors simply yields an empty list — this is additive context, never fatal.
    """

    async def _safe(coro) -> dict:
        try:
            return await coro
        except Exception:  # noqa: BLE001 — guidance is additive; never break the caller
            return {"items": []}

    opps = await _safe(citation_opportunities(
        db, persona=persona, therapeutic_area=therapeutic_area, brand=brand, limit=top))
    gaps = await _safe(preferred_source_gaps(db, therapeutic_area=therapeutic_area))
    fan = await _safe(query_fanouts(
        db, persona=persona, therapeutic_area=therapeutic_area, brand=brand, limit=top))

    earn = [
        {
            "domain": i.get("domain"),
            "authority_type": i.get("authority_type"),
            "display_category": i.get("display_category"),
            "is_preferred": bool(i.get("is_preferred")),
            "response_count": i.get("response_count"),
            "opportunity_score": i.get("opportunity_score"),
        }
        for i in (opps.get("items") or [])[:top]
    ]
    preferred_gaps = [
        {
            "domain": g.get("authority_domain"),
            "absence_pct": g.get("absence_pct"),
            "absent": g.get("absent"),
        }
        for g in (gaps.get("items") or [])
        if (g.get("absent") or 0) > 0
    ][:top]
    target_queries = [
        {"query": q.get("query"), "count": q.get("count")}
        for q in (fan.get("items") or [])[:top]
    ]
    return {
        "scope": {"persona": persona, "therapeutic_area": therapeutic_area, "brand": brand},
        "earn_citations": earn,
        "preferred_gaps": preferred_gaps,
        "target_queries": target_queries,
    }
