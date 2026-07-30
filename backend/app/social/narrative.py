"""AI narrative brief for the Social Listening surface.

Turns the captured social sample (already-redacted, LLM-tagged SocialPost + SocialComment
rows) into a QUALITATIVE read of what people are actually saying: a short prose narrative
plus a handful of representative VERBATIM quotes. This complements the quantitative
aggregates in services/social_service.py, which only ever report the numbers (avg sentiment,
share of voice, theme counts) and never the story behind them.

ANTI-HALLUCINATION DESIGN: the model never authors quotes. It only SELECTS which captured
items are most representative (by index); the verbatim text is then pulled from the stored
(already de-identified) record, so a quote can never be fabricated. The prose narrative is
model-authored but constrained to the supplied sample.

Reuses the configured scoring model via insights.llm.chat_json. Best-effort: any failure
degrades to leaving the previous brief in place rather than breaking the ingest.
"""
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.taxonomy import is_abbvie_brand
from app.insights.llm import chat_json
from app.models.social_brief import SocialBrief
from app.models.social_comment import SocialComment
from app.models.social_post import SocialPost
from app.providers.registry import get_scoring_config
from app.utils.logging import get_logger

logger = get_logger("social.narrative")

# Bounds on how much of the sample we feed the model (keeps token cost predictable).
_MAX_POSTS = 60
_MAX_COMMENTS = 30
_SNIPPET_LEN = 240
_VERBATIM_LEN = 300
_UNATTRIBUTED = "Unattributed"


_SYSTEM = (
    "You are a pharmaceutical social-listening analyst. You receive REAL public social "
    "media posts and comments (already de-identified) about pharmaceutical brands and the "
    "health conditions they treat. Your job is to tell a brand team WHAT PEOPLE ARE "
    "ACTUALLY SAYING, beyond the sentiment numbers they already have.\n\n"
    "Write TWO things and return STRICT JSON only (no prose outside the JSON):\n"
    "1. narrative: 2 short paragraphs (about 110-150 words total) in plain, concrete "
    "English. Cover the recurring themes, the main complaints and the main praise, common "
    "questions, any switching/comparison talk between brands, and notable differences by "
    "channel. Explain the WHY behind the sentiment, citing specifics that appear in the "
    "sample. Do NOT invent facts, numbers, or quotes that are not present. Do NOT use em "
    "dashes; use periods, commas, or parentheses instead.\n"
    "2. verbatims: choose 4 to 6 items from the numbered list that are the most "
    "representative or revealing examples (aim for a spread of sentiment and channels, and "
    "include any clear adverse-event mention). For EACH, return its index and a very short "
    "label (max 8 words) of what it illustrates. Do not rewrite the quote text."
)


def _clip(text: str | None, n: int) -> str:
    t = (text or "").strip().replace("\n", " ")
    return t[:n].rstrip() + ("…" if len(t) > n else "")


def _select_posts(posts: list[SocialPost]) -> list[SocialPost]:
    """Pick a representative, diverse subset of posts (AE + engagement + negative + recent)."""
    scored = [p for p in posts if (p.text or "").strip()]
    picked: dict[int, SocialPost] = {}

    def take(items: list[SocialPost], cap: int) -> None:
        for p in items:
            if len(picked) >= _MAX_POSTS:
                break
            if p.id not in picked:
                picked[p.id] = p
                if cap <= 0:
                    break
                cap -= 1

    _epoch = datetime.min.replace(tzinfo=timezone.utc)
    take([p for p in scored if p.ae_flag], 10)
    take(sorted(scored, key=lambda p: (p.engagement_score or 0), reverse=True), 22)
    take(sorted(scored, key=lambda p: (p.sentiment if p.sentiment is not None else 1.0)), 16)
    take(sorted(scored, key=lambda p: (p.sentiment if p.sentiment is not None else -1.0), reverse=True), 8)
    take(sorted(scored, key=lambda p: (p.harvested_at or _epoch), reverse=True), _MAX_POSTS)
    return list(picked.values())


def _select_comments(comments: list[SocialComment]) -> list[SocialComment]:
    """Pick a smaller subset of comments (AE + most engaged + most negative)."""
    scored = [c for c in comments if (c.text or "").strip()]
    picked: dict[int, SocialComment] = {}

    def take(items: list[SocialComment], cap: int) -> None:
        for c in items:
            if len(picked) >= _MAX_COMMENTS:
                break
            if c.id not in picked:
                picked[c.id] = c
                if cap <= 0:
                    break
                cap -= 1

    take([c for c in scored if c.ae_flag], 8)
    take(sorted(scored, key=lambda c: (c.engagement_score or 0), reverse=True), 14)
    take(sorted(scored, key=lambda c: (c.sentiment if c.sentiment is not None else 1.0)), _MAX_COMMENTS)
    return list(picked.values())


