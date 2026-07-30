"""Sentiment & competitive scoring (FR-401..408) — Claude structured output.

Scores each stored response into a strict JSON schema, persists a versioned scoring
record (never mutating the response), evaluates alert rules, and computes diffs.
"""
import asyncio
import json
import re
import uuid
from functools import lru_cache

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import taxonomy
from app.config.settings import get_settings
from app.models.consensus import ConsensusRecord
from app.models.response import Response
from app.models.response_diff import ResponseDiff
from app.models.scoring import ScoringRecord
from app.providers.base import ModelParams
from app.providers.registry import get_provider_client, get_scoring_config
from app.scoring.alert_engine import evaluate_alerts
from app.scoring.differ import compute_diff, is_material_change
from app.services.model_release_service import detect_model_updates, find_correlated_release
from app.utils.audit import write_audit
from app.utils.logging import get_logger

logger = get_logger("scorer")

VALID_POSITIONS = {
    "FIRST_LINE_RECOMMENDED", "AMONG_OPTIONS", "SECOND_LINE",
    "NOT_RECOMMENDED", "NOT_MENTIONED",
    "LANDSCAPE",  # FR-108a: disease-state records hold a multi-competitor matrix, not a
                  # single focus-brand position — the matrix lives in brand_mentions.
}

# Lower number = worse for the focus brand. Used only to break modal-position ties
# toward the more negative stance so the consensus view never hides a risk signal.
_POSITION_SEVERITY = {
    "NOT_RECOMMENDED": 0,
    "NOT_MENTIONED": 1,
    "SECOND_LINE": 2,
    "AMONG_OPTIONS": 3,
    "FIRST_LINE_RECOMMENDED": 4,
}


@lru_cache
def _brand_context() -> dict:
    # Cleared by ``taxonomy.reload()`` — see ``taxonomy._DEPENDENT_CACHES``. A cache over the
    # taxonomy that outlived a reload would score answers against the previous config.
    return taxonomy.config()


def _competitive_field(therapeutic_area: str, disease: str | None) -> tuple[list[str], list[str], str | None]:
    """``(focus_brands, competitors, resolved_disease)`` for a scoring prompt.

    Prefers the DISEASE-level field when the question names an indication we track,
    because the therapeutic-area block flattens every indication into one list: a Rinvoq
    Atopic Dermatitis question was previously scored against Xeljanz and Olumiant while
    Cibinqo, Adbry and Ebglyss were missing entirely. Falls back to the area block when
    the disease is absent or not declared in the overlay, so nothing degrades to an empty
    competitor set.
    """
    resolved = taxonomy.canonical_disease(disease)
    if resolved:
        competitors = list(taxonomy.competitors_for_disease(resolved))
        if competitors:
            return list(taxonomy.brands_for_disease(resolved)), competitors, resolved

    cfg = _brand_context().get("therapeutic_areas", {}).get(therapeutic_area, {})
    return (
        [b["name"] for b in cfg.get("focus_brands", [])],
        [c["name"] for c in cfg.get("competitors", [])],
        resolved,
    )


def _context_for(therapeutic_area: str, focus_brand: str, disease: str | None = None) -> str:
    focus, competitors, resolved = _competitive_field(therapeutic_area, disease)
    scope = f"Therapeutic area: {therapeutic_area}\n"
    if resolved:
        scope += f"Indication under evaluation: {resolved}\n"
    return (
        f"{scope}"
        f"Focus brand under evaluation: {focus_brand}\n"
        f"All monitored focus brands: {', '.join(focus)}\n"
        f"Known competitors: {', '.join(competitors)}"
    )


def _landscape_context(
    therapeutic_area: str, competitor_focus: str | None, disease: str | None = None
) -> str:
    """Brand-less context for disease-state scoring (FR-108a): the competitive field for
    the indication (or the therapeutic area when none is known), merged with any
    question-level competitor tags."""
    focus, competitors, resolved = _competitive_field(therapeutic_area, disease)
    tagged: list[str] = []
    if competitor_focus:
        try:
            parsed = json.loads(competitor_focus)
            if isinstance(parsed, list):
                tagged = [str(x) for x in parsed]
        except (ValueError, TypeError):
            tagged = [competitor_focus]
    field = list(dict.fromkeys(focus + competitors + tagged))  # de-dup, keep order
    scope = f"Therapeutic area: {therapeutic_area}\n"
    if resolved:
        scope += f"Indication: {resolved}\n"
    return (
        f"{scope}"
        "Mode: DISEASE-STATE / PRE-LAUNCH landscape (NO primary brand under evaluation).\n"
        f"Question-tagged competitors: {', '.join(tagged) or 'none'}\n"
        f"Known agents in this landscape: {', '.join(field) or 'unknown'}"
    )


