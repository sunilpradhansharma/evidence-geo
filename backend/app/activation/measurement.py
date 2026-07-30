"""Activation & Impact — measurement core (thin v1).

Pure computation over the existing scoring layer: gather a cohort's latest ScoringRecords,
roll them up into AI-answer KPIs, freeze them as MeasurementSnapshots, and compute a
single-arm before/after InterventionResult with confounder flags and a confidence tier.
No LLM calls, no new scoring — reuses ``scorer._latest_scores_for``.

KPI definitions are pinned to the real ``competitive_position`` enum + ``sentiment_score``
so every number is transparent and auditable (raw position counts are stored alongside the
rates). The comparison is deliberately single-arm and NON-causal: "a change was observed
after publication", never "the intervention caused it".
"""
from __future__ import annotations

import json
import uuid
from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.intervention_result import InterventionResult
from app.models.measurement_snapshot import MeasurementSnapshot
from app.models.model_release import ModelReleaseLog
from app.models.response import Response
from app.models.run import Run
from app.models.scoring import ScoringRecord
from app.scoring.scorer import _latest_scores_for
from app.utils.logging import get_logger

logger = get_logger("activation.measurement")

# Position enum (BRAND mode); LANDSCAPE / None are excluded from KPI math.
_POSITIONS = (
    "FIRST_LINE_RECOMMENDED", "AMONG_OPTIONS", "SECOND_LINE",
    "NOT_RECOMMENDED", "NOT_MENTIONED",
)
_CONSIDERATION = {"FIRST_LINE_RECOMMENDED", "AMONG_OPTIONS"}
_WEAK = {"SECOND_LINE", "NOT_RECOMMENDED"}

# Runs whose results are stable enough to measure. AWAITING_OPENEVIDENCE is included so a
# stray Provider cohort can't deadlock the sweep (automated-target scores already exist);
# v1 cohorts are Patient/Prospect and COMPLETE normally.
READY_RUN_STATUSES = {"COMPLETED", "FAILED", "CANCELLED", "CANCELED", "AWAITING_OPENEVIDENCE"}

# KPI registry: key -> (label, kind). "rate" metrics are reported in percentage points.
METRIC_DEFS: dict[str, tuple[str, str]] = {
    "brand_mention_rate": ("Brand Mention Rate", "rate"),
    "leading_rate": ("Leading Rate", "rate"),
    "consideration_rate": ("Consideration Rate", "rate"),
    "missing_rate": ("Missing Rate", "rate"),
    "weak_position_rate": ("Weak-Position Rate", "rate"),
    "avg_sentiment": ("Average Sentiment", "score"),
}
RATE_METRICS = [k for k, (_, kind) in METRIC_DEFS.items() if kind == "rate"]

# Interpretation thresholds (tunable).
MIN_SAMPLE = 6           # below this a snapshot is too thin to interpret
MIN_RATE_CHANGE = 0.05   # 5 percentage points
MIN_SENTIMENT_CHANGE = 0.10
LOW_CONSISTENCY = 0.5    # modal-position agreement below this => high variability


# --------------------------------------------------------------------------- KPI math
def _common(values) -> str | None:
    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else None


def _model_versions(responses: list[Response]) -> dict[str, str]:
    """Most-common observed vendor version per target across the cohort."""
    versions: dict[str, Counter] = defaultdict(Counter)
    for r in responses:
        if r.llm_model_version:
            versions[r.llm_name][r.llm_model_version] += 1
    return {llm: c.most_common(1)[0][0] for llm, c in versions.items() if c}


def _consistency(by_question: dict[str, list[str]]) -> float | None:
    """Fraction of responses matching their question's modal position (stability)."""
    total = 0
    agree = 0
    for positions in by_question.values():
        if not positions:
            continue
        modal = Counter(positions).most_common(1)[0][1]
        agree += modal
        total += len(positions)
    return round(agree / total, 4) if total else None


