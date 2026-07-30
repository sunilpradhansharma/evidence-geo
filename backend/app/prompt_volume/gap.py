"""Gap analysis + demand ranking for Prompt Volume (FR-116.3/.4).

Dependency-free normalized token-overlap matching (Jaccard), consistent with
``insights.tagging``. These are pure functions over plain dicts so they are cheap to unit
test and safe to run off the event loop via ``asyncio.to_thread``.

  - normalize / similarity        : shared text normalization + overlap score
  - match_rows_to_questions       : best-matching approved question per staged query
  - cluster_gap_topics            : group near-duplicate UNMATCHED queries into topics
  - opportunity_score             : volume weighted DOWN by keyword difficulty (high vol + low KD wins)
  - flag_high_volume              : FR-116.3 threshold (absolute floor OR top-percentile)
  - question_demand               : FR-116.4 = SUM of DEDUPLICATED matched-query volume
"""
from __future__ import annotations

import re

# Aliased so the ``synthesize`` boolean parameter of ``cluster_gap_topics`` can't shadow it.
from app.prompt_volume import synthesize as _synthesize

_TOKEN = re.compile(r"[a-z0-9]+")

# Only true function words — we deliberately KEEP clinically meaningful words like
# "long", "term", "side", "dose" so near-duplicate topics still cluster correctly.
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "for", "to", "in", "on", "at", "is", "are", "be",
    "do", "does", "did", "how", "what", "why", "when", "where", "which", "who",
    "can", "could", "will", "would", "should", "with", "and", "or", "my", "me",
    "i", "you", "it", "this", "that", "vs", "versus", "about",
})


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, drop stopwords + 1-char tokens. Order-preserving."""
    tokens = [
        t for t in _TOKEN.findall((text or "").lower())
        if t not in _STOPWORDS and len(t) > 1
    ]
    return " ".join(tokens)


def tokens(text: str) -> set[str]:
    return set(normalize(text).split())


def similarity(a: set[str], b: set[str]) -> float:
    """Jaccard overlap of two token sets (0..1)."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def match_rows_to_questions(
    rows: list[dict], question_tokens: list[tuple[str, set[str]]], threshold: float
) -> None:
    """Set ``matched_question_id`` (best above threshold) + ``match_score`` on each row.

    ``match_score`` always records the best score found (for transparency); the id is only
    set when that best score clears ``threshold`` (a query short of it is a coverage gap).
    """
    for row in rows:
        best_qid, best_score = None, 0.0
        for qid, qtoks in question_tokens:
            score = similarity(row["tokens"], qtoks)
            if score > best_score:
                best_qid, best_score = qid, score
        row["match_score"] = round(best_score, 4)
        row["matched_question_id"] = best_qid if best_score >= threshold else None


