"""Model release event correlation (FR-707a).

Admins log known vendor model releases; when material drift is detected the differ
asks this service whether a release for the same platform falls within the lookback
window and, if so, annotates the drift ("Possible model update"). Also exposes the
operational reporting ratio (correlated / total unexplained drifts)."""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.models.model_release import ModelReleaseLog
from app.models.response import Response
from app.models.response_diff import ResponseDiff
from app.utils.audit import write_audit

_SNIPPET_LEN = 240


def _snippet(text: str | None) -> str | None:
    if not text:
        return None
    text = text.strip()
    return text if len(text) <= _SNIPPET_LEN else text[:_SNIPPET_LEN].rstrip() + "…"


async def create_release(
    db: AsyncSession, *, target_platform: str, release_date: date,
    version: str | None = None, release_notes: str | None = None, url: str | None = None,
    source: str = "manual",
) -> ModelReleaseLog:
    row = ModelReleaseLog(
        target_platform=target_platform, release_date=release_date,
        version=version, release_notes=release_notes, url=url, source=source,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def list_releases(db: AsyncSession, *, target_platform: str | None = None) -> list[ModelReleaseLog]:
    stmt = select(ModelReleaseLog)
    if target_platform:
        stmt = stmt.where(func.lower(ModelReleaseLog.target_platform) == target_platform.lower())
    stmt = stmt.order_by(ModelReleaseLog.release_date.desc())
    return list((await db.execute(stmt)).scalars().all())


async def find_release_for_version(
    db: AsyncSession, *, llm_name: str, version: str | None,
) -> ModelReleaseLog | None:
    """The real update event for a specific (platform, version) — the authoritative
    version-boundary anchor. Prefers api/changelog provenance over inferred/legacy."""
    if not version:
        return None
    from app.models.model_release import SOURCE_API, SOURCE_CHANGELOG

    stmt = (
        select(ModelReleaseLog)
        .where(
            func.lower(ModelReleaseLog.target_platform) == llm_name.lower(),
            ModelReleaseLog.version == version,
        )
        # api (ground truth) first, then changelog, then anything else; newest wins on ties.
        .order_by(
            case(
                (ModelReleaseLog.source == SOURCE_API, 0),
                (ModelReleaseLog.source == SOURCE_CHANGELOG, 1),
                else_=2,
            ),
            ModelReleaseLog.release_date.desc(),
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def find_correlated_release(
    db: AsyncSession, *, llm_name: str, observed_on: date, lookback_days: int | None = None,
    version: str | None = None,
) -> ModelReleaseLog | None:
    """Best release explaining a material drift for `llm_name`.

    Version-anchored first: if `version` is supplied and we have a real update event for
    that exact (platform, version), that boundary IS the cause — return it. Otherwise fall
    back to the most recent release on/before `observed_on` within the lookback window."""
    if version:
        anchored = await find_release_for_version(db, llm_name=llm_name, version=version)
        if anchored is not None:
            return anchored
    days = lookback_days if lookback_days is not None else get_settings().model_release_lookback_days
    window_start = observed_on - timedelta(days=days)
    stmt = (
        select(ModelReleaseLog)
        .where(
            func.lower(ModelReleaseLog.target_platform) == llm_name.lower(),
            ModelReleaseLog.release_date <= observed_on,
            ModelReleaseLog.release_date >= window_start,
        )
        .order_by(ModelReleaseLog.release_date.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def drift_timeline(db: AsyncSession, *, target_platform: str | None = None) -> dict:
    """FR-707a.5: material-drift counts per day plus release markers, for the timeline
    overlay. Drifts and releases share the same date X-axis."""
    diff_stmt = select(
        func.date(ResponseDiff.created_at).label("day"),
        func.count().label("total"),
        func.sum(
            case((ResponseDiff.correlated_release_id.is_not(None), 1), else_=0)
        ).label("correlated"),
    ).where(ResponseDiff.material_change.is_(True))
    if target_platform:
        diff_stmt = diff_stmt.where(func.lower(ResponseDiff.llm_name) == target_platform.lower())
    diff_stmt = diff_stmt.group_by("day").order_by("day")

    drifts = [
        {"date": r.day, "material_drifts": int(r.total or 0), "correlated_drifts": int(r.correlated or 0)}
        for r in (await db.execute(diff_stmt)).all()
    ]

    releases = [
        {
            "id": r.id, "date": r.release_date.isoformat(),
            "target_platform": r.target_platform, "version": r.version, "url": r.url,
        }
        for r in await list_releases(db, target_platform=target_platform)
    ]
    return {"drifts": drifts, "releases": releases}


async def correlation_ratio(db: AsyncSession) -> dict:
    """FR-707a.7: correlated material drifts / total material (unexplained) drifts."""
    total = (await db.execute(
        select(func.count()).select_from(ResponseDiff).where(ResponseDiff.material_change.is_(True))
    )).scalar_one()
    correlated = (await db.execute(
        select(func.count()).select_from(ResponseDiff).where(
            ResponseDiff.material_change.is_(True),
            ResponseDiff.correlated_release_id.is_not(None),
        )
    )).scalar_one()
    ratio = round(correlated / total, 4) if total else 0.0
    return {
        "material_drifts": int(total),
        "correlated_drifts": int(correlated),
        "unexplained_drifts": int(total - correlated),
        "correlation_ratio": ratio,
    }


# --- Auto-detection of model updates from drift spikes (FR-707a, no manual logging) ----
async def _recorrelate_unlinked(db: AsyncSession, *, lookback_days: int | None = None) -> int:
    """Link material drifts that don't yet reference a release. Version-anchored first: if
    the two responses used different model versions, link to that version's event; else fall
    back to the most recent release within the lookback window. Only fills empty links."""
    diffs = (await db.execute(
        select(ResponseDiff).where(
            ResponseDiff.material_change.is_(True),
            ResponseDiff.correlated_release_id.is_(None),
        )
    )).scalars().all()
    if not diffs:
        return 0

    # Batch-load the versions of the responses referenced by these diffs.
    resp_ids: set[str] = set()
    for d in diffs:
        if d.current_response_id:
            resp_ids.add(d.current_response_id)
        if d.previous_response_id:
            resp_ids.add(d.previous_response_id)
    version_map: dict[str, str | None] = {}
    if resp_ids:
        rows = (await db.execute(
            select(Response.response_id, Response.llm_model_version).where(
                Response.response_id.in_(resp_ids)
            )
        )).all()
        version_map = {r.response_id: r.llm_model_version for r in rows}

    linked = 0
    for d in diffs:
        observed = d.created_at.date() if d.created_at else None
        if observed is None:
            continue
        cur_v = version_map.get(d.current_response_id)
        prev_v = version_map.get(d.previous_response_id) if d.previous_response_id else None
        transition_version = cur_v if (cur_v and cur_v != prev_v) else None
        rel = await find_correlated_release(
            db, llm_name=d.llm_name, observed_on=observed, lookback_days=lookback_days,
            version=transition_version,
        )
        if rel is not None:
            d.correlated_release_id = rel.id
            linked += 1
    if linked:
        await db.commit()
    return linked


async def detect_model_updates(
    db: AsyncSession, *, min_drifts: int | None = None, lookback_days: int | None = None,
) -> dict:
    """Auto-log a "Detected model update" whenever a single platform shows a spike of
    material response drifts on one day (>= threshold), then (re)correlate drifts to
    releases. Idempotent: an auto event per (platform, day) is created at most once.

    This replaces manual model-update logging — it runs automatically after scoring."""
    settings = get_settings()
    threshold = min_drifts if min_drifts is not None else settings.model_update_min_drifts

    spikes = (await db.execute(
        select(
            ResponseDiff.llm_name.label("platform"),
            func.date(ResponseDiff.created_at).label("day"),
            func.count().label("cnt"),
        )
        .where(ResponseDiff.material_change.is_(True))
        .group_by("platform", "day")
    )).all()

    created = 0
    for row in spikes:
        if (row.cnt or 0) < threshold or not row.platform or not row.day:
            continue
        day = row.day if isinstance(row.day, date) else date.fromisoformat(str(row.day))
        exists = (await db.execute(
            select(ModelReleaseLog.id).where(
                func.lower(ModelReleaseLog.target_platform) == row.platform.lower(),
                ModelReleaseLog.release_date == day,
                ModelReleaseLog.source == "auto",
            )
        )).first()
        if exists:
            continue
        db.add(ModelReleaseLog(
            target_platform=row.platform,
            release_date=day,
            version=None,
            release_notes=(
                f"Auto-detected from a spike of {int(row.cnt)} material response "
                f"changes on {day.isoformat()}."
            ),
            url=None,
            source="auto",
        ))
        created += 1
    if created:
        await db.commit()

    linked = await _recorrelate_unlinked(db, lookback_days=lookback_days)
    # Emit the high-impact alert signal for any update that just crossed the threshold.
    flagged = await flag_high_impact_updates(db)
    return {
        "events_created": created,
        "diffs_linked": linked,
        "high_impact_flagged": flagged.get("flagged", 0),
    }


async def version_impact(db: AsyncSession, *, target_platform: str | None = None) -> list[dict]:
    """Per-update PRODUCT IMPACT (FR-707a): for each release/version event, how many of our
    tracked answers materially changed across it, plus the net brand-sentiment shift and the
    count of competitive-position changes. Answers "how did version X→Y affect our answers?"."""
    from app.models.scoring import ScoringRecord

    releases = await list_releases(db, target_platform=target_platform)
    if not releases:
        return []
    rel_map = {r.id: r for r in releases}

    diffs = (await db.execute(
        select(ResponseDiff).where(
            ResponseDiff.material_change.is_(True),
            ResponseDiff.correlated_release_id.in_(list(rel_map.keys())),
        )
    )).scalars().all()

    # Scores for the before/after responses referenced by these diffs.
    resp_ids: set[str] = set()
    for d in diffs:
        if d.current_response_id:
            resp_ids.add(d.current_response_id)
        if d.previous_response_id:
            resp_ids.add(d.previous_response_id)
    score_map: dict[str, ScoringRecord] = {}
    if resp_ids:
        score_map = {
            s.response_id: s for s in
            (await db.execute(
                select(ScoringRecord).where(ScoringRecord.response_id.in_(resp_ids))
            )).scalars().all()
        }

    grouped: dict[int, list[ResponseDiff]] = {}
    for d in diffs:
        grouped.setdefault(d.correlated_release_id, []).append(d)

    out: list[dict] = []
    for rel in releases:
        rel_diffs = grouped.get(rel.id, [])
        questions = {d.question_id for d in rel_diffs if d.question_id}
        before_vals: list[float] = []
        after_vals: list[float] = []
        position_changes = 0
        for d in rel_diffs:
            cur = score_map.get(d.current_response_id)
            prev = score_map.get(d.previous_response_id) if d.previous_response_id else None
            if cur and cur.sentiment_score is not None:
                after_vals.append(cur.sentiment_score)
            if prev and prev.sentiment_score is not None:
                before_vals.append(prev.sentiment_score)
            if (cur and prev and cur.competitive_position and prev.competitive_position
                    and cur.competitive_position != prev.competitive_position):
                position_changes += 1
        sent_before = round(sum(before_vals) / len(before_vals), 4) if before_vals else None
        sent_after = round(sum(after_vals) / len(after_vals), 4) if after_vals else None
        sent_delta = (round(sent_after - sent_before, 4)
                      if sent_before is not None and sent_after is not None else None)
        out.append({
            "release_id": rel.id,
            "target_platform": rel.target_platform,
            "version": rel.version,
            "release_date": rel.release_date,
            "effective_date": rel.effective_date,
            "source": rel.source,
            "event_type": rel.event_type,
            "summary": rel.summary,
            "confidence": rel.confidence,
            "url": rel.url,
            "questions_changed": len(questions),
            "drift_count": len(rel_diffs),
            "sentiment_before": sent_before,
            "sentiment_after": sent_after,
            "sentiment_delta": sent_delta,
            "position_changes": position_changes,
            "is_high_impact": _is_high_impact(len(questions), sent_delta),
        })
    # Most impactful first (most answers changed), then most recent.
    out.sort(key=lambda x: (x["questions_changed"], x["release_date"] or date.min), reverse=True)
    return out


def _is_high_impact(questions_changed: int, sentiment_delta: float | None) -> bool:
    """A model update is 'high impact' when it flips many tracked answers OR drops mean
    brand sentiment materially across the version boundary (FR-707a alert/digest hook)."""
    settings = get_settings()
    if questions_changed >= settings.model_update_high_impact_min_questions:
        return True
    if sentiment_delta is not None and sentiment_delta <= -abs(settings.model_update_high_impact_sentiment_drop):
        return True
    return False


async def high_impact_updates(
    db: AsyncSession, *, target_platform: str | None = None, since: date | None = None,
) -> list[dict]:
    """High-impact model updates (optionally on/after `since` by effective/release date),
    most impactful first. Backs the alerts feed + the digest section."""
    items = [i for i in await version_impact(db, target_platform=target_platform) if i["is_high_impact"]]
    if since is not None:
        items = [i for i in items if (i["effective_date"] or i["release_date"]) >= since]
    return items


async def flag_high_impact_updates(db: AsyncSession) -> dict:
    """Emit the high-impact alert signal for any update that has newly crossed the impact
    threshold. Idempotent via ModelReleaseLog.alerted_at (set once). Writes an immutable
    audit record per newly-flagged event so it is picked up by the stakeholder digest and
    any audit-driven alerting. Best-effort; returns a small summary."""
    settings = get_settings()
    if not settings.model_update_high_impact_alert_enabled:
        return {"enabled": False, "flagged": 0}

    impacts = {i["release_id"]: i for i in await version_impact(db) if i["is_high_impact"]}
    if not impacts:
        return {"enabled": True, "flagged": 0}

    rows = (await db.execute(
        select(ModelReleaseLog).where(
            ModelReleaseLog.id.in_(list(impacts.keys())),
            ModelReleaseLog.alerted_at.is_(None),
        )
    )).scalars().all()

    now = datetime.now(timezone.utc)
    flagged: list[dict] = []
    for rel in rows:
        item = impacts[rel.id]
        rel.alerted_at = now
        await write_audit(
            db, role="SYSTEM", event="MODEL_UPDATE_HIGH_IMPACT",
            context={
                "release_id": rel.id,
                "target_platform": item["target_platform"],
                "version": item["version"],
                "source": item["source"],
                "event_type": item["event_type"],
                "questions_changed": item["questions_changed"],
                "sentiment_delta": item["sentiment_delta"],
                "position_changes": item["position_changes"],
                "summary": item["summary"],
            },
            commit=False,
        )
        flagged.append(item)
    if flagged:
        await db.commit()
    return {"enabled": True, "flagged": len(flagged), "events": flagged}


async def list_drifts(
    db: AsyncSession, *, target_platform: str | None = None, limit: int = 100,
) -> list[dict]:
    """Material response drifts for the AI Update Impact list, each with the question,
    platform, correlated update (if any), and short before/after snippets."""
    stmt = select(ResponseDiff).where(ResponseDiff.material_change.is_(True))
    if target_platform:
        stmt = stmt.where(func.lower(ResponseDiff.llm_name) == target_platform.lower())
    stmt = stmt.order_by(ResponseDiff.created_at.desc()).limit(limit)
    diffs = (await db.execute(stmt)).scalars().all()

    resp_ids: set[str] = set()
    rel_ids: set[int] = set()
    for d in diffs:
        if d.current_response_id:
            resp_ids.add(d.current_response_id)
        if d.previous_response_id:
            resp_ids.add(d.previous_response_id)
        if d.correlated_release_id:
            rel_ids.add(d.correlated_release_id)

    resp_map: dict[str, Response] = {}
    if resp_ids:
        resp_map = {
            r.response_id: r for r in
            (await db.execute(select(Response).where(Response.response_id.in_(resp_ids)))).scalars().all()
        }
    rel_map: dict[int, ModelReleaseLog] = {}
    if rel_ids:
        rel_map = {
            r.id: r for r in
            (await db.execute(select(ModelReleaseLog).where(ModelReleaseLog.id.in_(rel_ids)))).scalars().all()
        }

    items: list[dict] = []
    for d in diffs:
        cur = resp_map.get(d.current_response_id)
        prev = resp_map.get(d.previous_response_id) if d.previous_response_id else None
        rel = rel_map.get(d.correlated_release_id) if d.correlated_release_id else None
        items.append({
            "id": d.id,
            "question_id": d.question_id,
            "question_text": (cur.question_text if cur else (prev.question_text if prev else None)),
            "llm_name": d.llm_name,
            "observed_date": d.created_at.date() if d.created_at else None,
            "similarity_ratio": d.similarity_ratio,
            "correlated_release_id": d.correlated_release_id,
            "correlated_release_platform": rel.target_platform if rel else None,
            "correlated_release_date": rel.release_date if rel else None,
            "previous_snippet": _snippet(prev.response_text) if prev else None,
            "current_snippet": _snippet(cur.response_text) if cur else None,
        })
    return items


async def get_drift_detail(db: AsyncSession, diff_id: int) -> dict | None:
    """Full before/after for one drift — the 'View responses' drawer content."""
    d = await db.get(ResponseDiff, diff_id)
    if d is None:
        return None
    cur = await db.get(Response, d.current_response_id) if d.current_response_id else None
    prev = await db.get(Response, d.previous_response_id) if d.previous_response_id else None
    rel = await db.get(ModelReleaseLog, d.correlated_release_id) if d.correlated_release_id else None
    return {
        "id": d.id,
        "question_id": d.question_id,
        "question_text": (cur.question_text if cur else (prev.question_text if prev else None)),
        "llm_name": d.llm_name,
        "observed_date": d.created_at.date() if d.created_at else None,
        "similarity_ratio": d.similarity_ratio,
        "material_change": bool(d.material_change),
        "diff_text": d.diff_text,
        "previous_response_id": d.previous_response_id,
        "previous_response_text": prev.response_text if prev else None,
        "current_response_id": d.current_response_id,
        "current_response_text": cur.response_text if cur else None,
        "correlated_release_id": d.correlated_release_id,
        "correlated_release_platform": rel.target_platform if rel else None,
        "correlated_release_date": rel.release_date if rel else None,
        "correlated_release_notes": rel.release_notes if rel else None,
    }
