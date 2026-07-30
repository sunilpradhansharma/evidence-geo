"""The head-to-head scoreboard: who wins when AI is asked "us vs them".

Reads only what a run already produced — ``Response`` joined to its latest
``ScoringRecord``. **No model calls**, so opening the board costs nothing.

Two counting rules that the numbers depend on:

* **One answer can inform several comparisons.** A question naming two rivals is a real
  data point about both, and ``coverage.covers`` already treats it as covering both cells.
  So per-pair ``answers`` figures may sum to more than the number of answers examined, and
  the page-level total is a DISTINCT response count, never a sum of the rows beneath it.
* **Unresolved answers are counted, not dropped.** 58% of stored comparison answers name no
  tracked rival. A board that silently discarded them would present a filtered universe as
  if it were the whole one, so the excluded count and its reasons are returned alongside.

Ranking is by absolute exposure — the number of answers actually being lost — then by loss
rate. A pair losing 15 of 25 answers outranks one losing 2 of 2, because acting on the
first changes more of what buyers are told.
"""
from __future__ import annotations

import collections
import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.competitive import claims as claims_mod
from app.competitive import mentions as mentions_mod
from app.competitive import pairs as pairs_mod
from app.competitive import verdict as verdict_mod
from app.config.labels import HIDDEN_LLM_NAMES
from app.config.taxonomy import area_for
from app.models.question import Question
from app.models.response import Response
from app.models.scoring import ScoringRecord
from app.utils.logging import get_logger

logger = get_logger("competitive.head_to_head")

COMPARATIVE_DOMAIN = "Comparative"
BRAND_MODE = "BRAND"

# Enough answers to sample without loading a whole corpus into one response.
MAX_SAMPLE_ANSWERS = 8
# A trend needs a before and an after. Stated rather than faked, mirroring the sparse-history
# state ``CitationInsights`` renders instead of drawing a one-point line.
MIN_TREND_RUNS = 2
# A board-level LINE needs more than a before and an after: two points joined up read as a
# direction when they are really one observation each. Matches the three-day threshold the
# citation trend already holds itself to.
MIN_TIMELINE_PERIODS = 3

# The dimensions the board can be sliced by. Two of them — indication and rival — describe
# the COMPARISON rather than the answer row, so they are applied after grouping while the
# rest are applied to the answers themselves.
DIM_AREA = "areas"
DIM_DISEASE = "diseases"
DIM_BRAND = "brands"
DIM_COMPETITOR = "competitors"
DIM_PERSONA = "personas"
DIM_MODEL = "models"
FILTER_DIMENSIONS = (
    DIM_AREA, DIM_DISEASE, DIM_BRAND, DIM_COMPETITOR, DIM_PERSONA, DIM_MODEL,
)

# An answer the reader's own filters set aside. Deliberately NOT one of the
# ``pairs.REASON_LABELS``: those describe answers the board cannot use, this describes
# answers the reader asked not to see, and merging the two would make a narrowed board look
# like a corpus full of unusable data.
FILTERED_OUT = "filtered_out"
FILTERED_OUT_LABEL = (
    "Set aside by the filters you chose — they inform comparisons outside this selection."
)


@dataclass
class Answer:
    """One scored answer, with the comparisons it informs already resolved."""

    response_id: str
    run_id: str
    question_id: str
    question_text: str
    llm_name: str
    persona: str
    brand_focus: str | None
    therapeutic_area: str | None
    disease: str | None
    timestamp: object
    position: str | None
    our_sentiment: float | None
    brand_mentions: list = field(default_factory=list)
    key_claims: list = field(default_factory=list)
    rationale: str | None = None
    resolution: pairs_mod.Resolution = field(default_factory=pairs_mod.Resolution)

    def their_sentiment(self, competitor: str) -> float | None:
        return _mention_sentiment(self.brand_mentions, competitor)


