"""Service layer for AI Prompt Volume Intelligence (FR-116).

Wraps the ingestion engine and provides read helpers for the dashboard: volume aggregated by
therapeutic area + competitor (FR-116.2), grouped high-volume gap topics (FR-116.3), a
CSV export (with formula-injection escaping), and the demand-ranked question bank (FR-116.4).
Reads default to the LATEST uploaded batch so the dashboard shows a stable view; pass
``batch_id`` to pin a specific upload.
"""
import asyncio
import csv
import io
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.config.taxonomy import area_for
from app.models.prompt_volume import PromptVolumeBatch, PromptVolumeStaging
from app.models.prompt_volume_alert import PromptVolumeGapAlert
from app.models.question import Question
from app.prompt_volume import engine, gap, gap_alerts, persona, semrush_source

logger = logging.getLogger(__name__)

_CSV_FIELDS = [
    "query_text", "prompt_text", "search_volume", "keyword_difficulty", "cpc",
    "matched_therapeutic_area", "matched_competitor", "matched_brand",
    "mapping_confidence", "matched_question_id", "match_score",
]


async def ingest(
    db: AsyncSession,
    *,
    content: bytes,
    source_tool: str,
    source_label: str,
    dataset_date: str,
    filename: str | None = None,
    synthesize: bool = True,
) -> dict:
    """Parse + PII-lint + map + analyze + persist one upload (FR-116.1/.3/.5), then reconcile
    coverage-gap alerts against it (FR-116.3 enhancement).

    ``synthesize`` = auto-generate natural questions for bare-keyword gaps (analyst choice)."""
    result = await engine.ingest(
        db,
        content=content,
        source_tool=source_tool,
        source_label=source_label,
        dataset_date=dataset_date,
        filename=filename,
        synthesize=synthesize,
    )
    try:
        result["gap_alerts"] = await sync_gap_alerts(
            db, batch_id=result["batch_id"], flagged=result.get("gap_topics")
        )
    except Exception:  # noqa: BLE001 - alerting must never fail an otherwise-good ingest
        await db.rollback()
        logger.warning("gap-alert sync failed for batch %s", result.get("batch_id"), exc_info=True)
    return result


async def _latest_batch_id(db: AsyncSession) -> str | None:
    row = await db.execute(
        select(PromptVolumeBatch.batch_id).order_by(PromptVolumeBatch.created_at.desc()).limit(1)
    )
    return row.scalars().first()


def _batch_dict(b: PromptVolumeBatch) -> dict:
    return {
        "batch_id": b.batch_id,
        "source_tool": b.source_tool,
        "source_label": b.source_label,
        "dataset_date": b.dataset_date,
        "metric_type": b.metric_type,
        "filename": b.filename,
        "synthesize_questions": b.synthesize_questions,
        "rows_total": b.rows_total,
        "rows_ingested": b.rows_ingested,
        "rows_rejected": b.rows_rejected,
        "gap_topics_flagged": b.gap_topics_flagged,
        "created_at": b.created_at.isoformat() if b.created_at else None,
    }


async def list_batches(db: AsyncSession) -> dict:
    """Upload history, newest first (FR-116.5 auditability)."""
    rows = list(
        (await db.execute(
            select(PromptVolumeBatch).order_by(PromptVolumeBatch.created_at.desc())
        )).scalars().all()
    )
    return {"count": len(rows), "batches": [_batch_dict(b) for b in rows]}


async def _staging_rows(db: AsyncSession, batch_id: str) -> list[PromptVolumeStaging]:
    stmt = (
        select(PromptVolumeStaging)
        .where(PromptVolumeStaging.batch_id == batch_id)
        .order_by(PromptVolumeStaging.search_volume.desc())
    )
    return list((await db.execute(stmt)).scalars().all())


def _dedupe_by_query(rows: list[PromptVolumeStaging]) -> list[PromptVolumeStaging]:
    """Collapse duplicate/near-duplicate keywords to one row each (highest volume wins).

    Merged SEO exports routinely repeat the same keyword (it ranks for several seed terms),
    so summing raw rows double-counts demand. Rows arrive sorted by volume desc, so keeping
    the FIRST occurrence per ``normalized_query`` keeps the highest-volume copy. This mirrors
    how gap topics + demand ranking already dedupe (FR-116.3/.4) — so every panel counts
    DISTINCT queries, and the headline total can't be inflated by an export merge.
    """
    seen: set[str] = set()
    out: list[PromptVolumeStaging] = []
    for r in rows:
        key = r.normalized_query or f"__id_{r.id}"
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _empty_share() -> dict:
    return {"brand_volume": 0, "competitor_volume": 0, "category_volume": 0,
            "brand_share_pct": 0.0, "competitor_share_pct": 0.0, "by_area": []}


