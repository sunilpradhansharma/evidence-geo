"""What the models said about any one agent, across every scored answer.

``head_to_head`` answers a narrow question: who wins the us-vs-them comparisons we
deliberately asked. This module answers the broader one underneath it — **across every
scored answer, whatever its question was about, how is one agent being talked about?**

That question needs its own home because a competitor is never a ``brand_focus``: that
column holds the monitored AbbVie brand, so filtering it by a rival's name is guaranteed
to return nothing no matter how much the models talk about them. A competitor's own
sentiment and position live inside ``ScoringRecord.brand_mentions``, which the scorer
writes in both modes — a mention list in BRAND mode, the whole landscape matrix in
DISEASE_STATE (see ``scoring.scorer._persist_score``).

Counting rules the numbers depend on:

* **Silence is not neutral.** An agent is only scored in the answers that NAMED it, so
  every mean is reported beside the number of mentions it was taken over, and answers
  that named nothing are reported separately rather than folded in as zeroes. A mean of
  +0.6 over 3 mentions is not the same fact as a mean of +0.6 over 300.
* **Ownership is a curated fact, not a model's guess.** The ``is_competitor`` flag in the
  payload is whatever the scoring model believed. Whose drug it is is declared in
  brands.yaml, so ``side`` resolves through ``taxonomy`` — which is also why entries filed
  under ``competitors:`` that AbbVie actually owns come back as ours.
* **Aliases collapse to one row.** The scorer echoes back whatever spelling the model used,
  so ``upadacitinib``, ``UPA`` and ``Rinvoq`` are one agent. An uncurated name is kept
  verbatim and marked UNTRACKED rather than silently dropped — a rival nobody has added to
  the config yet is exactly the thing worth seeing.

Read-only. No model calls, so any of this costs nothing to open.
"""
from __future__ import annotations

import collections
import json
from dataclasses import dataclass, field

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.labels import HIDDEN_LLM_NAMES
from app.config.taxonomy import drug_index, is_abbvie_brand, keys_for_area
from app.models.response import Response
from app.models.scoring import ScoringRecord
from app.prompt_volume import mapping
from app.snowflake.client import is_enabled

SIDE_OURS = "OURS"
SIDE_COMPETITOR = "COMPETITOR"
SIDE_UNTRACKED = "UNTRACKED"

# The buckets ``/analytics/sentiment-distribution`` uses. Restated here rather than
# re-invented so a competitor's sentiment mix and the brand-level chart can never disagree
# about what the word "positive" means.
POSITIVE_ABOVE = 0.2
NEGATIVE_BELOW = -0.2

# Enough answers to read without shipping a corpus inside one payload.
MAX_SAMPLE_ANSWERS = 8

COUNTING_NOTE = (
    "Sentiment for an agent is measured only in the answers that NAMED it. Answers that "
    "never named it are reported as not_named_answers, not averaged in as neutral."
)

# Which store the numbers came from. These reads run entirely on the application database,
# while several dashboard KPIs (sentiment_distribution, positioning, llm_comparison) are
# served from Snowflake whenever it is configured. Snowflake is a batched MIRROR that
# accumulates from every environment writing to it, so the warehouse can hold far more
# answers than any single app database — 7.6k against 0.9k on a dev box is a real, observed
# gap, not a hypothetical one.
#
# A share or a mean only means anything against the population it was taken over. Quoting
# "named in 6.1% of answers" beside a KPI counted over a different, larger corpus invites
# exactly the wrong inference, so every payload states its own store rather than leaving the
# denominator to be assumed.
STORE_APP_DB = "application_database"

_WAREHOUSE_BESIDE_NOTE = (
    "Counted over the {n} scored answer(s) in the application database. Snowflake is "
    "enabled and mirrors runs from every environment, so the warehouse can hold more "
    "answers than this store, and these mention reads do NOT query it. Do not compare this "
    "denominator against warehouse-served KPIs (sentiment_distribution, positioning, "
    "llm_comparison) — they may be counted over a different, larger corpus."
)