def _loads_list(raw) -> list:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _mention_sentiment(mentions: list, competitor: str) -> float | None:
    """The scorer's sentiment for one named agent, matched alias-aware.

    Delegates to ``mentions`` so the alias rule that decides whether a payload names a
    drug lives in exactly one place: the board and the cross-corpus competitor reads must
    never disagree about whether an answer mentioned someone.
    """
    return mentions_mod.mention_sentiment(mentions, competitor)


def _clean(values: Sequence[str] | str | None) -> tuple[str, ...]:
    """Filter values with the blanks and duplicates removed.

    An EMPTY result means "no filter on this dimension", never "match nothing" — the same
    contract the ``MultiSelect`` in the UI keeps, where unticking everything is the same as
    ticking everything. A bare string is accepted so a hand-written ``?persona=Patient``
    still works.
    """
    if not values:
        return ()
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)


def _selected(values: tuple[str, ...], candidate: str | None) -> bool:
    """Case-insensitive membership, where an empty selection matches everything."""
    if not values:
        return True
    if not candidate:
        return False
    return candidate.strip().lower() in {v.lower() for v in values}


@dataclass(frozen=True)
class Selection:
    """What the reader asked to see. One object so the board, the facet lists and the
    detail drawer cannot end up holding three slightly different readings of it.

    Every dimension is a tuple because the pickers are multi-select: an empty tuple is the
    unfiltered case.
    """

    areas: tuple[str, ...] = ()
    diseases: tuple[str, ...] = ()
    brands: tuple[str, ...] = ()
    competitors: tuple[str, ...] = ()
    personas: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    verdicts: tuple[str, ...] = ()

    @classmethod
    def of(cls, **values) -> "Selection":
        return cls(**{key: _clean(value) for key, value in values.items()})

    def as_dict(self) -> dict:
        return {
            dim: list(getattr(self, dim))
            for dim in (*FILTER_DIMENSIONS, "verdicts")
        }


def answer_matches(answer: "Answer", sel: Selection, *, skip: str = "") -> bool:
    """Does this answer row survive the response-level filters?

    *skip* leaves one dimension out, which is what lets a facet list stay complete: a
    dimension that narrowed its own options would remove every unpicked brand from the
    brand picker, leaving no way to select a second one.
    """
    if skip != DIM_BRAND and not _selected(sel.brands, answer.brand_focus):
        return False
    if skip != DIM_PERSONA and not _selected(sel.personas, answer.persona):
        return False
    if skip != DIM_MODEL and not _selected(sel.models, answer.llm_name):
        return False
    if skip != DIM_AREA and sel.areas:
        # A stored value is either a TA key ("Rheumatology") or a broad area name
        # ("Immunology"), and both spellings reach this filter, so both are accepted.
        key = answer.therapeutic_area
        if not (_selected(sel.areas, key) or _selected(sel.areas, area_for(key))):
            return False
    return True


def pair_matches(pair: pairs_mod.Pair, sel: Selection, *, skip: str = "") -> bool:
    """Does this comparison survive the filters that describe the comparison itself?"""
    if skip != DIM_DISEASE and not _selected(sel.diseases, pair.disease):
        return False
    if skip != DIM_COMPETITOR and not _selected(sel.competitors, pair.competitor):
        return False
    return True


def _latest_score_subquery():
    """Highest ``score_version`` per response — a re-score supersedes, never accumulates."""
    return (
        select(
            ScoringRecord.response_id,
            func.max(ScoringRecord.score_version).label("maxv"),
        )
        .group_by(ScoringRecord.response_id)
        .subquery()
    )