def _pct(part: float, whole: float) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


async def intelligence(db: AsyncSession, *, batch_id: str | None = None) -> dict:
    """Demand intelligence for a batch (FR-116.2): volume by therapeutic area + competitor,
    plus brand-vs-competitor share of demand and a heuristic persona split.

    NOTE: volume is a SEARCH-DEMAND PROXY (third-party SEO data), not literal AI-prompt counts.
    """
    if batch_id is None:
        batch_id = await _latest_batch_id(db)
    if batch_id is None:
        return {"batch_id": None, "batch": None, "total_volume": 0,
                "by_therapeutic_area": [], "by_competitor": [], "unmapped_volume": 0,
                "raw_row_count": 0, "distinct_query_count": 0, "prompt_backed_count": 0,
                "share_of_demand": _empty_share(), "by_persona": []}

    batch = await db.get(PromptVolumeBatch, batch_id)
    raw_rows = await _staging_rows(db, batch_id)
    rows = _dedupe_by_query(raw_rows)

    by_area: dict[str, dict] = {}
    by_competitor: dict[str, int] = {}
    kd_by_area: dict[str, list[float]] = {}
    area_split: dict[str, dict] = {}
    persona_bucket: dict[str, dict] = {}
    total = 0
    unmapped = 0
    prompt_backed = 0  # rows carrying a REAL question/prompt (not a bare keyword)
    brand_volume = competitor_volume = category_volume = 0

    for r in rows:
        vol = r.search_volume or 0
        total += vol
        if (r.prompt_text or "").strip():
            prompt_backed += 1
        raw_area = r.matched_therapeutic_area
        area = area_for(raw_area) if raw_area and raw_area != "Unmapped" else "Unmapped"
        if area == "Unmapped":
            unmapped += vol
        bucket = by_area.setdefault(area, {"therapeutic_area": area, "volume": 0, "query_count": 0})
        bucket["volume"] += vol
        bucket["query_count"] += 1
        if r.keyword_difficulty is not None:
            kd_by_area.setdefault(area, []).append(float(r.keyword_difficulty))

        split = area_split.setdefault(
            area, {"therapeutic_area": area, "brand_volume": 0, "competitor_volume": 0}
        )
        if r.matched_brand:
            brand_volume += vol
            split["brand_volume"] += vol
        elif r.matched_competitor:
            competitor_volume += vol
            split["competitor_volume"] += vol
            by_competitor[r.matched_competitor] = by_competitor.get(r.matched_competitor, 0) + vol
        elif area != "Unmapped":
            category_volume += vol

        p = persona.classify_persona(r.query_text)
        pb = persona_bucket.setdefault(p, {"persona": p, "volume": 0, "query_count": 0})
        pb["volume"] += vol
        pb["query_count"] += 1

    for a in by_area.values():
        kds = kd_by_area.get(a["therapeutic_area"], [])
        a["share_pct"] = _pct(a["volume"], total)
        a["avg_difficulty"] = round(sum(kds) / len(kds), 1) if kds else None
    ta_list = sorted(by_area.values(), key=lambda x: x["volume"], reverse=True)

    comp_total = sum(by_competitor.values())
    comp_list = sorted(
        ({"competitor": k, "volume": v, "share_pct": _pct(v, comp_total)}
         for k, v in by_competitor.items()),
        key=lambda x: x["volume"], reverse=True,
    )

    head = brand_volume + competitor_volume
    area_share_list = []
    for a in area_split.values():
        if a["therapeutic_area"] == "Unmapped":
            continue
        h = a["brand_volume"] + a["competitor_volume"]
        if h == 0:
            continue
        a["brand_share_pct"] = _pct(a["brand_volume"], h)
        a["competitor_share_pct"] = _pct(a["competitor_volume"], h)
        area_share_list.append(a)
    area_share_list.sort(key=lambda x: x["brand_volume"] + x["competitor_volume"], reverse=True)

    persona_list = sorted(persona_bucket.values(), key=lambda x: x["volume"], reverse=True)

    return {
        "batch_id": batch_id,
        "batch": _batch_dict(batch) if batch else None,
        "metric_type": batch.metric_type if batch else None,
        "total_volume": total,
        "unmapped_volume": unmapped,
        "raw_row_count": len(raw_rows),
        "distinct_query_count": len(rows),
        "prompt_backed_count": prompt_backed,
        "by_therapeutic_area": ta_list,
        "by_competitor": comp_list,
        "share_of_demand": {
            "brand_volume": brand_volume,
            "competitor_volume": competitor_volume,
            "category_volume": category_volume,
            "brand_share_pct": _pct(brand_volume, head),
            "competitor_share_pct": _pct(competitor_volume, head),
            "by_area": area_share_list,
        },
        "by_persona": persona_list,
    }


