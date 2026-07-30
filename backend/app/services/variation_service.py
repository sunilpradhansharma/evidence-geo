"""Question Variations service — generate, review, promote, run, and roll up results.

Flow (compliance-gated, mirrors harvested-question double-gate):
  1. generate_for_question : Claude drafts N paraphrases -> staged as DRAFT rows (never run yet)
  2. edit / approve / reject: human review; approve promotes a draft to an APPROVED Question
     tagged into the base question's variation group
  3. build_group_run       : run the base + approved variations together (reuses run pipeline)
  4. group_results         : roll responses up by variation and compute a divergence summary
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.question import Question
from app.models.question_variation import QuestionVariation
from app.models.response import Response
from app.models.scoring import ScoringRecord
from app.schemas import RunCreate
from app.utils.audit import write_audit
from app.utils.logging import get_logger
from app.utils.pii_lint import scan_for_pii
from app.variations import generator

logger = get_logger("variation_service")

_ANSWER_EXCERPT = 280


# --- small helpers ---------------------------------------------------------------
def _hash(text: str) -> str:
    return hashlib.sha1(generator.normalize(text).encode("utf-8")).hexdigest()[:16]


def _mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def _modal(xs: list[str]) -> str | None:
    xs = [x for x in xs if x]
    return Counter(xs).most_common(1)[0][0] if xs else None


def _competitor_focus(q: Question) -> list[str] | None:
    if not q.competitor_focus:
        return None
    try:
        parsed = json.loads(q.competitor_focus)
        return parsed if isinstance(parsed, list) else [str(parsed)]
    except (ValueError, TypeError):
        return [q.competitor_focus]


async def _current_question(db: AsyncSession, question_id: str) -> Question | None:
    """The current (non-superseded, non-deleted) row for a logical question_id."""
    stmt = select(Question).where(
        Question.question_id == question_id,
        Question.deleted_at.is_(None),
        Question.superseded_by.is_(None),
    )
    return (await db.execute(stmt)).scalars().first()


async def _latest_scores_map(db: AsyncSession, response_ids: list[str]) -> dict[str, ScoringRecord]:
    """Latest scoring record per response (max score_version)."""
    if not response_ids:
        return {}
    subq = (
        select(ScoringRecord.response_id, func.max(ScoringRecord.score_version).label("maxv"))
        .where(ScoringRecord.response_id.in_(response_ids))
        .group_by(ScoringRecord.response_id)
        .subquery()
    )
    stmt = select(ScoringRecord).join(
        subq,
        (ScoringRecord.response_id == subq.c.response_id)
        & (ScoringRecord.score_version == subq.c.maxv),
    )
    return {r.response_id: r for r in (await db.execute(stmt)).scalars().all()}


def _serialize_variation(v: QuestionVariation) -> dict:
    return {
        "id": v.id,
        "variation_group_id": v.variation_group_id,
        "base_question_id": v.base_question_id,
        "variation_text": v.variation_text,
        "dedupe_hash": v.dedupe_hash,
        "generation_method": v.generation_method,
        "generation_model": v.generation_model,
        "pii_flags": json.loads(v.pii_flags) if v.pii_flags else None,
        "status": v.status,
        "promoted_question_id": v.promoted_question_id,
        "reviewer_name": v.reviewer_name,
        "review_note": v.review_note,
        "edited": v.edited,
        "created_at": v.created_at,
        "updated_at": v.updated_at,
    }


# --- 1. generate + stage ---------------------------------------------------------
async def generate_for_question(
    db: AsyncSession, base_row_id: int, *, n: int = 4, reviewer_name: str | None = None
) -> dict:
    """Generate N paraphrases for a base question and stage them as DRAFT rows."""
    base = await db.get(Question, base_row_id)
    if base is None or base.deleted_at is not None:
        return {"error": "not_found"}

    group_id = base.question_id
    # Stamp the base into its own group so it appears alongside its variations.
    if base.variation_group_id != group_id:
        base.variation_group_id = group_id
        await db.flush()

    texts, model_id = await generator.generate_variations(
        question_text=base.question_text,
        persona=base.persona,
        therapeutic_area=base.therapeutic_area,
        brand_focus=base.brand_focus,
        domain=base.domain,
        monitoring_mode=base.monitoring_mode,
        competitor_focus=_competitor_focus(base),
        n=n,
    )

    # Existing dedupe keys for this group (drafts + already-approved variation texts).
    existing = {
        h for (h,) in (
            await db.execute(
                select(QuestionVariation.dedupe_hash).where(
                    QuestionVariation.variation_group_id == group_id
                )
            )
        ).all()
    }

    created: list[QuestionVariation] = []
    for text in texts:
        h = _hash(text)
        if h in existing:
            continue
        existing.add(h)
        pii = scan_for_pii(text)
        row = QuestionVariation(
            variation_group_id=group_id,
            base_question_id=group_id,
            variation_text=text,
            dedupe_hash=h,
            generation_method="CLAUDE",
            generation_model=model_id,
            pii_flags=json.dumps(pii) if pii else None,
            status="DRAFT",
        )
        db.add(row)
        created.append(row)

    await db.flush()
    await write_audit(
        db, role="SYSTEM", event="VARIATIONS_GENERATED", question_id=group_id,
        context={"created": len(created), "requested": n, "model": model_id,
                 "reviewer": reviewer_name},
        commit=False,
    )
    await db.commit()
    for row in created:
        await db.refresh(row)
    return {
        "group_id": group_id,
        "base_question_id": group_id,
        "created": len(created),
        "variations": [_serialize_variation(v) for v in created],
    }


# --- 2. review: edit / approve / reject ------------------------------------------
async def edit_variation(db: AsyncSession, var_id: int, new_text: str) -> dict | None:
    v = await db.get(QuestionVariation, var_id)
    if v is None:
        return None
    if v.status != "DRAFT":
        return {"error": "not_draft", "status": v.status}
    v.variation_text = new_text.strip()
    v.dedupe_hash = _hash(v.variation_text)
    v.edited = True
    pii = scan_for_pii(v.variation_text)
    v.pii_flags = json.dumps(pii) if pii else None
    await db.commit()
    await db.refresh(v)
    return _serialize_variation(v)


async def approve_variation(
    db: AsyncSession, var_id: int, *, reviewer_name: str | None = None, note: str | None = None
) -> dict | None:
    """Promote a DRAFT variation to an APPROVED Question in the base question's group."""
    v = await db.get(QuestionVariation, var_id)
    if v is None:
        return None
    if v.status != "DRAFT":
        return {"error": "not_draft", "status": v.status}

    pii = scan_for_pii(v.variation_text)
    if pii:
        v.pii_flags = json.dumps(pii)
        await db.commit()
        return {"error": "pii_detected", "pii_flags": pii}

    base = await _current_question(db, v.base_question_id)
    if base is None:
        return {"error": "base_not_found"}

    from app.services.question_service import _new_question_id  # local import avoids a cycle

    new_q = Question(
        question_id=_new_question_id(),
        question_text=v.variation_text,
        persona=base.persona,
        therapeutic_area=base.therapeutic_area,
        indication=base.indication,
        disease=base.disease,
        brand_focus=base.brand_focus,
        monitoring_mode=base.monitoring_mode,
        competitor_focus=base.competitor_focus,
        domain=base.domain,
        approval_status="APPROVED",
        approver_name=reviewer_name,
        active=True,
        priority_weight=base.priority_weight,
        version=1,
        variation_group_id=v.variation_group_id,
        variation_of=v.base_question_id,
        is_variation=True,
        generation_method=v.generation_method,
    )
    db.add(new_q)
    await db.flush()

    v.status = "APPROVED"
    v.promoted_question_id = new_q.question_id
    v.reviewer_name = reviewer_name
    v.review_note = note
    await write_audit(
        db, role="REVIEWER", event="VARIATION_APPROVED", question_id=v.base_question_id,
        context={"variation_id": v.id, "promoted_question_id": new_q.question_id,
                 "reviewer": reviewer_name, "edited": v.edited},
        commit=False,
    )
    await db.commit()
    await db.refresh(v)
    out = _serialize_variation(v)
    out["promoted_question_id"] = new_q.question_id
    return out