async def load_answers(db: AsyncSession) -> list[Answer]:
    """Every scored BRAND-mode comparison answer, with its comparisons resolved.

    The pair is read off the CURRENT question row first. Responses were written before the
    promote fix and carry no comparator of their own, so joining to the question is what
    lets one backfill light up answers already in the table without rewriting history.

    **Deliberately unfiltered.** The reader's selection is applied in memory by
    ``answer_matches``/``pair_matches`` instead, for two reasons: half the dimensions
    (indication, rival) only exist after ``pairs.resolve`` has run and cannot be expressed
    as a WHERE clause at all, and the facet lists have to be computed over the whole corpus
    or picking one value would delete the others from its own picker. The cost is bounded —
    this is the comparison slice of the response table, not the table.
    """
    subq = _latest_score_subquery()
    stmt = (
        select(Response, ScoringRecord)
        .join(ScoringRecord, ScoringRecord.response_id == Response.response_id)
        .join(
            subq,
            and_(
                ScoringRecord.response_id == subq.c.response_id,
                ScoringRecord.score_version == subq.c.maxv,
            ),
        )
        .where(
            Response.domain == COMPARATIVE_DOMAIN,
            Response.monitoring_mode == BRAND_MODE,
        )
    )
    if HIDDEN_LLM_NAMES:
        stmt = stmt.where(Response.llm_name.notin_(HIDDEN_LLM_NAMES))

    rows = (await db.execute(stmt)).all()
    if not rows:
        return []

    question_ids = {resp.question_id for resp, _ in rows}
    tags = dict((await db.execute(
        select(Question.question_id, Question.competitor_focus).where(
            Question.question_id.in_(question_ids),
            Question.deleted_at.is_(None),
            Question.superseded_by.is_(None),
        )
    )).all())
    diseases = dict((await db.execute(
        select(Question.question_id, Question.disease).where(
            Question.question_id.in_(question_ids),
            Question.deleted_at.is_(None),
            Question.superseded_by.is_(None),
        )
    )).all())

    out: list[Answer] = []
    for resp, score in rows:
        answer = Answer(
            response_id=resp.response_id,
            run_id=resp.run_id,
            question_id=resp.question_id,
            question_text=resp.question_text,
            llm_name=resp.llm_name,
            persona=resp.persona,
            brand_focus=resp.brand_focus,
            therapeutic_area=resp.therapeutic_area,
            disease=resp.disease or diseases.get(resp.question_id),
            timestamp=resp.timestamp_utc,
            position=score.competitive_position,
            our_sentiment=score.sentiment_score,
            brand_mentions=_loads_list(score.brand_mentions),
            key_claims=_loads_list(score.key_claims),
            rationale=score.scoring_rationale,
        )
        answer.resolution = pairs_mod.resolve(
            answer.question_text,
            brand_focus=answer.brand_focus,
            therapeutic_area=answer.therapeutic_area,
            disease=answer.disease,
            competitor_focus=resp.competitor_focus or tags.get(resp.question_id),
        )
        out.append(answer)

    return out


@dataclass
class _Bucket:
    pair: pairs_mod.Pair
    answers: list[Answer] = field(default_factory=list)
    verdicts: list[str] = field(default_factory=list)
    origins: set[str] = field(default_factory=set)


def group_by_pair(answers: list[Answer]) -> tuple[dict[str, _Bucket], dict[str, int]]:
    """``({pair_key: bucket}, {reason: count})`` — resolved comparisons and what was excluded."""
    buckets: dict[str, _Bucket] = {}
    excluded: collections.Counter = collections.Counter()
    for answer in answers:
        if not answer.resolution.resolved:
            excluded[answer.resolution.reason or "unknown"] += 1
            continue
        for pair in answer.resolution.pairs:
            bucket = buckets.setdefault(pair.key, _Bucket(pair=pair))
            bucket.answers.append(answer)
            bucket.origins.add(answer.resolution.origin)
            bucket.verdicts.append(verdict_mod.for_answer(
                answer.position, answer.our_sentiment, answer.their_sentiment(pair.competitor)
            ))
    return buckets, dict(excluded)


