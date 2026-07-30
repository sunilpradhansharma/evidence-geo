"""Extract competitive-position gaps + supporting evidence for the GEO engine (BR-012 step 1).

A "gap" is the latest scored response for which the focus brand landed in a weak
competitive position (SECOND_LINE, NOT_RECOMMENDED, or NOT_MENTIONED — the brand entirely
absent, a GEO opportunity to earn a mention). For each gap we derive:
  - the **outperforming competitor** (highest-sentiment competitor mention), BR-012.5
  - the competitor's **domain**, resolved best-effort from the response's grounding
    ``sources`` (the citations the AI relied on), BR-012 external-enrichment input
  - **missing_citations**: authoritative sources cited in the competitor-favourable
    answer that the focus brand is absent from, BR-012.5
"""
import json
from urllib.parse import urlparse

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.taxonomy import keys_for_area
from app.models.response import Response
from app.models.response_citation import ResponseCitation
from app.models.scoring import ScoringRecord
from app.models.source_domain import CONTROL_COMPETITOR, SourceDomain

# Worse position => higher severity weight (drives the impact score, BR-012.3).
# NOT_MENTIONED (brand absent from the answer entirely) is a GEO gap too — ranked between
# SECOND_LINE and NOT_RECOMMENDED: less acute than an active recommendation against the
# brand, but a clearer miss than a mere second-line placement.
POSITION_SEVERITY = {"NOT_RECOMMENDED": 2.0, "NOT_MENTIONED": 1.5, "SECOND_LINE": 1.0}
GAP_POSITIONS = tuple(POSITION_SEVERITY.keys())


def _loads(raw: str | None) -> list:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def _domain_of(source: dict) -> str | None:
    dom = (source.get("domain") or "").strip()
    if dom:
        return dom.lower().lstrip("www.")
    url = (source.get("url") or source.get("redirect_url") or "").strip()
    if url:
        netloc = urlparse(url).netloc.lower()
        return netloc.lstrip("www.") or None
    return None


def _pick_outperforming_competitor(mentions: list, focus_brand: str, focus_sentiment: float | None):
    """Return (name, sentiment) of the strongest competitor mention, or (None, None).

    Prefers mentions explicitly flagged ``is_competitor``; among candidates it keeps the
    highest sentiment (the one the AI spoke most favourably about vs the focus brand).
    """
    focus = (focus_brand or "").strip().lower()
    candidates: list[tuple[int, float, str, float | None]] = []
    for m in mentions:
        if not isinstance(m, dict):
            continue
        name = (m.get("brand") or m.get("name") or "").strip()
        if not name or name.lower() == focus:
            continue
        sent = m.get("sentiment")
        try:
            sent = float(sent) if sent is not None else None
        except (TypeError, ValueError):
            sent = None
        is_comp = 1 if m.get("is_competitor", False) else 0
        candidates.append((is_comp, sent if sent is not None else -2.0, name, sent))
    if not candidates:
        return None, None
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    _, _, name, sent = candidates[0]
    return name, sent


def _competitor_domain(sources: list, competitor: str | None) -> str | None:
    """Best-effort: a cited domain whose title/url references the competitor name."""
    if not competitor:
        return None
    needle = competitor.strip().lower().split()[0]  # first token, e.g. "Stelara"
    for s in sources:
        if not isinstance(s, dict):
            continue
        haystack = " ".join(
            str(s.get(k) or "") for k in ("title", "url", "redirect_url", "domain")
        ).lower()
        if needle and needle in haystack:
            dom = _domain_of(s)
            if dom:
                return dom
    return None


def _competitor_citation_count(sources: list, competitor: str | None) -> int:
    """How many cited sources reference the outperforming competitor (BR-005 signal).

    A rough measure of how strongly the AI's evidence base favours the competitor; feeds the
    transparent citation multiplier in the impact score.
    """
    needle = (competitor or "").strip().lower().split()[0] if (competitor or "").strip() else ""
    if not needle:
        return 0
    count = 0
    for s in sources:
        if not isinstance(s, dict):
            continue
        haystack = " ".join(
            str(s.get(k) or "") for k in ("title", "url", "redirect_url", "domain")
        ).lower()
        if needle in haystack:
            count += 1
    return count


def _missing_citations(sources: list, focus_brand: str, cap: int = 8) -> list[str]:
    """Citations the AI relied on that the focus brand is absent from (BR-012.5)."""
    focus = (focus_brand or "").strip().lower()
    out: list[str] = []
    seen: set[str] = set()
    for s in sources:
        if not isinstance(s, dict):
            continue
        title = (s.get("title") or "").strip()
        dom = _domain_of(s) or ""
        blob = f"{title} {dom} {s.get('url') or ''}".lower()
        if focus and focus in blob:
            continue  # brand already present in this citation -> not "missing"
        label = f"{dom} — {title}" if dom and title else (dom or title or (s.get("url") or "")).strip()
        key = label.lower()
        if label and key not in seen:
            seen.add(key)
            out.append(label)
        if len(out) >= cap:
            break
    return out