def _verbatim_from_candidate(cand: dict, why: str) -> dict:
    return {
        "quote": _clip(cand["text"], _VERBATIM_LEN),
        "channel": cand["channel"],
        "brand": cand["brand"],
        "sentiment": cand["sentiment"],
        "sentiment_label": cand["sentiment_label"],
        "topic": cand["topic"],
        "ae_flag": cand["ae_flag"],
        "kind": cand["kind"],
        "why": (why or "").strip()[:80] or None,
    }


async def generate_social_brief(db: AsyncSession, *, therapeutic_area: str = "Obesity") -> dict:
    """Synthesize + persist the narrative brief for one therapeutic area.

    Returns a small status dict. Never raises: on any error it logs and returns
    ``{"status": "error"|"empty"|"skipped", ...}`` so the caller's ingest can't fail.
    """
    posts = (await db.execute(
        select(SocialPost).where(SocialPost.therapeutic_area == therapeutic_area)
    )).scalars().all()
    if not posts:
        return {"status": "empty", "reason": "no posts captured for this area yet"}

    comments = (await db.execute(
        select(SocialComment)
        .join(SocialPost, SocialComment.post_id == SocialPost.id)
        .where(SocialPost.therapeutic_area == therapeutic_area)
    )).scalars().all()

    sel_posts = _select_posts(list(posts))
    sel_comments = _select_comments(list(comments))

    # Unified, indexed candidate pool. The index is what the model selects against and what
    # we resolve verbatims from (so the quote text is always the real stored text).
    candidates: list[dict] = []
    for p in sel_posts:
        candidates.append({
            "kind": "post", "channel": p.channel,
            "brand": p.brand_focus or _UNATTRIBUTED,
            "sentiment": p.sentiment, "sentiment_label": p.sentiment_label,
            "topic": p.topic, "ae_flag": bool(p.ae_flag), "text": p.text,
        })
    for c in sel_comments:
        candidates.append({
            "kind": "comment", "channel": c.channel,
            "brand": _UNATTRIBUTED,
            "sentiment": c.sentiment, "sentiment_label": c.sentiment_label,
            "topic": c.topic, "ae_flag": bool(c.ae_flag), "text": c.text,
        })
    if not candidates:
        return {"status": "empty", "reason": "no usable text in the captured sample"}

    lines = []
    for i, cand in enumerate(candidates):
        tags = []
        if cand["brand"] and cand["brand"] != _UNATTRIBUTED:
            tags.append(cand["brand"])
        if cand["sentiment_label"]:
            tags.append(cand["sentiment_label"])
        if cand["ae_flag"]:
            tags.append("possible AE")
        meta = f" [{cand['channel']}" + (f", {', '.join(tags)}" if tags else "") + "]"
        lines.append(f"{i}. ({cand['kind']}){meta} {_clip(cand['text'], _SNIPPET_LEN)}")
    numbered = "\n".join(lines)

    user = (
        f"Therapeutic area: {therapeutic_area}.\n"
        f"Captured public posts and comments ({len(candidates)} items):\n{numbered}\n\n"
        "Return STRICT JSON of the form: {\"narrative\": \"...\", \"verbatims\": "
        "[{\"id\": <index int>, \"why\": \"short label\"}]}."
    )

    try:
        data = await chat_json(_SYSTEM, user, max_tokens=1400)
    except Exception as e:  # noqa: BLE001 — leave any previous brief in place
        logger.warning("social brief generation failed for %s: %s", therapeutic_area, e)
        return {"status": "error", "reason": str(e)}

    if not isinstance(data, dict):
        return {"status": "error", "reason": "model did not return an object"}

    narrative = data.get("narrative")
    narrative = narrative.strip() if isinstance(narrative, str) else None

    raw_verbatims = data.get("verbatims")
    verbatims: list[dict] = []
    seen: set[int] = set()
    if isinstance(raw_verbatims, list):
        for v in raw_verbatims:
            if not isinstance(v, dict):
                continue
            idx = v.get("id")
            if not isinstance(idx, int) or idx < 0 or idx >= len(candidates) or idx in seen:
                continue
            seen.add(idx)
            verbatims.append(_verbatim_from_candidate(candidates[idx], str(v.get("why") or "")))

    if not narrative and not verbatims:
        return {"status": "error", "reason": "model returned nothing usable"}

    model_id = None
    try:
        model_id = get_scoring_config().model_id
    except Exception:  # noqa: BLE001 — provenance only
        model_id = None

    brief = await db.get(SocialBrief, therapeutic_area)
    if brief is None:
        brief = SocialBrief(therapeutic_area=therapeutic_area)
        db.add(brief)
    brief.narrative = narrative
    brief.verbatims = json.dumps(verbatims) if verbatims else None
    brief.posts_analyzed = len(candidates)
    brief.model = model_id
    brief.updated_at = datetime.now(timezone.utc)
    await db.commit()

    logger.info("social brief generated for %s: %d verbatims, %d items analyzed",
                therapeutic_area, len(verbatims), len(candidates))
    return {"status": "ok", "verbatims": len(verbatims), "analyzed": len(candidates)}