def compute_metrics(pairs: list[tuple[Response, ScoringRecord]]) -> dict:
    """Roll a set of (response, latest-score) pairs into the v1 KPI bundle."""
    positions: list[str] = []
    sentiments: list[float] = []
    by_question: dict[str, list[str]] = defaultdict(list)
    by_model: dict[str, dict[str, list]] = defaultdict(lambda: {"pos": [], "sent": []})

    for resp, score in pairs:
        pos = score.competitive_position
        if pos in _POSITIONS:
            positions.append(pos)
            by_question[resp.question_id].append(pos)
            by_model[resp.llm_name]["pos"].append(pos)
        if score.sentiment_score is not None:
            sentiments.append(float(score.sentiment_score))
            by_model[resp.llm_name]["sent"].append(float(score.sentiment_score))

    n = len(positions)
    counts = Counter(positions)

    def rate(subset: set[str]) -> float | None:
        return round(sum(counts[p] for p in subset) / n, 4) if n else None

    def by_model_block(vals: dict[str, list]) -> dict:
        mp = vals["pos"]
        ms = vals["sent"]
        mc = Counter(mp)
        mn = len(mp)
        return {
            "n": mn,
            "leading": mc.get("FIRST_LINE_RECOMMENDED", 0),
            "consideration": sum(mc[p] for p in _CONSIDERATION),
            "avg_sentiment": round(sum(ms) / len(ms), 4) if ms else None,
        }

    return {
        "n": n,
        "n_sentiment": len(sentiments),
        "position_counts": {p: int(counts.get(p, 0)) for p in _POSITIONS},
        "brand_mention_rate": round((n - counts.get("NOT_MENTIONED", 0)) / n, 4) if n else None,
        "leading_rate": round(counts.get("FIRST_LINE_RECOMMENDED", 0) / n, 4) if n else None,
        "consideration_rate": rate(_CONSIDERATION),
        "missing_rate": round(counts.get("NOT_MENTIONED", 0) / n, 4) if n else None,
        "weak_position_rate": rate(_WEAK),
        "avg_sentiment": round(sum(sentiments) / len(sentiments), 4) if sentiments else None,
        "response_consistency": _consistency(by_question),
        "by_model": {llm: by_model_block(v) for llm, v in by_model.items()},
    }


# --------------------------------------------------------------- cohort gathering
async def _cohort_responses(
    db: AsyncSession,
    *,
    run_ids: list[str] | None = None,
    question_ids: list[str] | None = None,
    personas: list[str] | None = None,
    models: list[str] | None = None,
) -> list[Response]:
    stmt = select(Response).where(Response.status.in_(["SUCCESS", "TRUNCATED"]))
    if run_ids is not None:
        stmt = stmt.where(Response.run_id.in_(run_ids))
    if question_ids:
        stmt = stmt.where(Response.question_id.in_(question_ids))
    if personas:
        stmt = stmt.where(Response.persona.in_(personas))
    if models:
        stmt = stmt.where(Response.llm_name.in_(models))
    return list((await db.execute(stmt)).scalars().all())


async def _pairs_for(db: AsyncSession, responses: list[Response]) -> list[tuple[Response, ScoringRecord]]:
    ids = [r.response_id for r in responses]
    scores = await _latest_scores_for(db, ids)
    return [(r, scores[r.response_id]) for r in responses if r.response_id in scores]


async def _latest_historical_responses(
    db: AsyncSession, *, question_ids: list[str], personas: list[str] | None, models: list[str] | None
) -> list[Response]:
    """The most-recent existing SUCCESS/TRUNCATED response per (question, target) — the free
    'discovery' baseline read directly from history (no new runs)."""
    if not question_ids:
        return []
    stmt = select(Response).where(
        Response.status.in_(["SUCCESS", "TRUNCATED"]),
        Response.question_id.in_(question_ids),
    )
    if personas:
        stmt = stmt.where(Response.persona.in_(personas))
    if models:
        stmt = stmt.where(Response.llm_name.in_(models))
    rows = list((await db.execute(stmt.order_by(Response.created_at.desc()))).scalars().all())
    seen: set[tuple[str, str]] = set()
    latest: list[Response] = []
    for r in rows:
        key = (r.question_id, r.llm_name)
        if key in seen:
            continue
        seen.add(key)
        latest.append(r)
    return latest