async def reject_variation(
    db: AsyncSession, var_id: int, *, reviewer_name: str | None = None, note: str | None = None
) -> dict | None:
    v = await db.get(QuestionVariation, var_id)
    if v is None:
        return None
    if v.status == "APPROVED":
        return {"error": "already_approved"}
    v.status = "REJECTED"
    v.reviewer_name = reviewer_name
    v.review_note = note
    await write_audit(
        db, role="REVIEWER", event="VARIATION_REJECTED", question_id=v.base_question_id,
        context={"variation_id": v.id, "reviewer": reviewer_name, "note": note},
        commit=False,
    )
    await db.commit()
    await db.refresh(v)
    return _serialize_variation(v)


# --- listing ---------------------------------------------------------------------
async def list_group(db: AsyncSession, group_id: str) -> dict:
    base = await _current_question(db, group_id)
    drafts = (await db.execute(
        select(QuestionVariation)
        .where(QuestionVariation.variation_group_id == group_id)
        .order_by(QuestionVariation.created_at)
    )).scalars().all()
    approved_qs = (await db.execute(
        select(Question).where(
            Question.variation_group_id == group_id,
            Question.is_variation.is_(True),
            Question.deleted_at.is_(None),
            Question.superseded_by.is_(None),
        ).order_by(Question.created_at)
    )).scalars().all()

    counts = Counter(v.status for v in drafts)
    return {
        "group_id": group_id,
        "base": _base_summary(base),
        "drafts": [_serialize_variation(v) for v in drafts],
        "approved_variation_count": len(approved_qs),
        "counts": {"draft": counts.get("DRAFT", 0), "approved": counts.get("APPROVED", 0),
                   "rejected": counts.get("REJECTED", 0)},
    }


