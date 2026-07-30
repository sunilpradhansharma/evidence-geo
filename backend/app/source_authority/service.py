"""Source Authority service (FR-706a) — classification orchestration + dashboard queries.

Read paths (distribution/top-domains/coverage/observations) are strictly read-only. Write
paths (classify_response/run/sweep, preferred CRUD) persist citations, cache domain
classifications, record preferred-source observations, and raise source-authority alerts.
Everything degrades gracefully: with RDAP unavailable and the LLM classifier off, the curated
taxonomy alone classifies (and enrichment never fabricates an owner).
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import Integer, delete, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.config.taxonomy import area_for, keys_for_area
from app.models.alert import ENTITY_SOURCE_AUTHORITY, Alert
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
    STATUS_CURATED,
    STATUS_UNCLASSIFIED,
    SourceDomain,
)
from app.models.theme import ResponseTheme, Theme
from app.source_authority import classifier, domains, enrichment, taxonomy
from app.source_authority.alerts import evaluate_source_alerts
from app.utils.logging import get_logger

logger = get_logger("source_authority.service")

_OK_STATUSES = ("SUCCESS", "TRUNCATED")
# Hosts that are search-engine redirect wrappers, not the real publisher.
_REDIRECT_MARKERS = ("vertexaisearch", "grounding-api-redirect", "googleusercontent")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Citation capability (coverage denominator)
# ---------------------------------------------------------------------------
def _citation_capable_llm_names() -> set[str]:
    """Target names that CAN return citations (grounded web search, EvidenceMD, or OpenEvidence).

    Parametric Bedrock targets return no sources, so they're excluded from the coverage
    denominator (otherwise they'd look like they "cited nothing of quality"). EvidenceMD is
    a clinical-reasoning LLM that cites peer-reviewed literature inline, so it counts too.
    """
    try:
        from app.providers.registry import load_targets

        names = set()
        for t in load_targets():
            if (
                t.provider in ("open-evidence", "evidencemd")
                or (t.params.extra or {}).get("grounding")
            ):
                names.add(t.name)
        if names:
            return names
    except Exception as e:  # noqa: BLE001
        logger.info("Falling back to default citation-capable set: %s", e)
    return {"gpt-4o", "gemini", "evidencemd", "open-evidence"}


# Legacy Claude answers came from AWS Bedrock (parametric, no web search) and carry a Bedrock
# inference-profile / ARN model id like "us.anthropic.claude-sonnet-4-5-20250929-v1:0" (note the
# "anthropic." provider namespace). The direct Anthropic API — the only Claude path that runs
# web_search and can return citations — reports a BARE id like "claude-sonnet-4-5-20250929" with
# no such namespace. We use that to keep pre-cutover Bedrock Claude answers OUT of the citation
# coverage denominator, so history does not drag coverage down after the switch.
_BEDROCK_MODEL_MARKER = "anthropic."


def _claude_is_grounded(model_version: str | None) -> bool:
    """True only for Claude answers produced on the direct Anthropic API (web-search capable)."""
    return bool(model_version) and _BEDROCK_MODEL_MARKER not in model_version


def _response_is_citation_capable(
    llm_name: str, model_version: str | None, capable: set[str]
) -> bool:
    """Whether a response should count toward the citation-coverage denominator.

    A target is citation-capable by config (grounded web search / EvidenceMD / OpenEvidence).
    Claude is special-cased: only responses produced on the direct Anthropic API count — legacy
    Bedrock Claude answers are excluded, since that path never had a citation capability.
    """
    if llm_name not in capable:
        return False
    if llm_name == "claude" and not _claude_is_grounded(model_version):
        return False
    return True


# ---------------------------------------------------------------------------
# Source parsing
# ---------------------------------------------------------------------------
def _parts_for_source(s: dict):
    """Resolve a source dict to DomainParts, preferring the resolved target over a redirect."""
    candidates = [s.get("redirect_url"), s.get("url")]
    for val in candidates:
        parts = domains.parse_url(val)
        if parts and not any(m in parts.normalized_host for m in _REDIRECT_MARKERS):
            return parts
    # Fall back to the provider-resolved domain field (Gemini stores the real publisher here).
    dom = s.get("domain")
    if dom:
        parts = domains.parse_url("http://" + str(dom).strip())
        if parts and not any(m in parts.normalized_host for m in _REDIRECT_MARKERS):
            return parts
    title = str(s.get("title") or "").strip()
    if title and "." in title and "/" not in title and not any(ch.isspace() for ch in title):
        parts = domains.parse_url("http://" + title)
        if parts and not any(m in parts.normalized_host for m in _REDIRECT_MARKERS):
            return parts
    return None


def _group_sources(sources: list[dict]) -> dict[str, dict]:
    """Group a response's sources by authority domain, preserving count/urls/first position."""
    groups: dict[str, dict] = {}
    for idx, s in enumerate(sources):
        if not isinstance(s, dict):
            continue
        parts = _parts_for_source(s)
        if not parts:
            continue
        ad = taxonomy.authority_domain_for(parts.normalized_host, parts.registrable_domain)
        display_url = s.get("redirect_url") or s.get("url") or ad
        g = groups.get(ad)
        if g is None:
            groups[ad] = {
                "registrable_domain": parts.registrable_domain,
                "normalized_host": parts.normalized_host,
                "urls": [display_url],
                "first_position": idx,
            }
        else:
            g["urls"].append(display_url)
            g["first_position"] = min(g["first_position"], idx)
    return groups


# ---------------------------------------------------------------------------
# Domain classification cache
# ---------------------------------------------------------------------------
async def _get_or_refresh_domain(
    db: AsyncSession, *, authority_domain: str, registrable_domain: str, normalized_host: str,
    enrich: bool = True,
) -> SourceDomain:
    existing = await db.scalar(
        select(SourceDomain).where(SourceDomain.authority_domain == authority_domain)
    )
    now = _utcnow()
    # SQLite returns naive datetimes; treat a naive expiry as UTC so the comparison is safe.
    expires_at = existing.enrichment_expires_at if existing else None
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    fresh = bool(
        existing
        and existing.rules_version == taxonomy.rules_version()
        and (expires_at is None or expires_at > now)
    )
    if existing and fresh:
        return existing

    # Backfill fast path (enrich=False): never touch the network or the LLM. Reuse any cached
    # classification AS-IS — even if it is stale by rules_version — so bumping rules_version can
    # never trigger a mass re-enrichment storm inside a single backfill request (that is what was
    # timing the sweep out at the proxy). For a domain we have never cached, classify from the
    # curated taxonomy only; if it stays unclassified, stamp rules_version=0 so the normal lazy
    # path still enriches it (RDAP + LLM) the next time it is cited in a scored run.
    if not enrich:
        if existing:
            return existing
        result = classifier.classify(normalized_host, registrable_domain, authority_domain)
        if result.get("classification_status") == STATUS_UNCLASSIFIED:
            result["rules_version"] = 0
        obj = SourceDomain(
            domain_id=str(uuid.uuid4()),
            authority_domain=authority_domain,
            registrable_domain=registrable_domain,
            enriched_at=now,
            enrichment_expires_at=None,
            **result,
        )
        db.add(obj)
        await db.flush()
        return obj

    whois = await enrichment.registration_lookup(authority_domain)
    # Evidence-based LLM authority enrichment runs ONLY for domains the curated taxonomy does
    # not resolve, so curated domains never incur a homepage fetch or an LLM call.
    llm_result = None
    if taxonomy.authority_type_for(normalized_host) is None and not taxonomy.control_for(
        normalized_host, registrable_domain
    ):
        llm_result = await enrichment.classify_domain_llm(
            authority_domain, normalized_host,
            registrant_org=whois.get("registrant_organization"),
        )
    result = classifier.classify(
        normalized_host, registrable_domain, authority_domain,
        whois=whois, llm=llm_result,
    )
    ttl_days = get_settings().source_authority_enrichment_ttl_days
    expires = now + timedelta(days=ttl_days) if ttl_days else None

    if existing:
        for key, val in result.items():
            setattr(existing, key, val)
        existing.registrable_domain = registrable_domain
        existing.enriched_at = now
        existing.enrichment_expires_at = expires
        obj = existing
    else:
        obj = SourceDomain(
            domain_id=str(uuid.uuid4()),
            authority_domain=authority_domain,
            registrable_domain=registrable_domain,
            enriched_at=now,
            enrichment_expires_at=expires,
            **result,
        )
        db.add(obj)
    await db.flush()
    return obj


# ---------------------------------------------------------------------------
# Classify one response
# ---------------------------------------------------------------------------
async def classify_response(
    db: AsyncSession, response: Response, *, commit: bool = True, enrich: bool = True
) -> dict:
    """Parse + classify a response's cited sources, record observations, raise alerts."""
    capable = _citation_capable_llm_names()
    sources = []
    if response.sources:
        try:
            sources = json.loads(response.sources) or []
        except (ValueError, TypeError):
            sources = []

    groups = _group_sources(sources)

    # Upsert citation rows (idempotent on response_id + authority_domain).
    existing_rows = {
        r.authority_domain: r
        for r in (await db.execute(
            select(ResponseCitation).where(ResponseCitation.response_id == response.response_id)
        )).scalars().all()
    }
    citation_meta: list[dict] = []
    for authority_domain, g in groups.items():
        domain_obj = await _get_or_refresh_domain(
            db,
            authority_domain=authority_domain,
            registrable_domain=g["registrable_domain"],
            normalized_host=g["normalized_host"],
            enrich=enrich,
        )
        count = len(g["urls"])
        row = existing_rows.get(authority_domain)
        if row is None:
            row = ResponseCitation(
                citation_id=str(uuid.uuid4()),
                response_id=response.response_id,
                run_id=response.run_id,
                domain_id=domain_obj.domain_id,
                authority_domain=authority_domain,
                llm_name=response.llm_name,
                persona=response.persona,
                therapeutic_area=response.therapeutic_area,
                indication=response.indication,
                brand_focus=response.brand_focus,
            )
            db.add(row)
        row.domain_id = domain_obj.domain_id
        row.citation_count = count
        row.citation_urls = json.dumps(g["urls"])
        row.first_citation_position = g["first_position"]
        citation_meta.append({
            "authority_domain": authority_domain,
            "control_type": domain_obj.control_type,
            "verification": domain_obj.verification,
            "citation_count": count,
            "first_citation_position": g["first_position"],
        })

    # Refresh source-authority alerts for this response (idempotent).
    await db.execute(
        delete(Alert).where(
            Alert.response_id == response.response_id,
            Alert.entity_type == ENTITY_SOURCE_AUTHORITY,
        )
    )
    for alert in evaluate_source_alerts(response_id=response.response_id, citations=citation_meta):
        db.add(alert)

    # Preferred-source observations — only for citation-capable responses (FR-706a.7).
    observations = 0
    if _response_is_citation_capable(response.llm_name, response.llm_model_version, capable):
        observations = await _record_observations(db, response, set(groups.keys()))

    await db.flush()
    if commit:
        await db.commit()
    return {
        "response_id": response.response_id,
        "domains": len(groups),
        "citations": sum(len(g["urls"]) for g in groups.values()),
        "observations": observations,
    }


async def _record_observations(
    db: AsyncSession, response: Response, cited_domains: set[str]
) -> int:
    """Record presence/absence of each preferred source for the response's TA (idempotent)."""
    prefs = await _active_prefs_for_ta(db, response.therapeutic_area)
    if not prefs:
        return 0
    existing = {
        o.preferred_source_id: o
        for o in (await db.execute(
            select(PreferredSourceObservation).where(
                PreferredSourceObservation.response_id == response.response_id
            )
        )).scalars().all()
    }
    now = _utcnow()
    for pref in prefs:
        present = pref.authority_domain in cited_domains
        obs = existing.get(pref.pref_id)
        if obs is None:
            obs = PreferredSourceObservation(
                observation_id=str(uuid.uuid4()),
                preferred_source_id=pref.pref_id,
                run_id=response.run_id,
                response_id=response.response_id,
                llm_name=response.llm_name,
                therapeutic_area=response.therapeutic_area,
                authority_domain=pref.authority_domain,
            )
            db.add(obs)
        obs.was_present = present
        obs.observed_at = now
    return len(prefs)


async def _active_prefs_for_ta(db: AsyncSession, therapeutic_area: str | None) -> list[PreferredSource]:
    if not therapeutic_area:
        return []
    tas = {therapeutic_area, area_for(therapeutic_area)}
    rows = await db.execute(
        select(PreferredSource).where(
            PreferredSource.active.is_(True),
            PreferredSource.therapeutic_area.in_(tas),
        )
    )
    return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# Classify a run / backfill sweep
# ---------------------------------------------------------------------------
async def _classify_batch(
    db: AsyncSession, responses, *, enrich: bool
) -> tuple[int, int]:
    """Classify each response in its OWN transaction (commit-per-response, rollback-on-error).

    Committing per response means a single response that trips (e.g. a flush error) is rolled
    back and skipped WITHOUT poisoning the session. Previously the whole batch shared one
    transaction, so one bad response left it in a rollback-pending state and the final commit
    raised — 500'ing the endpoint and discarding every response that had classified cleanly.
    """
    processed = failed = 0
    for r in responses:
        # Capture the id up front: a failed flush + rollback EXPIRES `r` (rollback expires
        # regardless of expire_on_commit), so reading r.response_id afterwards in the except
        # would trigger a lazy reload outside the async greenlet -> MissingGreenlet -> 500.
        rid = r.response_id
        try:
            await classify_response(db, r, commit=False, enrich=enrich)
            await db.commit()
            processed += 1
        except Exception as e:  # noqa: BLE001 — one bad response must not sink the batch
            await db.rollback()
            failed += 1
            logger.warning("Source classification failed for response %s: %s", rid, e)
    return processed, failed


async def classify_run(db: AsyncSession, run_id: str) -> dict:
    responses = (await db.execute(
        select(Response).where(
            Response.run_id == run_id, Response.status.in_(_OK_STATUSES)
        )
    )).scalars().all()
    processed, failed = await _classify_batch(db, responses, enrich=True)
    return {"run_id": run_id, "processed": processed, "failed": failed}


async def classify_unclassified_sweep(
    db: AsyncSession, *, limit: int = 200, offset: int = 0
) -> dict:
    """Backfill: classify responses that carry sources but have no citation rows yet.

    Runs with ``enrich=False`` so it only does DB work — it reuses cached domain classifications
    and classifies brand-new domains from the curated taxonomy only (no RDAP / homepage fetch /
    Bedrock). That keeps a backfill bounded and fast so it cannot time out behind the proxy, even
    right after a rules_version bump has marked every cached domain stale. Domain enrichment
    still happens lazily on scored runs.
    """
    classified = select(distinct(ResponseCitation.response_id))
    stmt = (
        select(Response)
        .where(
            Response.status.in_(_OK_STATUSES),
            Response.sources.isnot(None),
            func.trim(Response.sources).notin_(("", "[]", "null")),
            Response.response_id.notin_(classified),
        )
        .order_by(Response.created_at)
        .limit(limit)
        .offset(offset)
    )
    responses = (await db.execute(stmt)).scalars().all()
    processed, failed = await _classify_batch(db, responses, enrich=False)
    remaining = await db.scalar(
        select(func.count()).select_from(
            select(Response.response_id)
            .where(
                Response.status.in_(_OK_STATUSES),
                Response.sources.isnot(None),
                func.trim(Response.sources).notin_(("", "[]", "null")),
                Response.response_id.notin_(select(distinct(ResponseCitation.response_id))),
            )
            .subquery()
        )
    )
    return {"processed": processed, "failed": failed, "remaining": int(remaining or 0)}


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------
# OpenEvidence's manual-capture step was retired in favour of the automated EvidenceMD
# target. Its historical citations stay in the DB (nothing is deleted) but are hidden from
# every Source Authority view — excluded at the two shared query choke points below so the
# dashboards, share-of-voice, coverage, per-model breakdowns, and trends never surface it.
_HIDDEN_CITATION_LLM_NAMES = ("open-evidence",)


def _apply_citation_filters(
    stmt, *, llm_name, therapeutic_area, indication, brand, persona, response_ids=None,
):
    """Standard citation scoping, plus an optional explicit response cohort.

    ``response_ids`` exists so a caller that has already computed *which answers it means*
    — the head-to-head board, which resolves a comparison from question text the taxonomy
    filters cannot express — can reuse this query rather than growing a second citation
    reader that would drift from the hidden-LLM and TA-expansion rules held here.
    An EMPTY cohort must match nothing, so it is checked with ``is not None``: treating an
    empty list as "no filter" would silently widen a scoped question to the whole corpus.
    """
    stmt = stmt.where(ResponseCitation.llm_name.notin_(_HIDDEN_CITATION_LLM_NAMES))
    if response_ids is not None:
        stmt = stmt.where(ResponseCitation.response_id.in_(list(response_ids)))
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


def _apply_response_filters(stmt, *, llm_name, therapeutic_area, indication, brand, persona):
    stmt = stmt.where(Response.llm_name.notin_(_HIDDEN_CITATION_LLM_NAMES))
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


def _largest_remainder_pct(counts: list[int], total: int) -> list[float]:
    """Round shares to one decimal so the displayed percentages sum to exactly 100.0."""
    if total <= 0 or not counts:
        return [0.0 for _ in counts]
    exact = [c * 1000 / total for c in counts]  # tenths of a percent
    floor = [int(x) for x in exact]
    remainder = 1000 - sum(floor)
    order = sorted(range(len(counts)), key=lambda i: exact[i] - floor[i], reverse=True)
    for k in range(max(0, remainder)):
        floor[order[k % len(order)]] += 1
    return [f / 10.0 for f in floor]


# ---------------------------------------------------------------------------
# Read: distribution / top-domains / coverage
# ---------------------------------------------------------------------------
async def distribution(
    db: AsyncSession, *, llm_name=None, therapeutic_area=None, indication=None,
    brand=None, persona=None,
) -> dict:
    stmt = (
        select(
            SourceDomain.display_category,
            func.sum(ResponseCitation.citation_count),
            func.count(distinct(ResponseCitation.response_id)),
        )
        .select_from(ResponseCitation)
        .join(SourceDomain, SourceDomain.domain_id == ResponseCitation.domain_id)
        .group_by(SourceDomain.display_category)
    )
    stmt = _apply_citation_filters(
        stmt, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona,
    )
    rows = (await db.execute(stmt)).all()
    cats = [
        {"display_category": cat, "citation_count": int(cc or 0), "response_count": int(rc or 0)}
        for cat, cc, rc in rows
    ]
    cats.sort(key=lambda c: c["citation_count"], reverse=True)
    total = sum(c["citation_count"] for c in cats)
    pcts = _largest_remainder_pct([c["citation_count"] for c in cats], total)
    for c, p in zip(cats, pcts):
        c["citation_share_pct"] = p

    cov = await coverage(
        db, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona,
    )
    return {"total_citations": total, "categories": cats, "coverage": cov}


async def top_domains(
    db: AsyncSession, *, llm_name=None, therapeutic_area=None, indication=None,
    brand=None, persona=None, group_by: str | None = None, limit: int = 10,
) -> dict:
    group_cols = [
        ResponseCitation.authority_domain,
        SourceDomain.display_category,
        SourceDomain.control_type,
        SourceDomain.authority_type,
        SourceDomain.verification,
        SourceDomain.publisher_name,
    ]
    select_cols = list(group_cols)
    if group_by == "llm_name":
        select_cols.insert(0, ResponseCitation.llm_name)
        group_cols.insert(0, ResponseCitation.llm_name)

    stmt = (
        select(
            *select_cols,
            func.sum(ResponseCitation.citation_count),
            func.count(distinct(ResponseCitation.response_id)),
        )
        .select_from(ResponseCitation)
        .join(SourceDomain, SourceDomain.domain_id == ResponseCitation.domain_id)
        .group_by(*group_cols)
    )
    stmt = _apply_citation_filters(
        stmt, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona,
    )
    rows = (await db.execute(stmt)).all()

    def _row_to_item(r) -> dict:
        off = 1 if group_by == "llm_name" else 0
        return {
            "authority_domain": r[off],
            "display_category": r[off + 1],
            "control_type": r[off + 2],
            "authority_type": r[off + 3],
            "verification": r[off + 4],
            "publisher_name": r[off + 5],
            "citation_count": int(r[off + 6] or 0),
            "response_count": int(r[off + 7] or 0),
        }

    def _rank(items: list[dict]) -> list[dict]:
        items.sort(key=lambda d: (-d["citation_count"], d["authority_domain"]))
        return items[:limit]

    if group_by == "llm_name":
        by_model: dict[str, list[dict]] = {}
        for r in rows:
            by_model.setdefault(r[0], []).append(_row_to_item(r))
        groups = [
            {"llm_name": name, "items": _rank(items)}
            for name, items in sorted(by_model.items())
        ]
        return {"group_by": "llm_name", "groups": groups}

    return {"group_by": None, "items": _rank([_row_to_item(r) for r in rows])}


async def coverage(
    db: AsyncSession, *, llm_name=None, therapeutic_area=None, indication=None,
    brand=None, persona=None,
) -> dict:
    """Share of citation-capable responses that carried classified citations (4 states)."""
    stmt = select(
        Response.response_id, Response.llm_name, Response.llm_model_version, Response.sources
    ).where(Response.status.in_(_OK_STATUSES))
    stmt = _apply_response_filters(
        stmt, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona,
    )
    rows = (await db.execute(stmt)).all()

    cited_stmt = _apply_citation_filters(
        select(distinct(ResponseCitation.response_id)),
        llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona,
    )
    classified_ids = {rid for (rid,) in (await db.execute(cited_stmt)).all()}

    capable = _citation_capable_llm_names()
    states = {
        "NO_CITATION_CAPABILITY": 0,
        "GROUNDED_ZERO_CITATIONS": 0,
        "CLASSIFICATION_FAILED": 0,
        "CLASSIFIED": 0,
    }
    for rid, llm, model_version, sources in rows:
        if not _response_is_citation_capable(llm, model_version, capable):
            states["NO_CITATION_CAPABILITY"] += 1
        elif rid in classified_ids:
            states["CLASSIFIED"] += 1
        elif sources and str(sources).strip() not in ("", "null", "[]"):
            states["CLASSIFICATION_FAILED"] += 1
        else:
            states["GROUNDED_ZERO_CITATIONS"] += 1

    capable_total = (
        states["CLASSIFIED"] + states["GROUNDED_ZERO_CITATIONS"] + states["CLASSIFICATION_FAILED"]
    )
    pct = round(states["CLASSIFIED"] / capable_total * 100, 1) if capable_total else 0.0
    return {
        "total_responses": len(rows),
        "citation_capable": capable_total,
        "with_citations": states["CLASSIFIED"],
        "coverage_pct": pct,
        "states": states,
    }


# ---------------------------------------------------------------------------
# Read: trends over time / domain drill-down / sentiment x source (enhancements)
# ---------------------------------------------------------------------------
_CONTROL_LABELS = {
    CONTROL_COMPETITOR: "Competitor-controlled",
    CONTROL_INDEPENDENT: "Independent",
    CONTROL_ABBVIE: "AbbVie-controlled",
    CONTROL_UNKNOWN: "Unclassified",
}
_CONTROL_ORDER = {CONTROL_COMPETITOR: 0, CONTROL_INDEPENDENT: 1, CONTROL_ABBVIE: 2, CONTROL_UNKNOWN: 3}
# Weak competitive positions (mirrors the recommendation-gap definition).
_WEAK_POSITIONS = ("SECOND_LINE", "NOT_RECOMMENDED")


async def _latest_scores(db: AsyncSession, response_ids: set[str]) -> dict[str, ScoringRecord]:
    """Latest ScoringRecord per response (highest score_version, then newest)."""
    if not response_ids:
        return {}
    rows = (await db.execute(
        select(ScoringRecord)
        .where(ScoringRecord.response_id.in_(list(response_ids)))
        .order_by(
            ScoringRecord.response_id,
            ScoringRecord.score_version.desc(),
            ScoringRecord.created_at.desc(),
        )
    )).scalars().all()
    out: dict[str, ScoringRecord] = {}
    for r in rows:
        out.setdefault(r.response_id, r)  # first seen for a response = its latest score
    return out


async def citation_trends(
    db: AsyncSession, *, llm_name=None, therapeutic_area=None, indication=None,
    brand=None, persona=None,
) -> dict:
    """Citation volume per display_category, bucketed by day — powers the trend timeline.

    Returns raw per-category counts per day; the client buckets them into the
    trusted/neutral/risk spectrum (that mapping's single source of truth lives client-side).
    """
    day = func.date(ResponseCitation.created_at)
    stmt = (
        select(day, SourceDomain.display_category, func.sum(ResponseCitation.citation_count))
        .select_from(ResponseCitation)
        .join(SourceDomain, SourceDomain.domain_id == ResponseCitation.domain_id)
        .group_by(day, SourceDomain.display_category)
        .order_by(day)
    )
    stmt = _apply_citation_filters(
        stmt, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona,
    )
    rows = (await db.execute(stmt)).all()

    periods: dict[str, dict] = {}
    cats_seen: set[str] = set()
    for day_val, cat, cc in rows:
        key = str(day_val)[:10]
        n = int(cc or 0)
        p = periods.setdefault(key, {"period": key, "total_citations": 0, "categories": {}})
        p["categories"][cat] = p["categories"].get(cat, 0) + n
        p["total_citations"] += n
        cats_seen.add(cat)
    return {
        "granularity": "day",
        "periods": [periods[k] for k in sorted(periods)],
        "categories_seen": sorted(cats_seen),
    }


async def domain_detail(
    db: AsyncSession, *, authority_domain: str, llm_name=None, therapeutic_area=None,
    indication=None, brand=None, persona=None, limit: int = 50,
) -> dict:
    """Every response that cited a given authority domain, with the real cited URLs + scores."""
    dom = await db.scalar(
        select(SourceDomain).where(SourceDomain.authority_domain == authority_domain)
    )
    stmt = (
        select(ResponseCitation, Response)
        .join(Response, Response.response_id == ResponseCitation.response_id)
        .where(ResponseCitation.authority_domain == authority_domain)
    )
    stmt = _apply_citation_filters(
        stmt, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona,
    )
    stmt = stmt.order_by(
        ResponseCitation.citation_count.desc(), ResponseCitation.first_citation_position
    )
    rows = (await db.execute(stmt)).all()

    total_citations = sum(int(rc.citation_count or 0) for rc, _ in rows)
    scores = await _latest_scores(db, {rc.response_id for rc, _ in rows})

    items = []
    for rc, resp in rows[:limit]:
        try:
            urls = json.loads(rc.citation_urls) if rc.citation_urls else []
        except (ValueError, TypeError):
            urls = []
        sr = scores.get(rc.response_id)
        items.append({
            "response_id": rc.response_id,
            "run_id": rc.run_id,
            "question_id": resp.question_id,
            "question_text": resp.question_text,
            "persona": resp.persona,
            "llm_name": resp.llm_name,
            "therapeutic_area": resp.therapeutic_area,
            "indication": resp.indication,
            "brand_focus": resp.brand_focus,
            "timestamp": resp.timestamp_utc.isoformat() if resp.timestamp_utc else None,
            "citation_count": int(rc.citation_count or 0),
            "urls": urls if isinstance(urls, list) else [],
            "sentiment_score": sr.sentiment_score if sr else None,
            "competitive_position": sr.competitive_position if sr else None,
        })

    return {
        "authority_domain": authority_domain,
        "classification": ({
            "display_category": dom.display_category,
            "control_type": dom.control_type,
            "authority_type": dom.authority_type,
            "verification": dom.verification,
            "publisher_name": dom.publisher_name,
        } if dom else None),
        "total_citations": total_citations,
        "response_count": len(rows),
        "items": items,
    }


async def sentiment_by_source(
    db: AsyncSession, *, llm_name=None, therapeutic_area=None, indication=None,
    brand=None, persona=None,
) -> dict:
    """Correlate the source an answer leaned on (its top-cited domain's control) with the brand
    sentiment/positioning that answer earned. Answers: do responses built on
    competitor-controlled sources skew toward weaker brand positioning?"""
    stmt = (
        select(
            ResponseCitation.response_id,
            ResponseCitation.citation_count,
            ResponseCitation.first_citation_position,
            ResponseCitation.authority_domain,
            SourceDomain.control_type,
        )
        .select_from(ResponseCitation)
        .join(SourceDomain, SourceDomain.domain_id == ResponseCitation.domain_id)
    )
    stmt = _apply_citation_filters(
        stmt, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona,
    )
    rows = (await db.execute(stmt)).all()

    per_resp: dict[str, list[tuple]] = {}
    for rid, cc, fpos, dom, control in rows:
        per_resp.setdefault(rid, []).append(
            (int(cc or 0), int(fpos or 0), dom or "", control or CONTROL_UNKNOWN)
        )

    # One bucket per response = control of its TOP-cited domain (freq, then earliest, then name).
    top_control: dict[str, str] = {}
    for rid, cits in per_resp.items():
        top = sorted(cits, key=lambda c: (-c[0], c[1], c[2]))[0]
        top_control[rid] = top[3]

    scores = await _latest_scores(db, set(per_resp.keys()))

    buckets: dict[str, dict] = {}
    for rid, control in top_control.items():
        sr = scores.get(rid)
        if sr is None:
            continue
        b = buckets.setdefault(control, {
            "response_count": 0, "sentiment_sum": 0.0, "sentiment_n": 0,
            "position_distribution": {},
        })
        b["response_count"] += 1
        if sr.sentiment_score is not None:
            b["sentiment_sum"] += sr.sentiment_score
            b["sentiment_n"] += 1
        if sr.competitive_position:
            b["position_distribution"][sr.competitive_position] = (
                b["position_distribution"].get(sr.competitive_position, 0) + 1
            )

    out = []
    for control, b in buckets.items():
        n = b["response_count"]
        weak = sum(b["position_distribution"].get(p, 0) for p in _WEAK_POSITIONS)
        out.append({
            "control_type": control,
            "label": _CONTROL_LABELS.get(control, control.title()),
            "response_count": n,
            "avg_sentiment": round(b["sentiment_sum"] / b["sentiment_n"], 3) if b["sentiment_n"] else None,
            "position_distribution": b["position_distribution"],
            "weak_position_pct": round(weak / n * 100, 1) if n else 0.0,
        })
    out.sort(key=lambda x: _CONTROL_ORDER.get(x["control_type"], 9))
    return {"total_scored_responses": sum(x["response_count"] for x in out), "buckets": out}


async def share_of_voice(
    db: AsyncSession, *, llm_name=None, therapeutic_area=None, indication=None,
    brand=None, persona=None, response_ids=None,
) -> dict:
    """Citation 'share of voice' — AbbVie vs Competitor vs Independent, plus the specific
    competitor domains winning AI citations. Marketer framing of the ownership axis.

    Pass ``response_ids`` to scope to an explicit cohort of answers (e.g. the ones behind one
    head-to-head comparison), which the taxonomy filters alone cannot express.
    """
    # Voice split by control_type.
    stmt = (
        select(
            SourceDomain.control_type,
            func.sum(ResponseCitation.citation_count),
            func.count(distinct(ResponseCitation.response_id)),
        )
        .select_from(ResponseCitation)
        .join(SourceDomain, SourceDomain.domain_id == ResponseCitation.domain_id)
        .group_by(SourceDomain.control_type)
    )
    stmt = _apply_citation_filters(
        stmt, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona, response_ids=response_ids,
    )
    rows = (await db.execute(stmt)).all()
    voice = [
        {
            "control_type": (control or CONTROL_UNKNOWN),
            "label": _CONTROL_LABELS.get(control or CONTROL_UNKNOWN, "Unclassified"),
            "citation_count": int(cc or 0),
            "response_count": int(rc or 0),
        }
        for control, cc, rc in rows
    ]
    total = sum(v["citation_count"] for v in voice)
    pcts = _largest_remainder_pct([v["citation_count"] for v in voice], total)
    for v, p in zip(voice, pcts):
        v["share_pct"] = p
    voice.sort(key=lambda v: _CONTROL_ORDER.get(v["control_type"], 9))

    response_stmt = select(func.count(distinct(ResponseCitation.response_id))).select_from(
        ResponseCitation
    )
    response_stmt = _apply_citation_filters(
        response_stmt, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona, response_ids=response_ids,
    )
    response_count = int(await db.scalar(response_stmt) or 0)

    # Which competitor domains specifically win citations.
    comp_stmt = (
        select(
            ResponseCitation.authority_domain,
            SourceDomain.publisher_name,
            func.sum(ResponseCitation.citation_count),
            func.count(distinct(ResponseCitation.response_id)),
        )
        .select_from(ResponseCitation)
        .join(SourceDomain, SourceDomain.domain_id == ResponseCitation.domain_id)
        .where(SourceDomain.control_type == CONTROL_COMPETITOR)
        .group_by(ResponseCitation.authority_domain, SourceDomain.publisher_name)
    )
    comp_stmt = _apply_citation_filters(
        comp_stmt, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona, response_ids=response_ids,
    )
    comp_rows = (await db.execute(comp_stmt)).all()
    competitor_total = sum(int(cc or 0) for _, _, cc, _ in comp_rows)
    competitors = [
        {
            "authority_domain": dom,
            "publisher_name": pub,
            "citation_count": int(cc or 0),
            "response_count": int(rc or 0),
            "share_pct": round(int(cc or 0) / competitor_total * 100, 1) if competitor_total else 0.0,
        }
        for dom, pub, cc, rc in comp_rows
    ]
    competitors.sort(key=lambda c: (-c["citation_count"], c["authority_domain"]))

    def _share(control: str) -> float:
        return next((v["share_pct"] for v in voice if v["control_type"] == control), 0.0)

    return {
        "total_citations": total,
        "response_count": response_count,
        "voice": voice,
        "abbvie_share_pct": _share(CONTROL_ABBVIE),
        "competitor_share_pct": _share(CONTROL_COMPETITOR),
        "independent_share_pct": _share(CONTROL_INDEPENDENT),
        "competitor_total_citations": competitor_total,
        "competitors": competitors[:12],
    }


async def top_pages(
    db: AsyncSession, *, llm_name=None, therapeutic_area=None, indication=None,
    brand=None, persona=None, control: str | None = None, limit: int = 25,
    response_ids=None,
) -> dict:
    """Most-cited individual PAGES (URLs) aggregated across responses. An optional control
    filter (e.g. COMPETITOR) surfaces the exact competitor articles AI keeps citing."""
    stmt = (
        select(
            ResponseCitation.response_id,
            ResponseCitation.authority_domain,
            ResponseCitation.citation_urls,
            SourceDomain.control_type,
            SourceDomain.display_category,
            SourceDomain.publisher_name,
        )
        .select_from(ResponseCitation)
        .join(SourceDomain, SourceDomain.domain_id == ResponseCitation.domain_id)
    )
    if control:
        stmt = stmt.where(SourceDomain.control_type == control)
    stmt = _apply_citation_filters(
        stmt, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona, response_ids=response_ids,
    )
    rows = (await db.execute(stmt)).all()

    pages: dict[str, dict] = {}
    for rid, dom, urls_json, control_type, cat, pub in rows:
        try:
            urls = json.loads(urls_json) if urls_json else []
        except (ValueError, TypeError):
            urls = []
        if not isinstance(urls, list):
            continue
        for u in urls:
            if not isinstance(u, str) or not u.strip():
                continue
            p = pages.setdefault(u, {
                "url": u,
                "authority_domain": dom,
                "control_type": control_type,
                "display_category": cat,
                "publisher_name": pub,
                "citation_count": 0,
                "_responses": set(),
            })
            p["citation_count"] += 1
            p["_responses"].add(rid)

    items = []
    for p in pages.values():
        p["response_count"] = len(p.pop("_responses"))
        items.append(p)
    items.sort(key=lambda x: (-x["response_count"], -x["citation_count"], x["url"]))
    return {"total_pages": len(items), "items": items[:limit]}


async def curation_candidates(
    db: AsyncSession, *, llm_name=None, therapeutic_area=None, indication=None,
    brand=None, persona=None, min_citations: int = 1, limit: int = 100,
) -> dict:
    """Cited domains NOT resolved by the curated taxonomy, ranked by citation frequency.

    An engineering aid for keeping config/source_authority.yaml rich: every domain here is one
    AI actually cited but the curated lists don't cover, so it's classified only by the LLM (a
    guess) or not at all. The most-cited rows are the best candidates to add to the YAML. Each
    carries the LLM's suggested authority + confidence + evidence so an engineer can decide.
    """
    cite_stmt = select(
        ResponseCitation.authority_domain,
        func.sum(ResponseCitation.citation_count).label("citations"),
        func.count(distinct(ResponseCitation.response_id)).label("responses"),
    )
    cite_stmt = _apply_citation_filters(
        cite_stmt, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona,
    ).group_by(ResponseCitation.authority_domain)
    cite_rows = (await db.execute(cite_stmt)).all()
    cite_map = {
        r.authority_domain: (int(r.citations or 0), int(r.responses or 0))
        for r in cite_rows
        if int(r.citations or 0) >= min_citations
    }
    if not cite_map:
        return {"total": 0, "items": []}

    # "Uncurated" = anything the YAML did not classify (LLM-guessed or still unclassified). A
    # confident LLM guess is still a candidate: pinning it in the YAML makes it deterministic.
    dom_rows = (await db.execute(
        select(SourceDomain).where(
            SourceDomain.authority_domain.in_(list(cite_map.keys())),
            SourceDomain.classification_status != STATUS_CURATED,
        )
    )).scalars().all()

    items = []
    for sd in dom_rows:
        citations, responses = cite_map.get(sd.authority_domain, (0, 0))
        try:
            evidence = json.loads(sd.classification_evidence) if sd.classification_evidence else []
        except (TypeError, ValueError):
            evidence = []
        items.append({
            "authority_domain": sd.authority_domain,
            "suggested_authority": sd.authority_type,
            "display_category": sd.display_category,
            "confidence": sd.classification_confidence,
            "publisher_name": sd.publisher_name,
            "registrant_organization": sd.registrant_organization,
            "requires_review": bool(sd.requires_review),
            "classification_status": sd.classification_status,
            "classification_source": sd.classification_source,
            "evidence": evidence,
            "citation_count": citations,
            "response_count": responses,
        })
    items.sort(key=lambda x: (-x["citation_count"], -x["response_count"], x["authority_domain"]))
    return {"total": len(items), "items": items[:limit]}


_TRUSTED_CATS = {"ABBVIE_CONTROLLED", "REGULATORY", "GUIDELINE", "PEER_REVIEWED", "MEDICAL_REFERENCE"}


def _trust_bucket(cat: str | None) -> str:
    if cat in _TRUSTED_CATS:
        return "TRUSTED"
    if cat == "HEALTH_MEDIA":
        return "NEUTRAL"
    return "RISK"  # COMPETITOR_CONTROLLED / SOCIAL_UGC / OTHER / unclassified


async def response_provenance(db: AsyncSession, response_id: str) -> dict:
    """Per-claim source trust for one response: map each grounded claim to the trust of the
    sources backing it (trusted / neutral / risk / unsourced). Uses Response.grounding_supports
    (claim -> source indices) + Response.sources, classified via the cached SourceDomain table."""
    resp = await db.get(Response, response_id)
    if resp is None:
        return {"response_id": response_id, "found": False, "claims_total": 0, "summary": {}, "claims": []}
    try:
        sources = json.loads(resp.sources) if resp.sources else []
    except (ValueError, TypeError):
        sources = []
    try:
        supports = json.loads(resp.grounding_supports) if resp.grounding_supports else []
    except (ValueError, TypeError):
        supports = []

    # Resolve each source index -> authority_domain.
    src_meta: list[dict] = []
    needed: set[str] = set()
    for s in sources:
        adom = None
        if isinstance(s, dict):
            parts = _parts_for_source(s)
            if parts:
                adom = taxonomy.authority_domain_for(parts.normalized_host, parts.registrable_domain)
        src_meta.append({
            "url": (s.get("redirect_url") or s.get("url")) if isinstance(s, dict) else None,
            "authority_domain": adom,
        })
        if adom:
            needed.add(adom)

    cls: dict[str, dict] = {}
    if needed:
        rows = (await db.execute(
            select(SourceDomain).where(SourceDomain.authority_domain.in_(list(needed)))
        )).scalars().all()
        for d in rows:
            cls[d.authority_domain] = {"control_type": d.control_type, "display_category": d.display_category}

    claims = []
    summary = {"TRUSTED": 0, "NEUTRAL": 0, "RISK": 0, "UNSOURCED": 0}
    for sup in supports:
        if not isinstance(sup, dict):
            continue
        claim_sources, buckets = [], []
        for i in sup.get("source_indices") or []:
            if not isinstance(i, int) or i < 0 or i >= len(src_meta):
                continue
            meta = src_meta[i]
            c = cls.get(meta["authority_domain"] or "", {})
            cat = c.get("display_category")
            claim_sources.append({
                "authority_domain": meta["authority_domain"],
                "url": meta["url"],
                "display_category": cat,
                "control_type": c.get("control_type"),
            })
            buckets.append(_trust_bucket(cat))
        if not buckets:
            bucket = "UNSOURCED"
        elif "RISK" in buckets:
            bucket = "RISK"
        elif "NEUTRAL" in buckets:
            bucket = "NEUTRAL"
        else:
            bucket = "TRUSTED"
        summary[bucket] += 1
        claims.append({"text": sup.get("text") or "", "bucket": bucket, "sources": claim_sources})

    return {
        "response_id": response_id,
        "found": True,
        "question_text": resp.question_text,
        "llm_name": resp.llm_name,
        "claims_total": len(claims),
        "summary": summary,
        "claims": claims,
    }


# ---------------------------------------------------------------------------
# Preferred sources (FR-706a.7) — CRUD + read-only observations
# ---------------------------------------------------------------------------
def _serialize_pref(p: PreferredSource) -> dict:
    return {
        "pref_id": p.pref_id,
        "therapeutic_area": p.therapeutic_area,
        "authority_domain": p.authority_domain,
        "registrable_domain": p.registrable_domain,
        "note": p.note,
        "active": p.active,
        "created_by": p.created_by,
        "updated_by": p.updated_by,
        "change_reason": p.change_reason,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


async def list_preferred(
    db: AsyncSession, *, therapeutic_area: str | None = None, include_inactive: bool = False
) -> list[dict]:
    stmt = select(PreferredSource)
    if not include_inactive:
        stmt = stmt.where(PreferredSource.active.is_(True))
    if therapeutic_area:
        stmt = stmt.where(PreferredSource.therapeutic_area == therapeutic_area)
    stmt = stmt.order_by(PreferredSource.therapeutic_area, PreferredSource.authority_domain)
    return [_serialize_pref(p) for p in (await db.execute(stmt)).scalars().all()]


async def add_preferred(
    db: AsyncSession, *, therapeutic_area: str, domain: str, note: str | None = None,
    created_by: str = "Medical Affairs", change_reason: str | None = None,
) -> dict:
    """Designate (or reactivate) a preferred domain for a TA. Normalises the input to a root domain."""
    parts = domains.parse_url(domain if "://" in domain else "http://" + domain.strip())
    if not parts:
        raise ValueError(f"Could not parse a domain from {domain!r}")
    authority_domain = taxonomy.authority_domain_for(parts.normalized_host, parts.registrable_domain)

    existing = await db.scalar(
        select(PreferredSource).where(
            PreferredSource.therapeutic_area == therapeutic_area,
            PreferredSource.authority_domain == authority_domain,
        )
    )
    now = _utcnow()
    if existing:
        existing.active = True
        existing.effective_from = now
        existing.effective_to = None
        existing.note = note if note is not None else existing.note
        existing.registrable_domain = parts.registrable_domain
        existing.updated_by = created_by
        existing.change_reason = change_reason
        pref = existing
    else:
        pref = PreferredSource(
            pref_id=str(uuid.uuid4()),
            therapeutic_area=therapeutic_area,
            authority_domain=authority_domain,
            registrable_domain=parts.registrable_domain,
            note=note,
            created_by=created_by,
            change_reason=change_reason,
        )
        db.add(pref)
    await db.commit()
    await db.refresh(pref)
    return _serialize_pref(pref)


async def delete_preferred(
    db: AsyncSession, pref_id: str, *, updated_by: str = "Medical Affairs",
    change_reason: str | None = None,
) -> bool:
    """Soft-delete (deactivate) a preferred source, preserving the audit trail."""
    pref = await db.get(PreferredSource, pref_id)
    if pref is None or not pref.active:
        return False
    pref.active = False
    pref.effective_to = _utcnow()
    pref.updated_by = updated_by
    pref.change_reason = change_reason
    await db.commit()
    return True


async def preferred_observations(
    db: AsyncSession, *, therapeutic_area: str | None = None, llm_name: str | None = None,
) -> dict:
    """Read stored presence/absence observations per preferred source (READ-ONLY)."""
    prefs_stmt = select(PreferredSource).where(PreferredSource.active.is_(True))
    if therapeutic_area:
        prefs_stmt = prefs_stmt.where(PreferredSource.therapeutic_area == therapeutic_area)
    prefs = list((await db.execute(prefs_stmt)).scalars().all())

    items = []
    for pref in prefs:
        obs_stmt = select(
            func.count(),
            func.sum(func.cast(PreferredSourceObservation.was_present, Integer)),
        ).where(PreferredSourceObservation.preferred_source_id == pref.pref_id)
        if llm_name:
            obs_stmt = obs_stmt.where(PreferredSourceObservation.llm_name == llm_name)
        total, present = (await db.execute(obs_stmt)).one()
        total = int(total or 0)
        present = int(present or 0)
        absent = total - present
        items.append({
            "pref_id": pref.pref_id,
            "therapeutic_area": pref.therapeutic_area,
            "authority_domain": pref.authority_domain,
            "note": pref.note,
            "observations": total,
            "present": present,
            "absent": absent,
            "presence_pct": round(present / total * 100, 1) if total else None,
        })
    items.sort(key=lambda i: (i["therapeutic_area"], i["authority_domain"]))
    return {"count": len(items), "items": items}


# ---------------------------------------------------------------------------
# Source-to-Claim Influence Graph (corpus-wide provenance aggregation)
# ---------------------------------------------------------------------------
def _norm_claim(text: str) -> str:
    """Normalise claim text for dedup across responses (case/whitespace-insensitive)."""
    return " ".join((text or "").lower().split())[:400]


_CITATION_ONLY_RE = re.compile(
    r"^\s*\(?\s*\[[^\]]+\]\(\s*https?://[^)]+\)\s*\)?[.,;:]?\s*$",
    re.IGNORECASE,
)
_CITATION_LINK_RE = re.compile(r"\[[^\]]+\]\(\s*https?://[^)]+\)", re.IGNORECASE)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def _clean_claim_display(text: str) -> str:
    value = _MARKDOWN_LINK_RE.sub(r"\1", text or "")
    value = re.sub(r"(?m)^\s{0,3}(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+)", "", value)
    value = re.sub(r"[*_`~]+", "", value)
    value = re.sub(r"^[)\]}>.,;:\s]+", "", value)
    return re.sub(r"\s+", " ", value).strip(" \t\r\n-–—")


def _clip_claim_display(text: str, limit: int = 180) -> str:
    value = _clean_claim_display(text)
    if len(value) <= limit:
        return value
    clipped = value[: limit + 1]
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return clipped.rstrip(" ,;:-") + "…"


def _claim_display_label(response_text: str | None, support: dict) -> str:
    support_text = str(support.get("text") or "")
    if not _CITATION_ONLY_RE.match(support_text):
        return _clip_claim_display(support_text)
    start = support.get("start_index")
    if response_text and isinstance(start, int) and 0 < start <= len(response_text):
        prefix = response_text[:start].rstrip()
        line = next((part.strip() for part in reversed(re.split(r"[\r\n]+", prefix)) if part.strip()), "")
        prior_citations = list(_CITATION_LINK_RE.finditer(line))
        if prior_citations:
            line = line[prior_citations[-1].end():].strip()
        candidate = _clip_claim_display(line)
        if len(candidate) >= 12 and not _CITATION_ONLY_RE.match(line):
            return candidate
    match = re.search(r"\[([^\]]+)\]\(", support_text)
    domain = _clean_claim_display(match.group(1)) if match else ""
    return f"Supporting citation from {domain}" if domain else "Supporting citation"


def _pick_claim_display(candidates: dict[str, int]) -> str:
    if not candidates:
        return ""
    return min(candidates, key=lambda value: (-candidates[value], value.casefold()))


async def _response_theme_labels(
    db: AsyncSession, response_ids: set[str]
) -> dict[str, list[str]]:
    """Map response_id -> [theme label] at the CURRENT (max) taxonomy version.

    Themes are assigned per response (not per claim), so a response's themes apply to all of
    its grounded claims — the documented claim->theme approximation the graph relies on.
    """
    if not response_ids:
        return {}
    version = await db.scalar(select(func.max(Theme.taxonomy_version)))
    if version is None:
        return {}
    rows = (await db.execute(
        select(ResponseTheme.response_id, Theme.label)
        .join(
            Theme,
            (Theme.theme_id == ResponseTheme.theme_id)
            & (Theme.taxonomy_version == ResponseTheme.taxonomy_version),
        )
        .where(
            ResponseTheme.response_id.in_(list(response_ids)),
            ResponseTheme.taxonomy_version == version,
        )
    )).all()
    out: dict[str, list[str]] = {}
    for rid, label in rows:
        if label:
            out.setdefault(rid, []).append(label)
    return out


async def influence_graph(
    db: AsyncSession, *, llm_name=None, therapeutic_area=None, indication=None,
    brand=None, persona=None, theme=None, focus_domain=None, top_n: int = 60,
) -> dict:
    """Corpus-wide Source -> Claim -> Theme -> Position influence graph.

    A whole-corpus aggregation of the per-response grounding provenance used by
    ``response_provenance``: for every GROUNDED response (one carrying ``sources`` +
    ``grounding_supports``), each claim is linked to the sources backing it, to the response's
    themes, and to its latest competitive position. Emits a node/link graph for the
    force-directed web plus ``theme_drivers`` (the specific source domains driving each
    narrative) and a coverage denominator — parametric answers carry no sources, so they are
    invisible here and ``coverage_pct`` keeps that honest.
    """
    stmt = select(
        Response.response_id, Response.sources, Response.grounding_supports, Response.response_text,
    ).where(
        Response.status.in_(_OK_STATUSES),
        Response.grounding_supports.isnot(None),
        Response.sources.isnot(None),
    )
    stmt = _apply_response_filters(
        stmt, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona,
    )
    rows = (await db.execute(stmt)).all()

    # Denominator: all responses under the same filters, without the grounding requirement.
    total_stmt = select(func.count(distinct(Response.response_id))).where(
        Response.status.in_(_OK_STATUSES)
    )
    total_stmt = _apply_response_filters(
        total_stmt, llm_name=llm_name, therapeutic_area=therapeutic_area,
        indication=indication, brand=brand, persona=persona,
    )
    total_responses = int(await db.scalar(total_stmt) or 0)

    # Resolve each response's source indices -> authority_domain.
    parsed: list[tuple[str, list[dict], list, str | None]] = []
    needed_domains: set[str] = set()
    all_ids: set[str] = set()
    for rid, sources_json, supports_json, response_text in rows:
        try:
            sources = json.loads(sources_json) if sources_json else []
        except (ValueError, TypeError):
            sources = []
        try:
            supports = json.loads(supports_json) if supports_json else []
        except (ValueError, TypeError):
            supports = []
        if not isinstance(sources, list) or not isinstance(supports, list) or not supports:
            continue
        src_meta: list[dict] = []
        for s in sources:
            adom = url = None
            if isinstance(s, dict):
                parts = _parts_for_source(s)
                if parts:
                    adom = taxonomy.authority_domain_for(parts.normalized_host, parts.registrable_domain)
                url = s.get("redirect_url") or s.get("url")
            src_meta.append({"authority_domain": adom, "url": url})
            if adom:
                needed_domains.add(adom)
        parsed.append((rid, src_meta, supports, response_text))
        all_ids.add(rid)

    cls: dict[str, dict] = {}
    if needed_domains:
        drows = (await db.execute(
            select(SourceDomain).where(SourceDomain.authority_domain.in_(list(needed_domains)))
        )).scalars().all()
        for d in drows:
            cls[d.authority_domain] = {
                "control_type": d.control_type,
                "authority_type": d.authority_type,
                "display_category": d.display_category,
                "publisher_name": d.publisher_name,
            }
    resp_themes = await _response_theme_labels(db, all_ids)
    scores = await _latest_scores(db, all_ids)

    sources_acc: dict[str, dict] = {}
    claims_acc: dict[str, dict] = {}
    themes_acc: dict[str, set] = {}
    positions_acc: dict[str, set] = {}
    links_acc: dict[tuple[str, str], set] = {}
    theme_source: dict[str, dict[str, set]] = {}
    contributing: set[str] = set()

    def _link(a: str, b: str, rid: str) -> None:
        links_acc.setdefault((a, b), set()).add(rid)

    for rid, src_meta, supports, response_text in parsed:
        labels = resp_themes.get(rid, [])
        if theme and theme not in labels:
            continue
        sr = scores.get(rid)
        position = sr.competitive_position if sr else None
        resp_domains: set[str] = set()
        used = False
        for sup in supports:
            if not isinstance(sup, dict):
                continue
            text = sup.get("text") or ""
            norm = _norm_claim(text)
            if not norm:
                continue
            backing: set[str] = set()
            backing_urls: dict[str, str] = {}
            for i in sup.get("source_indices") or []:
                if not isinstance(i, int) or i < 0 or i >= len(src_meta):
                    continue
                adom = src_meta[i]["authority_domain"]
                if not adom:
                    continue
                backing.add(adom)
                if src_meta[i]["url"] and adom not in backing_urls:
                    backing_urls[adom] = src_meta[i]["url"]
            if not backing or (focus_domain and focus_domain not in backing):
                continue
            claim_id = f"claim:{norm}"
            c = claims_acc.setdefault(
                norm,
                {"label": text[:90], "text": text, "responses": set(), "display_labels": {}},
            )
            c["responses"].add(rid)
            display_label = _claim_display_label(response_text, sup)
            if display_label:
                labels_acc = c["display_labels"]
                labels_acc[display_label] = labels_acc.get(display_label, 0) + 1
            for adom in backing:
                meta = cls.get(adom, {})
                s = sources_acc.setdefault(adom, {"responses": set()})
                s["responses"].add(rid)
                s["control_type"] = meta.get("control_type") or CONTROL_UNKNOWN
                s["authority_type"] = meta.get("authority_type")
                s["display_category"] = meta.get("display_category")
                s["publisher_name"] = meta.get("publisher_name")
                if adom in backing_urls:
                    s.setdefault("url", backing_urls[adom])
                _link(f"src:{adom}", claim_id, rid)
                resp_domains.add(adom)
            for label in labels:
                _link(claim_id, f"theme:{label}", rid)
            used = True
        if not used:
            continue
        contributing.add(rid)
        for label in labels:
            themes_acc.setdefault(label, set()).add(rid)
            td = theme_source.setdefault(label, {})
            for adom in resp_domains:
                td.setdefault(adom, set()).add(rid)
            if position:
                positions_acc.setdefault(position, set()).add(rid)
                _link(f"theme:{label}", f"pos:{position}", rid)

    # Rank + cap for legibility: top_n sources, then claims connected to kept sources.
    kept_sources = sorted(
        sources_acc.items(), key=lambda kv: -len(kv[1].get("responses", ()))
    )[:top_n]
    kept_source_ids = {f"src:{adom}" for adom, _ in kept_sources}
    truncated = len(sources_acc) > len(kept_sources)

    kept_claim_ids: set[str] = set()
    for (a, b) in links_acc:
        if a in kept_source_ids and b.startswith("claim:"):
            kept_claim_ids.add(b)
    if len(kept_claim_ids) > top_n:
        ranked = sorted(
            kept_claim_ids,
            key=lambda cid: -len(claims_acc.get(cid[len("claim:"):], {}).get("responses", ())),
        )
        kept_claim_ids = set(ranked[:top_n])
        truncated = True

    kept_theme_ids: set[str] = set()
    kept_pos_ids: set[str] = set()
    for (a, b) in links_acc:
        if a in kept_claim_ids and b.startswith("theme:"):
            kept_theme_ids.add(b)
    for (a, b) in links_acc:
        if a in kept_theme_ids and b.startswith("pos:"):
            kept_pos_ids.add(b)
    kept_ids = kept_source_ids | kept_claim_ids | kept_theme_ids | kept_pos_ids

    nodes: list[dict] = []
    for adom, s in kept_sources:
        nodes.append({
            "id": f"src:{adom}",
            "type": "source",
            "label": s.get("publisher_name") or adom,
            "authority_domain": adom,
            "control_type": s.get("control_type") or CONTROL_UNKNOWN,
            "authority_type": s.get("authority_type"),
            "display_category": s.get("display_category"),
            "url": s.get("url"),
            "weight": len(s.get("responses", ())),
        })
    for cid in kept_claim_ids:
        norm = cid[len("claim:"):]
        c = claims_acc.get(norm, {})
        nodes.append({
            "id": cid, "type": "claim",
            "label": c.get("label") or "", "text": c.get("text") or "",
            "display_label": _pick_claim_display(c.get("display_labels", {})),
            "weight": len(c.get("responses", ())),
        })
    for tid in kept_theme_ids:
        label = tid[len("theme:"):]
        nodes.append({
            "id": tid, "type": "theme", "label": label,
            "weight": len(themes_acc.get(label, ())),
        })
    for pid in kept_pos_ids:
        pos = pid[len("pos:"):]
        nodes.append({
            "id": pid, "type": "position", "label": pos,
            "weight": len(positions_acc.get(pos, ())),
        })

    links = [
        {"source": a, "target": b, "value": len(rids)}
        for (a, b), rids in links_acc.items()
        if a in kept_ids and b in kept_ids
    ]
    links.sort(key=lambda link: -link["value"])

    # Theme drivers — the specific source domains driving each narrative (the punchline).
    theme_drivers: list[dict] = []
    for label, dommap in theme_source.items():
        total = len(themes_acc.get(label, ()))
        if not total:
            continue
        tops = sorted(dommap.items(), key=lambda kv: -len(kv[1]))[:5]
        drivers = []
        for adom, rids in tops:
            meta = cls.get(adom, {})
            drivers.append({
                "authority_domain": adom,
                "publisher_name": meta.get("publisher_name"),
                "control_type": meta.get("control_type") or CONTROL_UNKNOWN,
                "responses": len(rids),
                "share_pct": round(len(rids) / total * 100, 1),
            })
        theme_drivers.append({"theme": label, "theme_responses": total, "top_sources": drivers})
    theme_drivers.sort(key=lambda t: -t["theme_responses"])

    grounded = len(contributing)
    return {
        "nodes": nodes,
        "links": links,
        "meta": {
            "grounded_responses": grounded,
            "total_responses": total_responses,
            "coverage_pct": round(grounded / total_responses * 100, 1) if total_responses else 0.0,
            "node_count": len(nodes),
            "link_count": len(links),
            "truncated": truncated,
            "theme_drivers": theme_drivers[:12],
            "filters": {
                "llm_name": llm_name, "therapeutic_area": therapeutic_area,
                "indication": indication, "brand": brand, "persona": persona,
                "theme": theme, "focus_domain": focus_domain, "top_n": top_n,
            },
            "generated_at": _utcnow().isoformat(),
        },
    }


async def node_evidence(
    db: AsyncSession, *, node_type: str, key: str,
    llm_name=None, therapeutic_area=None, indication=None, brand=None, persona=None,
    limit: int = 50,
) -> dict:
    """Top real answers behind a narrative (theme label) or a brand-position node.

    Mirrors ``domain_detail``'s item shape so the Influence Graph side panel can render
    narrative/position evidence with the same rollup + list UI. Narratives resolve via
    ``ResponseTheme`` at the current (max) taxonomy version; positions via each response's
    latest ``ScoringRecord.competitive_position``. Uses the same response filters as the graph.
    """
    ntype = (node_type or "").strip().lower()
    resp_rows: list[Response] = []
    relevance_by_id: dict[str, float] = {}
    scores: dict[str, ScoringRecord] = {}

    if ntype in ("theme", "narrative"):
        version = await db.scalar(select(func.max(Theme.taxonomy_version)))
        if version is not None:
            stmt = (
                select(Response, ResponseTheme.relevance)
                .join(ResponseTheme, ResponseTheme.response_id == Response.response_id)
                .join(
                    Theme,
                    (Theme.theme_id == ResponseTheme.theme_id)
                    & (Theme.taxonomy_version == ResponseTheme.taxonomy_version),
                )
                .where(
                    Response.status.in_(_OK_STATUSES),
                    ResponseTheme.taxonomy_version == version,
                    Theme.label == key,
                )
            )
            stmt = _apply_response_filters(
                stmt, llm_name=llm_name, therapeutic_area=therapeutic_area,
                indication=indication, brand=brand, persona=persona,
            )
            for resp, rel in (await db.execute(stmt)).all():
                resp_rows.append(resp)
                relevance_by_id[resp.response_id] = float(rel or 0.0)
        scores = await _latest_scores(db, {r.response_id for r in resp_rows})
        resp_rows.sort(
            key=lambda r: (
                -relevance_by_id.get(r.response_id, 0.0),
                -(r.timestamp_utc.timestamp() if r.timestamp_utc else 0.0),
            )
        )
    elif ntype == "position":
        stmt = select(Response).where(Response.status.in_(_OK_STATUSES))
        stmt = _apply_response_filters(
            stmt, llm_name=llm_name, therapeutic_area=therapeutic_area,
            indication=indication, brand=brand, persona=persona,
        )
        candidates = list((await db.execute(stmt)).scalars().all())
        scores = await _latest_scores(db, {r.response_id for r in candidates})
        resp_rows = [
            r for r in candidates
            if (sr := scores.get(r.response_id)) is not None and sr.competitive_position == key
        ]
        resp_rows.sort(key=lambda r: -(r.timestamp_utc.timestamp() if r.timestamp_utc else 0.0))
    else:
        return {"node_type": ntype, "key": key, "response_count": 0, "items": []}

    items = []
    for r in resp_rows[:limit]:
        sr = scores.get(r.response_id)
        items.append({
            "response_id": r.response_id,
            "run_id": None,
            "question_id": r.question_id,
            "question_text": r.question_text,
            "persona": r.persona,
            "llm_name": r.llm_name,
            "therapeutic_area": r.therapeutic_area,
            "indication": r.indication,
            "brand_focus": r.brand_focus,
            "timestamp": r.timestamp_utc.isoformat() if r.timestamp_utc else None,
            "citation_count": 0,
            "urls": [],
            "sentiment_score": sr.sentiment_score if sr else None,
            "competitive_position": sr.competitive_position if sr else None,
        })

    return {
        "node_type": ntype,
        "key": key,
        "response_count": len(resp_rows),
        "items": items,
    }