# Per-platform gist: brand-attributed channels get an "AbbVie vs other brands" read; channels
# with posts but no brand attribution (e.g. general community-site crawls) get a "what this
# community is discussing" read instead. --------------------------------------------------
_MAX_PLATFORM_POSTS = 18  # brand-attributed channels, per channel (bounds the single call)
_MAX_GENERAL_POSTS = 12   # general (unattributed) channels, per channel

_PLATFORM_SYSTEM = (
    "You are a pharmaceutical social-listening analyst. Below are one or more social "
    "platforms. Each platform shows its name on a 'Platform:' line, then a 'Kind:' line, then "
    "its REAL public posts (already de-identified). Handle the two kinds differently:\n"
    "1. BRAND-ATTRIBUTED: each post is tagged with the brand discussed, that brand's OWNER "
    "(AbbVie or Competitor), and the sentiment. Write a tight, concrete gist of what THAT "
    "platform is saying about AbbVie's brand(s) versus the specific competitor brands present. "
    "Contrast the two sides: who is praised or criticized and why, and any switching or "
    "head-to-head comparison talk. Name the specific brands. If a platform has no AbbVie posts, "
    "say so briefly and summarize the competitor conversation instead.\n"
    "2. GENERAL COMMUNITY: the posts carry NO brand (tagged only with sentiment and topic). "
    "Write a gist of what this community is discussing: the recurring themes, concerns, "
    "questions, and overall tone. Do NOT name or invent any brand for these platforms.\n\n"
    "For EVERY platform keep the gist to 1-2 sentences (about 45 words max). Reference ONLY "
    "brands that appear in that platform's own list. Do NOT invent facts, numbers, or quotes. "
    "Do NOT use em dashes; use periods, commas, or parentheses instead.\n\n"
    "Return STRICT JSON only (no prose outside the JSON): "
    '{"platforms": {"<channel>": "gist text"}} using the exact platform name from each '
    "'Platform:' line as the key."
)