async def gap_topics(db: AsyncSession, *, batch_id: str | None = None) -> dict:
    """High-volume topics with no Approved-bank coverage, grouped from staged rows (FR-116.3)."""
    if batch_id is None:
        batch_id = await _latest_batch_id(db)
    if batch_id is None:
        return {"batch_id": None, "count": 0, "topics": []}

    settings = get_settings()
    batch = await db.get(PromptVolumeBatch, batch_id)
    # Honour the analyst's upload-time synthesis choice on every on-demand recompute.
    synthesize = True if batch is None or batch.synthesize_questions is None else bool(batch.synthesize_questions)
    rows = await _staging_rows(db, batch_id)
    row_dicts = [
        {
            "query_text": r.query_text,
            "prompt_text": r.prompt_text,
            "normalized_query": r.normalized_query,
            "search_volume": r.search_volume or 0,
            "keyword_difficulty": r.keyword_difficulty,
            "cpc": r.cpc,
            "tokens": set((r.normalized_query or "").split()),
            "matched_therapeutic_area": r.matched_therapeutic_area,
            "matched_competitor": r.matched_competitor,
            "matched_brand": r.matched_brand,
        }
        for r in rows
    ]
    question_matches = await _approved_question_matches(db)
    await asyncio.to_thread(
        gap.match_rows_to_questions,
        row_dicts,
        question_matches,
        settings.prompt_volume_match_threshold,
    )
    unmatched = [row for row in row_dicts if not row.get("matched_question_id")]
    topics = gap.cluster_gap_topics(
        unmatched, group_threshold=settings.prompt_volume_match_threshold, synthesize=synthesize
    )
    flagged = gap.flag_high_volume(
        topics,
        abs_floor=settings.prompt_volume_abs_volume_floor,
        top_percentile=settings.prompt_volume_top_percentile,
    )
    return {"batch_id": batch_id, "count": len(flagged), "topics": flagged}


# ---------------------------------------------------------------------------------
#  Coverage-gap alerts (FR-116.3 enhancement) - trackable, auto-resolving gap signals
# ---------------------------------------------------------------------------------
async def _approved_question_matches(db: AsyncSession) -> list[tuple[str, set[str]]]:
    stmt = select(Question.question_id, Question.question_text).where(
        Question.approval_status == "APPROVED",
        Question.active.is_(True),
        Question.deleted_at.is_(None),
        Question.superseded_by.is_(None),
    )
    return [
        (question_id, gap.tokens(question_text))
        for question_id, question_text in (await db.execute(stmt)).all()
        if question_text
    ]


async def _approved_question_tokens(db: AsyncSession) -> list[set[str]]:
    return [tokens for _, tokens in await _approved_question_matches(db)]


def _serialize_gap_alert(r: PromptVolumeGapAlert) -> dict:
    return {
        "alert_id": r.alert_id,
        "topic_key": r.topic_key,
        "label": r.label,
        "question": r.question,
        "therapeutic_area": r.therapeutic_area,
        "competitor": r.competitor,
        "status": r.status,
        "combined_volume": r.combined_volume,
        "opportunity_score": r.opportunity_score,
        "query_count": r.query_count,
        "first_seen_batch_id": r.first_seen_batch_id,
        "first_seen_at": r.first_seen_at.isoformat() if r.first_seen_at else None,
        "last_seen_batch_id": r.last_seen_batch_id,
        "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
        "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
        "resolved_reason": r.resolved_reason,
        "is_new": r.first_seen_batch_id == r.last_seen_batch_id,
    }


def _refresh_alert(r: PromptVolumeGapAlert, f: dict, batch_id: str, now: datetime) -> None:
    r.label = f["label"]
    r.question = f.get("question")
    r.therapeutic_area = f["therapeutic_area"]
    r.competitor = f["competitor"]
    r.combined_volume = f["combined_volume"]
    r.opportunity_score = f["opportunity_score"]
    r.query_count = f["query_count"]
    r.last_seen_batch_id = batch_id
    r.last_seen_at = now