_SINGLE_STORE_NOTE = (
    "Counted over the {n} scored answer(s) in the application database, which is the same "
    "store every other read uses here, so these denominators are comparable."
)


def corpus_note(answers_counted: int) -> dict:
    """Which store produced these counts, and whether a bigger one sits beside it."""
    enabled = is_enabled()
    template = _WAREHOUSE_BESIDE_NOTE if enabled else _SINGLE_STORE_NOTE
    return {
        "store": STORE_APP_DB,
        "warehouse_enabled": enabled,
        "note": template.format(n=answers_counted),
    }


# ---------------------------------------------------------------------------
# Naming — one rule, shared
# ---------------------------------------------------------------------------
def entry_name(entry: object) -> str:
    """The agent name inside a scorer mention payload, whichever key it used."""
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("brand") or entry.get("name") or "").strip()


def mention_matches(name: str | None, wanted: str | None) -> bool:
    """True when a scorer-echoed *name* refers to *wanted*.

    THE name-comparison rule for mention payloads, so ``head_to_head`` and the response
    filter cannot drift apart. An exact compare is not enough — the scorer echoes whatever
    spelling the model used, so ``upadacitinib`` has to match ``Rinvoq`` — and alias
    resolution goes through the shared ``mapping`` matcher rather than a second opinion.
    """
    name = (name or "").strip()
    wanted = (wanted or "").strip()
    if not name or not wanted:
        return False
    return name.lower() == wanted.lower() or mapping.mentions(name, wanted)


def mention_sentiment(mentions: list | None, wanted: str) -> float | None:
    """The scorer's sentiment for one named agent.

    ``None`` means "no number available" — either the agent was not named or the payload
    carried no parseable sentiment. Callers that need to tell those apart should check
    ``names_agent`` as well.
    """
    for entry in mentions or []:
        if not mention_matches(entry_name(entry), wanted):
            continue
        value = entry.get("sentiment")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    return None


def names_agent(mentions: list | None, wanted: str) -> bool:
    """True when the payload names *wanted* AND says it was actually mentioned.

    A DISEASE_STATE landscape row lists every agent the scorer considered, including ones
    the answer never brought up (``mentioned: false``). Those are evidence of absence, not
    of presence, so they must not be counted as mentions.
    """
    for entry in mentions or []:
        if mention_matches(entry_name(entry), wanted):
            return bool(entry.get("mentioned", True))
    return False


def canonical_agent(name: str | None) -> str:
    """Collapse a scorer-echoed spelling onto its curated name; keep it verbatim if uncurated."""
    record = drug_index().get((name or "").strip().lower())
    return (record or {}).get("canonical") or (name or "").strip()


def side_of(canonical: str | None) -> str:
    """``OURS`` / ``COMPETITOR`` / ``UNTRACKED``, decided by brands.yaml.

    Ownership keys off the declared ``company`` rather than the block a drug is listed
    under, because some entries under ``competitors:`` are AbbVie's own.
    """
    if is_abbvie_brand(canonical):
        return SIDE_OURS
    if (canonical or "").strip().lower() in drug_index():
        return SIDE_COMPETITOR
    return SIDE_UNTRACKED