async def generate_platform_summaries(db: AsyncSession, *, therapeutic_area: str = "Obesity") -> dict:
    """Synthesize + persist a per-platform gist for one therapeutic area.

    A second, dedicated LLM pass (separate from generate_social_brief) grouped by channel.
    Channels with brand-attributed posts get an "AbbVie vs competitor brands" gist; channels
    that captured posts but no brand attribution (e.g. general community-site crawls such as
    myRAteam / Bezzy RA) get a general "what this community is discussing" gist instead, so
    every platform with usable text gets a read. Only POSTS are used (comments carry no
    channel-level brand). Never raises: on any error it logs and returns a small status dict so
    the caller's ingest can't fail, leaving any previous summaries in place.
    """
    posts = (await db.execute(
        select(SocialPost).where(SocialPost.therapeutic_area == therapeutic_area)
    )).scalars().all()
    if not posts:
        return {"status": "empty", "reason": "no posts captured for this area yet"}

    # Split posts (with text) into brand-attributed vs unattributed, grouped by channel.
    attributed: dict[str, list[SocialPost]] = {}
    with_text: dict[str, list[SocialPost]] = {}
    for p in posts:
        if not (p.text or "").strip():
            continue
        ch = p.channel or "unknown"
        with_text.setdefault(ch, []).append(p)
        if (p.brand_focus or "").strip():
            attributed.setdefault(ch, []).append(p)
    if not with_text:
        return {"status": "empty", "reason": "no usable posts to summarize"}

    # A channel gets a general-community section only when it has NO brand-attributed posts
    # (an attributed channel is already covered by its brand-vs-brand section).
    general = {ch: ps for ch, ps in with_text.items() if ch not in attributed}

    sections: list[str] = []
    # Brand-attributed platforms first (most valuable), ordered by attributed volume.
    for ch in sorted(attributed, key=lambda c: len(attributed[c]), reverse=True):
        sel = _select_posts(attributed[ch])[:_MAX_PLATFORM_POSTS]
        if not sel:
            continue
        lines = []
        for p in sel:
            brand = (p.brand_focus or "").strip() or _UNATTRIBUTED
            owner = "AbbVie" if is_abbvie_brand(brand) else "Competitor"
            tags = [owner, brand]
            if p.sentiment_label:
                tags.append(p.sentiment_label)
            if p.ae_flag:
                tags.append("possible AE")
            lines.append(f"  - [{', '.join(tags)}] {_clip(p.text, _SNIPPET_LEN)}")
        sections.append(f"Platform: {ch}\nKind: BRAND-ATTRIBUTED\n" + "\n".join(lines))

    # General-community platforms (no brand attributed), ordered by post volume.
    for ch in sorted(general, key=lambda c: len(general[c]), reverse=True):
        sel = _select_posts(general[ch])[:_MAX_GENERAL_POSTS]
        if not sel:
            continue
        lines = []
        for p in sel:
            tags = []
            if p.sentiment_label:
                tags.append(p.sentiment_label)
            if p.topic:
                tags.append(p.topic)
            if p.ae_flag:
                tags.append("possible AE")
            meta = f" [{', '.join(tags)}]" if tags else ""
            lines.append(f"  -{meta} {_clip(p.text, _SNIPPET_LEN)}")
        sections.append(f"Platform: {ch}\nKind: GENERAL COMMUNITY (no brand attributed)\n"
                        + "\n".join(lines))

    if not sections:
        return {"status": "empty", "reason": "no usable posts to summarize"}

    sent_channels = set(attributed) | set(general)
    user = (
        f"Therapeutic area: {therapeutic_area}.\n\n"
        + "\n\n".join(sections)
        + "\n\nReturn STRICT JSON: {\"platforms\": {\"<channel>\": \"1-2 sentence gist\"}}."
    )

    try:
        data = await chat_json(_PLATFORM_SYSTEM, user, max_tokens=1500)
    except Exception as e:  # noqa: BLE001 — leave any previous summaries in place
        logger.warning("platform summaries generation failed for %s: %s", therapeutic_area, e)
        return {"status": "error", "reason": str(e)}

    if not isinstance(data, dict):
        return {"status": "error", "reason": "model did not return an object"}
    platforms = data.get("platforms")
    if not isinstance(platforms, dict):
        return {"status": "error", "reason": "model returned no platforms object"}

    # Keep only channels we actually sent (anti-hallucination), clip length, drop empties.
    summaries: dict[str, str] = {}
    for ch, text_val in platforms.items():
        key = str(ch)
        if key in sent_channels and isinstance(text_val, str) and text_val.strip():
            summaries[key] = text_val.strip()[:600]
    if not summaries:
        return {"status": "error", "reason": "model returned nothing usable"}

    brief = await db.get(SocialBrief, therapeutic_area)
    if brief is None:
        brief = SocialBrief(therapeutic_area=therapeutic_area)
        db.add(brief)
    brief.platform_summaries = json.dumps(summaries)
    brief.updated_at = datetime.now(timezone.utc)
    await db.commit()

    logger.info("platform summaries generated for %s: %d platforms (%d attributed, %d general)",
                therapeutic_area, len(summaries), len(attributed), len(general))
    return {"status": "ok", "platforms": len(summaries),
            "attributed": len(attributed), "general": len(general)}


# Unmet-need questions: cluster the per-post questions the community pass extracted from the
# patient-community crawls (myRAteam / Bezzy) into a concise voice-of-patient list. --------
_MAX_UNMET_INPUT = 60   # raw questions fed to the clustering call
_MAX_UNMET_OUTPUT = 12  # distinct questions persisted