# --------------------------------------------------------------- readiness
async def runs_ready(db: AsyncSession, run_ids: list[str]) -> bool:
    """True once every measurement run is terminal AND every produced response is scored."""
    if not run_ids:
        return False
    runs = list((await db.execute(select(Run).where(Run.run_id.in_(run_ids)))).scalars().all())
    if len(runs) < len(set(run_ids)):
        return False
    if any((r.status or "").upper() not in READY_RUN_STATUSES for r in runs):
        return False
    resp_ids = list((await db.execute(
        select(Response.response_id).where(
            Response.run_id.in_(run_ids),
            Response.status.in_(["SUCCESS", "TRUNCATED"]),
        )
    )).scalars().all())
    if not resp_ids:
        return True  # all runs terminal, nothing to score
    scores = await _latest_scores_for(db, resp_ids)
    return all(rid in scores for rid in resp_ids)


# --------------------------------------------------------------- snapshots
def create_pending_snapshot(
    *, intervention_id: str, snapshot_type: str, run_ids: list[str], question_ids: list[str] | None
) -> MeasurementSnapshot:
    """A snapshot placeholder for freshly-launched runs; metrics filled later by the sweep."""
    return MeasurementSnapshot(
        id=str(uuid.uuid4()),
        intervention_id=intervention_id,
        snapshot_type=snapshot_type,
        run_ids_json=json.dumps(run_ids),
        question_ids_json=json.dumps(question_ids) if question_ids else None,
        response_count=0,
        metric_values_json=None,
    )


async def finalize_snapshot(
    db: AsyncSession, snapshot: MeasurementSnapshot, *,
    personas: list[str] | None, models: list[str] | None, commit: bool = True,
) -> MeasurementSnapshot:
    """Compute + persist a pending snapshot's KPIs from its now-complete runs."""
    run_ids = json.loads(snapshot.run_ids_json or "[]")
    question_ids = json.loads(snapshot.question_ids_json or "null")
    responses = await _cohort_responses(
        db, run_ids=run_ids, question_ids=question_ids, personas=personas, models=models
    )
    pairs = await _pairs_for(db, responses)
    snapshot.metric_values_json = json.dumps(compute_metrics(pairs))
    snapshot.model_versions_json = json.dumps(_model_versions(responses))
    snapshot.response_count = len(pairs)
    snapshot.scorer_version = _common(s.scored_by for _, s in pairs)
    snapshot.prompt_version = _common(s.prompt_version for _, s in pairs)
    if commit:
        await db.commit()
    return snapshot


async def build_discovery_snapshot(
    db: AsyncSession, *, intervention_id: str, question_ids: list[str],
    personas: list[str] | None, models: list[str] | None, commit: bool = True,
) -> MeasurementSnapshot:
    """Discovery baseline from existing history (free; context, not the comparison point)."""
    responses = await _latest_historical_responses(
        db, question_ids=question_ids, personas=personas, models=models
    )
    pairs = await _pairs_for(db, responses)
    snap = MeasurementSnapshot(
        id=str(uuid.uuid4()),
        intervention_id=intervention_id,
        snapshot_type="DISCOVERY",
        run_ids_json=None,
        question_ids_json=json.dumps(question_ids),
        response_count=len(pairs),
        metric_values_json=json.dumps(compute_metrics(pairs)),
        model_versions_json=json.dumps(_model_versions(responses)),
        scorer_version=_common(s.scored_by for _, s in pairs),
        prompt_version=_common(s.prompt_version for _, s in pairs),
    )
    db.add(snap)
    if commit:
        await db.commit()
    return snap


