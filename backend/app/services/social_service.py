"""Social-listening analytics (Obesity/GLP-1 demo).

Aggregates SocialPost rows (TA-scoped) into the four outcomes: share of voice, sentiment,
volume over time, and top themes — plus adverse-event counts and per-channel engagement
leaders.

METHODOLOGY (surfaced in the UI via ``basis``): these are CAPTURED-SAMPLE metrics over
Apify-scraped public posts, NOT market-level share of voice. Post COUNTS are comparable
across channels, but ENGAGEMENT (upvotes vs views vs likes) is NOT — it is reported and
ranked per channel only, never summed across channels.
"""
import hashlib
import json
from collections import defaultdict
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.taxonomy import company_for, is_abbvie_brand
from app.guardrails import adverse_event
from app.guardrails.injection import scan_injection
from app.models.harvested_question import HarvestedQuestion
from app.models.social_brief import SocialBrief
from app.models.social_comment import SocialComment
from app.models.social_post import SocialPost
from app.utils.pii_lint import scan_for_pii

# Raw engagement metric label per channel (what engagement_score means there).
CHANNEL_METRIC = {
    "reddit": "upvotes",
    "tiktok": "likes",
    "instagram": "likes",
    "facebook": "reactions",
    "x": "likes",
}

BASIS = (
    "Captured social sample — Apify-scraped public posts. Share of voice is the share of "
    "THIS captured sample, not market-level share of voice. Engagement metrics differ by "
    "channel (upvotes vs views vs likes) and are compared per channel only, never summed "
    "across channels."
)

_UNATTRIBUTED = "Unattributed"


def _metric_for(channel: str | None) -> str:
    return CHANNEL_METRIC.get((channel or "").lower(), "engagement")


def _loads_list(raw) -> list:
    """Parse a JSON-list column, degrading to [] on null/garbage."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _loads_obj(raw) -> dict | None:
    """Parse a JSON-object column, degrading to None on null/garbage."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _top_counts(counter: dict, cap: int = 10) -> list[dict]:
    """Frequency map -> ranked ``[{label, count}]`` list (desc, capped)."""
    return sorted([{"label": str(k), "count": int(v)} for k, v in counter.items()],
                  key=lambda r: r["count"], reverse=True)[:cap]


def serialize(p: SocialPost) -> dict:
    return {
        "id": p.id,
        "channel": p.channel,
        "source": p.source,
        "post_url": p.post_url,
        "source_domain": p.source_domain,
        "search_term": p.search_term,
        "text": p.text,
        "text_original": p.text_original,
        "language": p.language,
        "is_translated": bool(p.is_translated),
        "brand_focus": p.brand_focus,
        "therapeutic_area": p.therapeutic_area,
        "domain": p.domain,
        "topic": p.topic,
        "sentiment": p.sentiment,
        "sentiment_label": p.sentiment_label,
        "engagement_score": p.engagement_score,
        "engagement_metric": _metric_for(p.channel),
        "comment_count": p.comment_count,
        "comment_sentiment": p.comment_sentiment,
        "comments_captured": p.comments_captured or 0,
        "brand_mentions": _loads_list(p.brand_mentions),
        "patient_signals": _loads_obj(p.patient_signals),
        "posted_at": p.posted_at.isoformat() if p.posted_at else None,
        "ae_flag": bool(p.ae_flag),
        "pii_flags": json.loads(p.pii_flags) if p.pii_flags else [],
        "harvested_at": p.harvested_at.isoformat() if p.harvested_at else None,
    }