def _mean(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(sum(clean) / len(clean), 3) if clean else None


# Least trustworthy first, so ``_weakest_origin`` reads down the list and stops.
_ORIGIN_ORDER = (
    pairs_mod.ORIGIN_TEXT_ONLY,
    pairs_mod.ORIGIN_DERIVED,
    pairs_mod.ORIGIN_STORED,
)


def _weakest_origin(origins: set[str]) -> str:
    for origin in _ORIGIN_ORDER:
        if origin in origins:
            return origin
    return pairs_mod.ORIGIN_DERIVED


def _trend(bucket: _Bucket) -> dict:
    """Latest run's loss rate against the run before it, or an explicit "not yet" state."""
    by_run: dict[str, list[tuple[object, str]]] = {}
    for answer, result in zip(bucket.answers, bucket.verdicts):
        by_run.setdefault(answer.run_id, []).append((answer.timestamp, result))
    if len(by_run) < MIN_TREND_RUNS:
        return {
            "available": False,
            "runs": len(by_run),
            "note": f"Seen in {len(by_run)} run so far — a trend needs at least "
                    f"{MIN_TREND_RUNS}.",
        }
    ordered = sorted(
        by_run.items(),
        key=lambda kv: min(ts for ts, _ in kv[1] if ts is not None) if any(
            ts is not None for ts, _ in kv[1]) else 0,
    )

    def loss_rate(entries: list[tuple[object, str]]) -> float:
        losing = sum(1 for _, v in entries if v == verdict_mod.LOSING)
        return round(losing / len(entries), 3) if entries else 0.0

    previous, latest = ordered[-2], ordered[-1]
    before, now = loss_rate(previous[1]), loss_rate(latest[1])
    return {
        "available": True,
        "runs": len(ordered),
        "previous_loss_rate": before,
        "latest_loss_rate": now,
        "delta": round(now - before, 3),
        # Losing more of the same comparison is the bad direction; named so the UI never has
        # to decide whether a rising number is good.
        "direction": "worse" if now > before else ("better" if now < before else "flat"),
    }


def _slice_by(bucket: _Bucket, attr: str) -> list[dict]:
    """Loss rate per value of one answer attribute, worst absolute exposure first.

    *attr* names the field on ``Answer`` AND the key it is reported under, so a slice can
    never be grouped by one dimension and labelled as another.
    """
    grouped: dict[str, list[str]] = {}
    for answer, result in zip(bucket.answers, bucket.verdicts):
        grouped.setdefault(getattr(answer, attr), []).append(result)
    out = []
    for value, results in grouped.items():
        losing = sum(1 for v in results if v == verdict_mod.LOSING)
        out.append({
            attr: value,
            "answers": len(results),
            "losing": losing,
            "loss_rate": round(losing / len(results), 3) if results else 0.0,
            "verdict": verdict_mod.overall(collections.Counter(results)),
        })
    out.sort(key=lambda r: (-r["losing"], -r["loss_rate"], r[attr]))
    return out


def _by_model(bucket: _Bucket) -> list[dict]:
    """Per-model loss rate — which AI channel is actually costing the comparison."""
    return _slice_by(bucket, "llm_name")


def _by_persona(bucket: _Bucket) -> list[dict]:
    """Per-audience loss rate — whether a patient is told something a prescriber is not.

    Worth separating from ``by_model`` because the remedy differs: a platform losing the
    comparison is a content-placement problem, an audience losing it is a messaging one.
    """
    return _slice_by(bucket, "persona")


def _day(timestamp: object) -> str | None:
    """The ISO date an answer was collected, or ``None`` when none was recorded.

    Anything that is not recognisably a date returns ``None`` rather than being coerced. A
    row with no usable timestamp is real data about a real comparison; dating it to today
    would move it onto a period it was never observed in, which is worse than leaving it
    off the line and saying how many were left off.
    """
    if timestamp is None:
        return None
    text = timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp)
    day = text[:10]
    return day if len(day) == 10 and day[4] == "-" and day[7] == "-" else None