async def _classified_competitor_counts(
    db: AsyncSession, response_ids: list[str]
) -> dict[str, int]:
    """Competitor-controlled citation counts per response from the classified graph (FR-706a).

    Preferred over the per-source string-match heuristic when Source-Authority classification
    has run; returns {} when it hasn't, so callers fall back gracefully.
    """
    if not response_ids:
        return {}
    rows = (await db.execute(
        select(
            ResponseCitation.response_id,
            func.sum(ResponseCitation.citation_count),
        )
        .select_from(ResponseCitation)
        .join(SourceDomain, SourceDomain.domain_id == ResponseCitation.domain_id)
        .where(
            ResponseCitation.response_id.in_(response_ids),
            SourceDomain.control_type == CONTROL_COMPETITOR,
        )
        .group_by(ResponseCitation.response_id)
    )).all()
    return {rid: int(c or 0) for rid, c in rows}


async def find_gaps(
    db: AsyncSession,
    *,
    persona: str | None = None,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    brand: str | None = None,
    llm_name: str | None = None,
    response_ids: list[str] | None = None,
    limit: int = 25,
) -> list[dict]:
    """Return the most recent gap records (latest score is SECOND_LINE/NOT_RECOMMENDED/NOT_MENTIONED).

    Pass ``response_ids`` to restrict the search to a specific cohort of responses (e.g. the
    "not mentioned" answers behind one source in the Influence Graph); the position filter
    still applies, so non-gap responses in the list are simply skipped.
    """
    subq = (
        select(
            ScoringRecord.response_id,
            func.max(ScoringRecord.score_version).label("maxv"),
        )
        .group_by(ScoringRecord.response_id)
        .subquery()
    )
    stmt = (
        select(Response, ScoringRecord)
        .join(ScoringRecord, ScoringRecord.response_id == Response.response_id)
        .join(
            subq,
            and_(
                ScoringRecord.response_id == subq.c.response_id,
                ScoringRecord.score_version == subq.c.maxv,
            ),
        )
        .where(ScoringRecord.competitive_position.in_(GAP_POSITIONS))
    )

    if response_ids:
        stmt = stmt.where(Response.response_id.in_(list(response_ids)))
    if persona:
        stmt = stmt.where(Response.persona == persona)
    if therapeutic_area:
        child_keys = keys_for_area(therapeutic_area)
        if child_keys:
            stmt = stmt.where(Response.therapeutic_area.in_(child_keys))
        else:
            stmt = stmt.where(Response.therapeutic_area == therapeutic_area)
    if indication:
        stmt = stmt.where(Response.indication == indication)
    if brand:
        stmt = stmt.where(Response.brand_focus == brand)
    if llm_name:
        stmt = stmt.where(Response.llm_name == llm_name)

    stmt = stmt.order_by(Response.timestamp_utc.desc()).limit(limit)
    rows = (await db.execute(stmt)).all()

    gaps: list[dict] = []
    for resp, score in rows:
        mentions = _loads(score.brand_mentions)
        sources = _loads(resp.sources)
        competitor, _comp_sent = _pick_outperforming_competitor(
            mentions, resp.brand_focus, score.sentiment_score
        )
        gaps.append(
            {
                "source_response_id": resp.response_id,
                "question_id": resp.question_id,
                "run_id": resp.run_id,
                "persona": resp.persona,
                "therapeutic_area": resp.therapeutic_area,
                "indication": resp.indication,
                "brand_focus": resp.brand_focus,
                "llm_name": resp.llm_name,
                "question_text": resp.question_text,
                "competitive_position": score.competitive_position,
                "gap_severity": POSITION_SEVERITY.get(score.competitive_position, 1.0),
                "sentiment_score": score.sentiment_score,
                "outperforming_competitor": competitor,
                "competitor_domain": _competitor_domain(sources, competitor),
                "competitor_citation_count": _competitor_citation_count(sources, competitor),
                "missing_citations": _missing_citations(sources, resp.brand_focus),
            }
        )

    # Prefer the classified citation graph for the competitor-citation signal where available
    # (falls back to the per-source string match when Source-Authority classification hasn't
    # run yet for these responses).
    classified = await _classified_competitor_counts(
        db, [g["source_response_id"] for g in gaps]
    )
    for g in gaps:
        override = classified.get(g["source_response_id"])
        if override is not None:
            g["competitor_citation_count"] = override
    return gaps