def _base_summary(base: Question | None) -> dict | None:
    if base is None:
        return None
    return {
        "question_id": base.question_id,
        "question_text": base.question_text,
        "persona": base.persona,
        "therapeutic_area": base.therapeutic_area,
        "brand_focus": base.brand_focus,
        "domain": base.domain,
        "monitoring_mode": base.monitoring_mode,
        "approval_status": base.approval_status,
    }


async def list_groups(db: AsyncSession) -> dict:
    """All variation groups (any base question that has generated drafts), with counts."""
    group_ids = [
        gid for (gid,) in (
            await db.execute(
                select(QuestionVariation.variation_group_id)
                .group_by(QuestionVariation.variation_group_id)
            )
        ).all()
    ]
    groups = []
    for gid in group_ids:
        base = await _current_question(db, gid)
        drafts = (await db.execute(
            select(QuestionVariation.status).where(QuestionVariation.variation_group_id == gid)
        )).scalars().all()
        counts = Counter(drafts)
        groups.append({
            "group_id": gid,
            "base_question_text": base.question_text if base else None,
            "persona": base.persona if base else None,
            "therapeutic_area": base.therapeutic_area if base else None,
            "brand_focus": base.brand_focus if base else None,
            "draft_count": counts.get("DRAFT", 0),
            "approved_count": counts.get("APPROVED", 0),
            "rejected_count": counts.get("REJECTED", 0),
            "total": len(drafts),
        })
    groups.sort(key=lambda g: g["draft_count"], reverse=True)
    return {"count": len(groups), "groups": groups}


# --- 3. group run ----------------------------------------------------------------
async def _approved_variation_ids(
    db: AsyncSession, group_ids: list[str]
) -> dict[str, list[str]]:
    """Which variation question_ids of each group are allowed to run — the review gate.

    Stated ONCE here so a single-group run and a multi-question expansion can never disagree
    about what a human has cleared. The orchestrator executes explicit question_ids regardless
    of approval, so this filter is the only thing standing between an unreviewed paraphrase and
    a monitored model. Batched: one query for every group.
    """
    if not group_ids:
        return {}
    rows = (await db.execute(
        select(Question.variation_group_id, Question.question_id)
        .where(
            Question.variation_group_id.in_(group_ids),
            Question.is_variation.is_(True),
            Question.approval_status == "APPROVED",
            Question.active.is_(True),
            Question.deleted_at.is_(None),
            Question.superseded_by.is_(None),
        )
        .order_by(Question.created_at, Question.id)
    )).all()
    out: dict[str, list[str]] = {gid: [] for gid in group_ids}
    for gid, qid in rows:
        out.setdefault(gid, []).append(qid)
    return out


async def build_group_run(
    db: AsyncSession, group_id: str, *, include_base: bool = True, dry_run: bool = False
) -> tuple[RunCreate | None, list[str]]:
    """Build a RunCreate scoped to the base + approved variation question_ids of a group.

    Returns (None, []) when there is nothing runnable so the caller can 400. The orchestrator
    runs explicit question_ids regardless of approval, so we include ONLY approved variations
    to preserve the review gate."""
    base = await _current_question(db, group_id)
    ids: list[str] = []
    if include_base and base is not None:
        ids.append(base.question_id)

    approved = (await _approved_variation_ids(db, [group_id])).get(group_id, [])
    ids.extend(approved)
    ids = list(dict.fromkeys(ids))  # dedupe, preserve order
    if not ids:
        return None, []

    mode = base.monitoring_mode if base else "BRAND"
    return RunCreate(trigger="ADHOC", monitoring_mode=mode, question_ids=ids, dry_run=dry_run), ids