def timeline(buckets: dict[str, _Bucket]) -> dict:
    """Board-level loss rate per day — is the comparison front as a whole moving?

    Three decisions the numbers depend on:

    * **Counts comparison-answers, not distinct responses.** A verdict only exists against a
      named rival, so an answer naming two rivals contributes one graded point to each — the
      same unit ``_by_model`` counts. Said in ``note`` so a caption can repeat it rather than
      leaving the reader to reconcile it against ``answers_on_the_board``.
    * **Bucketed by day, not by run.** Several runs land on one date, and a per-run axis
      would draw them as separate periods of a handful of answers each.
    * **Undated answers are counted, not dropped.** The same rule as the exclusion lines: a
      row the board used but could not place in time is reported, never quietly discarded.

    Per-pair history is usually one or two runs deep, which is why this is aggregated across
    the whole board — ``_trend`` remains the honest per-pair read.
    """
    periods: dict[str, dict] = {}
    runs_by_day: dict[str, set[str]] = {}
    undated = 0
    for bucket in buckets.values():
        for answer, result in zip(bucket.answers, bucket.verdicts):
            day = _day(answer.timestamp)
            if day is None:
                undated += 1
                continue
            period = periods.setdefault(day, {
                "period": day,
                "answers": 0,
                # Seeded off VERDICTS so a new verdict can never be silently dropped from
                # the stack while still being counted in the total.
                **{v.lower(): 0 for v in verdict_mod.VERDICTS},
            })
            period["answers"] += 1
            period[result.lower()] += 1
            runs_by_day.setdefault(day, set()).add(answer.run_id)

    ordered = []
    for day in sorted(periods):
        period = periods[day]
        period["loss_rate"] = (
            round(period[verdict_mod.LOSING.lower()] / period["answers"], 3)
            if period["answers"] else 0.0
        )
        period["runs"] = len(runs_by_day[day])
        ordered.append(period)

    return {
        "granularity": "day",
        "periods": ordered,
        "runs": len({run for runs in runs_by_day.values() for run in runs}),
        "undated": undated,
        "min_periods": MIN_TIMELINE_PERIODS,
        "available": len(ordered) >= MIN_TIMELINE_PERIODS,
        "note": "One point per comparison an answer informs, so an answer naming two rivals "
                "counts once against each — the same unit the per-platform rows use.",
    }


def _disagreement(bucket: _Bucket) -> dict:
    """How often models contradict each other on the SAME question.

    Derived from the answers already loaded rather than from
    ``ConsensusRecord.position_distribution``: that column is a cached aggregate of exactly
    these rows, so recomputing keeps this figure and the position mix shown beside it
    arithmetically consistent, and it still works before consensus aggregation has run.
    """
    by_question: dict[tuple[str, str], set[str]] = {}
    for answer, result in zip(bucket.answers, bucket.verdicts):
        if answer.position:
            by_question.setdefault((answer.run_id, answer.question_id), set()).add(result)
    comparable = {k: v for k, v in by_question.items() if len(v) >= 1}
    multi = [k for k, v in by_question.items() if len(v) > 1]
    return {
        "questions_compared": len(comparable),
        "questions_with_disagreement": len(multi),
        "rate": round(len(multi) / len(comparable), 3) if comparable else 0.0,
    }


def summarize_pair(bucket: _Bucket) -> dict:
    counts = collections.Counter(bucket.verdicts)
    positions = collections.Counter(a.position or "UNSCORED" for a in bucket.answers)
    ours = _mean([a.our_sentiment for a in bucket.answers])
    theirs = _mean([a.their_sentiment(bucket.pair.competitor) for a in bucket.answers])
    losing = counts.get(verdict_mod.LOSING, 0)
    total = len(bucket.answers)
    return {
        **bucket.pair.as_dict(),
        "answers": total,
        "models": sorted({a.llm_name for a in bucket.answers}),
        "personas": sorted({a.persona for a in bucket.answers}),
        "runs": len({a.run_id for a in bucket.answers}),
        "verdict": verdict_mod.overall(counts),
        "verdict_counts": {v: counts.get(v, 0) for v in verdict_mod.VERDICTS},
        "losing_answers": losing,
        "loss_rate": round(losing / total, 3) if total else 0.0,
        "position_mix": dict(positions),
        "our_sentiment": ours,
        "their_sentiment": theirs,
        "sentiment_gap": verdict_mod.sentiment_gap(ours, theirs),
        # An indication-less pair is a weaker claim: the scorer graded it against the
        # flattened therapeutic-area competitor list, not one indication's real field.
        "indication_known": bucket.pair.disease is not None,
        # The WEAKEST origin backing the pair, not the strongest: if any answer landed here
        # only because both drugs happened to be named, the reader must be told that before
        # acting, and a single tagged answer must not launder the rest.
        "pair_source": _weakest_origin(bucket.origins),
        "pair_source_note": pairs_mod.ORIGIN_LABELS.get(_weakest_origin(bucket.origins), ""),
        "by_model": _by_model(bucket),
        "by_persona": _by_persona(bucket),
        "disagreement": _disagreement(bucket),
        "trend": _trend(bucket),
    }