def _build_landscape_prompt(response: Response) -> tuple[str, str]:
    """Disease-state scoring prompt (FR-108a.3/4): emit a per-competitor landscape
    matrix instead of a single focus-brand sentiment/position."""
    system = (
        "You are a pharmaceutical competitive-landscape analyst. There is NO single focus "
        "brand — analyze how EVERY therapy/agent mentioned in the assistant response is "
        "positioned. Return ONLY a JSON object, no prose, matching exactly this schema:\n"
        "{\n"
        '  "landscape": [\n'
        '    {"brand": str, "is_competitor": bool, "mentioned": bool,\n'
        '     "sentiment": float between -1.0 and 1.0,\n'
        '     "position": one of FIRST_LINE_RECOMMENDED|AMONG_OPTIONS|SECOND_LINE|NOT_RECOMMENDED|NOT_MENTIONED}\n'
        "  ],\n"
        '  "key_claims": [up to 5 short strings],\n'
        '  "scoring_rationale": brief string\n'
        "}\n"
        "Include one entry per agent you can identify (both the tagged competitors and any "
        "others the response names)."
    )
    user = (
        f"{_landscape_context(response.therapeutic_area, response.competitor_focus, response.disease)}\n\n"
        f"USER QUESTION:\n{response.question_text}\n\n"
        f"ASSISTANT RESPONSE:\n{response.response_text[:6000]}\n\n"
        "Produce the JSON landscape analysis now."
    )
    return system, user


def _build_prompt(response: Response) -> tuple[str, str]:
    system = (
        "You are a pharmaceutical brand-sentiment and competitive-positioning analyst. "
        "Analyze how the focus brand is represented in the assistant response. "
        "Return ONLY a JSON object, no prose, matching exactly this schema:\n"
        "{\n"
        '  "sentiment_score": float between -1.0 and 1.0 (toward the focus brand),\n'
        '  "competitive_position": one of FIRST_LINE_RECOMMENDED|AMONG_OPTIONS|SECOND_LINE|NOT_RECOMMENDED|NOT_MENTIONED,\n'
        '  "brand_mentions": [{"brand": str, "is_competitor": bool, "sentiment": float}],\n'
        '  "key_claims": [up to 5 short strings],\n'
        '  "scoring_rationale": brief string\n'
        "}"
    )
    user = (
        f"{_context_for(response.therapeutic_area, response.brand_focus, response.disease)}\n\n"
        f"USER QUESTION:\n{response.question_text}\n\n"
        f"ASSISTANT RESPONSE:\n{response.response_text[:6000]}\n\n"
        "Produce the JSON analysis now."
    )
    return system, user


def _extract_json(text: str) -> dict:
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in scoring output")
    return json.loads(match.group(0))


async def _score_llm(response: Response) -> dict | None:
    """Network-only: run the scoring LLM for ONE response and parse its JSON. No DB access.

    Returns a payload (parsed JSON + token counts + landscape flag + model id) or None on
    failure (transport error or unparseable output, after one retry). Kept DB-free and
    timeout-bounded so score_run can fan these calls out concurrently while persistence
    stays serialized on a single session."""
    cfg = get_scoring_config()
    client = get_provider_client(cfg.provider)
    is_landscape = response.monitoring_mode == "DISEASE_STATE"
    system, user = (
        _build_landscape_prompt(response) if is_landscape else _build_prompt(response)
    )
    timeout = get_settings().target_call_timeout_seconds + 30
    last_err: Exception | None = None
    for _ in range(2):  # one retry on transport/parse/timeout failure
        try:
            result = await asyncio.wait_for(
                client.chat(cfg.model_id, system, user,
                            ModelParams(max_tokens=1500, temperature=0.0)),
                timeout=timeout,
            )
            parsed = _extract_json(result.text)
            return {
                "parsed": parsed,
                "is_landscape": is_landscape,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "model_id": cfg.model_id,
            }
        except Exception as e:  # noqa: BLE001 — parse/transport/timeout; retry once then give up
            last_err = e
    logger.warning("Scoring LLM failed for %s: %s", response.response_id, last_err)
    return None


