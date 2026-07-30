"""Prompt Volume ingestion + analysis engine (FR-116).

One upload = one atomic transaction: parse (Pandas) -> PII pre-flight over the whole file
(reject the ENTIRE upload on any hit, before any DB write) -> map to the taxonomy -> analyze
(match against the Approved Question Bank, cluster gap topics, flag high-volume) -> persist
a batch + its staging rows and commit. If parsing or PII rejection raises, nothing is
persisted. CPU-bound token matching runs off the event loop via ``asyncio.to_thread``.
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance import phi
from app.config.settings import get_settings
from app.models.prompt_volume import (
    METRIC_PROMPT_FREQUENCY,
    METRIC_SEARCH_VOLUME_PROXY,
    PromptVolumeBatch,
    PromptVolumeStaging,
)
from app.models.question import Question
from app.prompt_volume import gap, mapping, parser
from app.prompt_volume.linter import lint


def _new_batch_id() -> str:
    return f"PV-{uuid.uuid4().hex[:10]}"


async def _approved_questions(db: AsyncSession) -> list[dict]:
    stmt = select(Question).where(
        Question.approval_status == "APPROVED",
        Question.active.is_(True),
        Question.deleted_at.is_(None),
        Question.superseded_by.is_(None),
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return [{"question_id": q.question_id, "question_text": q.question_text} for q in rows]


def _collapse_by_recurrence(rows: list[dict]) -> list[dict]:
    """Collapse duplicate prompts into distinct rows whose volume is their recurrence count.

    Used when an upload has NO volume column (a Profound / AlsoAsked prompt log): a prompt
    asked 12 times across models carries more demand signal than one asked once, so its
    frequency becomes the volume proxy. Groups on ``normalized_query`` (the same key the rest
    of the pipeline dedupes on) and keeps the first occurrence as the representative row.
    """
    grouped: dict[str, dict] = {}
    for r in rows:
        key = r["normalized_query"] or r["query_text"].strip().lower()
        existing = grouped.get(key)
        if existing:
            existing["search_volume"] += 1
        else:
            grouped[key] = {**r, "search_volume": 1}
    return list(grouped.values())


def _analyze(
    rows: list[dict], questions: list[dict], settings, synthesize: bool = True,
    derive_volume: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Pure, CPU-bound: normalize, map, (optionally derive volume), match, cluster, flag.

    ``derive_volume`` collapses duplicate prompts and uses their recurrence count as the
    volume, for uploads that carried no volume column. Safe for ``to_thread``.
    """
    for r in rows:
        r["normalized_query"] = gap.normalize(r["query_text"])
        r["tokens"] = gap.tokens(r["query_text"])
        m = mapping.map_query(r["query_text"])
        r["matched_therapeutic_area"] = m["therapeutic_area"]
        r["matched_competitor"] = m["competitor"]
        r["matched_brand"] = m["brand"]
        r["mapping_confidence"] = m["confidence"]

    if derive_volume:
        rows = _collapse_by_recurrence(rows)

    q_tokens = [(q["question_id"], gap.tokens(q["question_text"])) for q in questions]
    gap.match_rows_to_questions(rows, q_tokens, settings.prompt_volume_match_threshold)

    unmatched = [r for r in rows if not r.get("matched_question_id")]
    topics = gap.cluster_gap_topics(
        unmatched, group_threshold=settings.prompt_volume_match_threshold, synthesize=synthesize
    )
    flagged = gap.flag_high_volume(
        topics,
        abs_floor=settings.prompt_volume_abs_volume_floor,
        top_percentile=settings.prompt_volume_top_percentile,
    )
    return rows, flagged


async def analyze_rows(
    db: AsyncSession, rows: list[dict], *, synthesize: bool, volume_present: bool,
) -> tuple[list[dict], list[dict]]:
    """Map + match + cluster + flag a list of canonical row dicts (off the event loop).

    Shared by the CSV uploader and the in-app SEMrush fetch. A source with no volume has its
    demand derived from prompt recurrence (collapsing duplicate rows); a source WITH volume
    (SEO exports, SEMrush) keeps every distinct row. Returns ``(rows, flagged_gap_topics)``.
    """
    settings = get_settings()
    questions = await _approved_questions(db)
    return await asyncio.to_thread(
        _analyze, rows, questions, settings, synthesize, not volume_present
    )