async def list_posts(db: AsyncSession, *, therapeutic_area: str | None = None,
                     channel: str | None = None, brand_focus: str | None = None,
                     ae_only: bool = False, limit: int = 300, offset: int = 0) -> list[dict]:
    stmt = select(SocialPost)
    if therapeutic_area:
        stmt = stmt.where(SocialPost.therapeutic_area == therapeutic_area)
    if channel:
        stmt = stmt.where(SocialPost.channel == channel)
    if brand_focus:
        stmt = stmt.where(SocialPost.brand_focus == brand_focus)
    if ae_only:
        stmt = stmt.where(SocialPost.ae_flag.is_(True))
    stmt = stmt.order_by(SocialPost.harvested_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()
    return [serialize(p) for p in rows]


def serialize_comment(c: SocialComment) -> dict:
    return {
        "id": c.id,
        "post_id": c.post_id,
        "channel": c.channel,
        "text": c.text,
        "text_original": c.text_original,
        "language": c.language,
        "is_translated": bool(c.is_translated),
        "sentiment": c.sentiment,
        "sentiment_label": c.sentiment_label,
        "topic": c.topic,
        "engagement_score": c.engagement_score,
        "engagement_metric": _metric_for(c.channel),
        "ae_flag": bool(c.ae_flag),
        "pii_flags": json.loads(c.pii_flags) if c.pii_flags else [],
        "posted_at": c.posted_at.isoformat() if c.posted_at else None,
        "harvested_at": c.harvested_at.isoformat() if c.harvested_at else None,
    }


async def list_comments(db: AsyncSession, post_id: int, *, limit: int = 200) -> list[dict]:
    """Captured comments for one post (most recent first) — backs the drawer's crowd view."""
    stmt = (select(SocialComment).where(SocialComment.post_id == post_id)
            .order_by(SocialComment.harvested_at.desc()).limit(limit))
    rows = (await db.execute(stmt)).scalars().all()
    return [serialize_comment(c) for c in rows]


async def query_comments(db: AsyncSession, *, therapeutic_area: str | None = None,
                         channel: str | None = None, ae_only: bool = False,
                         post_id: int | None = None, limit: int = 25) -> list[dict]:
    """Filterable comment list (copilot read tool). Scopes TA via the parent post."""
    stmt = select(SocialComment).join(SocialPost, SocialComment.post_id == SocialPost.id)
    if therapeutic_area:
        stmt = stmt.where(SocialPost.therapeutic_area == therapeutic_area)
    if channel:
        stmt = stmt.where(SocialComment.channel == channel)
    if ae_only:
        stmt = stmt.where(SocialComment.ae_flag.is_(True))
    if post_id:
        stmt = stmt.where(SocialComment.post_id == post_id)
    stmt = stmt.order_by(SocialComment.harvested_at.desc()).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [serialize_comment(c) for c in rows]


def _new_sent() -> dict:
    return {"n": 0, "_sum": 0.0, "_scored": 0, "positive": 0, "neutral": 0, "negative": 0}


def _sent_bucket(acc: dict, sentiment, label) -> None:
    acc["n"] += 1
    if sentiment is not None:
        acc["_sum"] += sentiment
        acc["_scored"] += 1
    if label == "positive":
        acc["positive"] += 1
    elif label == "negative":
        acc["negative"] += 1
    else:
        acc["neutral"] += 1


def _sent_finalize(key_name: str, key_val: str, acc: dict) -> dict:
    avg = round(acc["_sum"] / acc["_scored"], 3) if acc["_scored"] else None
    return {key_name: key_val, "n": acc["n"], "avg_sentiment": avg,
            "positive": acc["positive"], "neutral": acc["neutral"], "negative": acc["negative"]}


def _bucket_finalize(acc: dict, denom: int) -> dict:
    """Finalize a sentiment accumulator into a public stat block with a within-channel share."""
    return {
        "posts": acc["n"],
        "post_share": round(acc["n"] / denom, 3) if denom else 0.0,
        "avg_sentiment": round(acc["_sum"] / acc["_scored"], 3) if acc["_scored"] else None,
        "positive": acc["positive"], "neutral": acc["neutral"], "negative": acc["negative"],
    }


def _platform_comparison(rows: list[SocialPost], brief_row) -> dict:
    """Per-channel AbbVie-vs-each-competitor-brand breakdown (captured sample; posts only).

    Ownership keys off brands.yaml ``company`` (taxonomy.is_abbvie_brand), so AbbVie-owned
    brands listed as competitors (Orilissa/Oriahnn) still count as AbbVie, and an area with no
    AbbVie asset (Obesity) reports ``abbvie_present=false``. Only brand-attributed posts populate
    the buckets; shares are over the attributed posts within each channel. Channels are ordered
    by captured volume so the most-discussed platform leads. The AI gist per channel is merged
    from the persisted brief (best-effort) — the numbers here never depend on the LLM.
    """
    gists: dict[str, str] = {}
    raw_summaries = getattr(brief_row, "platform_summaries", None) if brief_row is not None else None
    if raw_summaries:
        try:
            parsed = json.loads(raw_summaries)
            if isinstance(parsed, dict):
                gists = {str(k): str(v) for k, v in parsed.items() if v}
        except (TypeError, ValueError):
            gists = {}

    channels: dict[str, dict] = {}
    abbvie_present = False
    for p in rows:
        ch = p.channel or "unknown"
        c = channels.setdefault(ch, {
            "total": 0, "attributed": 0, "unattributed": 0,
            "abbvie": _new_sent(), "abbvie_brands": defaultdict(int),
            "competitors": {},  # brand -> {"acc": sentiment accumulator, "company": str|None}
        })
        c["total"] += 1
        brand = (p.brand_focus or "").strip()
        if not brand:
            c["unattributed"] += 1
            continue
        c["attributed"] += 1
        if is_abbvie_brand(brand):
            abbvie_present = True
            _sent_bucket(c["abbvie"], p.sentiment, p.sentiment_label)
            c["abbvie_brands"][brand] += 1
        else:
            entry = c["competitors"].setdefault(
                brand, {"acc": _new_sent(), "company": company_for(brand)})
            _sent_bucket(entry["acc"], p.sentiment, p.sentiment_label)

    out_channels = []
    for ch, c in channels.items():
        denom = c["attributed"]
        abbvie = _bucket_finalize(c["abbvie"], denom)
        abbvie["brands"] = sorted(c["abbvie_brands"], key=lambda b: c["abbvie_brands"][b], reverse=True)
        competitors = []
        for brand, entry in c["competitors"].items():
            fin = _bucket_finalize(entry["acc"], denom)
            fin["brand"] = brand
            fin["company"] = entry["company"]
            competitors.append(fin)
        competitors.sort(key=lambda r: r["posts"], reverse=True)
        out_channels.append({
            "channel": ch,
            "metric": _metric_for(ch),
            "total_posts": c["total"],
            "attributed_posts": c["attributed"],
            "unattributed_posts": c["unattributed"],
            "abbvie": abbvie,
            "competitors": competitors,
            "gist": gists.get(ch),
        })
    out_channels.sort(key=lambda r: r["total_posts"], reverse=True)
    return {"channels": out_channels, "abbvie_present": abbvie_present}


def _community_insights(rows: list[SocialPost], brief_row) -> dict | None:
    """Aggregate the community-crawl enrichment (myRAteam / Bezzy) into a patient-voice read.

    Only posts carrying community enrichment (``patient_signals`` / ``brand_mentions``)
    contribute, so this returns None for a pure platform ingest and the UI panel hides.
    Unlike ``_platform_comparison`` (one ``brand_focus`` per post), the drug-mention share of
    voice here counts EVERY monitored treatment named on each page. The unmet-question list
    prefers the deduped/clustered brief list, falling back to the per-post questions so the
    panel has content even before an AI brief has been generated.
    """
    community = [p for p in rows if p.patient_signals or p.brand_mentions]
    if not community:
        return None

    concerns: dict[str, int] = defaultdict(int)
    drivers: dict[str, int] = defaultdict(int)
    qol: dict[str, int] = defaultdict(int)
    access: dict[str, int] = defaultdict(int)
    stages: dict[str, int] = defaultdict(int)
    post_questions: list[str] = []
    q_seen: set[str] = set()

    mention_pages: dict[str, int] = defaultdict(int)
    mention_sum: dict[str, float] = defaultdict(float)
    mention_scored: dict[str, int] = defaultdict(int)
    mention_meta: dict[str, dict] = {}

    for p in community:
        sig = _loads_obj(p.patient_signals) or {}
        for label in sig.get("concerns") or []:
            concerns[str(label)] += 1
        for label in sig.get("switching_drivers") or []:
            drivers[str(label)] += 1
        for label in sig.get("qol_impacts") or []:
            qol[str(label)] += 1
        for label in sig.get("access_barriers") or []:
            access[str(label)] += 1
        if sig.get("journey_stage"):
            stages[str(sig["journey_stage"])] += 1
        for q in sig.get("questions") or []:
            key = str(q).strip().lower()
            if key and key not in q_seen:
                q_seen.add(key)
                post_questions.append(str(q).strip())

        seen_brand: set[str] = set()  # count each drug once per page
        for m in _loads_list(p.brand_mentions):
            if not isinstance(m, dict):
                continue
            name = (m.get("name") or "").strip()
            if not name or name.lower() in seen_brand:
                continue
            seen_brand.add(name.lower())
            mention_pages[name] += 1
            sentiment = m.get("sentiment")
            if sentiment is not None:
                try:
                    mention_sum[name] += float(sentiment)
                    mention_scored[name] += 1
                except (TypeError, ValueError):
                    pass
            mention_meta.setdefault(name, {
                "company": m.get("company"),
                "owner": m.get("owner") or ("AbbVie" if is_abbvie_brand(name) else "Competitor"),
            })

    total_mentions = sum(mention_pages.values())
    drug_mentions = sorted([
        {
            "name": name,
            "company": mention_meta[name]["company"],
            "owner": mention_meta[name]["owner"],
            "mentions": n,
            "mention_share": round(n / total_mentions, 3) if total_mentions else 0.0,
            "avg_sentiment": round(mention_sum[name] / mention_scored[name], 3) if mention_scored[name] else None,
        }
        for name, n in mention_pages.items()
    ], key=lambda r: r["mentions"], reverse=True)
    abbvie_mentions = sum(r["mentions"] for r in drug_mentions if r["owner"] == "AbbVie")
    competitor_mentions = total_mentions - abbvie_mentions

    unmet = _loads_list(getattr(brief_row, "unmet_questions", None))
    if not unmet:
        unmet = [{"question": q, "theme": None, "brand": None} for q in post_questions[:12]]

    return {
        "posts": len(community),
        "channels": sorted({p.channel for p in community if p.channel}),
        "concerns": _top_counts(concerns),
        "journey_stages": _top_counts(stages),
        "switching_drivers": _top_counts(drivers),
        "qol_impacts": _top_counts(qol),
        "access_barriers": _top_counts(access),
        "drug_mentions": drug_mentions,
        "drug_sov": {
            "total_mentions": total_mentions,
            "abbvie_mentions": abbvie_mentions,
            "competitor_mentions": competitor_mentions,
            "abbvie_share": round(abbvie_mentions / total_mentions, 3) if total_mentions else 0.0,
            "abbvie_present": abbvie_mentions > 0,
        },
        "unmet_questions": unmet,
    }


async def available_areas(db: AsyncSession) -> list[dict]:
    """Distinct therapeutic areas that have captured social posts, with post counts (desc).

    Backs the copilot's "which area?" prompt: Social Listening insights/ingests are per-area,
    so when no area is named the copilot lists what is actually captured rather than defaulting
    to one hard-coded area.
    """
    rows = (await db.execute(
        select(SocialPost.therapeutic_area, func.count().label("posts"))
        .group_by(SocialPost.therapeutic_area)
        .order_by(func.count().desc())
    )).all()
    return [
        {"therapeutic_area": ta, "posts": int(n)}
        for ta, n in rows
        if ta
    ]


async def insights(db: AsyncSession, *, therapeutic_area: str = "Obesity") -> dict:
    rows = (await db.execute(
        select(SocialPost).where(SocialPost.therapeutic_area == therapeutic_area)
    )).scalars().all()
    # Comments for these posts (separate sentiment dimension; scoped via the parent post's TA).
    crows = (await db.execute(
        select(SocialComment)
        .join(SocialPost, SocialComment.post_id == SocialPost.id)
        .where(SocialPost.therapeutic_area == therapeutic_area)
    )).scalars().all()
    post_brand = {p.id: (p.brand_focus or _UNATTRIBUTED) for p in rows}

    total = len(rows)
    channels = sorted({p.channel for p in rows if p.channel})
    channel_metrics = {c: _metric_for(c) for c in channels}

    # ---- Share of voice (captured sample) ----
    brand_posts: dict[str, int] = defaultdict(int)
    ch_posts: dict[str, int] = defaultdict(int)
    ch_eng: dict[str, int] = defaultdict(int)
    ch_brand_posts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    ch_brand_eng: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for p in rows:
        brand = p.brand_focus or _UNATTRIBUTED
        ch = p.channel or "unknown"
        eng = p.engagement_score or 0
        brand_posts[brand] += 1
        ch_posts[ch] += 1
        ch_eng[ch] += eng
        ch_brand_posts[ch][brand] += 1
        ch_brand_eng[ch][brand] += eng

    by_brand = sorted(
        [{"brand": b, "posts": n, "post_share": round(n / total, 3) if total else 0.0}
         for b, n in brand_posts.items()],
        key=lambda r: r["posts"], reverse=True,
    )
    by_channel = []
    for c in channels:
        cp = ch_posts[c]
        ce = ch_eng[c]
        brands = sorted(
            [{"brand": b, "posts": n,
              "post_share": round(n / cp, 3) if cp else 0.0,
              "engagement": ch_brand_eng[c][b],
              "engagement_share": round(ch_brand_eng[c][b] / ce, 3) if ce else 0.0}
             for b, n in ch_brand_posts[c].items()],
            key=lambda r: r["posts"], reverse=True,
        )
        by_channel.append({"channel": c, "metric": _metric_for(c), "posts": cp,
                           "engagement_total": ce, "brands": brands})

    # ---- Sentiment (by brand, by channel) ----
    sb: dict[str, dict] = defaultdict(_new_sent)
    sc: dict[str, dict] = defaultdict(_new_sent)
    for p in rows:
        _sent_bucket(sb[p.brand_focus or _UNATTRIBUTED], p.sentiment, p.sentiment_label)
        _sent_bucket(sc[p.channel or "unknown"], p.sentiment, p.sentiment_label)
    sentiment_by_brand = sorted(
        [_sent_finalize("brand", b, a) for b, a in sb.items()],
        key=lambda r: r["n"], reverse=True)
    sentiment_by_channel = sorted(
        [_sent_finalize("channel", c, a) for c, a in sc.items()],
        key=lambda r: r["n"], reverse=True)

    # ---- Overall sentiment (whole captured sample) ----
    overall = _new_sent()
    for p in rows:
        _sent_bucket(overall, p.sentiment, p.sentiment_label)
    sentiment_overall = {
        "n": overall["n"],
        "scored": overall["_scored"],
        "avg_sentiment": round(overall["_sum"] / overall["_scored"], 3) if overall["_scored"] else None,
        "positive": overall["positive"],
        "neutral": overall["neutral"],
        "negative": overall["negative"],
    }

    # ---- Comment sentiment (SEPARATE dimension: the crowd's reaction, not the post author) ----
    c_overall = _new_sent()
    csc: dict[str, dict] = defaultdict(_new_sent)
    for c in crows:
        _sent_bucket(c_overall, c.sentiment, c.sentiment_label)
        _sent_bucket(csc[c.channel or "unknown"], c.sentiment, c.sentiment_label)
    comment_sentiment_overall = {
        "n": c_overall["n"],
        "scored": c_overall["_scored"],
        "avg_sentiment": round(c_overall["_sum"] / c_overall["_scored"], 3) if c_overall["_scored"] else None,
        "positive": c_overall["positive"],
        "neutral": c_overall["neutral"],
        "negative": c_overall["negative"],
    }
    comment_sentiment_by_channel = sorted(
        [_sent_finalize("channel", ch, a) for ch, a in csc.items()],
        key=lambda r: r["n"], reverse=True)

    # ---- Volume over time (captured sample) — daily buckets stacked by channel ----
    day_ch: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for p in rows:
        if not p.posted_at:
            continue
        day = p.posted_at.date().isoformat()
        day_ch[day][p.channel or "unknown"] += 1
    vol_rows = []
    for day in sorted(day_ch.keys()):
        row: dict = {"date": day}
        for c in channels:
            row[c] = day_ch[day].get(c, 0)
        vol_rows.append(row)
    volume_over_time = {"channels": channels, "rows": vol_rows}

    # ---- Recent momentum (within the captured sample; anchored on the latest post) ----
    # Captured-sample timestamps can be old/sparse, so we anchor the window on the most
    # recent post rather than wall-clock now, and degrade to None when there isn't enough.
    window = None
    try:
        posted_dates = [p.posted_at for p in rows if p.posted_at]
        if posted_dates:
            as_of = max(posted_dates)
            recent_cut = as_of - timedelta(days=7)
            prior_cut = as_of - timedelta(days=14)
            recent_posts = sum(1 for d in posted_dates if d > recent_cut)
            prior_posts = sum(1 for d in posted_dates if prior_cut < d <= recent_cut)
            delta_pct = round((recent_posts - prior_posts) / prior_posts, 3) if prior_posts else None
            window = {
                "as_of": as_of.isoformat(),
                "recent_days": 7,
                "recent_posts": recent_posts,
                "prior_posts": prior_posts,
                "delta_pct": delta_pct,
            }
    except (TypeError, ValueError):
        window = None

    # ---- Top topics/themes ----
    topic_n: dict[str, int] = defaultdict(int)
    topic_sum: dict[str, float] = defaultdict(float)
    topic_scored: dict[str, int] = defaultdict(int)
    for p in rows:
        if not p.topic:
            continue
        topic_n[p.topic] += 1
        if p.sentiment is not None:
            topic_sum[p.topic] += p.sentiment
            topic_scored[p.topic] += 1
    top_topics = sorted(
        [{"topic": t, "count": n,
          "avg_sentiment": round(topic_sum[t] / topic_scored[t], 3) if topic_scored[t] else None}
         for t, n in topic_n.items()],
        key=lambda r: r["count"], reverse=True)[:12]

    # ---- Adverse-event signals (posts + comments; fail-closed for PV) ----
    ae_posts = ae_comments = 0
    ae_brand: dict[str, int] = defaultdict(int)
    ae_channel: dict[str, int] = defaultdict(int)
    for p in rows:
        if p.ae_flag:
            ae_posts += 1
            ae_brand[p.brand_focus or _UNATTRIBUTED] += 1
            ae_channel[p.channel or "unknown"] += 1
    for c in crows:
        if c.ae_flag:
            ae_comments += 1
            ae_brand[post_brand.get(c.post_id, _UNATTRIBUTED)] += 1
            ae_channel[c.channel or "unknown"] += 1
    ae_total = ae_posts + ae_comments
    ae_denom = total + len(crows)
    adverse_events = {
        "total": ae_total,
        "posts": ae_posts,
        "comments": ae_comments,
        "rate": round(ae_total / ae_denom, 4) if ae_denom else 0.0,
        "by_brand": sorted([{"brand": b, "count": n} for b, n in ae_brand.items()],
                           key=lambda r: r["count"], reverse=True),
        "by_channel": sorted([{"channel": c, "count": n} for c, n in ae_channel.items()],
                             key=lambda r: r["count"], reverse=True),
    }

    # ---- Engagement leaders (per channel, top by RAW engagement; never cross-channel) ----
    engagement_leaders = []
    for c in channels:
        cps = [p for p in rows if p.channel == c]
        cps.sort(key=lambda p: (p.engagement_score or 0), reverse=True)
        top = [{
            "brand": p.brand_focus or _UNATTRIBUTED,
            "topic": p.topic,
            "engagement": p.engagement_score,
            "comment_count": p.comment_count,
            "sentiment": p.sentiment,
            "sentiment_label": p.sentiment_label,
            "snippet": (p.text or "")[:200],
            "post_url": p.post_url,
            "posted_at": p.posted_at.isoformat() if p.posted_at else None,
        } for p in cps[:3]]
        engagement_leaders.append({"channel": c, "metric": _metric_for(c), "posts": top})

    # ---- AI narrative brief (qualitative read of the sample; generated post-ingest) ----
    brief_row = await db.get(SocialBrief, therapeutic_area)
    ai_brief = None
    if brief_row is not None and (brief_row.narrative or brief_row.verbatims):
        verbatims = []
        if brief_row.verbatims:
            try:
                verbatims = json.loads(brief_row.verbatims)
            except (TypeError, ValueError):
                verbatims = []
        ai_brief = {
            "narrative": brief_row.narrative,
            "verbatims": verbatims,
            "unmet_questions": _loads_list(brief_row.unmet_questions),
            "posts_analyzed": brief_row.posts_analyzed,
            "model": brief_row.model,
            "updated_at": brief_row.updated_at.isoformat() if brief_row.updated_at else None,
        }

    # ---- Per-platform "AbbVie vs each competitor brand" comparison (deterministic; AI gist
    # merged from brief_row when present) ----
    platform_comparison = _platform_comparison(list(rows), brief_row)

    # ---- Patient Community Insights (myRAteam / Bezzy enrichment; None for platform-only) ----
    community_insights = _community_insights(list(rows), brief_row)

    return {
        "therapeutic_area": therapeutic_area,
        "basis": BASIS,
        "ai_brief": ai_brief,
        "platform_comparison": platform_comparison,
        "community_insights": community_insights,
        "total_posts": total,
        "channels": channels,
        "channel_metrics": channel_metrics,
        "share_of_voice": {"by_brand": by_brand, "by_channel": by_channel},
        "sentiment_by_brand": sentiment_by_brand,
        "sentiment_by_channel": sentiment_by_channel,
        "sentiment_overall": sentiment_overall,
        "comment_sentiment_overall": comment_sentiment_overall,
        "comment_sentiment_by_channel": comment_sentiment_by_channel,
        "total_comments": len(crows),
        "volume_over_time": volume_over_time,
        "window": window,
        "top_topics": top_topics,
        "adverse_events": adverse_events,
        "engagement_leaders": engagement_leaders,
    }


async def promote_unmet_question(db: AsyncSession, data) -> dict:
    """Stage a community unmet-need question into the Discovery queue (double-gate governance).

    Real patient questions never enter the approved Question Repository directly: this creates
    a HarvestedQuestion staging row (CLASSIFIED, or QUARANTINED_AE for adverse-event content)
    that a reviewer then promotes to a PENDING Question via the existing Discovery flow. PII +
    prompt-injection guards run here as defense in depth (the text is already redacted at
    extraction). Deduped by normalized text, so re-sending the same question returns the
    existing staged row rather than duplicating it.
    """
    text = (data.question or "").strip()
    if len(text) < 8:
        raise HTTPException(422, "Question text is too short to stage.")
    pii = scan_for_pii(text)
    if pii:
        raise HTTPException(422, f"Cannot stage — possible PII detected: {pii}")
    inj = scan_injection(text)
    if inj:
        raise HTTPException(
            422, f"Cannot stage — possible prompt-injection content detected: {inj}")

    norm = " ".join(text.lower().split())
    dedupe_hash = hashlib.sha1(f"social:{norm}".encode("utf-8")).hexdigest()
    existing = (await db.execute(
        select(HarvestedQuestion).where(HarvestedQuestion.dedupe_hash == dedupe_hash)
    )).scalars().first()
    if existing is not None:
        return {"status": "exists", "id": existing.id,
                "harvested_status": existing.status,
                "promoted_question_id": existing.promoted_question_id}

    ae = adverse_event.looks_like_ae(text)
    hq = HarvestedQuestion(
        source="social",
        source_title="Social Listening — patient community",
        search_query=(data.theme or None),
        question_text=text,
        dedupe_hash=dedupe_hash,
        persona=data.persona,
        therapeutic_area=data.therapeutic_area,
        brand_focus=(data.brand or None),
        domain=data.domain,
        intent_type="EXPERIENTIAL",
        # Analyst-curated (explicitly "Sent to Discover"), so it outranks the model-scored web
        # harvest candidates and surfaces at the TOP of the relevance-sorted Discovery queue
        # rather than being buried below the list's fetch window.
        relevance_score=1.0,
        search_persona=data.persona,
        ae_flag=ae,
        status="QUARANTINED_AE" if ae else "CLASSIFIED",
    )
    db.add(hq)
    await db.commit()
    await db.refresh(hq)
    return {"status": "staged", "id": hq.id, "harvested_status": hq.status,
            "ae_flag": ae, "therapeutic_area": data.therapeutic_area}