def _loads_list(raw) -> list:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _mean(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(sum(clean) / len(clean), 3) if clean else None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _scope(stmt, scope: dict):
    """Apply the shared response scope to any statement over ``Response``."""
    area = scope.get("therapeutic_area")
    if area:
        child = keys_for_area(area)
        stmt = stmt.where(
            Response.therapeutic_area.in_(child) if child
            else Response.therapeutic_area == area
        )
    if scope.get("indication"):
        stmt = stmt.where(Response.indication == scope["indication"])
    if scope.get("disease"):
        stmt = stmt.where(Response.disease == scope["disease"])
    if scope.get("brand"):
        stmt = stmt.where(Response.brand_focus == scope["brand"])
    if scope.get("persona"):
        stmt = stmt.where(Response.persona == scope["persona"])
    if scope.get("llm_name"):
        stmt = stmt.where(Response.llm_name == scope["llm_name"])
    if scope.get("run_id"):
        stmt = stmt.where(Response.run_id == scope["run_id"])
    if scope.get("monitoring_mode"):
        stmt = stmt.where(Response.monitoring_mode == scope["monitoring_mode"])
    if HIDDEN_LLM_NAMES:
        stmt = stmt.where(Response.llm_name.notin_(HIDDEN_LLM_NAMES))
    return stmt


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


def _scored_stmt():
    subq = _latest_score_subquery()
    return (
        select(Response, ScoringRecord)
        .join(ScoringRecord, ScoringRecord.response_id == Response.response_id)
        .join(
            subq,
            and_(
                ScoringRecord.response_id == subq.c.response_id,
                ScoringRecord.score_version == subq.c.maxv,
            ),
        )
    )


def _alias_narrowing(agent: str):
    """A SQL pre-filter over the mention JSON. **Narrows only; never decides.**

    Any spelling ``mention_matches`` accepts must contain one of the agent's aliases
    verbatim, so a LIKE over those aliases cannot drop a real match. The authoritative
    answer is still ``mention_matches`` on the parsed payload — this only keeps the scan
    off rows that cannot possibly qualify.
    """
    aliases = mapping.aliases_for_drug(agent)
    if not aliases:
        return None
    return or_(*[
        func.lower(ScoringRecord.brand_mentions).like(f"%{alias}%") for alias in aliases
    ])


async def matching_response_ids(db: AsyncSession, agent: str) -> set[str]:
    """Response ids whose LATEST score names *agent*.

    Exists so ``response_service`` can offer a competitor filter with an honest ``total``:
    resolving the ids up front keeps the count and the page in agreement, which a
    post-pagination filter could not do.
    """
    if not (agent or "").strip():
        return set()
    subq = _latest_score_subquery()
    stmt = (
        select(ScoringRecord.response_id, ScoringRecord.brand_mentions)
        .join(
            subq,
            and_(
                ScoringRecord.response_id == subq.c.response_id,
                ScoringRecord.score_version == subq.c.maxv,
            ),
        )
        .where(ScoringRecord.brand_mentions.is_not(None))
    )
    narrowing = _alias_narrowing(agent)
    if narrowing is not None:
        stmt = stmt.where(narrowing)
    rows = (await db.execute(stmt)).all()
    return {
        response_id for response_id, payload in rows
        if names_agent(_loads_list(payload), agent)
    }


async def _load(db: AsyncSession, scope: dict) -> tuple[list, int]:
    """``(rows, answers_total)`` — scored rows in scope, and how many answers were in scope."""
    total = (await db.execute(
        _scope(select(func.count()).select_from(Response), scope)
    )).scalar() or 0
    rows = (await db.execute(_scope(_scored_stmt(), scope))).all()
    return list(rows), int(total)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
@dataclass
class _Slice:
    answers: int = 0
    sentiments: list[float] = field(default_factory=list)


@dataclass
class _Agg:
    canonical: str
    spellings: set[str] = field(default_factory=set)
    answers: set[str] = field(default_factory=set)
    not_named: set[str] = field(default_factory=set)
    sentiments: list[float] = field(default_factory=list)
    positions: collections.Counter = field(default_factory=collections.Counter)
    by_model: dict[str, _Slice] = field(default_factory=dict)
    by_persona: dict[str, _Slice] = field(default_factory=dict)
    by_area: dict[str, _Slice] = field(default_factory=dict)
    by_our_brand: dict[str, _Slice] = field(default_factory=dict)

    def note(self, bucket: dict[str, _Slice], key: str | None, sentiment: float | None) -> None:
        if not key:
            return
        slot = bucket.setdefault(key, _Slice())
        slot.answers += 1
        if sentiment is not None:
            slot.sentiments.append(sentiment)


def _slices(bucket: dict[str, _Slice], label: str) -> list[dict]:
    out = [
        {label: key, "answers": s.answers, "avg_sentiment": _mean(s.sentiments),
         "sentiment_n": len(s.sentiments)}
        for key, s in bucket.items()
    ]
    out.sort(key=lambda r: (-r["answers"], r[label]))
    return out


def _collect(rows: list) -> dict[str, _Agg]:
    """Fold every scored answer's mention payload into one aggregate per agent."""
    agg: dict[str, _Agg] = {}
    for resp, score in rows:
        for entry in _loads_list(score.brand_mentions):
            raw = entry_name(entry)
            if not raw:
                continue
            canonical = canonical_agent(raw)
            record = agg.setdefault(canonical, _Agg(canonical=canonical))
            record.spellings.add(raw)
            if not entry.get("mentioned", True):
                # A landscape row the answer never actually brought up.
                record.not_named.add(resp.response_id)
                continue
            record.answers.add(resp.response_id)
            sentiment = entry.get("sentiment")
            try:
                sentiment = float(sentiment) if sentiment is not None else None
            except (TypeError, ValueError):
                sentiment = None
            if sentiment is not None:
                record.sentiments.append(sentiment)
            position = entry.get("position")
            if position:
                record.positions[str(position)] += 1
            record.note(record.by_model, resp.llm_name, sentiment)
            record.note(record.by_persona, resp.persona, sentiment)
            record.note(record.by_area, resp.therapeutic_area, sentiment)
            record.note(record.by_our_brand, resp.brand_focus, sentiment)
    return agg


def _mix(sentiments: list[float]) -> dict[str, int]:
    mix = {"positive": 0, "neutral": 0, "negative": 0}
    for s in sentiments:
        if s > POSITIVE_ABOVE:
            mix["positive"] += 1
        elif s < NEGATIVE_BELOW:
            mix["negative"] += 1
        else:
            mix["neutral"] += 1
    return mix


def _serialize(record: _Agg, *, answers_scored: int, detail: bool = False) -> dict:
    named = len(record.answers)
    out = {
        "agent": record.canonical,
        "side": side_of(record.canonical),
        "answers_naming_it": named,
        # Of the answers that were SCORED, since an unscored answer has no mention payload
        # to have named anyone in.
        "share_of_scored_answers_pct": round(100.0 * named / answers_scored, 1)
        if answers_scored else 0.0,
        "avg_sentiment": _mean(record.sentiments),
        "sentiment_n": len(record.sentiments),
        "sentiment_mix": _mix(record.sentiments),
        # Populated only by DISEASE_STATE landscape scoring; BRAND-mode mention payloads
        # carry no per-agent position.
        "positions": dict(record.positions),
        "considered_but_not_named_answers": len(record.not_named),
        "spellings": sorted(record.spellings),
    }
    if detail:
        out["by_model"] = _slices(record.by_model, "llm_name")
        out["by_persona"] = _slices(record.by_persona, "persona")
        out["by_therapeutic_area"] = _slices(record.by_area, "therapeutic_area")
        out["by_our_brand"] = _slices(record.by_our_brand, "brand_focus")
    return out


def _scope_dict(**kwargs) -> dict:
    return {k: (v.strip() if isinstance(v, str) else v) or None for k, v in kwargs.items()}


# ---------------------------------------------------------------------------
# Public reads
# ---------------------------------------------------------------------------
async def rollup(
    db: AsyncSession,
    *,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    disease: str | None = None,
    brand: str | None = None,
    persona: str | None = None,
    llm_name: str | None = None,
    run_id: str | None = None,
    monitoring_mode: str | None = None,
    side: str | None = None,
    limit: int = 25,
) -> dict:
    """Every agent the models named in scope, ranked by how many answers name it."""
    scope = _scope_dict(
        therapeutic_area=therapeutic_area, indication=indication, disease=disease,
        brand=brand, persona=persona, llm_name=llm_name, run_id=run_id,
        monitoring_mode=monitoring_mode,
    )
    rows, answers_total = await _load(db, scope)
    agg = _collect(rows)
    answers_scored = len(rows)

    agents = [_serialize(r, answers_scored=answers_scored) for r in agg.values()]
    if side:
        wanted = side.strip().upper()
        agents = [a for a in agents if a["side"] == wanted]
    agents.sort(key=lambda a: (-a["answers_naming_it"], -(a["avg_sentiment"] or 0), a["agent"]))

    return {
        "scope": {k: v for k, v in scope.items() if v} or {"all": True},
        "answers_total": answers_total,
        "answers_scored": answers_scored,
        # Scoring is best-effort after a run, so an unscored answer is a real state, not an
        # error. Stated so a small agent count is never mistaken for a quiet market.
        "answers_unscored": max(0, answers_total - answers_scored),
        "agents_total": len(agents),
        "agents": agents[:limit],
        "agents_truncated": max(0, len(agents) - limit),
        "counting_note": COUNTING_NOTE,
        "corpus": corpus_note(answers_scored),
    }


async def agent_detail(
    db: AsyncSession,
    agent: str,
    *,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    disease: str | None = None,
    brand: str | None = None,
    persona: str | None = None,
    llm_name: str | None = None,
    run_id: str | None = None,
    monitoring_mode: str | None = None,
) -> dict:
    """One agent in full: the rollup entry, its breakdowns, and sample answers naming it.

    Returns ``found: False`` rather than raising when nothing in scope names the agent —
    "no model brought them up" is a real, reportable answer, not an error.
    """
    scope = _scope_dict(
        therapeutic_area=therapeutic_area, indication=indication, disease=disease,
        brand=brand, persona=persona, llm_name=llm_name, run_id=run_id,
        monitoring_mode=monitoring_mode,
    )
    rows, answers_total = await _load(db, scope)
    answers_scored = len(rows)
    canonical = canonical_agent(agent)
    agg = _collect(rows)

    record = agg.get(canonical)
    if record is None:
        # Fall back to an alias-aware scan: the models may only ever have used a spelling
        # that resolves to a different canonical key than the one the caller typed.
        for key, candidate in agg.items():
            if mention_matches(key, agent) or any(
                mention_matches(s, agent) for s in candidate.spellings
            ):
                record = candidate
                canonical = key
                break

    base = {
        "agent": canonical,
        "requested": agent,
        "side": side_of(canonical),
        "scope": {k: v for k, v in scope.items() if v} or {"all": True},
        "answers_total": answers_total,
        "answers_scored": answers_scored,
        "answers_unscored": max(0, answers_total - answers_scored),
        "counting_note": COUNTING_NOTE,
        "corpus": corpus_note(answers_scored),
    }
    if record is None:
        return {
            **base,
            "found": False,
            "note": (
                f"No scored answer in this scope names {agent}. That is a finding about the "
                "answers, not a missing filter — a competitor is never a brand_focus, so it "
                "only appears when a model actually brought it up."
            ),
            "sample_answers": [],
        }

    summary = _serialize(record, answers_scored=answers_scored, detail=True)
    naming = [(resp, score) for resp, score in rows if resp.response_id in record.answers]
    # Most negative first: the reader is here to see where the agent is beating us.
    naming.sort(key=lambda rs: (
        mention_sentiment(_loads_list(rs[1].brand_mentions), canonical) or 0.0
    ))
    return {
        **base,
        "found": True,
        "summary": summary,
        "sample_answers": [
            {
                "response_id": resp.response_id,
                "run_id": resp.run_id,
                "question_id": resp.question_id,
                "question_text": resp.question_text,
                "llm_name": resp.llm_name,
                "persona": resp.persona,
                "brand_focus": resp.brand_focus,
                "therapeutic_area": resp.therapeutic_area,
                "their_sentiment": mention_sentiment(
                    _loads_list(score.brand_mentions), canonical
                ),
                # Our brand's position in the SAME answer, so the two are read together.
                "our_position": score.competitive_position,
                "our_sentiment": score.sentiment_score,
                "key_claims": _loads_list(score.key_claims),
            }
            for resp, score in naming[:MAX_SAMPLE_ANSWERS]
        ],
    }