# --------------------------------------------------------------- confounders + result
async def detect_confounders(
    db: AsyncSession, baseline: MeasurementSnapshot, post: MeasurementSnapshot
) -> list[dict]:
    """Flags that annotate confidence (never assert causation)."""
    out: list[dict] = []
    bmv = json.loads(baseline.model_versions_json or "{}")
    pmv = json.loads(post.model_versions_json or "{}")
    changed = [m for m in pmv if m in bmv and bmv[m] != pmv[m]]
    if changed:
        out.append({"code": "MODEL_VERSION_CHANGED",
                    "detail": f"Model version changed during the window for: {', '.join(sorted(changed))}."})

    if baseline.captured_at and post.captured_at:
        d0 = baseline.captured_at.date()
        d1 = post.captured_at.date()
        rels = list((await db.execute(
            select(ModelReleaseLog).where(
                ModelReleaseLog.release_date >= d0,
                ModelReleaseLog.release_date <= d1,
            )
        )).scalars().all())
        cohort_models = [m.lower() for m in (list(bmv) + list(pmv))]
        if cohort_models:
            hits = [
                r for r in rels
                if any(r.target_platform.lower() in m or m in r.target_platform.lower()
                       for m in cohort_models)
            ]
        else:
            hits = rels
        if hits:
            names = sorted({r.target_platform for r in hits})
            out.append({"code": "MODEL_RELEASE_IN_WINDOW",
                        "detail": f"A model release was logged during the window for: {', '.join(names)}."})

    if (baseline.scorer_version or "") != (post.scorer_version or ""):
        out.append({"code": "SCORER_VERSION_CHANGED",
                    "detail": f"Scoring model changed ({baseline.scorer_version} -> {post.scorer_version})."})
    if (baseline.prompt_version or "") != (post.prompt_version or ""):
        out.append({"code": "PROMPT_VERSION_CHANGED",
                    "detail": f"Scoring prompt version changed ({baseline.prompt_version} -> {post.prompt_version})."})
    if baseline.response_count < MIN_SAMPLE or post.response_count < MIN_SAMPLE:
        out.append({"code": "LOW_SAMPLE",
                    "detail": f"Small sample (baseline {baseline.response_count}, post {post.response_count}; want >= {MIN_SAMPLE})."})
    post_metrics = json.loads(post.metric_values_json or "{}")
    consistency = post_metrics.get("response_consistency")
    if consistency is not None and consistency < LOW_CONSISTENCY:
        out.append({"code": "HIGH_VARIABILITY",
                    "detail": f"Answers were unstable across repeated samples (consistency {consistency})."})
    return out


def _classify(
    primary: str, changes: dict, confounders: list[dict],
    baseline: MeasurementSnapshot, post: MeasurementSnapshot,
) -> tuple[str, str, str]:
    codes = {c["code"] for c in confounders}
    low_sample = "LOW_SAMPLE" in codes
    high_var = "HIGH_VARIABILITY" in codes
    prim = changes.get(primary)
    kind = METRIC_DEFS.get(primary, (primary, "rate"))[1]
    threshold = MIN_RATE_CHANGE if kind == "rate" else MIN_SENTIMENT_CHANGE

    if low_sample or prim is None:
        outcome = "INCONCLUSIVE"
    else:
        change = prim["change"]
        if change >= threshold:
            outcome = "IMPROVED"
        elif change <= -threshold:
            outcome = "WORSENED"
        else:
            outcome = "NO_CLEAR_CHANGE"

    method_confounders = codes & {
        "MODEL_VERSION_CHANGED", "MODEL_RELEASE_IN_WINDOW",
        "SCORER_VERSION_CHANGED", "PROMPT_VERSION_CHANGED",
    }
    if outcome == "INCONCLUSIVE" or high_var or low_sample:
        confidence = "LOW"
    elif method_confounders:
        confidence = "MEDIUM"
    else:
        confidence = "HIGH"

    label = METRIC_DEFS.get(primary, (primary, "rate"))[0]
    if prim is None:
        interpretation = f"Could not compare {label}: the metric was unavailable in one period."
    elif kind == "rate":
        interpretation = (
            f"{label} moved {prim['change_pp']:+.1f} pp after publication "
            f"(baseline {prim['baseline'] * 100:.0f}% -> post {prim['post'] * 100:.0f}%). "
            f"Observed association only, not proven causation."
        )
    else:
        interpretation = (
            f"{label} moved {prim['change']:+.2f} after publication "
            f"(baseline {prim['baseline']:.2f} -> post {prim['post']:.2f}). "
            f"Observed association only, not proven causation."
        )
    if method_confounders:
        interpretation += " A concurrent platform/scoring change may partly explain the shift."
    return outcome, confidence, interpretation


