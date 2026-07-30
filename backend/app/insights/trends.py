"""Trend detection & signal extraction over theme assignments.

All analytics join ResponseTheme (for the active taxonomy version) -> Response (for time,
persona, brand, model) -> latest ScoringRecord (for sentiment). Trends are bucketed by day in
Python so the logic is database-agnostic. Signals surface the "needles": risk themes (negative
sentiment), emerging themes (rising recently), dominant themes (highest volume), and
model-skewed themes (a single model driving most of a narrative).
"""
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.taxonomy import keys_for_area
from app.models.response import Response
from app.models.scoring import ScoringRecord
from app.models.theme import ResponseTheme, Theme

_RECENT_DAYS = 7
_RISK_SENTIMENT = -0.15
_MIN_SIGNAL_COUNT = 2
_SKEW_SHARE = 0.6
_SKEW_MIN_COUNT = 3
_TIMESERIES_TOP = 8


async def current_version(db: AsyncSession) -> int:
    v = (await db.execute(select(func.max(Theme.taxonomy_version)))).scalar()
    return int(v) if v else 0


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _sentiment_label(s: float | None) -> str:
    if s is None:
        return "neutral"
    if s > 0.2:
        return "positive"
    if s < -0.2:
        return "negative"
    return "neutral"


def _trend(recent: int, prior: int) -> tuple[str, float]:
    if recent == 0 and prior == 0:
        return "flat", 0.0
    if prior == 0 and recent > 0:
        return "new", float(recent)
    growth = (recent - prior) / prior
    if growth >= 0.5:
        return "up", growth
    if growth <= -0.5:
        return "down", growth
    return "flat", growth


async def _themes_for_version(db: AsyncSession, version: int) -> dict[str, dict]:
    rows = (await db.execute(select(Theme).where(Theme.taxonomy_version == version))).scalars().all()
    out: dict[str, dict] = {}
    for t in rows:
        try:
            kws = json.loads(t.keywords) if t.keywords else []
        except Exception:  # noqa: BLE001
            kws = []
        out[t.theme_id] = {
            "theme_id": t.theme_id,
            "label": t.label,
            "description": t.description,
            "category": t.category,
            "keywords": kws,
        }
    return out


async def _assignment_rows(
    db: AsyncSession,
    version: int,
    theme_id: str | None = None,
    persona: str | None = None,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    disease: str | None = None,
    brand: str | None = None,
):
    subq = (
        select(ScoringRecord.response_id, func.max(ScoringRecord.score_version).label("maxv"))
        .group_by(ScoringRecord.response_id)
        .subquery()
    )
    stmt = (
        select(
            ResponseTheme.theme_id,
            ResponseTheme.response_id,
            ResponseTheme.relevance,
            ResponseTheme.matched_keywords,
            Response.timestamp_utc,
            Response.persona,
            Response.brand_focus,
            Response.llm_name,
            Response.therapeutic_area,
            ScoringRecord.sentiment_score,
        )
        .join(Response, Response.response_id == ResponseTheme.response_id)
        .outerjoin(subq, subq.c.response_id == ResponseTheme.response_id)
        .outerjoin(
            ScoringRecord,
            and_(
                ScoringRecord.response_id == ResponseTheme.response_id,
                ScoringRecord.score_version == subq.c.maxv,
            ),
        )
        .where(ResponseTheme.taxonomy_version == version)
    )
    if theme_id:
        stmt = stmt.where(ResponseTheme.theme_id == theme_id)
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
    if disease:
        stmt = stmt.where(Response.disease == disease)
    if brand:
        stmt = stmt.where(Response.brand_focus == brand)
    return (await db.execute(stmt)).all()