def rank_key(row: dict):
    """Board order: absolute exposure first, rate second.

    Deliberately NOT rate-first. A comparison losing 15 of 25 answers is a bigger problem
    than one losing 2 of 2, but a rate-first sort puts the 100% pair on top and pushes the
    real damage down the page — and since the list is truncated, off it entirely.
    """
    return (-row["losing_answers"], -row["loss_rate"], -row["answers"], row["key"])


def _dimension_value(answer: Answer, dim: str) -> str | None:
    if dim == DIM_AREA:
        return area_for(answer.therapeutic_area) or None
    if dim == DIM_BRAND:
        return answer.brand_focus
    if dim == DIM_PERSONA:
        return answer.persona
    return answer.llm_name


def facet_options(answers: list[Answer], sel: Selection) -> dict[str, list[str]]:
    """The values worth offering on each picker, given what is picked elsewhere.

    Two rules, both of which exist to stop a filter bar from painting the reader into a
    corner:

    * **A dimension never narrows its own list.** Otherwise choosing one brand would remove
      every other brand from the brand picker and there would be no way to add a second.
    * **Only values with an answer behind them are offered.** Every option here is reachable
      through a comparison that is currently on the board, so no combination the UI can
      produce lands on an empty page.

    The verdict filter is deliberately not a facet: it is read off the pair's aggregate, so
    letting it prune the other pickers would hide the very brands a reader clears the
    verdict filter to go looking for.
    """
    found: dict[str, set[str]] = {dim: set() for dim in FILTER_DIMENSIONS}
    for dim in FILTER_DIMENSIONS:
        scoped = [a for a in answers if answer_matches(a, sel, skip=dim)]
        buckets, _ = group_by_pair(scoped)
        for bucket in buckets.values():
            if not pair_matches(bucket.pair, sel, skip=dim):
                continue
            if dim == DIM_DISEASE:
                # A pair with no indication is real and stays on the board; it just cannot
                # be offered as something to filter TO.
                if bucket.pair.disease:
                    found[dim].add(bucket.pair.disease)
            elif dim == DIM_COMPETITOR:
                found[dim].add(bucket.pair.competitor)
            else:
                for answer in bucket.answers:
                    value = _dimension_value(answer, dim)
                    if value:
                        found[dim].add(value)
    return {dim: sorted(values) for dim, values in found.items()}