async def compute_result(
    db: AsyncSession, *, intervention, baseline: MeasurementSnapshot, post: MeasurementSnapshot,
    commit: bool = True,
) -> InterventionResult:
    """Single-arm before/after result: per-KPI change, confounders, confidence, outcome."""
    base_metrics = json.loads(baseline.metric_values_json or "{}")
    post_metrics = json.loads(post.metric_values_json or "{}")

    changes: dict[str, dict] = {}
    for key, (label, kind) in METRIC_DEFS.items():
        b = base_metrics.get(key)
        p = post_metrics.get(key)
        if b is None or p is None:
            continue
        change = round(p - b, 4)
        changes[key] = {
            "label": label,
            "kind": kind,
            "baseline": b,
            "post": p,
            "change": change,
            "change_pp": round(change * 100, 1) if kind == "rate" else None,
        }

    confounders = await detect_confounders(db, baseline, post)
    primary = intervention.primary_metric or "consideration_rate"
    outcome, confidence, interpretation = _classify(primary, changes, confounders, baseline, post)

    result = InterventionResult(
        id=str(uuid.uuid4()),
        intervention_id=intervention.id,
        baseline_snapshot_id=baseline.id,
        post_snapshot_id=post.id,
        metric_changes_json=json.dumps(changes),
        confounders_json=json.dumps(confounders),
        confidence=confidence,
        outcome_status=outcome,
        interpretation=interpretation,
    )
    db.add(result)
    if commit:
        await db.commit()
    return result


# --------------------------------------------------------------- serialization
def serialize_snapshot(s: MeasurementSnapshot | None) -> dict | None:
    if s is None:
        return None
    return {
        "id": s.id,
        "snapshot_type": s.snapshot_type,
        "response_count": s.response_count,
        "metrics": json.loads(s.metric_values_json) if s.metric_values_json else None,
        "model_versions": json.loads(s.model_versions_json) if s.model_versions_json else {},
        "scorer_version": s.scorer_version,
        "prompt_version": s.prompt_version,
        "run_ids": json.loads(s.run_ids_json) if s.run_ids_json else [],
        "pending": s.metric_values_json is None,
        "captured_at": s.captured_at.isoformat() if s.captured_at else None,
    }


def serialize_result(r: InterventionResult | None) -> dict | None:
    if r is None:
        return None
    return {
        "id": r.id,
        "baseline_snapshot_id": r.baseline_snapshot_id,
        "post_snapshot_id": r.post_snapshot_id,
        "metric_changes": json.loads(r.metric_changes_json) if r.metric_changes_json else {},
        "confounders": json.loads(r.confounders_json) if r.confounders_json else [],
        "confidence": r.confidence,
        "outcome_status": r.outcome_status,
        "interpretation": r.interpretation,
        "measured_at": r.measured_at.isoformat() if r.measured_at else None,
    }