_UNMET_SYSTEM = (
    "You are a patient-insights analyst. Below are REAL questions patients asked in public "
    "rheumatoid-arthritis community sites (already de-identified). Cluster near-duplicates and "
    "return a concise, DEDUPLICATED list of the distinct questions patients are asking, each "
    "rewritten as one clear standalone question. Return STRICT JSON only.\n"
    "For each distinct question return: {question, theme (a 1-3 word topic such as 'side "
    "effects', 'switching', 'cost/access', 'diet', 'efficacy', 'pregnancy'), brand (a specific "
    "drug name ONLY when the question is about one, else null)}. Merge duplicates and keep the "
    "clearest phrasing. Return at most 12 questions. Do NOT invent questions that are not "
    "represented in the list. Do NOT use em dashes; use periods, commas, or parentheses."
)


def _collect_community_questions(posts: list[SocialPost]) -> list[str]:
    """Deduplicated per-post questions extracted by the community enrichment pass."""
    out: list[str] = []
    seen: set[str] = set()
    for p in posts:
        if not p.patient_signals:
            continue
        try:
            sig = json.loads(p.patient_signals)
        except (TypeError, ValueError):
            continue
        if not isinstance(sig, dict):
            continue
        for q in sig.get("questions") or []:
            s = str(q).strip()
            key = s.lower()
            if len(s) >= 8 and key not in seen:
                seen.add(key)
                out.append(s)
    return out


async def generate_unmet_questions(db: AsyncSession, *, therapeutic_area: str = "Obesity") -> dict:
    """Cluster + persist the community unmet-need questions for one therapeutic area.

    Reads the per-post questions the community pass stored on ``SocialPost.patient_signals``
    (patient-community crawls only), clusters near-duplicates via one LLM call into distinct
    voice-of-patient questions with a theme + optional brand, and persists them to
    ``SocialBrief.unmet_questions``. Best-effort: on any LLM error it falls back to the
    deduplicated raw questions rather than failing, and returns a small status dict so the
    caller's ingest can't fail. Returns ``{"status": "empty"}`` when no community questions
    were captured (a pure platform ingest), leaving any previous list in place.
    """
    posts = (await db.execute(
        select(SocialPost).where(SocialPost.therapeutic_area == therapeutic_area)
    )).scalars().all()
    raw_qs = _collect_community_questions(list(posts))
    if not raw_qs:
        return {"status": "empty", "reason": "no community questions captured for this area"}

    # Deterministic fallback (used verbatim if the clustering call fails or returns nothing).
    result_qs = [{"question": q, "theme": None, "brand": None} for q in raw_qs[:_MAX_UNMET_OUTPUT]]

    numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(raw_qs[:_MAX_UNMET_INPUT]))
    user = (
        f"Therapeutic area: {therapeutic_area}.\nPatient questions:\n{numbered}\n\n"
        "Return STRICT JSON: {\"questions\": [{\"question\": \"...\", \"theme\": \"...\", "
        "\"brand\": null}]}."
    )
    try:
        data = await chat_json(_UNMET_SYSTEM, user, max_tokens=1200)
        items = None
        if isinstance(data, dict):
            items = data.get("questions") or data.get("items")
        elif isinstance(data, list):
            items = data
        if isinstance(items, list):
            cleaned: list[dict] = []
            seen: set[str] = set()
            for it in items:
                if isinstance(it, dict):
                    q = str(it.get("question") or "").strip()
                    theme = it.get("theme")
                    brand = it.get("brand")
                elif isinstance(it, str):
                    q, theme, brand = it.strip(), None, None
                else:
                    continue
                key = q.lower()
                if len(q) < 8 or key in seen:
                    continue
                seen.add(key)
                cleaned.append({
                    "question": q[:200],
                    "theme": (str(theme).strip()[:40] or None) if theme else None,
                    "brand": (str(brand).strip()[:64] or None) if brand else None,
                })
                if len(cleaned) >= _MAX_UNMET_OUTPUT:
                    break
            if cleaned:
                result_qs = cleaned
    except Exception as e:  # noqa: BLE001 — fall back to the deduped raw questions
        logger.warning("unmet questions clustering failed for %s: %s", therapeutic_area, e)

    brief = await db.get(SocialBrief, therapeutic_area)
    if brief is None:
        brief = SocialBrief(therapeutic_area=therapeutic_area)
        db.add(brief)
    brief.unmet_questions = json.dumps(result_qs)
    brief.updated_at = datetime.now(timezone.utc)
    await db.commit()

    logger.info("unmet questions generated for %s: %d distinct (from %d raw)",
                therapeutic_area, len(result_qs), len(raw_qs))
    return {"status": "ok", "questions": len(result_qs), "raw": len(raw_qs)}