async def sync_gap_alerts(
    db: AsyncSession, *, batch_id: str, flagged: list[dict] | None = None
) -> dict:
    """Reconcile coverage-gap alerts against a batch's flagged gaps (FR-116.3 enhancement).

    Creates alerts for NEW high-opportunity gaps, refreshes recurring ones, re-opens returned
    ones, and AUTO-RESOLVES any open alert the Approved Question Bank now covers. Only the top
    ``prompt_volume_gap_alert_limit`` gaps (by opportunity) are eligible, to avoid alert fatigue.
    """
    settings = get_settings()
    if flagged is None:
        flagged = (await gap_topics(db, batch_id=batch_id))["topics"]

    alertable = sorted(
        flagged, key=lambda t: t.get("opportunity_score") or 0, reverse=True
    )[: settings.prompt_volume_gap_alert_limit]

    existing = list((await db.execute(select(PromptVolumeGapAlert))).scalars().all())
    existing_by_key = {r.topic_key: r for r in existing}
    existing_plain = {
        k: {"status": r.status, "label": r.label} for k, r in existing_by_key.items()
    }

    current_keys = {gap_alerts.topic_key(t.get("label") or "") for t in alertable}
    qtokens = await _approved_question_tokens(db)
    covered_keys = {
        k for k, r in existing_by_key.items()
        if r.status == gap_alerts.STATUS_OPEN
        and k not in current_keys
        and gap_alerts.is_covered(r.label, qtokens, settings.prompt_volume_match_threshold)
    }

    plan = gap_alerts.plan_sync(alertable, existing_plain, covered_keys, batch_id=batch_id)
    now = datetime.now(timezone.utc)

    for f in plan.create:
        db.add(PromptVolumeGapAlert(
            alert_id=f"PVGAP-{uuid.uuid4().hex[:10]}",
            topic_key=f["topic_key"], label=f["label"], question=f.get("question"),
            therapeutic_area=f["therapeutic_area"], competitor=f["competitor"],
            status=gap_alerts.STATUS_OPEN,
            combined_volume=f["combined_volume"], opportunity_score=f["opportunity_score"],
            query_count=f["query_count"],
            first_seen_batch_id=batch_id, first_seen_at=now,
            last_seen_batch_id=batch_id, last_seen_at=now,
        ))
    for f in plan.update + plan.touch:
        _refresh_alert(existing_by_key[f["topic_key"]], f, batch_id, now)
    for f in plan.reopen:
        r = existing_by_key[f["topic_key"]]
        r.status = gap_alerts.STATUS_OPEN
        r.resolved_at = None
        r.resolved_reason = None
        _refresh_alert(r, f, batch_id, now)
    for key in plan.resolve:
        r = existing_by_key[key]
        r.status = gap_alerts.STATUS_RESOLVED
        r.resolved_at = now
        r.resolved_reason = gap_alerts.REASON_COVERED

    await db.commit()
    return {
        "created": len(plan.create),
        "updated": len(plan.update) + len(plan.touch),
        "reopened": len(plan.reopen),
        "resolved": len(plan.resolve),
    }