def assemble(universe: list[Answer], sel: Selection, *, limit: int = 50) -> dict:
    """The board for one selection, out of the whole comparison corpus. Pure.

    Takes the UNFILTERED corpus rather than a pre-filtered list because the facet lists are
    part of the answer: they have to be computed against everything, or the pickers would
    only ever offer what is already selected.
    """
    answers = [a for a in universe if answer_matches(a, sel)]
    buckets, excluded = group_by_pair(answers)
    kept = {k: b for k, b in buckets.items() if pair_matches(b.pair, sel)}

    rows = [summarize_pair(b) for b in kept.values()]
    if sel.verdicts:
        wanted = {v.strip().upper() for v in sel.verdicts}
        rows = [r for r in rows if r["verdict"] in wanted]
    rows.sort(key=rank_key)

    on_board_keys = {r["key"] for r in rows}
    on_board_ids = {
        a.response_id
        for key, bucket in kept.items() if key in on_board_keys
        for a in bucket.answers
    }
    resolved_ids = {a.response_id for a in answers if a.resolution.resolved}
    exclusions = [
        {"reason": reason, "answers": n,
         "explanation": pairs_mod.REASON_LABELS.get(reason, reason)}
        for reason, n in sorted(excluded.items(), key=lambda kv: -kv[1])
    ]
    # Answers a filter removed are reported as their own line rather than folded into the
    # resolution reasons, and last however large: the reader chose this, so it is not a
    # finding about the corpus. The arithmetic still closes — an answer is either on the
    # board, unusable for a stated reason, or filtered out.
    withheld = len(resolved_ids - on_board_ids)
    if withheld:
        exclusions.append(
            {"reason": FILTERED_OUT, "answers": withheld,
             "explanation": FILTERED_OUT_LABEL}
        )

    return {
        "verdict_rule": verdict_mod.RULE_NOTE,
        # Distinct answers, NOT the sum of the per-pair counts below: one answer naming two
        # rivals is counted once here and once under each comparison it informs.
        "answers_examined": len(answers),
        "answers_on_the_board": len(on_board_ids),
        "answers_excluded": len(answers) - len(on_board_ids),
        "exclusions": exclusions,
        "pairs_total": len(rows),
        "pairs": rows[:limit],
        "pairs_truncated": max(0, len(rows) - limit),
        # Over the comparisons that SURVIVED the verdict filter, never every resolved bucket,
        # so the line describes the board being read rather than one the filters removed.
        "timeline": timeline({k: b for k, b in kept.items() if k in on_board_keys}),
        # What the server actually applied, echoed back so a stale or misspelt value in the
        # URL is visible in the UI rather than silently doing nothing.
        "filters_applied": sel.as_dict(),
        "filter_options": facet_options(universe, sel),
        "answers_in_corpus": len(universe),
    }


async def scoreboard(
    db: AsyncSession,
    *,
    therapeutic_areas: Sequence[str] | str | None = None,
    diseases: Sequence[str] | str | None = None,
    brands: Sequence[str] | str | None = None,
    competitors: Sequence[str] | str | None = None,
    personas: Sequence[str] | str | None = None,
    llm_names: Sequence[str] | str | None = None,
    verdicts: Sequence[str] | str | None = None,
    limit: int = 50,
) -> dict:
    """Ranked head-to-head board. Read-only, no model calls.

    Every dimension takes a LIST, and an empty one means "everything" — so a reader can
    watch two brands against three rivals in one view instead of paging through six.
    """
    sel = Selection.of(
        areas=therapeutic_areas, diseases=diseases, brands=brands,
        competitors=competitors, personas=personas, models=llm_names, verdicts=verdicts,
    )
    return assemble(await load_answers(db), sel, limit=limit)


async def _sources_for(db: AsyncSession, cohort: list[str]) -> dict:
    """Whose content AI leaned on when answering THIS comparison.

    Delegates to ``source_authority.service`` scoped to the cohort rather than counting
    citations here, so ownership classification, the hidden-target rule and the
    largest-remainder percentages stay in one place. Best-effort: a comparison is still worth
    reading if the citation graph has not been classified.
    """
    try:
        from app.source_authority import service as sa_svc

        voice = await sa_svc.share_of_voice(db, response_ids=cohort)
        pages = await sa_svc.top_pages(db, response_ids=cohort, control="COMPETITOR", limit=8)
        return {
            "available": bool(voice.get("total_citations")),
            "total_citations": voice.get("total_citations", 0),
            "sourced_answers": voice.get("response_count", 0),
            "abbvie_share_pct": voice.get("abbvie_share_pct", 0.0),
            "competitor_share_pct": voice.get("competitor_share_pct", 0.0),
            "independent_share_pct": voice.get("independent_share_pct", 0.0),
            "competitors": voice.get("competitors", [])[:8],
            "competitor_pages": pages.get("items", []),
            # Parametric targets cite nothing, so a zero here is "answered from model
            # knowledge", not "we found no sources". Said plainly rather than implied.
            "note": "Counts only answers that returned citations. Models answering from "
                    "internal knowledge alone contribute none.",
        }
    except Exception as e:  # noqa: BLE001 — citation colouring is additive
        logger.warning("Head-to-head source read skipped: %s", e)
        return {"available": False, "error": str(e)}