async def _persist_score(
    db: AsyncSession, response: Response, payload: dict, *,
    prompt_version: str = "v1", commit: bool = True,
) -> ScoringRecord:
    """Persist a scoring payload (from _score_llm) as a versioned record + alerts. DB-only."""
    parsed = payload["parsed"]
    is_landscape = payload["is_landscape"]

    if is_landscape:
        # FR-108a: comparative matrix — one entry per agent. Stored in brand_mentions so
        # existing consumers keep working; competitive_position is the LANDSCAPE marker and
        # sentiment is the mean over mentioned agents (dispersion is visible in the matrix).
        matrix = parsed.get("landscape", []) or []
        brand_mentions = matrix
        position = "LANDSCAPE"
        mentioned = [
            float(m["sentiment"]) for m in matrix
            if isinstance(m, dict) and m.get("mentioned", True) and m.get("sentiment") is not None
        ]
        sentiment = round(max(-1.0, min(1.0, sum(mentioned) / len(mentioned))), 4) if mentioned else None
    else:
        position = parsed.get("competitive_position")
        if position not in VALID_POSITIONS:
            position = "NOT_MENTIONED"
        sentiment = parsed.get("sentiment_score")
        try:
            sentiment = max(-1.0, min(1.0, float(sentiment)))
        except (TypeError, ValueError):
            sentiment = 0.0
        brand_mentions = parsed.get("brand_mentions", []) or []

    key_claims = (parsed.get("key_claims", []) or [])[:5]

    # Determine next version for this response
    existing = await db.execute(
        select(ScoringRecord).where(ScoringRecord.response_id == response.response_id)
    )
    versions = [s.score_version for s in existing.scalars().all()]
    next_version = (max(versions) + 1) if versions else 1

    record = ScoringRecord(
        score_id=str(uuid.uuid4()),
        response_id=response.response_id,
        score_version=next_version,
        prompt_version=prompt_version,
        sentiment_score=sentiment,
        competitive_position=position,
        brand_mentions=json.dumps(brand_mentions),
        key_claims=json.dumps(key_claims),
        scoring_rationale=parsed.get("scoring_rationale", ""),
        scored_by=payload["model_id"],
    )
    db.add(record)
    await db.flush()

    # Focus-brand alert rules only apply in BRAND mode — disease-state has no focus brand.
    alerts = []
    if not is_landscape:
        alerts = evaluate_alerts(
            response_id=response.response_id,
            score_id=record.score_id,
            sentiment_score=sentiment,
            competitive_position=position,
            brand_mentions=brand_mentions,
            focus_brand=response.brand_focus,
        )
    for a in alerts:
        db.add(a)

    await write_audit(db, role="ORCHESTRATOR", event="SCORED", run_id=response.run_id,
                      question_id=response.question_id, llm_target=response.llm_name,
                      tokens=payload["prompt_tokens"] + payload["completion_tokens"],
                      context={"sentiment": sentiment, "position": position,
                               "alerts": len(alerts)}, commit=False)
    if commit:
        await db.commit()
    return record


async def score_response(
    db: AsyncSession, response: Response, *, prompt_version: str = "v1", commit: bool = True
) -> ScoringRecord | None:
    """Score a single response; persist a versioned record + alerts (FR-401..405).

    Two scoring modes (FR-108a): BRAND produces a single focus-brand sentiment/position;
    DISEASE_STATE produces a multi-competitor landscape matrix (no focus brand)."""
    payload = await _score_llm(response)
    if payload is None:
        await write_audit(db, role="ORCHESTRATOR", event="SCORING_PARSE_FAIL",
                          question_id=response.question_id, llm_target=response.llm_name,
                          context={"error": "scoring failed after retry"})
        return None
    return await _persist_score(db, response, payload,
                                prompt_version=prompt_version, commit=commit)