async def theme_overview(
    db: AsyncSession,
    version: int | None = None,
    persona: str | None = None,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    disease: str | None = None,
    brand: str | None = None,
) -> dict:
    if version is None:
        version = await current_version(db)
    if not version:
        return {"taxonomy_version": 0, "themes": []}

    meta = await _themes_for_version(db, version)
    rows = await _assignment_rows(
        db, version, persona=persona,
        therapeutic_area=therapeutic_area, indication=indication, disease=disease, brand=brand,
    )

    now = datetime.now(timezone.utc)
    recent_cut = now - timedelta(days=_RECENT_DAYS)
    prior_cut = now - timedelta(days=2 * _RECENT_DAYS)

    agg: dict[str, dict] = {
        tid: {
            "count": 0, "sent_sum": 0.0, "sent_n": 0,
            "by_llm": defaultdict(int), "by_persona": defaultdict(int), "by_brand": defaultdict(int),
            "sentiment": defaultdict(int), "recent": 0, "prior": 0, "first": None, "last": None,
        }
        for tid in meta
    }

    for (tid, _rid, _rel, _mk, ts, persona, brand, llm, _ta, sent) in rows:
        a = agg.get(tid)
        if a is None:
            continue
        a["count"] += 1
        a["by_llm"][llm] += 1
        a["by_persona"][persona] += 1
        a["by_brand"][brand] += 1
        a["sentiment"][_sentiment_label(sent)] += 1
        if sent is not None:
            a["sent_sum"] += sent
            a["sent_n"] += 1
        ts_u = _as_utc(ts)
        if ts_u is not None:
            if a["first"] is None or ts_u < a["first"]:
                a["first"] = ts_u
            if a["last"] is None or ts_u > a["last"]:
                a["last"] = ts_u
            if ts_u >= recent_cut:
                a["recent"] += 1
            elif ts_u >= prior_cut:
                a["prior"] += 1

    items: list[dict] = []
    for tid, m in meta.items():
        a = agg[tid]
        top_llm, top_llm_n = ("", 0)
        if a["by_llm"]:
            top_llm, top_llm_n = max(a["by_llm"].items(), key=lambda kv: kv[1])
        trend, growth = _trend(a["recent"], a["prior"])
        items.append({
            **m,
            "count": a["count"],
            "scored_count": a["sent_n"],
            "avg_sentiment": round(a["sent_sum"] / a["sent_n"], 3) if a["sent_n"] else None,
            "sentiment": dict(a["sentiment"]),
            "by_llm": dict(a["by_llm"]),
            "by_persona": dict(a["by_persona"]),
            "by_brand": dict(a["by_brand"]),
            "top_llm": top_llm,
            "top_llm_share": round(top_llm_n / a["count"], 3) if a["count"] else 0.0,
            "first_seen": a["first"].isoformat() if a["first"] else None,
            "last_seen": a["last"].isoformat() if a["last"] else None,
            "recent_count": a["recent"],
            "prior_count": a["prior"],
            "trend": trend,
            "growth": round(growth, 2),
        })

    items.sort(key=lambda x: x["count"], reverse=True)
    return {"taxonomy_version": version, "themes": items}


async def theme_timeseries(
    db: AsyncSession,
    version: int | None = None,
    top: int = _TIMESERIES_TOP,
    persona: str | None = None,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    disease: str | None = None,
    brand: str | None = None,
) -> dict:
    if version is None:
        version = await current_version(db)
    if not version:
        return {"taxonomy_version": 0, "themes": [], "rows": []}

    meta = await _themes_for_version(db, version)
    rows = await _assignment_rows(
        db, version, persona=persona,
        therapeutic_area=therapeutic_area, indication=indication, disease=disease, brand=brand,
    )

    totals: dict[str, int] = defaultdict(int)
    by_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))  # day -> theme -> count
    days: set[str] = set()
    for (tid, _rid, _rel, _mk, ts, *_rest) in rows:
        ts_u = _as_utc(ts)
        if ts_u is None or tid not in meta:
            continue
        day = ts_u.date().isoformat()
        days.add(day)
        by_day[day][tid] += 1
        totals[tid] += 1

    top_ids = [tid for tid, _ in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:top]]
    sorted_days = sorted(days)
    out_rows: list[dict] = []
    for day in sorted_days:
        row = {"period": day}
        for tid in top_ids:
            row[tid] = by_day[day].get(tid, 0)
        out_rows.append(row)

    return {
        "taxonomy_version": version,
        "themes": [{"theme_id": tid, "label": meta[tid]["label"]} for tid in top_ids],
        "rows": out_rows,
    }