async def expand_with_variations(db: AsyncSession, question_ids: list[str]) -> dict:
    """Expand a bank selection into itself + each question's APPROVED variations.

    Read-only: nothing is created, approved, or run here. The caller shows these exact numbers
    to the reviewer and then runs this exact id list, so the size promised before the click is
    the size bought after it.

    Per requested question we also report how many staged drafts are still PENDING and how many
    were REJECTED. That matters because the bank's "N variations" chip counts every staged
    status, so a question advertising six variations can legitimately expand to none that may
    run — and the UI has to be able to say so instead of quietly running the original alone.
    """
    requested = list(dict.fromkeys(q for q in question_ids if q))
    if not requested:
        return {"question_ids": [], "base_count": 0, "variation_count": 0, "total": 0,
                "groups": [], "missing": []}

    rows = (await db.execute(
        select(Question).where(
            Question.question_id.in_(requested),
            Question.deleted_at.is_(None),
            Question.superseded_by.is_(None),
        )
    )).scalars().all()
    by_qid = {q.question_id: q for q in rows}
    missing = [qid for qid in requested if qid not in by_qid]

    # A requested id that is itself a variation runs alone — expanding it would pull in its
    # siblings, which is not what selecting one row asked for.
    base_ids = [qid for qid in requested if qid in by_qid and not by_qid[qid].is_variation]
    approved_by_group = await _approved_variation_ids(db, base_ids)

    status_counts: dict[str, Counter] = {}
    if base_ids:
        counted = (await db.execute(
            select(QuestionVariation.variation_group_id, QuestionVariation.status, func.count())
            .where(QuestionVariation.variation_group_id.in_(base_ids))
            .group_by(QuestionVariation.variation_group_id, QuestionVariation.status)
        )).all()
        for gid, status, n in counted:
            status_counts.setdefault(gid, Counter())[status] = n

    # role per id, first write wins. A base selected twice, or selected alongside one of its
    # own variations, must not be asked twice: the responses table is UNIQUE(run, question,
    # llm), and paying twice for the same answer is not a rounding error. Counting roles off
    # the deduped mapping (rather than summing per group) keeps base + variations == total, so
    # the headline the reviewer approves can never overstate what actually runs.
    role: dict[str, str] = {}
    groups: list[dict] = []
    for qid in requested:
        q = by_qid.get(qid)
        if q is None:
            continue
        approved = approved_by_group.get(qid, []) if not q.is_variation else []
        counts = status_counts.get(qid, Counter())
        groups.append({
            "question_id": q.question_id,
            "question_text": q.question_text,
            "approval_status": q.approval_status,
            "is_variation": bool(q.is_variation),
            "approved_variation_ids": approved,
            "approved_count": len(approved),
            "pending_count": counts.get("DRAFT", 0),
            "rejected_count": counts.get("REJECTED", 0),
        })
        role.setdefault(q.question_id, "base")
        for vid in approved:
            role.setdefault(vid, "variation")

    ids = list(role)
    return {
        "question_ids": ids,
        "base_count": sum(1 for r in role.values() if r == "base"),
        "variation_count": sum(1 for r in role.values() if r == "variation"),
        "total": len(ids),
        "groups": groups,
        "missing": missing,
    }


# --- 4. results rollup + divergence ---------------------------------------------
def summarize_divergence(stats: list[dict]) -> dict:
    """Pure divergence/consistency summary across per-variation stats.

    Each stat: {question_id, is_base, mean_sentiment, modal_position, mention_rate}.
    consistency_score in [0,1]: high when sentiment is tight AND positions agree across
    phrasings. Flags outliers whose sentiment or modal position diverges from the group."""
    sentiments = [s["mean_sentiment"] for s in stats if s.get("mean_sentiment") is not None]
    positions = [s["modal_position"] for s in stats if s.get("modal_position")]
    mention_rates = [s["mention_rate"] for s in stats if s.get("mention_rate") is not None]

    sentiment_mean = _mean(sentiments)
    sentiment_spread = round(max(sentiments) - min(sentiments), 4) if len(sentiments) >= 2 else 0.0
    group_modal = _modal(positions)
    position_agreement = (
        round(sum(1 for p in positions if p == group_modal) / len(positions), 4)
        if positions else None
    )
    mention_rate_spread = (
        round(max(mention_rates) - min(mention_rates), 4) if len(mention_rates) >= 2 else 0.0
    )

    sent_component = 1 - min(sentiment_spread / 2.0, 1.0)  # sentiment domain is [-1,1] -> spread<=2
    pos_component = position_agreement if position_agreement is not None else 1.0
    consistency_score = (
        round(0.5 * sent_component + 0.5 * pos_component, 4)
        if (sentiments or positions) else None
    )

    outliers = []
    for s in stats:
        reasons = []
        if (s.get("mean_sentiment") is not None and sentiment_mean is not None
                and abs(s["mean_sentiment"] - sentiment_mean) >= 0.4):
            reasons.append("sentiment")
        if group_modal and s.get("modal_position") and s["modal_position"] != group_modal:
            reasons.append("position")
        if reasons:
            outliers.append({"question_id": s["question_id"], "reasons": reasons})

    return {
        "variations_scored": len([s for s in stats if s.get("mean_sentiment") is not None]),
        "sentiment_mean": sentiment_mean,
        "sentiment_spread": sentiment_spread,
        "group_modal_position": group_modal,
        "position_agreement": position_agreement,
        "mention_rate_spread": mention_rate_spread,
        "consistency_score": consistency_score,
        "outliers": outliers,
    }