async def _compute_response_diff(db: AsyncSession, response: Response) -> None:
    """Diff vs the most recent prior response for the same (question, llm) (FR-306)."""
    stmt = (
        select(Response)
        .where(
            Response.question_id == response.question_id,
            Response.llm_name == response.llm_name,
            Response.run_id != response.run_id,
            Response.status.in_(["SUCCESS", "TRUNCATED"]),
        )
        .order_by(Response.created_at.desc())
        .limit(1)
    )
    prev = (await db.execute(stmt)).scalars().first()
    if prev is None:
        return
    ratio, diff_text = compute_diff(prev.response_text, response.response_text)
    material = is_material_change(ratio)

    # FR-707a: correlate material drift with a real vendor model update. When the model
    # VERSION changed between the two responses, that transition IS the cause — anchor the
    # drift to that version's event; otherwise fall back to the date-window heuristic.
    correlated_release_id = None
    if material:
        observed_on = (response.created_at or response.timestamp_utc)
        observed_on = observed_on.date() if hasattr(observed_on, "date") else observed_on
        transition_version = (
            response.llm_model_version
            if (response.llm_model_version and response.llm_model_version != prev.llm_model_version)
            else None
        )
        if observed_on is not None:
            release = await find_correlated_release(
                db, llm_name=response.llm_name, observed_on=observed_on,
                version=transition_version,
            )
            if release is not None:
                correlated_release_id = release.id

    db.add(ResponseDiff(
        question_id=response.question_id,
        llm_name=response.llm_name,
        current_response_id=response.response_id,
        previous_response_id=prev.response_id,
        similarity_ratio=ratio,
        material_change=material,
        diff_text=diff_text[:20000],
        correlated_release_id=correlated_release_id,
    ))


async def score_run(db: AsyncSession, run_id: str) -> dict:
    """Score every SUCCESS/TRUNCATED response in a run (FR-401, FR-406).

    The (slow) scoring LLM calls run CONCURRENTLY, bounded by max_concurrent_scoring, so a
    large run's scoring pass overlaps instead of running one response at a time. DB writes
    stay serialized on this single session afterward (SQLite is single-writer)."""
    stmt = select(Response).where(
        Response.run_id == run_id,
        Response.status.in_(["SUCCESS", "TRUNCATED"]),
    )
    responses = list((await db.execute(stmt)).scalars().all())

    # Idempotent / resume-safe: skip responses that already have a scoring record.
    to_score: list[Response] = []
    for r in responses:
        existing = await db.execute(
            select(ScoringRecord.score_id).where(
                ScoringRecord.response_id == r.response_id
            ).limit(1)
        )
        if existing.scalars().first() is None:
            to_score.append(r)

    # Phase 1 — network only, concurrent (bounded). No DB access inside the gather.
    sem = asyncio.Semaphore(max(get_settings().max_concurrent_scoring, 1))

    async def _bounded(r: Response) -> tuple[Response, dict | None]:
        async with sem:
            return r, await _score_llm(r)

    results = await asyncio.gather(*(_bounded(r) for r in to_score))

    # Phase 2 — persist sequentially on the shared session (SQLite single-writer safe).
    scored = 0
    for r, payload in results:
        if payload is None:
            await write_audit(db, role="ORCHESTRATOR", event="SCORING_PARSE_FAIL",
                              question_id=r.question_id, llm_target=r.llm_name,
                              context={"error": "scoring failed after retry"}, commit=False)
            await db.commit()
            continue
        await _persist_score(db, r, payload, commit=False)
        await _compute_response_diff(db, r)
        scored += 1
        await db.commit()

    logger.info("Scored %d responses for run %s", scored, run_id)
    await aggregate_consensus_scores(db, run_id)
    # FR-707a: anchor real vendor version transitions from this run's responses, then
    # auto-log any drift-spike updates and (re)correlate drifts — no manual step.
    await _anchor_model_versions(db)
    await detect_model_updates(db)
    return {"run_id": run_id, "scored": scored, "total": len(responses)}