async def signals(
    db: AsyncSession,
    version: int | None = None,
    persona: str | None = None,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    disease: str | None = None,
    brand: str | None = None,
) -> dict:
    overview = await theme_overview(
        db, version, persona=persona,
        therapeutic_area=therapeutic_area, indication=indication, disease=disease, brand=brand,
    )
    items = overview["themes"]

    dominant = sorted(items, key=lambda x: x["count"], reverse=True)[:5]
    risks = sorted(
        [x for x in items if x["avg_sentiment"] is not None
         and x["avg_sentiment"] <= _RISK_SENTIMENT and x["count"] >= _MIN_SIGNAL_COUNT],
        key=lambda x: x["avg_sentiment"],
    )[:6]
    emerging = sorted(
        [x for x in items if x["trend"] in ("up", "new") and x["recent_count"] >= _MIN_SIGNAL_COUNT],
        key=lambda x: x["growth"], reverse=True,
    )[:6]
    model_skew = sorted(
        [x for x in items if x["count"] >= _SKEW_MIN_COUNT and x["top_llm_share"] >= _SKEW_SHARE],
        key=lambda x: x["top_llm_share"], reverse=True,
    )[:6]

    return {
        "taxonomy_version": overview["taxonomy_version"],
        "dominant": dominant,
        "risks": risks,
        "emerging": emerging,
        "model_skew": model_skew,
    }


async def theme_detail(db: AsyncSession, theme_id: str, sample_limit: int = 8) -> dict | None:
    theme = (await db.execute(select(Theme).where(Theme.theme_id == theme_id))).scalar_one_or_none()
    if theme is None:
        return None
    version = theme.taxonomy_version
    rows = await _assignment_rows(db, version, theme_id=theme_id)

    by_llm: dict[str, int] = defaultdict(int)
    by_persona: dict[str, int] = defaultdict(int)
    by_brand: dict[str, int] = defaultdict(int)
    by_ta: dict[str, int] = defaultdict(int)
    sentiment = defaultdict(int)
    sent_sum, sent_n = 0.0, 0
    ts_day: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "sent_sum": 0.0, "sent_n": 0})
    samples_raw: list[tuple] = []  # (relevance, response_id, llm, persona, brand, sent, matched)

    for (tid, rid, rel, mk, ts, persona, brand, llm, ta, sent) in rows:
        by_llm[llm] += 1
        by_persona[persona] += 1
        by_brand[brand] += 1
        by_ta[ta] += 1
        sentiment[_sentiment_label(sent)] += 1
        if sent is not None:
            sent_sum += sent
            sent_n += 1
        ts_u = _as_utc(ts)
        if ts_u is not None:
            day = ts_u.date().isoformat()
            ts_day[day]["count"] += 1
            if sent is not None:
                ts_day[day]["sent_sum"] += sent
                ts_day[day]["sent_n"] += 1
        try:
            matched = json.loads(mk) if mk else []
        except Exception:  # noqa: BLE001
            matched = []
        samples_raw.append((rel or 0.0, rid, llm, persona, brand, sent, matched))

    timeseries = [
        {
            "period": day,
            "count": int(v["count"]),
            "avg_sentiment": round(v["sent_sum"] / v["sent_n"], 3) if v["sent_n"] else None,
        }
        for day, v in sorted(ts_day.items())
    ]

    # Sample responses (highest relevance first) with question + snippet
    samples_raw.sort(key=lambda x: x[0], reverse=True)
    top_samples = samples_raw[:sample_limit]
    sample_ids = [s[1] for s in top_samples]
    body_map: dict[str, Response] = {}
    if sample_ids:
        body_rows = (
            await db.execute(select(Response).where(Response.response_id.in_(sample_ids)))
        ).scalars().all()
        body_map = {r.response_id: r for r in body_rows}

    samples = []
    for rel, rid, llm, persona, brand, sent, matched in top_samples:
        r = body_map.get(rid)
        snippet = " ".join((r.response_text or "").split())[:320] if r else ""
        samples.append({
            "response_id": rid,
            "llm_name": llm,
            "persona": persona,
            "brand_focus": brand,
            "sentiment_score": sent,
            "relevance": round(rel, 2),
            "matched_keywords": matched,
            "question_text": r.question_text if r else None,
            "snippet": snippet,
        })

    try:
        keywords = json.loads(theme.keywords) if theme.keywords else []
    except Exception:  # noqa: BLE001
        keywords = []

    count = len(rows)
    return {
        "theme": {
            "theme_id": theme.theme_id,
            "taxonomy_version": version,
            "label": theme.label,
            "description": theme.description,
            "category": theme.category,
            "keywords": keywords,
        },
        "count": count,
        "scored_count": sent_n,
        "avg_sentiment": round(sent_sum / sent_n, 3) if sent_n else None,
        "sentiment": dict(sentiment),
        "by_llm": dict(by_llm),
        "by_persona": dict(by_persona),
        "by_brand": dict(by_brand),
        "by_therapeutic_area": dict(by_ta),
        "timeseries": timeseries,
        "samples": samples,
    }