async def _absence_for(db: AsyncSession, bucket: _Bucket, cohort: list[str]) -> dict:
    """The answers that never named us, with the competitor that took the space.

    Routed through ``remediation.gaps.find_gaps`` because that module already ranks weak
    positions, resolves the outperforming competitor and its domain, and feeds the GEO
    intervention engine — so the fix button on this panel and the one on the GEO page act on
    the same records rather than two different opinions about what a gap is.
    """
    absent = sum(1 for a in bucket.answers if a.position == "NOT_MENTIONED")
    out = {
        "not_mentioned_answers": absent,
        "not_mentioned_pct": round(100.0 * absent / len(bucket.answers), 1)
        if bucket.answers else 0.0,
        "gaps": [],
    }
    if not cohort:
        return out
    try:
        from app.remediation import gaps as gaps_mod

        found = await gaps_mod.find_gaps(db, response_ids=cohort, limit=10)
        out["gaps"] = [
            {
                "response_id": g.get("response_id"),
                "llm_name": g.get("llm_name"),
                "competitive_position": g.get("competitive_position"),
                "competitor": g.get("competitor"),
                "competitor_domain": g.get("competitor_domain"),
                "question_text": g.get("question_text"),
            }
            for g in found
        ]
    except Exception as e:  # noqa: BLE001 — absence count stands without the GEO detail
        logger.warning("Head-to-head absence read skipped: %s", e)
        out["error"] = str(e)
    return out


async def pair_detail(
    db: AsyncSession,
    pair_key: str,
    *,
    personas: Sequence[str] | str | None = None,
    llm_names: Sequence[str] | str | None = None,
) -> dict | None:
    """One comparison in full, plus the response cohort the claim/citation reads consume.

    Scoped through the same ``answer_matches`` the board uses, so a drawer opened under a
    two-persona selection reports the numbers the row it was opened from was showing.
    """
    sel = Selection.of(personas=personas, models=llm_names)
    answers = [a for a in await load_answers(db) if answer_matches(a, sel)]
    buckets, _ = group_by_pair(answers)
    bucket = buckets.get(pair_key)
    if bucket is None:
        return None

    ranked = sorted(
        zip(bucket.answers, bucket.verdicts),
        key=lambda pairing: verdict_mod.VERDICT_SEVERITY[pairing[1]],
    )
    cohort = [a.response_id for a in bucket.answers]
    return {
        "verdict_rule": verdict_mod.RULE_NOTE,
        "summary": summarize_pair(bucket),
        "response_ids": cohort,
        "claims": claims_mod.cluster(
            [
                (a.llm_name, result, a.key_claims)
                for a, result in zip(bucket.answers, bucket.verdicts)
            ],
            competitor=bucket.pair.competitor,
        ),
        "sources": await _sources_for(db, cohort),
        "absence": await _absence_for(db, bucket, cohort),
        # Worst answers first: the reader is here to see what is going wrong.
        "sample_answers": [
            {
                "response_id": a.response_id,
                "run_id": a.run_id,
                "question_id": a.question_id,
                "question_text": a.question_text,
                "llm_name": a.llm_name,
                "persona": a.persona,
                "position": a.position,
                "our_sentiment": a.our_sentiment,
                "their_sentiment": a.their_sentiment(bucket.pair.competitor),
                "verdict": result,
                "rationale": a.rationale,
                "key_claims": a.key_claims,
            }
            for a, result in ranked[:MAX_SAMPLE_ANSWERS]
        ],
    }