def _modal_position(distribution: dict[str, int]) -> str | None:
    """Most common position; ties broken toward the worst (most negative) for risk visibility."""
    if not distribution:
        return None
    return sorted(
        distribution.items(),
        key=lambda kv: (-kv[1], _POSITION_SEVERITY.get(kv[0], 1)),
    )[0][0]


async def _latest_scores_for(
    db: AsyncSession, response_ids: list[str]
) -> dict[str, ScoringRecord]:
    """Latest (max-version) scoring record per response id."""
    if not response_ids:
        return {}
    subq = (
        select(
            ScoringRecord.response_id,
            func.max(ScoringRecord.score_version).label("maxv"),
        )
        .where(ScoringRecord.response_id.in_(response_ids))
        .group_by(ScoringRecord.response_id)
        .subquery()
    )
    stmt = select(ScoringRecord).join(
        subq,
        and_(
            ScoringRecord.response_id == subq.c.response_id,
            ScoringRecord.score_version == subq.c.maxv,
        ),
    )
    rows = (await db.execute(stmt)).scalars().all()
    return {r.response_id: r for r in rows}


async def aggregate_consensus_scores(db: AsyncSession, run_id: str) -> None:
    """Aggregate per-model sentiment/position onto each ConsensusRecord in a run.

    Computes mean sentiment + dispersion (min/max) and the modal competitive position
    across all scored SUCCESS/TRUNCATED responses for each (run, question). No LLM call —
    pure math over the per-model ScoringRecords, so the consensus "overall" view stays
    faithful to the real distribution of model opinions and is cheap to refresh on re-score.
    """
    crecs = list((await db.execute(
        select(ConsensusRecord).where(ConsensusRecord.run_id == run_id)
    )).scalars().all())
    if not crecs:
        return

    for crec in crecs:
        resp_ids = list((await db.execute(
            select(Response.response_id).where(
                Response.run_id == run_id,
                Response.question_id == crec.question_id,
                Response.status.in_(["SUCCESS", "TRUNCATED"]),
            )
        )).scalars().all())
        scores = await _latest_scores_for(db, resp_ids)

        sentiments = [s.sentiment_score for s in scores.values() if s.sentiment_score is not None]
        positions = [s.competitive_position for s in scores.values() if s.competitive_position]

        if sentiments:
            crec.overall_sentiment = round(sum(sentiments) / len(sentiments), 4)
            crec.sentiment_min = min(sentiments)
            crec.sentiment_max = max(sentiments)
        if positions:
            distribution: dict[str, int] = {}
            for p in positions:
                distribution[p] = distribution.get(p, 0) + 1
            crec.position_distribution = json.dumps(distribution)
            crec.overall_position = _modal_position(distribution)
        crec.models_scored = len(scores)

    await db.commit()
    logger.info("Aggregated consensus scores for %d questions in run %s", len(crecs), run_id)


async def score_unscored_sweep(db: AsyncSession) -> dict:
    """Sweeper: score any unscored response across all runs (FR-406)."""
    sub = select(ScoringRecord.response_id)
    stmt = select(Response).where(
        Response.status.in_(["SUCCESS", "TRUNCATED"]),
        Response.response_id.notin_(sub),
    ).limit(200)
    responses = list((await db.execute(stmt)).scalars().all())
    scored = 0
    affected_runs: set[str] = set()
    for r in responses:
        rec = await score_response(db, r, commit=False)
        if rec is not None:
            await _compute_response_diff(db, r)
            scored += 1
        affected_runs.add(r.run_id)
        await db.commit()
    for rid in affected_runs:
        await aggregate_consensus_scores(db, rid)
    # FR-707a: anchor real vendor version transitions, then auto-log drift-spike updates
    # and (re)correlate drifts — no manual step.
    await _anchor_model_versions(db)
    await detect_model_updates(db)
    return {"scored": scored}


async def _anchor_model_versions(db: AsyncSession) -> None:
    """Refresh version observations + create real api-sourced version-transition events
    (FR-707a) so drift correlation can anchor to the exact version boundary. No network."""
    from app.model_updates.versions import detect_version_transitions, refresh_observations
    try:
        await refresh_observations(db)
        await detect_version_transitions(db)
    except Exception as e:  # noqa: BLE001 — anchoring is best-effort; never fail scoring
        logger.warning("Model-version anchoring skipped: %s", e)
