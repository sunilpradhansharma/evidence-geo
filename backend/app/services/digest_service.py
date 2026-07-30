"""Stakeholder-differentiated digest generation & delivery (BR-008a).

Per role-profile: select the alerts that match the profile's rules within the
lookback window, rank and take the top 3-5 findings, generate a 2-4 sentence
plain-English executive summary (Bedrock), render HTML + (best-effort) PDF, deliver
via AWS SES email (opt-in) and/or webhook, always store the digest in-app, and write
an immutable audit record mapping the recipient role to the digest reference.
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.labels import HIDDEN_LLM_NAMES, platform_label
from app.config.settings import PROJECT_ROOT, get_settings
from app.models.alert import Alert
from app.models.digest import DigestProfile, DigestRule, DigestRun
from app.models.response import Response
from app.models.scoring import ScoringRecord
from app.models.workshop_summary import WorkshopPlatformSummary
from app.models.source_domain import (
    CONTROL_ABBVIE,
    CONTROL_COMPETITOR,
    CONTROL_INDEPENDENT,
    CONTROL_UNKNOWN,
    SourceDomain,
)
from app.schemas import DigestProfileCreate, DigestProfileUpdate
from app.utils.audit import write_audit
from app.utils.logging import get_logger

logger = get_logger("digest_service")

# Worst-first ordering so the top findings surface the highest-risk items.
_RULE_SEVERITY = {"NOT_RECOMMENDED": 0, "COMPETITOR_ADVANTAGE": 1, "LOW_SENTIMENT": 2}
MAX_FINDINGS = 5
MIN_FINDINGS = 3

# Workshop Questions insights (the curated Rhem.csv set). Positioning buckets for the
# "how does AI position our brands" rollup, and a worst-first ordering so the most
# consequential claims/answers surface first in the digest.
_OK_STATUSES = ("SUCCESS", "TRUNCATED")
_FAVORABLE_POSITIONS = ("FIRST_LINE_RECOMMENDED", "AMONG_OPTIONS")
_WEAK_POSITIONS = ("SECOND_LINE", "NOT_RECOMMENDED")
_POSITION_RANK = {
    "NOT_RECOMMENDED": 0, "SECOND_LINE": 1, "NOT_MENTIONED": 2,
    "AMONG_OPTIONS": 3, "FIRST_LINE_RECOMMENDED": 4,
}
MAX_MODEL_SOURCES = 6    # per-platform cited domains shown
_SUMMARY_MAXLEN = 240    # truncate each answer synopsis so the digest stays scannable
_NEG_SENTIMENT = -0.25   # answer flagged "needs attention" at or below this sentiment
MAX_NEEDS_ATTENTION = 5  # worst answers surfaced for escalation
MAX_DESIGNATIONS = 20    # audience-by-indication rows shown (caps the all-questions scope)

# Insights scope: the curated Rhem.csv "workshop" set vs. every tracked question.
SCOPE_WORKSHOP = "workshop"
SCOPE_ALL = "all"
_SCOPES = (SCOPE_WORKSHOP, SCOPE_ALL)


def _dump(value) -> str | None:
    if not value:
        return None
    return value if isinstance(value, str) else json.dumps(value)


def _load(value) -> list:
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else [parsed]
    except (ValueError, TypeError):
        return [value]


# ------------------------------------------------------------------ CRUD --------
async def create_profile(db: AsyncSession, data: DigestProfileCreate) -> DigestProfile:
    profile = DigestProfile(
        role=data.role,
        description=data.description,
        enabled=data.enabled,
        cron=data.cron,
        timezone=data.timezone,
        recipients=_dump(data.recipients),
        delivery_methods=_dump(data.delivery_methods),
        webhook_url=data.webhook_url,
    )
    for r in data.rules:
        profile.rules.append(_rule_from_schema(r))
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    return profile


def _rule_from_schema(r) -> DigestRule:
    return DigestRule(
        alert_categories=_dump(r.alert_categories),
        domains=_dump(r.domains),
        therapeutic_areas=_dump(r.therapeutic_areas),
        personas=_dump(r.personas),
        llm_names=_dump(r.llm_names),
    )


async def list_profiles(db: AsyncSession) -> list[DigestProfile]:
    rows = await db.execute(select(DigestProfile).order_by(DigestProfile.role))
    return list(rows.scalars().all())


async def get_profile(db: AsyncSession, profile_id: int) -> DigestProfile | None:
    return await db.get(DigestProfile, profile_id)


async def update_profile(db: AsyncSession, profile_id: int, data: DigestProfileUpdate) -> DigestProfile | None:
    profile = await db.get(DigestProfile, profile_id)
    if profile is None:
        return None
    payload = data.model_dump(exclude_unset=True)
    for field in ("role", "description", "enabled", "cron", "timezone", "webhook_url"):
        if field in payload:
            setattr(profile, field, payload[field])
    if "recipients" in payload:
        profile.recipients = _dump(payload["recipients"])
    if "delivery_methods" in payload:
        profile.delivery_methods = _dump(payload["delivery_methods"])
    if data.rules is not None:
        profile.rules.clear()
        for r in data.rules:
            profile.rules.append(_rule_from_schema(r))
    await db.commit()
    await db.refresh(profile)
    return profile


async def delete_profile(db: AsyncSession, profile_id: int) -> bool:
    profile = await db.get(DigestProfile, profile_id)
    if profile is None:
        return False
    # Explicitly clear generated digests first. Existing SQLite tables were created before
    # the digest_runs FK had ON DELETE CASCADE, and PRAGMA foreign_keys=ON would otherwise
    # block deleting a profile that has history. This Core DELETE is DB-agnostic and avoids
    # async lazy-loading of the (unloaded) runs relationship at flush time. The `rules`
    # relationship is eager (selectin) + delete-orphan, so the ORM handles those on delete.
    from sqlalchemy import delete as sa_delete

    await db.execute(sa_delete(DigestRun).where(DigestRun.profile_id == profile_id))
    await db.delete(profile)
    await db.commit()
    return True


async def list_runs(db: AsyncSession, *, profile_id: int | None = None, limit: int = 50) -> list[DigestRun]:
    stmt = select(DigestRun).order_by(DigestRun.generated_at.desc()).limit(limit)
    if profile_id is not None:
        stmt = stmt.where(DigestRun.profile_id == profile_id)
    return list((await db.execute(stmt)).scalars().all())


async def get_run(db: AsyncSession, run_id: int) -> DigestRun | None:
    return await db.get(DigestRun, run_id)


# --------------------------------------------------- alert selection & ranking --
def _rule_matches(rule: DigestRule, *, category: str, domain, ta, persona, llm) -> bool:
    """A rule matches when every one of its NON-EMPTY filters matches (AND)."""
    def ok(raw, value) -> bool:
        allowed = _load(raw)
        if not allowed:
            return True
        return value in allowed

    return (
        ok(rule.alert_categories, category)
        and ok(rule.domains, domain)
        and ok(rule.therapeutic_areas, ta)
        and ok(rule.personas, persona)
        and ok(rule.llm_names, llm)
    )


async def _select_findings(db: AsyncSession, profile: DigestProfile, since: datetime) -> list[dict]:
    """Alerts since `since` that match the profile (ANY rule; no rules = all)."""
    stmt = (
        select(Alert, Response, ScoringRecord)
        .join(Response, Alert.response_id == Response.response_id)
        .join(ScoringRecord, Alert.score_id == ScoringRecord.score_id, isouter=True)
        .where(Alert.created_at >= since)
        .order_by(Alert.created_at.desc())
    )
    rows = (await db.execute(stmt)).all()
    rules = list(profile.rules)

    findings: list[dict] = []
    for alert, resp, score in rows:
        category = alert.rule_triggered
        domain = resp.domain
        ta = resp.therapeutic_area
        persona = resp.persona
        llm = resp.llm_name
        if rules and not any(
            _rule_matches(r, category=category, domain=domain, ta=ta, persona=persona, llm=llm)
            for r in rules
        ):
            continue
        findings.append({
            "rule": category,
            "detail": alert.detail,
            "llm": llm,
            "persona": persona,
            "domain": domain,
            "therapeutic_area": ta,
            "brand_focus": resp.brand_focus,
            "question_text": resp.question_text,
            "sentiment_score": score.sentiment_score if score else None,
            "competitive_position": score.competitive_position if score else None,
            "created_at": alert.created_at.isoformat() if alert.created_at else None,
        })

    findings.sort(key=lambda f: (
        _RULE_SEVERITY.get(f["rule"], 9),
        f["sentiment_score"] if f["sentiment_score"] is not None else 0.0,
    ))
    return findings[:MAX_FINDINGS]


async def _source_authority_summary(db: AsyncSession) -> dict | None:
    """Current AI citation 'source authority' standing for the digest (brand-wide snapshot).

    Best-effort: any failure returns None so the digest still generates."""
    try:
        from app.source_authority import service as sa_svc

        sov = await sa_svc.share_of_voice(db)
        if not sov.get("total_citations"):
            return None
        pages = await sa_svc.top_pages(db, control="COMPETITOR", limit=3)
        return {
            "total_citations": sov["total_citations"],
            "abbvie_share_pct": sov["abbvie_share_pct"],
            "competitor_share_pct": sov["competitor_share_pct"],
            "independent_share_pct": sov.get("independent_share_pct", 0.0),
            "top_competitors": sov.get("competitors", [])[:3],
            "top_competitor_pages": pages.get("items", [])[:3],
        }
    except Exception as e:  # noqa: BLE001 — SA summary is best-effort; never block the digest
        logger.warning("Digest source-authority summary skipped: %s", e)
        return None


async def _latest_scores_by_response(
    db: AsyncSession, response_ids: list[str]
) -> dict[str, ScoringRecord]:
    """Latest ScoringRecord per response (highest score_version, then newest)."""
    if not response_ids:
        return {}
    rows = (await db.execute(
        select(ScoringRecord)
        .where(ScoringRecord.response_id.in_(response_ids))
        .order_by(
            ScoringRecord.response_id,
            ScoringRecord.score_version.desc(),
            ScoringRecord.created_at.desc(),
        )
    )).scalars().all()
    out: dict[str, ScoringRecord] = {}
    for r in rows:
        out.setdefault(r.response_id, r)  # first seen for a response is its latest score
    return out


async def _latest_workshop_responses(
    db: AsyncSession,
) -> tuple[dict[str, str], list[Response]]:
    """(designation_map, latest-answer-per-(question, platform)) for the curated workshop set.

    The latest answer per (question, platform) is the 'current standing' (drops historical
    re-runs). Returns ({}, []) when the workshop set is not present in this environment."""
    from app.services import question_service

    designation_map = await question_service.analyst_designation_map(db)
    if not designation_map:
        return {}, []
    workshop_ids = list(designation_map.keys())
    rows = (await db.execute(
        select(Response)
        .where(Response.question_id.in_(workshop_ids), Response.status.in_(_OK_STATUSES))
        .order_by(Response.timestamp_utc.desc())
    )).scalars().all()
    latest: dict[tuple[str, str], Response] = {}
    for r in rows:
        if HIDDEN_LLM_NAMES and r.llm_name in HIDDEN_LLM_NAMES:
            continue
        latest.setdefault((r.question_id, r.llm_name), r)
    return designation_map, list(latest.values())


def _general_designation(r: Response) -> str:
    """Audience-by-area label for the all-questions scope (which has no Rhem.csv mapping):
    Persona + therapeutic area, e.g. "Patient \u00b7 Immunology". Falls back to whatever is set."""
    parts = [str(p).strip() for p in (r.persona, r.therapeutic_area) if p and str(p).strip()]
    return " \u00b7 ".join(parts) if parts else "Unspecified"


async def _latest_all_responses(
    db: AsyncSession,
) -> tuple[dict[str, str], list[Response]]:
    """(designation_map, latest-answer-per-(question, platform)) across EVERY tracked question.

    Same 'current standing' dedupe as the workshop scope, but unscoped, with the Persona+area
    designation derived from the answers themselves. Returns ({}, []) when there are none."""
    rows = (await db.execute(
        select(Response)
        .where(Response.status.in_(_OK_STATUSES))
        .order_by(Response.timestamp_utc.desc())
    )).scalars().all()
    latest: dict[tuple[str, str], Response] = {}
    designation_map: dict[str, str] = {}
    for r in rows:
        if HIDDEN_LLM_NAMES and r.llm_name in HIDDEN_LLM_NAMES:
            continue
        latest.setdefault((r.question_id, r.llm_name), r)
        designation_map.setdefault(r.question_id, _general_designation(r))
    return designation_map, list(latest.values())


async def _latest_scored_universe(
    db: AsyncSession, scope: str
) -> tuple[dict[str, str], list[Response]]:
    """Dispatch to the workshop (curated) or all-questions response universe for the scope."""
    if scope == SCOPE_ALL:
        return await _latest_all_responses(db)
    return await _latest_workshop_responses(db)


def _platform_answer_rows(
    responses: list[Response], scores: dict, designation_map: dict[str, str]
) -> dict[str, list[dict]]:
    """Per-platform (raw llm_name) list of grounded answer synopses, worst-position first."""
    def _rank(r: Response) -> tuple[int, float]:
        sr = scores.get(r.response_id)
        pos = sr.competitive_position if sr else None
        sent = sr.sentiment_score if sr and sr.sentiment_score is not None else 0.0
        return (_POSITION_RANK.get(pos, 5), sent)

    rows: dict[str, list[dict]] = {}
    for r in sorted(responses, key=_rank):
        sr = scores.get(r.response_id)
        rows.setdefault(r.llm_name, []).append({
            "question": r.question_text,
            "designation": designation_map.get(r.question_id),
            "brand_focus": r.brand_focus,
            "competitive_position": sr.competitive_position if sr else None,
            "sentiment_score": (sr.sentiment_score if sr and sr.sentiment_score is not None else None),
            "summary": _answer_summary(r, sr),
        })
    return rows


def _platform_signature(answer_rows: list[dict]) -> str:
    """Stable hash of a platform's answers (question + position + synopsis) so the cached
    LLM narrative is only regenerated when what the platform said actually changes."""
    key = [
        (a.get("question"), a.get("competitive_position"), a.get("summary"))
        for a in answer_rows
    ]
    blob = json.dumps(key, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


async def gather_workshop_platform_context(
    db: AsyncSession, scope: str = SCOPE_WORKSHOP
) -> dict[str, dict] | None:
    """Per-platform answer context for the LLM summary generator (workshop_narrative).

    ``{raw_llm_name: {"label": friendly, "answers": [...], "signature": str}}`` or None when
    the requested scope has no answers in this environment."""
    designation_map, responses = await _latest_scored_universe(db, scope)
    if not responses:
        return None
    scores = await _latest_scores_by_response(db, [r.response_id for r in responses])
    rows = _platform_answer_rows(responses, scores, designation_map)
    return {
        llm: {
            "label": platform_label(llm),
            "answers": answers,
            "signature": _platform_signature(answers),
        }
        for llm, answers in rows.items()
    }


def _truncate(text: str, n: int = _SUMMARY_MAXLEN) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "\u2026"


def _answer_summary(r: Response, sr: ScoringRecord | None) -> str | None:
    """A short, grounded synopsis of what a model actually said for one workshop question.
    Prefers the scorer's rationale (a neutral summary of the answer), then the leading key
    claim, so we never fabricate text the model did not produce."""
    if sr is not None:
        if sr.scoring_rationale and sr.scoring_rationale.strip():
            return _truncate(sr.scoring_rationale)
        if sr.key_claims:
            try:
                claims = json.loads(sr.key_claims) or []
            except (ValueError, TypeError):
                claims = []
            for c in claims:
                if str(c).strip():
                    return _truncate(str(c))
    return None


async def _workshop_source_intelligence(
    db: AsyncSession, responses: list[Response]
) -> tuple[dict | None, dict[str, dict]]:
    """Overall + per-platform source 'share of voice' built from each answer's RAW provenance
    (``Response.sources``), so it does NOT depend on the async Source-Authority classification
    pass having run. Ownership (AbbVie / Competitor / Independent) is tagged from the cached
    ``SourceDomain`` table when available, else UNKNOWN. Parametric answers (no retrieval) add
    no citations but are counted so the UI can say the platform 'answered from model knowledge'.

    Returns ``(overall_or_None, {raw_llm_name: per_platform_sources})``."""
    from app.source_authority.service import _group_sources

    per_llm: dict[str, dict] = {}
    all_domains: set[str] = set()
    for r in responses:
        try:
            raw = json.loads(r.sources) if r.sources else []
        except (ValueError, TypeError):
            raw = []
        groups = _group_sources(raw) if isinstance(raw, list) else {}
        acc = per_llm.setdefault(r.llm_name, {"domains": {}, "sourced": 0, "knowledge": 0})
        if not groups:
            acc["knowledge"] += 1
            continue
        acc["sourced"] += 1
        for ad, g in groups.items():
            all_domains.add(ad)
            urls = [u for u in (g.get("urls") or []) if isinstance(u, str) and u.strip()]
            d = acc["domains"].setdefault(ad, {"urls": []})
            d["urls"].extend(urls)

    classification: dict[str, tuple[str, str | None]] = {}
    if all_domains:
        cls_rows = (await db.execute(
            select(
                SourceDomain.authority_domain,
                SourceDomain.control_type,
                SourceDomain.publisher_name,
            ).where(SourceDomain.authority_domain.in_(list(all_domains)))
        )).all()
        for ad, control, pub in cls_rows:
            classification[ad] = (control or CONTROL_UNKNOWN, pub)

    overall_control: dict[str, int] = {}
    overall_total = 0
    competitors: dict[str, dict] = {}
    comp_pages: dict[str, dict] = {}
    by_llm: dict[str, dict] = {}
    for llm, acc in per_llm.items():
        control_counts: dict[str, int] = {}
        domains_out: list[dict] = []
        for ad, d in acc["domains"].items():
            urls = d["urls"]
            count = len(urls)
            control, pub = classification.get(ad, (CONTROL_UNKNOWN, None))
            control_counts[control] = control_counts.get(control, 0) + count
            domains_out.append({
                "authority_domain": ad, "publisher_name": pub, "control_type": control,
                "citation_count": count, "url": urls[0] if urls else None,
            })
            overall_control[control] = overall_control.get(control, 0) + count
            if control == CONTROL_COMPETITOR:
                c = competitors.setdefault(
                    ad, {"authority_domain": ad, "publisher_name": pub, "citation_count": 0})
                c["citation_count"] += count
                for u in urls:
                    p = comp_pages.setdefault(
                        u, {"url": u, "publisher_name": pub, "citation_count": 0})
                    p["citation_count"] += 1
        total = sum(control_counts.values())
        overall_total += total
        domains_out.sort(key=lambda x: (-x["citation_count"], x["authority_domain"]))
        by_llm[llm] = {
            "total_citations": total,
            "abbvie": control_counts.get(CONTROL_ABBVIE, 0),
            "competitor": control_counts.get(CONTROL_COMPETITOR, 0),
            "independent": control_counts.get(CONTROL_INDEPENDENT, 0),
            "domains": domains_out[:MAX_MODEL_SOURCES],
            "sourced_responses": acc["sourced"],
            "knowledge_responses": acc["knowledge"],
        }

    overall = None
    if overall_total > 0:
        def _share(c: str) -> float:
            return round(overall_control.get(c, 0) / overall_total * 100, 1)
        overall = {
            "total_citations": overall_total,
            "abbvie_share_pct": _share(CONTROL_ABBVIE),
            "competitor_share_pct": _share(CONTROL_COMPETITOR),
            "independent_share_pct": _share(CONTROL_INDEPENDENT),
            "top_competitors": sorted(
                competitors.values(), key=lambda d: (-d["citation_count"], d["authority_domain"])
            )[:3],
            "top_competitor_pages": sorted(
                comp_pages.values(), key=lambda p: -p["citation_count"]
            )[:3],
        }
    return overall, by_llm


async def _workshop_insights_summary(
    db: AsyncSession, scope: str = SCOPE_WORKSHOP
) -> dict | None:
    """Current standing of AI answers for the given ``scope`` (the curated Rhem.csv "workshop"
    set, or "all" tracked questions), for the digest + in-app panel.

    Latest answer per (question, AI platform), summarised for a marketing/business audience:
    how AI positions our brands (by Persona+area designation and per platform), a cached LLM
    'general summary' of what each platform is saying, the sources each platform cited (from raw
    provenance, tagged AbbVie/Competitor/Independent), the citation share of voice, and the
    answers that most need attention. Best-effort: any failure, or no answers for the scope in
    this environment, returns None so the digest still generates."""
    try:
        designation_map, responses = await _latest_scored_universe(db, scope)
        if not responses:
            return None

        response_ids = [r.response_id for r in responses]
        scores = await _latest_scores_by_response(db, response_ids)

        positioning: dict[str, int] = {}
        sentiment_sum, sentiment_n, scored_n = 0.0, 0, 0
        latest_at: datetime | None = None
        by_designation: dict[str, dict] = {}
        needs: list[dict] = []
        for r in responses:
            if r.timestamp_utc and (latest_at is None or r.timestamp_utc > latest_at):
                latest_at = r.timestamp_utc
            desig = designation_map.get(r.question_id) or "Unspecified"
            bucket = by_designation.setdefault(desig, {
                "designation": desig, "responses": 0,
                "sent_sum": 0.0, "sent_n": 0, "favorable": 0, "weak": 0,
            })
            bucket["responses"] += 1
            sr = scores.get(r.response_id)
            if sr is None:
                continue
            scored_n += 1
            pos = sr.competitive_position
            sent = sr.sentiment_score
            if pos:
                positioning[pos] = positioning.get(pos, 0) + 1
                if pos in _FAVORABLE_POSITIONS:
                    bucket["favorable"] += 1
                elif pos in _WEAK_POSITIONS:
                    bucket["weak"] += 1
            if sent is not None:
                sentiment_sum += sent
                sentiment_n += 1
                bucket["sent_sum"] += sent
                bucket["sent_n"] += 1
            # Needs-attention: weak positioning OR clearly negative sentiment (worst-first).
            if (pos in _WEAK_POSITIONS) or (sent is not None and sent <= _NEG_SENTIMENT):
                needs.append({
                    "platform": platform_label(r.llm_name),
                    "designation": designation_map.get(r.question_id),
                    "question": r.question_text,
                    "competitive_position": pos,
                    "sentiment_score": sent,
                    "summary": _answer_summary(r, sr),
                    "_rank": (_POSITION_RANK.get(pos, 5), sent if sent is not None else 0.0),
                })

        designations = [{
            "designation": b["designation"],
            "responses": b["responses"],
            "avg_sentiment": round(b["sent_sum"] / b["sent_n"], 2) if b["sent_n"] else None,
            "favorable": b["favorable"],
            "weak": b["weak"],
        } for b in by_designation.values()]
        designations.sort(key=lambda d: (-d["weak"], -d["responses"], d["designation"]))
        designations = designations[:MAX_DESIGNATIONS]

        needs.sort(key=lambda n: n["_rank"])
        needs_attention = [
            {k: v for k, v in n.items() if k != "_rank"} for n in needs[:MAX_NEEDS_ATTENTION]
        ]

        # Sources from RAW provenance (independent of the async classification pass).
        overall_citations, sources_by_llm = await _workshop_source_intelligence(db, responses)

        # Cached per-platform 'general summary' (LLM, refreshed in the background) + staleness.
        cached = {
            row.llm_name: row
            for row in (await db.execute(
                select(WorkshopPlatformSummary).where(WorkshopPlatformSummary.scope == scope)
            )).scalars().all()
        }
        answer_rows = _platform_answer_rows(responses, scores, designation_map)

        by_model: list[dict] = []
        needs_summary_refresh = False
        for raw_llm, rows in answer_rows.items():
            sig = _platform_signature(rows)
            row = cached.get(raw_llm)
            summary = (row.summary or None) if row else None
            summary_fresh = bool(row and row.input_signature == sig and (row.summary or "").strip())
            if not summary_fresh:
                needs_summary_refresh = True
            fav = sum(1 for a in rows if a["competitive_position"] in _FAVORABLE_POSITIONS)
            weak = sum(1 for a in rows if a["competitive_position"] in _WEAK_POSITIONS)
            sents = [a["sentiment_score"] for a in rows if a["sentiment_score"] is not None]
            src = sources_by_llm.get(raw_llm) or {}
            has_sources = int(src.get("total_citations", 0)) > 0
            by_model.append({
                "llm": platform_label(raw_llm),
                "responses": len(rows),
                "avg_sentiment": round(sum(sents) / len(sents), 2) if sents else None,
                "favorable": fav,
                "weak": weak,
                "summary": summary,
                "summary_stale": summary is not None and not summary_fresh,
                "sources": {
                    "total_citations": src["total_citations"],
                    "abbvie": src["abbvie"],
                    "competitor": src["competitor"],
                    "independent": src["independent"],
                    "domains": src["domains"],
                } if has_sources else None,
                "answered_from_knowledge": (not has_sources)
                and int(src.get("knowledge_responses", 0)) > 0,
            })
        by_model.sort(key=lambda x: (-x["weak"], -x["responses"], x["llm"]))

        positioned = sum(positioning.values())
        favorable = sum(positioning.get(p, 0) for p in _FAVORABLE_POSITIONS)
        weak_total = sum(positioning.get(p, 0) for p in _WEAK_POSITIONS)
        return {
            "scope": scope,
            "questions_covered": len({r.question_id for r in responses}),
            "responses": len(responses),
            "scored": scored_n,
            "models": sorted({platform_label(r.llm_name) for r in responses}),
            "latest_at": latest_at.strftime("%Y-%m-%d") if latest_at else None,
            "avg_sentiment": round(sentiment_sum / sentiment_n, 2) if sentiment_n else None,
            "favorable_pct": round(favorable / positioned * 100, 1) if positioned else 0.0,
            "weak_pct": round(weak_total / positioned * 100, 1) if positioned else 0.0,
            "positioning": positioning,
            "by_designation": designations,
            "by_model": by_model,
            "citations": overall_citations,
            "needs_attention": needs_attention,
            "needs_attention_count": len(needs),
            "abbvie_cited": bool(overall_citations and overall_citations["abbvie_share_pct"] > 0),
            "needs_summary_refresh": needs_summary_refresh,
        }
    except Exception as e:  # noqa: BLE001 - workshop section is best-effort; never block the digest
        logger.warning("Digest workshop-insights section skipped: %s", e)
        return None


async def workshop_insights(db: AsyncSession, scope: str = SCOPE_WORKSHOP) -> dict | None:
    """Public accessor for the AI-answer insights snapshot for a ``scope``.

    ``scope`` is "workshop" (the curated Rhem.csv set the digest renders) or "all" (every
    tracked question). Returns the same 'current standing' payload (positioning by designation,
    a per-platform summary + each platform's sources, and citation share of voice) so the in-app
    Stakeholder Digests panel can show it live. None when the scope has no answers here.
    """
    scope = scope if scope in _SCOPES else SCOPE_WORKSHOP
    return await _workshop_insights_summary(db, scope)


async def _model_update_impact(db: AsyncSession, since: datetime) -> list[dict]:
    """High-impact vendor model updates within the digest window (FR-707a hook).

    Surfaces updates that materially moved our tracked answers so stakeholders learn a
    shift was caused by a vendor model change, not organic drift. Best-effort: any failure
    returns [] so the digest still generates."""
    try:
        from app.services import model_release_service as mrs

        items = await mrs.high_impact_updates(db, since=since.date())
        return items[:MAX_FINDINGS]
    except Exception as e:  # noqa: BLE001 — never block the digest
        logger.warning("Digest model-update section skipped: %s", e)
        return []


# ------------------------------------------------------ executive summary (LLM) -
async def _exec_summary(role: str, findings: list[dict]) -> str:
    """2-4 sentence plain-English summary (BR-008a.6). Falls back to a deterministic
    summary when the LLM is unavailable so the digest still generates offline."""
    if not findings:
        return f"No priority findings for the {role} team in this period."

    fallback = (
        f"This {role} digest surfaces {len(findings)} priority finding"
        f"{'s' if len(findings) != 1 else ''}. "
        f"The most significant is a {findings[0]['rule'].replace('_', ' ').lower()} signal"
        + (f" for {findings[0]['brand_focus']}" if findings[0].get("brand_focus") else "")
        + f" on {findings[0]['llm']}. Review the items below and route as needed."
    )
    try:
        from app.providers.base import ModelParams
        from app.providers.registry import get_orchestrator_config, get_provider_client

        cfg = get_orchestrator_config()
        client = get_provider_client(cfg.provider)
        bullet_lines = "\n".join(
            f"- [{f['rule']}] {f.get('brand_focus') or 'landscape'} on {f['llm']} "
            f"({f['domain']}/{f['persona']}): {f.get('detail') or ''}"
            for f in findings
        )
        system = (
            "You are an intelligence analyst writing for a specific enterprise function. "
            "Write a concise plain-English executive summary of EXACTLY 2 to 4 sentences. "
            "No preamble, no bullet points, no markdown — just the sentences."
        )
        user = (
            f"Audience/role: {role}\n\nTop findings:\n{bullet_lines}\n\n"
            "Write the 2-4 sentence executive summary now."
        )
        result = await client.chat(cfg.model_id, system, user, ModelParams(max_tokens=250, temperature=0.2))
        text = (result.text or "").strip()
        return text or fallback
    except Exception as e:  # noqa: BLE001 — offline / no-AWS: use deterministic fallback
        logger.warning("Digest exec-summary LLM unavailable, using fallback: %s", e)
        return fallback


# ----------------------------------------------------------------- rendering ----
def _render_html(profile: DigestProfile, findings: list[dict], summary: str,
                 period_start: datetime, period_end: datetime, sa: dict | None = None,
                 model_updates: list[dict] | None = None, workshop: dict | None = None) -> str:
    from jinja2 import Template

    template = Template(_HTML_TEMPLATE)
    return template.render(
        role=profile.role,
        summary=summary,
        findings=findings,
        sa=sa,
        workshop=workshop,
        model_updates=model_updates or [],
        period_start=period_start.strftime("%Y-%m-%d"),
        period_end=period_end.strftime("%Y-%m-%d"),
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )


def _pdf_clean(value) -> str:
    """fpdf2 core fonts are latin-1 only; replace unmappable chars so rendering never
    crashes on smart quotes / em-dashes / emoji in LLM-generated text."""
    return ("" if value is None else str(value)).encode("latin-1", "replace").decode("latin-1")


def _render_pdf(profile: DigestProfile, findings: list[dict], summary: str,
                period_start: datetime, period_end: datetime, run_ref: str) -> str | None:
    """Render the digest to PDF via fpdf2 (pure-Python, no native GTK deps — works on
    Windows + Docker). Best-effort: returns None (HTML-only) on any failure.

    NOTE: fpdf2 is a stopgap chosen because it needs no native runtime. To restore
    full HTML/CSS fidelity, swap this body back to WeasyPrint (needs the GTK/Pango
    stack) once that runtime is installed."""
    settings = get_settings()
    out_dir = Path(settings.digest_output_dir) if settings.digest_output_dir else (PROJECT_ROOT / "data" / "digests")
    try:
        from fpdf import FPDF
        from fpdf.enums import XPos, YPos

        # fpdf2's multi_cell defaults to new_x=XPos.RIGHT, which leaves the cursor at the
        # right margin so the *next* cell starts there and its text is clipped. Force every
        # cell to begin at the left margin and flow downward.
        NX, NY = XPos.LMARGIN, YPos.NEXT

        pdf = FPDF(format="A4", unit="mm")
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()
        epw = pdf.w - pdf.l_margin - pdf.r_margin

        pdf.set_font("Helvetica", "B", 16)
        pdf.set_text_color(13, 79, 79)
        pdf.multi_cell(epw, 8, _pdf_clean(f"{profile.role} - Evidence Monitoring Digest"), new_x=NX, new_y=NY)

        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(100, 116, 139)
        generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        pdf.multi_cell(epw, 5, _pdf_clean(
            f"Coverage: {period_start.strftime('%Y-%m-%d')} to {period_end.strftime('%Y-%m-%d')}"
            f"  |  Generated {generated}"), new_x=NX, new_y=NY)
        pdf.ln(3)

        pdf.set_fill_color(240, 253, 250)
        pdf.set_draw_color(153, 246, 228)
        pdf.set_text_color(30, 41, 59)
        pdf.set_font("Helvetica", "", 11)
        pdf.multi_cell(epw, 6, _pdf_clean(summary), border=1, fill=True, new_x=NX, new_y=NY)
        pdf.ln(4)

        n = len(findings)
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(15, 118, 110)
        pdf.multi_cell(epw, 7, _pdf_clean(f"Top {n} Priority Finding{'s' if n != 1 else ''}"), new_x=NX, new_y=NY)
        pdf.ln(1)

        if not findings:
            pdf.set_font("Helvetica", "", 10)
            pdf.set_text_color(100, 116, 139)
            pdf.multi_cell(epw, 6, "No priority findings for this role in the coverage window.", new_x=NX, new_y=NY)

        for f in findings:
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(185, 28, 28)
            pdf.multi_cell(epw, 5, _pdf_clean(str(f.get("rule", "")).replace("_", " ").upper()), new_x=NX, new_y=NY)

            pdf.set_font("Helvetica", "B", 11)
            pdf.set_text_color(30, 41, 59)
            pdf.multi_cell(epw, 6, _pdf_clean(f.get("question_text", "")), new_x=NX, new_y=NY)

            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(100, 116, 139)
            ctx = (f"{f.get('brand_focus') or 'Landscape'} - {f.get('llm')} - "
                   f"{f.get('domain')} / {f.get('persona')}")
            if f.get("sentiment_score") is not None:
                ctx += f" - sentiment {f['sentiment_score']:.2f}"
            if f.get("competitive_position"):
                ctx += f" - {f['competitive_position']}"
            pdf.multi_cell(epw, 5, _pdf_clean(ctx), new_x=NX, new_y=NY)
            if f.get("detail"):
                pdf.multi_cell(epw, 5, _pdf_clean(f["detail"]), new_x=NX, new_y=NY)
            pdf.ln(3)

        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"digest_{run_ref}.pdf"
        pdf.output(str(path))
        return str(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("fpdf2 PDF generation skipped (HTML-only): %s", e)
        return None


# ------------------------------------------------------------------ delivery ----
def _ses_client(settings):
    """Build a boto3 SES client from settings (explicit creds when provided)."""
    import boto3

    region = settings.ses_region or settings.aws_region
    kwargs = {"region_name": region}
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    return boto3.client("ses", **kwargs)


def _is_authz_error(e: Exception) -> bool:
    """True when an SES call failed purely due to missing IAM permission (not connectivity).

    The send-only user often can't call introspection APIs; that must be treated as
    'unknown', not as a broken email pipeline."""
    s = str(e).lower()
    return "accessdenied" in s or "not authorized" in s or "access denied" in s


def ses_status() -> dict:
    """Diagnostic snapshot of the SES setup so the UI can explain why email won't send.

    Reports whether SES is enabled/configured, whether the sender identity (address OR
    its domain) is verified, and whether the account is still in the sandbox (which
    refuses any unverified recipient). Production readiness for the "no per-recipient
    verification" goal (BR-008a / refinement 3.1) requires BOTH: (a) mode == "production"
    (SES production access granted), and (b) a verified sender — ideally a DKIM-verified
    domain so any From address on it works without individual verification.
    Never raises — returns a reason string on any error."""
    settings = get_settings()
    out: dict = {
        "enabled": bool(settings.ses_enabled),
        "sender": settings.ses_sender or None,
        "region": settings.ses_region or settings.aws_region or None,
        "sender_verified": None,          # exact From address is a verified identity
        "sender_domain_verified": None,   # the sender's domain is a verified identity (DKIM)
        "sandbox": None,
        # Single, explicit signal for the UI: "production" (no recipient verification
        # needed), "sandbox" (must verify each recipient), or "unknown" (couldn't tell).
        "mode": "unknown",
        "verified_identities": [],
        "reason": None,   # blocking: email definitively won't/can't send
        "note": None,     # informational: couldn't fully introspect, sending may still work
    }
    if not settings.ses_enabled:
        out["reason"] = "SES is turned off (SES_ENABLED=false). Digests are stored in-app only."
        return out
    if not settings.ses_sender:
        out["reason"] = "No sender address set (SES_SENDER is blank)."
        return out
    try:
        client = _ses_client(settings)
    except Exception as e:  # noqa: BLE001
        out["reason"] = f"Could not initialize SES client: {e}"
        return out

    sender = settings.ses_sender
    domain = sender.split("@", 1)[1] if "@" in sender else ""

    # Verification/quota introspection is OPTIONAL: the send-only IAM user may lack
    # ses:GetIdentityVerificationAttributes / GetSendQuota. A missing permission must NOT
    # be reported as "email broken" — only a positive "not verified"/sandbox signal blocks.
    # We check the exact address AND its domain: a DKIM-verified domain makes every From
    # address on it valid, so per-address verification is unnecessary in production.
    try:
        identities = [sender] + ([domain] if domain else [])
        attrs = client.get_identity_verification_attributes(Identities=identities)
        va = attrs.get("VerificationAttributes") or {}
        out["sender_verified"] = va.get(sender, {}).get("VerificationStatus") == "Success"
        if domain:
            out["sender_domain_verified"] = (
                va.get(domain, {}).get("VerificationStatus") == "Success"
            )
    except Exception as e:  # noqa: BLE001
        if _is_authz_error(e):
            out["note"] = (
                "Can't confirm sender/domain verification — the SES IAM user lacks "
                "ses:GetIdentityVerificationAttributes. Sending should still work if the "
                "sender or its domain is verified in the SES console."
            )
        else:
            out["reason"] = f"Could not reach SES: {e}"
            return out
    try:
        quota = client.get_send_quota()
        # Sandbox accounts have a Max24HourSend of 200 and can only email verified addrs.
        out["sandbox"] = quota.get("Max24HourSend", 0) <= 200
    except Exception:  # noqa: BLE001
        pass
    try:
        ids = client.list_verified_email_addresses()
        out["verified_identities"] = ids.get("VerifiedEmailAddresses", [])
    except Exception:  # noqa: BLE001
        pass

    # Derive the explicit mode from the sandbox signal (None => couldn't introspect).
    if out["sandbox"] is True:
        out["mode"] = "sandbox"
    elif out["sandbox"] is False:
        out["mode"] = "production"

    # The sender is usable when EITHER the exact address OR its domain is verified.
    sender_ok = out["sender_verified"] is True or out["sender_domain_verified"] is True
    # Only block on a POSITIVE "not verified" signal (both known-false / no domain path).
    positively_unverified = not sender_ok and (
        out["sender_verified"] is False or out["sender_domain_verified"] is False
    )

    if positively_unverified:
        out["reason"] = (
            f"Neither the sender {sender} nor its domain "
            f"{domain or '(none)'} is a verified SES identity. "
            "Verify the domain (recommended — DKIM) or the address in the SES console."
        )
    elif out["sandbox"]:
        out["reason"] = (
            "SES is in sandbox mode: it will only deliver to verified recipient "
            "addresses. Verify each recipient, or request production access."
        )
    return out


def _send_email(recipients: list[str], subject: str, html: str, pdf_path: str | None) -> tuple[bool, str]:
    """Send via AWS SES. No-op (returns False) when disabled or unconfigured."""
    settings = get_settings()
    if not settings.ses_enabled:
        return False, "Email is turned off (SES_ENABLED=false). Digest stored in-app."
    if not settings.ses_sender:
        return False, "No sender address configured (SES_SENDER is blank). Digest stored in-app."
    if not recipients:
        return False, "No recipient emails on this profile. Digest stored in-app."
    try:
        from email.mime.application import MIMEApplication
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        client = _ses_client(settings)

        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = settings.ses_sender
        msg["To"] = ", ".join(recipients)
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(html, "html"))
        msg.attach(alt)
        if pdf_path:
            with open(pdf_path, "rb") as fh:
                part = MIMEApplication(fh.read(), _subtype="pdf")
            part.add_header("Content-Disposition", "attachment", filename="digest.pdf")
            msg.attach(part)

        client.send_raw_email(
            Source=settings.ses_sender,
            Destinations=recipients,
            RawMessage={"Data": msg.as_string()},
        )
        return True, f"Sent via SES to {len(recipients)} recipient(s)."
    except Exception as e:  # noqa: BLE001
        # SES sandbox rejects unverified senders/recipients with a MessageRejected error —
        # translate that into a clear, actionable message instead of the raw boto trace.
        msg = str(e)
        if "not verified" in msg.lower() or "MessageRejected" in msg:
            hint = (
                "SES rejected the message — the sender or a recipient isn't a verified "
                "identity (SES sandbox). Verify the address(es) or request production access."
            )
            logger.warning("SES delivery rejected: %s", e)
            return False, hint
        logger.warning("SES delivery failed: %s", e)
        return False, f"SES error: {msg}"


async def _post_webhook(url: str, payload: dict) -> tuple[bool, str]:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload)
        return resp.status_code == 200, f"Webhook status {resp.status_code}"
    except Exception as e:  # noqa: BLE001
        logger.warning("Digest webhook failed: %s", e)
        return False, f"Webhook error: {e}"


# --------------------------------------------------------------- orchestration --
async def generate_digest(db: AsyncSession, profile: DigestProfile, *, deliver: bool = True) -> DigestRun:
    """Build, store, and (optionally) deliver one digest for a profile."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    period_start = now - timedelta(days=settings.digest_lookback_days)

    findings = await _select_findings(db, profile, period_start)
    summary = await _exec_summary(profile.role, findings)
    sa = await _source_authority_summary(db)
    # The cached per-platform 'general summary' is warmed in the background by the live panel
    # (and can be refreshed on a schedule); the digest renders whatever narratives are cached.
    workshop = await _workshop_insights_summary(db)
    model_updates = await _model_update_impact(db, period_start)
    html = _render_html(profile, findings, summary, period_start, now, sa, model_updates, workshop)

    run = DigestRun(
        profile_id=profile.id,
        role=profile.role,
        period_start=period_start,
        period_end=now,
        findings_count=len(findings),
        findings=_dump(findings),
        summary=summary,
        html=html,
    )
    db.add(run)
    await db.flush()  # assign run.id for the PDF filename / audit ref

    run.pdf_path = _render_pdf(profile, findings, summary, period_start, now, str(run.id))

    methods = _load(profile.delivery_methods) or ["in_app"]
    recipients = _load(profile.recipients)
    delivery_detail: dict = {}
    if deliver and "email" in methods:
        ok, detail = _send_email(recipients, f"[{profile.role}] EMA Intelligence Digest", html, run.pdf_path)
        run.delivered_email = ok
        delivery_detail["email"] = detail
    if deliver and "webhook" in methods and profile.webhook_url:
        payload = {"role": profile.role, "summary": summary, "findings": findings,
                   "source_authority": sa, "workshop_questions": workshop,
                   "model_updates": model_updates,
                   "period_start": period_start.isoformat(), "period_end": now.isoformat()}
        ok, detail = await _post_webhook(profile.webhook_url, payload)
        run.delivered_webhook = ok
        delivery_detail["webhook"] = detail
    run.delivery_detail = _dump(delivery_detail)

    # BR-008a.7: immutable audit record mapping recipient role -> digest reference.
    await write_audit(
        db, role="SYSTEM", event="DIGEST_DELIVERED",
        context={
            "profile_id": profile.id, "digest_role": profile.role, "digest_run_id": run.id,
            "findings": len(findings), "recipients": len(recipients),
            "methods": methods, "delivery": delivery_detail,
        },
        commit=False,
    )
    await db.commit()
    await db.refresh(run)
    logger.info("Generated digest %s for role=%s (%d findings)", run.id, profile.role, len(findings))
    return run


async def generate_for_profile_id(profile_id: int) -> DigestRun | None:
    """Entry point for the scheduler / manual trigger (own session)."""
    from app.models.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        profile = await db.get(DigestProfile, profile_id)
        if profile is None or not profile.enabled:
            return None
        return await generate_digest(db, profile, deliver=True)


_HTML_TEMPLATE = """\
<!doctype html>
<html><head><meta charset="utf-8"><style>
  body { font-family: Arial, Helvetica, sans-serif; color: #1e293b; margin: 24px; }
  h1 { color: #0D4F4F; font-size: 20px; margin-bottom: 2px; }
  .meta { color: #64748b; font-size: 12px; margin-bottom: 16px; }
  .summary { background: #f0fdfa; border: 1px solid #99f6e4; border-radius: 8px; padding: 12px 14px; font-size: 14px; }
  h2 { font-size: 15px; margin-top: 22px; color: #0f766e; }
  .finding { border-left: 4px solid #0D4F4F; padding: 8px 12px; margin: 10px 0; background: #f8fafc; }
  .rule { display: inline-block; font-size: 11px; font-weight: bold; text-transform: uppercase;
          background: #fee2e2; color: #b91c1c; border-radius: 999px; padding: 2px 8px; }
  .q { font-weight: bold; margin: 6px 0 2px; font-size: 13px; }
  .ctx { color: #64748b; font-size: 12px; }
</style></head><body>
  <h1>{{ role }} — Evidence Monitoring Digest</h1>
  <div class="meta">Coverage: {{ period_start }} to {{ period_end }} &middot; Generated {{ generated_at }}</div>
  <div class="summary">{{ summary }}</div>
  <h2>Top {{ findings|length }} Priority Finding{{ 's' if findings|length != 1 else '' }}</h2>
  {% for f in findings %}
  <div class="finding">
    <span class="rule">{{ f.rule.replace('_', ' ') }}</span>
    <div class="q">{{ f.question_text }}</div>
    <div class="ctx">
      {{ f.brand_focus or 'Landscape' }} &middot; {{ f.llm }} &middot; {{ f.domain }} / {{ f.persona }}
      {% if f.sentiment_score is not none %} &middot; sentiment {{ '%.2f'|format(f.sentiment_score) }}{% endif %}
      {% if f.competitive_position %} &middot; {{ f.competitive_position }}{% endif %}
    </div>
    {% if f.detail %}<div class="ctx">{{ f.detail }}</div>{% endif %}
  </div>
  {% else %}
  <p class="ctx">No priority findings for this role in the coverage window.</p>
  {% endfor %}
  {% if workshop %}
  <h2>Workshop Questions &mdash; what AI is telling the market</h2>
  <div class="ctx" style="margin-bottom:6px;">
    Current standing across {{ workshop.questions_covered }} curated workshop question{{ 's' if workshop.questions_covered != 1 else '' }} &middot;
    {{ workshop.responses }} answer{{ 's' if workshop.responses != 1 else '' }} from {{ workshop.models|length }} AI platform{{ 's' if workshop.models|length != 1 else '' }}{% if workshop.latest_at %} &middot; latest {{ workshop.latest_at }}{% endif %}.
    {% if workshop.avg_sentiment is not none %} Average sentiment {{ '%+.2f'|format(workshop.avg_sentiment) }}.{% endif %}
    {% if workshop.scored %} AI positions our brands favorably in {{ workshop.favorable_pct }}% and weakly in {{ workshop.weak_pct }}% of scored answers.{% endif %}
  </div>
  {% if workshop.by_designation %}
  <table style="width:100%;border-collapse:collapse;font-size:12px;margin:6px 0;">
    <tr style="text-align:left;color:#64748b;">
      <th style="padding:4px 6px;">Audience &middot; indication</th>
      <th style="padding:4px 6px;">Answers</th>
      <th style="padding:4px 6px;">Avg sentiment</th>
      <th style="padding:4px 6px;">Favorable</th>
      <th style="padding:4px 6px;">Weak</th>
    </tr>
    {% for d in workshop.by_designation %}
    <tr style="border-top:1px solid #e2e8f0;">
      <td style="padding:4px 6px;font-weight:bold;">{{ d.designation }}</td>
      <td style="padding:4px 6px;">{{ d.responses }}</td>
      <td style="padding:4px 6px;">{% if d.avg_sentiment is not none %}{{ '%+.2f'|format(d.avg_sentiment) }}{% else %}&mdash;{% endif %}</td>
      <td style="padding:4px 6px;color:#0f766e;">{{ d.favorable }}</td>
      <td style="padding:4px 6px;color:#b91c1c;">{{ d.weak }}</td>
    </tr>
    {% endfor %}
  </table>
  {% endif %}
  {% if workshop.by_model %}
  <div class="ctx" style="margin-top:8px;"><b>What each AI platform is saying</b></div>
  {% for m in workshop.by_model %}
  <div style="border:1px solid #e2e8f0;border-radius:8px;padding:8px 10px;margin:6px 0;">
    <div style="font-size:12px;"><b>{{ m.llm }}</b><span style="color:#64748b;"> &middot; {{ m.responses }} answer{{ 's' if m.responses != 1 else '' }}{% if m.avg_sentiment is not none %} &middot; avg sentiment {{ '%+.2f'|format(m.avg_sentiment) }}{% endif %}{% if m.favorable or m.weak %} &middot; {{ m.favorable }} favorable / {{ m.weak }} weak{% endif %}</span></div>
    {% if m.summary %}
    <div style="font-size:12px;color:#334155;margin-top:4px;">{{ m.summary }}</div>
    {% endif %}
    {% if m.sources %}
    <div class="ctx" style="font-size:11px;margin-top:4px;"><b>Sources {{ m.llm }} cited</b> ({{ m.sources.total_citations }}: AbbVie {{ m.sources.abbvie }} &middot; Competitor {{ m.sources.competitor }} &middot; Independent {{ m.sources.independent }}): {% for d in m.sources.domains %}{{ d.publisher_name or d.authority_domain }} ({{ d.citation_count }}){% if not loop.last %}, {% endif %}{% endfor %}</div>
    {% elif m.answered_from_knowledge %}
    <div class="ctx" style="font-size:11px;margin-top:4px;color:#64748b;">Answered from the model's own knowledge (no web sources cited).</div>
    {% endif %}
  </div>
  {% endfor %}
  {% endif %}
  {% if workshop.needs_attention %}
  <div class="ctx" style="margin-top:8px;"><b>Needs attention</b> ({{ workshop.needs_attention_count }} answer{{ 's' if workshop.needs_attention_count != 1 else '' }} positioned weakly or negative):</div>
  <ul style="margin:4px 0;padding-left:18px;font-size:12px;">
    {% for n in workshop.needs_attention %}
    <li style="margin:4px 0;"><b>{{ n.platform }}</b>{% if n.designation %} &middot; {{ n.designation }}{% endif %}{% if n.competitive_position %} &middot; {{ n.competitive_position.replace('_', ' ')|lower }}{% endif %}: {{ n.question }}{% if n.summary %}<div style="color:#475569;margin-top:2px;">{{ n.summary }}</div>{% endif %}</li>
    {% endfor %}
  </ul>
  {% endif %}
  {% if workshop.citations %}
  <div class="ctx" style="margin-top:8px;"><b>Where those answers come from</b> ({{ workshop.citations.total_citations }} source{{ 's' if workshop.citations.total_citations != 1 else '' }} cited):
    AbbVie {{ workshop.citations.abbvie_share_pct }}% &middot; Competitor {{ workshop.citations.competitor_share_pct }}% &middot; Independent {{ workshop.citations.independent_share_pct }}%.
  </div>
  {% if workshop.citations.top_competitors %}
  <div class="ctx"><b>Top competitor sources:</b>
    {% for c in workshop.citations.top_competitors %}{{ c.publisher_name or c.authority_domain }} ({{ c.citation_count }}){% if not loop.last %}, {% endif %}{% endfor %}
  </div>
  {% endif %}
  {% if workshop.citations.top_competitor_pages %}
  <div class="ctx" style="margin-top:4px;"><b>Most-cited competitor pages:</b>
    <ul style="margin:4px 0;padding-left:18px;">{% for p in workshop.citations.top_competitor_pages %}<li><a href="{{ p.url }}">{{ p.url }}</a></li>{% endfor %}</ul>
  </div>
  {% endif %}
  {% endif %}
  {% endif %}
  {% if sa %}
  <h2>AI Source Authority &mdash; current standing</h2>
  <div class="ctx" style="margin-bottom:6px;">
    Of {{ sa.total_citations }} classified AI citations: AbbVie {{ sa.abbvie_share_pct }}% &middot;
    Competitor {{ sa.competitor_share_pct }}% &middot; Independent {{ sa.independent_share_pct }}%.
  </div>
  {% if sa.top_competitors %}
  <div class="ctx"><b>Top competitor sources:</b>
    {% for c in sa.top_competitors %}{{ c.authority_domain }} ({{ c.citation_count }}){% if not loop.last %}, {% endif %}{% endfor %}
  </div>
  {% endif %}
  {% if sa.top_competitor_pages %}
  <div class="ctx" style="margin-top:4px;"><b>Most-cited competitor pages:</b>
    <ul style="margin:4px 0;padding-left:18px;">{% for p in sa.top_competitor_pages %}<li><a href="{{ p.url }}">{{ p.url }}</a> &middot; {{ p.response_count }} answer(s)</li>{% endfor %}</ul>
  </div>
  {% endif %}
  {% endif %}
  {% if model_updates %}
  <h2>High-impact AI model updates</h2>
  <div class="ctx" style="margin-bottom:6px;">
    Vendor model changes in this window that materially moved our tracked answers &mdash; likely a cause of drift, not organic change.
  </div>
  {% for u in model_updates %}
  <div class="finding" style="border-left-color:#7c3aed;">
    <span class="rule" style="background:#ede9fe;color:#6d28d9;">{{ u.target_platform }}{% if u.version %} &middot; {{ u.version }}{% endif %}</span>
    <div class="q">{{ u.summary or 'New model version observed.' }}</div>
    <div class="ctx">
      {{ u.effective_date or u.release_date }} &middot; {{ u.questions_changed }} answer(s) changed
      {% if u.sentiment_delta is not none %} &middot; sentiment {{ '%+.2f'|format(u.sentiment_delta) }}{% endif %}
      {% if u.position_changes %} &middot; {{ u.position_changes }} position change(s){% endif %}
      {% if u.url %} &middot; <a href="{{ u.url }}">vendor changelog</a>{% endif %}
    </div>
  </div>
  {% endfor %}
  {% endif %}
</body></html>
"""