async def list_gap_alerts(db: AsyncSession, *, status: str = "OPEN") -> dict:
    normalized = (status or "OPEN").upper()
    stmt = select(PromptVolumeGapAlert)
    if normalized != "ALL":
        stmt = stmt.where(PromptVolumeGapAlert.status == normalized)
    stmt = stmt.order_by(
        PromptVolumeGapAlert.opportunity_score.desc(),
        PromptVolumeGapAlert.combined_volume.desc(),
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return {"count": len(rows), "status": normalized, "alerts": [_serialize_gap_alert(r) for r in rows]}


async def dismiss_gap_alert(db: AsyncSession, alert_id: str) -> dict | None:
    r = await db.get(PromptVolumeGapAlert, alert_id)
    if r is None:
        return None
    r.status = gap_alerts.STATUS_DISMISSED
    await db.commit()
    return _serialize_gap_alert(r)


async def gap_alert_summary(db: AsyncSession) -> dict:
    rows = (await db.execute(
        select(PromptVolumeGapAlert.status, func.count()).group_by(PromptVolumeGapAlert.status)
    )).all()
    counts = {s: n for s, n in rows}
    return {
        "open": counts.get(gap_alerts.STATUS_OPEN, 0),
        "resolved": counts.get(gap_alerts.STATUS_RESOLVED, 0),
        "dismissed": counts.get(gap_alerts.STATUS_DISMISSED, 0),
    }


async def sync_gap_alerts_latest(db: AsyncSession) -> dict:
    """Reconcile gap alerts against the latest upload (populate without re-uploading)."""
    batch_id = await _latest_batch_id(db)
    if batch_id is None:
        return {"batch_id": None, "created": 0, "updated": 0, "reopened": 0, "resolved": 0}
    result = await sync_gap_alerts(db, batch_id=batch_id)
    result["batch_id"] = batch_id
    return result


async def prioritized_questions(db: AsyncSession, *, batch_id: str | None = None) -> dict:
    """Approved bank questions ranked by demand = priority_weight × matched search volume (FR-116.4)."""
    if batch_id is None:
        batch_id = await _latest_batch_id(db)

    qstmt = select(Question).where(
        Question.approval_status == "APPROVED",
        Question.active.is_(True),
        Question.deleted_at.is_(None),
        Question.superseded_by.is_(None),
    )
    questions = list((await db.execute(qstmt)).scalars().all())
    weights = {q.question_id: (q.priority_weight if q.priority_weight is not None else 1.0) for q in questions}

    demand: dict[str, dict] = {}
    if batch_id is not None:
        rows = await _staging_rows(db, batch_id)
        row_dicts = [
            {
                "normalized_query": r.normalized_query,
                "search_volume": r.search_volume or 0,
                "tokens": set((r.normalized_query or "").split()),
            }
            for r in rows
        ]
        question_matches = [
            (question.question_id, gap.tokens(question.question_text))
            for question in questions
        ]
        await asyncio.to_thread(
            gap.match_rows_to_questions,
            row_dicts,
            question_matches,
            get_settings().prompt_volume_match_threshold,
        )
        demand = gap.question_demand(row_dicts, weights)

    items = []
    for q in questions:
        d = demand.get(q.question_id, {"matched_volume": 0, "matched_queries": 0, "demand_score": 0.0})
        items.append({
            "id": q.id,
            "question_id": q.question_id,
            "question_text": q.question_text,
            "persona": q.persona,
            "therapeutic_area": q.therapeutic_area,
            "brand_focus": q.brand_focus,
            "domain": q.domain,
            "approval_status": q.approval_status,
            "priority_weight": q.priority_weight if q.priority_weight is not None else 1.0,
            "search_volume": d["matched_volume"],
            "matched_queries": d["matched_queries"],
            "demand_score": d["demand_score"],
        })
    items.sort(key=lambda x: (x["demand_score"], x["priority_weight"]), reverse=True)
    return {"batch_id": batch_id, "count": len(items), "items": items}


def _area_of(raw_area: str | None) -> str:
    return area_for(raw_area) if raw_area and raw_area != "Unmapped" else "Unmapped"


def _emerging_topics(batches: list, per_batch_rows: dict, *, limit: int) -> dict | None:
    """Rising queries in the most recent upload vs the one before it (delta by normalized query).

    Only meaningful when two datasets overlap, so it is explicitly labelled with both dataset
    names. A query absent from the previous upload is flagged ``is_new``. Directional only —
    each point is a distinct third-party upload, not a continuous panel.
    """
    if len(batches) < 2:
        return None
    curr, prev = batches[-1], batches[-2]

    def _vol_map(rows) -> tuple[dict, dict]:
        vols: dict[str, int] = {}
        meta: dict[str, dict] = {}
        for r in rows:
            nq = r.normalized_query
            if not nq:
                continue
            vols[nq] = max(vols.get(nq, 0), r.search_volume or 0)
            meta.setdefault(nq, {
                "query_text": r.query_text,
                "therapeutic_area": _area_of(r.matched_therapeutic_area),
                "competitor": r.matched_competitor,
            })
        return vols, meta

    curr_vols, curr_meta = _vol_map(per_batch_rows[curr.batch_id])
    prev_vols, _ = _vol_map(per_batch_rows[prev.batch_id])

    topics = []
    for nq, cv in curr_vols.items():
        pv = prev_vols.get(nq, 0)
        delta = cv - pv
        if delta <= 0:
            continue
        m = curr_meta[nq]
        topics.append({
            "query_text": m["query_text"],
            "therapeutic_area": m["therapeutic_area"],
            "competitor": m["competitor"],
            "previous_volume": pv,
            "current_volume": cv,
            "delta": delta,
            "pct_change": _pct(delta, pv) if pv else None,
            "is_new": pv == 0,
        })
    topics.sort(key=lambda t: t["delta"], reverse=True)
    return {
        "current_label": curr.source_label,
        "current_date": curr.dataset_date,
        "previous_label": prev.source_label,
        "previous_date": prev.dataset_date,
        "topics": topics[:limit],
    }


async def demand_trend(db: AsyncSession, *, top_n: int = 6, emerging_limit: int = 10) -> dict:
    """Demand over time across ALL uploads (FR-116, Profound "Prompt Volumes" trend parity).

    Orders batches by analyst-provided ``dataset_date`` and returns a per-upload series (total,
    brand, competitor, category volume + per-area/per-competitor volume) plus the rising topics
    in the latest upload. Each series point is a SEPARATE third-party upload used as a demand
    proxy, so the trend is directional context, not a continuous measured panel.
    """
    batches = list((await db.execute(
        select(PromptVolumeBatch).order_by(
            PromptVolumeBatch.dataset_date, PromptVolumeBatch.created_at
        )
    )).scalars().all())
    if not batches:
        return {"count": 0, "series": [], "top_areas": [], "top_competitors": [], "emerging": None}

    series: list[dict] = []
    area_totals: dict[str, int] = {}
    comp_totals: dict[str, int] = {}
    per_batch_rows: dict[str, list] = {}

    for b in batches:
        rows = _dedupe_by_query(await _staging_rows(db, b.batch_id))
        per_batch_rows[b.batch_id] = rows
        total = brand_v = comp_v = cat_v = 0
        areas: dict[str, int] = {}
        comps: dict[str, int] = {}
        for r in rows:
            vol = r.search_volume or 0
            total += vol
            area = _area_of(r.matched_therapeutic_area)
            if area != "Unmapped":
                areas[area] = areas.get(area, 0) + vol
                area_totals[area] = area_totals.get(area, 0) + vol
            if r.matched_brand:
                brand_v += vol
            elif r.matched_competitor:
                comp_v += vol
                comps[r.matched_competitor] = comps.get(r.matched_competitor, 0) + vol
                comp_totals[r.matched_competitor] = comp_totals.get(r.matched_competitor, 0) + vol
            elif area != "Unmapped":
                cat_v += vol
        series.append({
            "batch_id": b.batch_id,
            "dataset_date": b.dataset_date,
            "source_label": b.source_label,
            "source_tool": b.source_tool,
            "total_volume": total,
            "brand_volume": brand_v,
            "competitor_volume": comp_v,
            "category_volume": cat_v,
            "areas": areas,
            "competitors": comps,
        })

    top_areas = [a for a, _ in sorted(area_totals.items(), key=lambda kv: kv[1], reverse=True)[:top_n]]
    top_competitors = [c for c, _ in sorted(comp_totals.items(), key=lambda kv: kv[1], reverse=True)[:top_n]]

    return {
        "count": len(batches),
        "series": series,
        "top_areas": top_areas,
        "top_competitors": top_competitors,
        "emerging": _emerging_topics(batches, per_batch_rows, limit=emerging_limit),
    }


def _csv_safe(value) -> str:
    """Neutralize CSV formula injection: prefix a leading = + - @ with a quote."""
    s = "" if value is None else str(value)
    if s and s[0] in ("=", "+", "-", "@"):
        return "'" + s
    return s


def to_csv(rows: list[dict]) -> str:
    """Render staged rows to CSV with formula-injection escaping."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _csv_safe(row.get(k)) for k in _CSV_FIELDS})
    return buf.getvalue()


def serialize_row(r: PromptVolumeStaging) -> dict:
    return {
        "query_text": r.query_text,
        "prompt_text": r.prompt_text,
        "search_volume": r.search_volume,
        "keyword_difficulty": r.keyword_difficulty,
        "cpc": r.cpc,
        "matched_therapeutic_area": r.matched_therapeutic_area,
        "matched_competitor": r.matched_competitor,
        "matched_brand": r.matched_brand,
        "mapping_confidence": r.mapping_confidence,
        "matched_question_id": r.matched_question_id,
        "match_score": r.match_score,
    }


async def export_rows(db: AsyncSession, *, batch_id: str | None = None) -> list[dict]:
    if batch_id is None:
        batch_id = await _latest_batch_id(db)
    if batch_id is None:
        return []
    return [serialize_row(r) for r in await _staging_rows(db, batch_id)]


# ---------------------------------------------------------------------------------
#  In-app SEMrush fetch: preview -> confirm (FR-116)
# ---------------------------------------------------------------------------------
# Preview does the BILLED SEMrush calls once; the raw fetched rows are cached under a
# fetch_id so "Ingest" persists WITHOUT re-billing. In-memory + TTL'd, matching the app's
# other ephemeral job-state (single-worker POC). Nothing is dropped: ingest stores the full
# snapshot so volume/share/trend stay accurate (redundancy is only FLAGGED at preview).
_FETCH_CACHE: dict[str, dict] = {}
_FETCH_TTL_SEC = 1800          # 30 min
_FETCH_CACHE_MAX = 20          # cap distinct pending previews (evict oldest)


def _cache_prune(now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    for k in [k for k, v in _FETCH_CACHE.items()
              if (now - v["created_at"]).total_seconds() > _FETCH_TTL_SEC]:
        _FETCH_CACHE.pop(k, None)
    if len(_FETCH_CACHE) > _FETCH_CACHE_MAX:
        oldest = sorted(_FETCH_CACHE, key=lambda k: _FETCH_CACHE[k]["created_at"])
        for k in oldest[: len(_FETCH_CACHE) - _FETCH_CACHE_MAX]:
            _FETCH_CACHE.pop(k, None)


def _cache_get(fetch_id: str) -> dict | None:
    _cache_prune()
    return _FETCH_CACHE.get(fetch_id)


def semrush_status() -> dict:
    """Whether the in-app SEMrush fetch is available + its cost-guard defaults (for the UI)."""
    s = get_settings()
    return {
        "configured": bool(s.semrush_api_key),
        "database": s.semrush_database or "us",
        "per_seed_limit": s.prompt_volume_semrush_per_seed_limit,
        "max_seeds": s.prompt_volume_semrush_max_seeds,
        "reports": s.prompt_volume_semrush_reports,
    }


def _novelty_compute(
    rows: list[dict], prev_norms: set[str], prev_tokens: list[set[str]],
    qtokens: list[set[str]], threshold: float,
) -> dict:
    """Pure: split fetched rows into covered / already-seen / new (flag-only; nothing dropped).

    covered   = near-matches an APPROVED question (definitely not a gap).
    seen       = exact OR near-duplicate of the LAST dataset's queries.
    new        = neither -> net-new demand; ``novel_volume`` sums their volume.
    Precedence covered > seen > new so a row is counted once. Counts sum to len(rows). Each row
    is TAGGED in place with ``_novelty`` (new|seen|covered) so ingest can subset by novelty.
    """
    new_count = seen = covered = 0
    novel_volume = 0
    for r in rows:
        nq = r.get("normalized_query") or gap.normalize(r["query_text"])
        toks = set(nq.split())
        vol = r.get("search_volume") or 0
        if any(gap.similarity(toks, qt) >= threshold for qt in qtokens):
            covered += 1
            r["_novelty"] = "covered"
        elif nq in prev_norms or any(gap.similarity(toks, pt) >= threshold for pt in prev_tokens):
            seen += 1
            r["_novelty"] = "seen"
        else:
            new_count += 1
            novel_volume += vol
            r["_novelty"] = "new"
    return {
        "new_count": new_count,
        "seen_in_last_count": seen,
        "covered_count": covered,
        "novel_volume": novel_volume,
    }


async def _novelty(db: AsyncSession, rows: list[dict]) -> dict:
    """Redundancy breakdown vs the latest batch + the Approved Question Bank (read-only)."""
    latest_id = await _latest_batch_id(db)
    prev_norms: set[str] = set()
    prev_tokens: list[set[str]] = []
    if latest_id:
        for r in await _staging_rows(db, latest_id):
            nq = r.normalized_query
            if nq:
                prev_norms.add(nq)
                prev_tokens.append(set(nq.split()))
    qtokens = await _approved_question_tokens(db)
    threshold = get_settings().prompt_volume_match_threshold
    return await asyncio.to_thread(
        _novelty_compute, rows, prev_norms, prev_tokens, qtokens, threshold
    )


async def semrush_preview(
    db: AsyncSession,
    *,
    therapeutic_area: str,
    brand: str | None = None,
    include_generics: bool = True,
    include_indications: bool = True,
    include_competitors: bool = True,
    per_seed_limit: int | None = None,
    reports: str | None = None,
) -> dict:
    """Fetch (BILLED) + analyze for preview only; cache raw rows under a fetch_id (no persist).

    Raises ``semrush_source.NotConfigured`` when no API key is set (never fabricates demand).
    """
    settings = get_settings()
    reports = reports or settings.prompt_volume_semrush_reports
    limit = per_seed_limit or settings.prompt_volume_semrush_per_seed_limit
    limit = max(1, min(int(limit), 100))

    fetched = await semrush_source.fetch(
        therapeutic_area, brand=brand, include_generics=include_generics,
        include_indications=include_indications, include_competitors=include_competitors,
        per_seed_limit=limit, reports=reports,
    )
    raw_rows = fetched["rows"]

    # Analyze COPIES for preview (mapping summary + gap topics). The cached raw rows stay lean
    # and are re-analyzed at ingest so the persisted batch honours the analyst's synthesis choice.
    analyzed, flagged = await engine.analyze_rows(
        db, [dict(r) for r in raw_rows], synthesize=True, volume_present=True
    )
    # Classify the CACHED rows in place (tags each _novelty new/seen/covered) so ingest can
    # honour an analyst's "only new" / "top N" subset choice later without re-billing.
    novelty = await _novelty(db, raw_rows)

    by_area: dict[str, int] = {}
    by_comp: dict[str, int] = {}
    total = 0
    for r in analyzed:
        vol = r.get("search_volume") or 0
        total += vol
        raw_area = r.get("matched_therapeutic_area")
        area = area_for(raw_area) if raw_area and raw_area != "Unmapped" else "Unmapped"
        by_area[area] = by_area.get(area, 0) + vol
        if r.get("matched_competitor"):
            by_comp[r["matched_competitor"]] = by_comp.get(r["matched_competitor"], 0) + vol
    ta_list = sorted(
        ({"therapeutic_area": k, "volume": v} for k, v in by_area.items()),
        key=lambda x: x["volume"], reverse=True,
    )
    comp_list = sorted(
        ({"competitor": k, "volume": v} for k, v in by_comp.items()),
        key=lambda x: x["volume"], reverse=True,
    )
    sample = [
        {
            "query_text": r["query_text"],
            "prompt_text": r.get("prompt_text"),
            "search_volume": r.get("search_volume") or 0,
            "cpc": r.get("cpc"),
            "report": r.get("report"),
            "therapeutic_area": r.get("matched_therapeutic_area"),
            "competitor": r.get("matched_competitor"),
            "brand": r.get("matched_brand"),
        }
        for r in analyzed[:25]
    ]

    fetch_id = f"PVF-{uuid.uuid4().hex[:12]}"
    _cache_prune()
    _FETCH_CACHE[fetch_id] = {
        "rows": raw_rows,
        "therapeutic_area": therapeutic_area,
        "brand": brand,
        "reports": fetched["reports"],
        "seeds_queried": fetched["seeds_queried"],
        "lines_returned": fetched["lines_returned"],
        "created_at": datetime.now(timezone.utc),
    }

    return {
        "fetch_id": fetch_id if raw_rows else None,
        "therapeutic_area": therapeutic_area,
        "brand": brand,
        "seeds_queried": fetched["seeds_queried"],
        "lines_returned": fetched["lines_returned"],
        "distinct_query_count": len(raw_rows),
        "total_volume": total,
        "novelty": novelty,
        "by_therapeutic_area": ta_list[:8],
        "by_competitor": comp_list[:8],
        "gap_topics": flagged[:25],
        "sample": sample,
        "reports": fetched["reports"],
        "estimated_units": semrush_source.estimate_units(fetched["seeds_queried"], limit, reports),
        "expires_in_sec": _FETCH_TTL_SEC,
    }


async def semrush_ingest(
    db: AsyncSession, *, fetch_id: str, source_label: str, dataset_date: str,
    synthesize: bool = True, only_new: bool = False, limit: int | None = None,
) -> dict | None:
    """Persist a previewed fetch (optionally a subset) as a batch, then sync gap alerts.

    ``only_new`` keeps just net-new rows (skips already-seen / already-tracked); ``limit`` keeps
    the top-N by demand. With neither set, the FULL snapshot is stored so volume/share/trend stay
    accurate. Returns ``None`` when the fetch_id is unknown/expired (API -> 404); no re-fetch/
    re-bill -> the cached rows are ingested through the same pipeline as a CSV upload.
    """
    entry = _cache_get(fetch_id)
    if entry is None:
        return None
    rows = entry.get("rows") or []
    if not rows:
        raise ValueError("No fetched rows to ingest.")

    if only_new:
        rows = [r for r in rows if r.get("_novelty") == "new"]
    rows = sorted(rows, key=lambda r: r.get("search_volume") or 0, reverse=True)
    if limit is not None and limit > 0:
        rows = rows[:limit]
    if not rows:
        raise ValueError("No searches match the selected filters.")

    result = await engine.ingest_rows(
        db, rows=[{k: v for k, v in r.items() if k != "_novelty"} for r in rows],
        volume_present=True, source_tool="Semrush API", source_label=source_label,
        dataset_date=dataset_date, synthesize=synthesize,
    )
    try:
        result["gap_alerts"] = await sync_gap_alerts(
            db, batch_id=result["batch_id"], flagged=result.get("gap_topics")
        )
    except Exception:  # noqa: BLE001 - alerting must never fail an otherwise-good ingest
        await db.rollback()
        logger.warning("gap-alert sync failed for batch %s", result.get("batch_id"), exc_info=True)
    _FETCH_CACHE.pop(fetch_id, None)
    return result