def opportunity_score(combined_volume: int, avg_difficulty: float | None) -> float:
    """Volume discounted by keyword difficulty: high demand + low KD = high opportunity.

    ``avg_difficulty`` is a 0-100 SEO keyword-difficulty score. The discount factor is
    ``1 - KD/100`` (clamped to [0.05, 1.0]); an UNKNOWN difficulty applies NO penalty
    (factor 1.0) so a query is never buried just because its export lacked a KD column —
    the analyst can verify difficulty before acting.
    """
    if avg_difficulty is None:
        factor = 1.0
    else:
        kd = min(max(avg_difficulty, 0.0), 100.0)
        factor = max(0.05, 1.0 - kd / 100.0)
    return round(combined_volume * factor, 2)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def cluster_gap_topics(
    unmatched_rows: list[dict], *, group_threshold: float, synthesize: bool = True
) -> list[dict]:
    """Greedily cluster near-duplicate unmatched queries into topics (highest volume first).

    Volume is summed over DEDUPLICATED ``normalized_query`` values so three phrasings of the
    same question count once, not three times. Each topic also carries the mean keyword
    difficulty / CPC of its member queries (when present) and an ``opportunity_score`` that
    weights demand down by difficulty.
    """
    clusters: list[dict] = []
    for row in sorted(unmatched_rows, key=lambda r: r.get("search_volume") or 0, reverse=True):
        placed = False
        for c in clusters:
            if similarity(row["tokens"], c["_rep_tokens"]) >= group_threshold:
                c["queries"].append(row["query_text"])
                c["_vol_by_norm"][row["normalized_query"]] = max(
                    c["_vol_by_norm"].get(row["normalized_query"], 0), row.get("search_volume") or 0
                )
                if row.get("keyword_difficulty") is not None:
                    c["_kd"].append(float(row["keyword_difficulty"]))
                if row.get("cpc") is not None:
                    c["_cpc"].append(float(row["cpc"]))
                placed = True
                break
        if not placed:
            clusters.append({
                "label": row["query_text"],
                "therapeutic_area": row.get("matched_therapeutic_area", "Unmapped"),
                "competitor": row.get("matched_competitor"),
                "queries": [row["query_text"]],
                "_rep_tokens": row["tokens"],
                # Representative (highest-volume) member drives the monitorable question.
                "_rep_prompt": row.get("prompt_text"),
                "_rep_brand": row.get("matched_brand"),
                "_vol_by_norm": {row["normalized_query"]: row.get("search_volume") or 0},
                "_kd": [float(row["keyword_difficulty"])] if row.get("keyword_difficulty") is not None else [],
                "_cpc": [float(row["cpc"])] if row.get("cpc") is not None else [],
            })

    topics = []
    for c in clusters:
        combined = sum(c["_vol_by_norm"].values())
        avg_kd = _mean(c["_kd"])
        # Decide the monitorable question + its provenance (question_origin):
        #   prompt      -> the REAL question the audience asked (question/PAA export)
        #   synthesized -> auto-generated from a bare keyword (synthesis ON)
        #   keyword     -> raw keyword used as-is (analyst declined synthesis)
        rep_prompt = (c.get("_rep_prompt") or "").strip()
        if rep_prompt:
            question, question_origin = rep_prompt, "prompt"
        elif synthesize:
            question = _synthesize.to_question(
                c["label"], brand=c.get("_rep_brand"), competitor=c["competitor"]
            )
            question_origin = "synthesized"
        else:
            question, question_origin = c["label"], "keyword"
        topics.append({
            "label": c["label"],
            "question": question,
            "question_origin": question_origin,
            "brand": c.get("_rep_brand"),
            "therapeutic_area": c["therapeutic_area"],
            "competitor": c["competitor"],
            "query_count": len(c["queries"]),
            "queries": c["queries"],
            "combined_volume": combined,
            "avg_difficulty": avg_kd,
            "avg_cpc": _mean(c["_cpc"]),
            "opportunity_score": opportunity_score(combined, avg_kd),
        })
    topics.sort(key=lambda t: t["combined_volume"], reverse=True)
    return topics


def flag_high_volume(
    topics: list[dict], *, abs_floor: int, top_percentile: float
) -> list[dict]:
    """Return the high-volume topics: combined_volume >= abs_floor OR in the top-N%.

    The percentile floor is the volume at the top-``top_percentile`` rank of the upload, so
    on a large upload only genuinely high-demand topics survive while a tiny upload still
    surfaces its leaders.
    """
    if not topics:
        return []
    volumes = sorted((t["combined_volume"] for t in topics), reverse=True)
    cutoff_index = max(0, int(len(volumes) * max(0.0, min(1.0, top_percentile))) - 1)
    percentile_floor = volumes[cutoff_index]
    return [
        t for t in topics
        if t["combined_volume"] >= abs_floor or t["combined_volume"] >= percentile_floor
    ]


def question_demand(rows: list[dict], weights: dict[str, float]) -> dict[str, dict]:
    """FR-116.4 demand per question = priority_weight × SUM(deduped matched-query volume).

    ``weights`` maps question_id -> priority_weight. Volume is summed over DISTINCT
    ``normalized_query`` values matched to the question so duplicate export rows never
    double-count.
    """
    by_q: dict[str, dict[str, int]] = {}
    for row in rows:
        qid = row.get("matched_question_id")
        if not qid:
            continue
        vols = by_q.setdefault(qid, {})
        nq = row["normalized_query"]
        vols[nq] = max(vols.get(nq, 0), row.get("search_volume") or 0)

    out: dict[str, dict] = {}
    for qid, vol_by_norm in by_q.items():
        matched_volume = sum(vol_by_norm.values())
        weight = weights.get(qid, 1.0)
        out[qid] = {
            "matched_volume": matched_volume,
            "matched_queries": len(vol_by_norm),
            "demand_score": round(weight * matched_volume, 4),
        }
    return out