async def _latest_run_for_questions(db: AsyncSession, qids: list[str]) -> str | None:
    stmt = (
        select(Response.run_id, func.max(Response.timestamp_utc).label("ts"))
        .where(Response.question_id.in_(qids))
        .group_by(Response.run_id)
        .order_by(func.max(Response.timestamp_utc).desc())
        .limit(1)
    )
    row = (await db.execute(stmt)).first()
    return row[0] if row else None


def _mentioned(score: ScoringRecord | None) -> bool:
    if score is None:
        return False
    if score.competitive_position and score.competitive_position != "NOT_MENTIONED":
        return True
    try:
        return bool(score.brand_mentions and json.loads(score.brand_mentions))
    except (ValueError, TypeError):
        return False


async def group_results(db: AsyncSession, group_id: str, *, run_id: str | None = None) -> dict:
    """Roll a group's run responses up by variation: a variation x model matrix plus a
    group-level divergence summary. Uses the latest covering run when run_id is omitted."""
    base = await _current_question(db, group_id)
    members: list[tuple[Question, bool]] = []
    if base is not None:
        members.append((base, True))
    variation_qs = (await db.execute(
        select(Question).where(
            Question.variation_group_id == group_id,
            Question.is_variation.is_(True),
            Question.deleted_at.is_(None),
            Question.superseded_by.is_(None),
        ).order_by(Question.created_at)
    )).scalars().all()
    members.extend((q, False) for q in variation_qs)

    empty_summary = summarize_divergence([])
    if not members:
        return {"group_id": group_id, "run_id": None, "base": _base_summary(base),
                "variations": [], "summary": empty_summary}

    qids = [q.question_id for q, _ in members]
    if run_id is None:
        run_id = await _latest_run_for_questions(db, qids)

    responses: list[Response] = []
    if run_id:
        responses = list((await db.execute(
            select(Response).where(Response.run_id == run_id, Response.question_id.in_(qids))
        )).scalars().all())
    scores = await _latest_scores_map(db, [r.response_id for r in responses])

    by_qid: dict[str, list[Response]] = {}
    for r in responses:
        by_qid.setdefault(r.question_id, []).append(r)

    variations_out: list[dict] = []
    stats: list[dict] = []
    for q, is_base in members:
        cells = []
        sentiments: list[float] = []
        positions: list[str] = []
        mention_flags: list[bool] = []
        for r in by_qid.get(q.question_id, []):
            score = scores.get(r.response_id)
            mentioned = _mentioned(score)
            sentiment = score.sentiment_score if score else None
            position = score.competitive_position if score else None
            if sentiment is not None:
                sentiments.append(sentiment)
            if position:
                positions.append(position)
            mention_flags.append(mentioned)
            cells.append({
                "llm_name": r.llm_name,
                "status": r.status,
                "response_id": r.response_id,
                "sentiment_score": sentiment,
                "competitive_position": position,
                "mentioned": mentioned,
                "answer_excerpt": (r.response_text or "")[:_ANSWER_EXCERPT],
            })
        mention_rate = round(sum(mention_flags) / len(mention_flags), 4) if mention_flags else None
        stat = {
            "question_id": q.question_id,
            "is_base": is_base,
            "mean_sentiment": _mean(sentiments),
            "modal_position": _modal(positions),
            "mention_rate": mention_rate,
        }
        stats.append(stat)
        variations_out.append({
            "question_id": q.question_id,
            "question_text": q.question_text,
            "is_base": is_base,
            "generation_method": q.generation_method,
            "answers": cells,
            **{k: stat[k] for k in ("mean_sentiment", "modal_position", "mention_rate")},
        })

    return {
        "group_id": group_id,
        "run_id": run_id,
        "base": _base_summary(base),
        "variations": variations_out,
        "summary": summarize_divergence(stats),
    }