async def persist_batch(
    db: AsyncSession,
    *,
    rows: list[dict],
    flagged: list[dict],
    volume_present: bool,
    source_tool: str,
    source_label: str,
    dataset_date: str,
    synthesize: bool,
    filename: str | None = None,
    raw_row_count: int | None = None,
) -> dict:
    """Persist one analyzed batch + its staging rows atomically (single commit).

    Rollback on any failure -> no partial batch. ``rows`` must already carry the analysis
    fields set by :func:`analyze_rows`. Shared by the CSV and SEMrush ingest paths.
    """
    # Label the batch honestly: a real search-volume proxy vs a frequency-derived proxy.
    metric_type = METRIC_SEARCH_VOLUME_PROXY if volume_present else METRIC_PROMPT_FREQUENCY
    batch_id = _new_batch_id()
    try:
        db.add(PromptVolumeBatch(
            batch_id=batch_id,
            source_tool=source_tool,
            source_label=source_label,
            dataset_date=dataset_date,
            metric_type=metric_type,
            filename=filename,
            synthesize_questions=synthesize,
            rows_total=raw_row_count if raw_row_count is not None else len(rows),
            rows_ingested=len(rows),
            rows_rejected=0,
            gap_topics_flagged=len(flagged),
        ))
        for r in rows:
            # Defensive redaction — CSV uploads already passed the PII gate and SEMrush rows
            # are public SEO terms, so this is a no-op on clean data but guarantees no
            # identifier is ever persisted.
            clean_query, _ = phi.redact(r["query_text"])
            clean_prompt = phi.redact(r["prompt_text"])[0] if r.get("prompt_text") else None
            db.add(PromptVolumeStaging(
                batch_id=batch_id,
                query_text=clean_query,
                prompt_text=clean_prompt,
                normalized_query=r["normalized_query"],
                search_volume=r.get("search_volume") or 0,
                keyword_difficulty=r.get("keyword_difficulty"),
                cpc=r.get("cpc"),
                matched_therapeutic_area=r["matched_therapeutic_area"],
                matched_competitor=r["matched_competitor"],
                matched_brand=r["matched_brand"],
                mapping_confidence=r["mapping_confidence"],
                matched_question_id=r.get("matched_question_id"),
                match_score=r.get("match_score") or 0.0,
            ))
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return {
        "batch_id": batch_id,
        "source_tool": source_tool,
        "source_label": source_label,
        "dataset_date": dataset_date,
        "metric_type": metric_type,
        "synthesize_questions": synthesize,
        "rows_ingested": len(rows),
        "gap_topics_flagged": len(flagged),
        "gap_topics": flagged,
    }


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
    """Ingest one CSV upload. Raises CsvValidationError / PiiRejection before any DB write.

    ``synthesize`` = auto-generate a natural question for bare-keyword gaps (analyst choice,
    persisted on the batch so on-demand gap re-computation honours it).
    """
    settings = get_settings()

    max_bytes = settings.prompt_volume_max_upload_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise parser.CsvValidationError(
            f"File exceeds the {settings.prompt_volume_max_upload_mb} MB upload limit."
        )

    # 1) Parse (Pandas). 2) PII pre-flight over the WHOLE file — both may raise before writes.
    df, rows, volume_present = parser.read_csv(content)
    lint(df)  # raises PiiRejection -> caller returns 422, nothing persisted

    # 3) Map + 4) analyze (off the event loop; pure over plain dicts).
    raw_row_count = len(rows)
    rows, flagged = await analyze_rows(db, rows, synthesize=synthesize, volume_present=volume_present)

    # 5) Persist atomically.
    return await persist_batch(
        db, rows=rows, flagged=flagged, volume_present=volume_present,
        source_tool=source_tool, source_label=source_label, dataset_date=dataset_date,
        filename=filename, synthesize=synthesize, raw_row_count=raw_row_count,
    )


async def ingest_rows(
    db: AsyncSession,
    *,
    rows: list[dict],
    volume_present: bool,
    source_tool: str,
    source_label: str,
    dataset_date: str,
    synthesize: bool = True,
    filename: str | None = None,
) -> dict:
    """Ingest already-fetched canonical rows (the in-app SEMrush path — no CSV/PII step).

    Rows are machine-fetched public keywords/questions (``query_text`` + optional
    ``prompt_text``/``search_volume``/``cpc``), so there is no file to lint; persistence still
    applies defensive PII redaction. Reuses the same analyze + persist as the CSV uploader.
    """
    raw_row_count = len(rows)
    rows, flagged = await analyze_rows(db, rows, synthesize=synthesize, volume_present=volume_present)
    return await persist_batch(
        db, rows=rows, flagged=flagged, volume_present=volume_present,
        source_tool=source_tool, source_label=source_label, dataset_date=dataset_date,
        filename=filename, synthesize=synthesize, raw_row_count=raw_row_count,
    )
