"""Read coverage, generate the missing questions, stage them for review.

Staging targets ``HarvestedQuestion`` with ``source="curation"``, so generated candidates
land in the SAME reviewer queue as web-harvested and evidence-derived ones and reach a
monitoring run only through the existing promote -> Medical-Affairs approval path. There
is deliberately no second promotion route here: a question the model wrote is not more
trusted than a question a patient wrote.
"""
from __future__ import annotations

import json
import math

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.curation import coverage, generator
from app.curation.coverage import Cell, CellCoverage
from app.guardrails.adverse_event import looks_like_ae
from app.guardrails.injection import scan_injection
from app.models.harvested_question import HarvestedQuestion, utcnow
from app.models.question import Question
from app.models.response import Response
from app.models.scoring import ScoringRecord
from app.utils.audit import write_audit
from app.utils.logging import get_logger
from app.utils.pii_lint import scan_for_pii

logger = get_logger("curation.service")

SOURCE = "curation"

# Cells per model call. Small enough that one malformed reply loses little work, large
# enough that a 40-gap run is 8 calls rather than 40.
BATCH_SIZE = 5

# Hard ceiling on one generate request, so a mis-scoped call cannot bill for the whole
# matrix. The caller must page rather than raise this.
MAX_CELLS_PER_RUN = 50


def estimate_model_calls(cell_count: int) -> int:
    """Exact number of model calls a run of this size will make (shown before the click)."""
    return math.ceil(max(0, cell_count) / BATCH_SIZE)


async def _existing_questions(db: AsyncSession) -> list[dict]:
    """Everything that already counts as coverage: the bank plus the pending queue.

    A candidate still sitting in review counts. Regenerating it would show the reviewer
    the same question twice and make the gap count lie about what is outstanding.
    """
    bank = (await db.execute(
        select(Question.question_text, Question.persona).where(Question.deleted_at.is_(None))
    )).all()
    staged = (await db.execute(
        select(HarvestedQuestion.question_text, HarvestedQuestion.persona).where(
            HarvestedQuestion.status.in_(("CLASSIFIED", "PROMOTED"))
        )
    )).all()
    return [
        {"question_text": text, "persona": persona}
        for text, persona in [*bank, *staged]
    ]


async def coverage_report(
    db: AsyncSession,
    *,
    brands: list[str] | None = None,
    therapeutic_areas: list[str] | None = None,
    diseases: list[str] | None = None,
    personas: list[str] | None = None,
    limit: int = 100,
) -> dict:
    """Which comparisons the bank covers, and the ranked list of the ones it does not."""
    cells = coverage.build_matrix(
        brands=brands,
        therapeutic_areas=therapeutic_areas,
        diseases=diseases,
        personas=personas,
    )
    questions = await _existing_questions(db)
    items = coverage.apply_coverage(cells, questions)
    gaps = coverage.rank(items)
    return {
        "scope": {
            "brands": brands or [],
            "therapeutic_areas": therapeutic_areas or [],
            "diseases": diseases or [],
            "personas": list(personas or coverage.DEFAULT_PERSONAS),
        },
        "summary": coverage.summarize(items),
        "gaps": [item.cell.as_dict() for item in gaps[:limit]],
        "gaps_truncated": max(0, len(gaps) - limit),
        "estimated_model_calls": estimate_model_calls(min(len(gaps), MAX_CELLS_PER_RUN)),
    }


# --- coverage vs actually monitored -------------------------------------------------
# ``coverage_report`` answers "does a question for this comparison exist". That is not the
# question a brand team is asking, and the difference is not cosmetic: ``_existing_questions``
# counts a candidate still sitting in review as coverage, so a scope can read as fully
# covered while not one of its comparisons has ever been put to a model.
STATE_NOT_ASKED = "NOT_ASKED"
STATE_IN_REVIEW = "IN_REVIEW"
STATE_APPROVED_NOT_RUN = "APPROVED_NOT_RUN"
STATE_ANSWERED = "ANSWERED"
# A reviewer looked at this comparison and said no. NOT backlog, and deliberately not folded
# into NOT_ASKED: ``_stage_one`` refuses to overwrite a decided row, so a declined cell can
# never be generated again and would otherwise sit in the gap count forever, inviting work
# that the generator will silently decline to do.
STATE_DECLINED = "DECLINED"

# Best (most monitored) first — the state of a cell is the best state any question covering
# it has reached, because one answered question means the comparison IS being watched.
FUNNEL_STATES = (
    STATE_ANSWERED,
    STATE_APPROVED_NOT_RUN,
    STATE_IN_REVIEW,
    STATE_DECLINED,
    STATE_NOT_ASKED,
)

STATE_LABELS = {
    STATE_ANSWERED: "Monitored — models have answered it and the answers are scored",
    STATE_APPROVED_NOT_RUN: "Approved but never run — no answer exists yet",
    STATE_IN_REVIEW: "Waiting on Medical-Affairs review — not monitored yet",
    STATE_DECLINED: "Declined by a reviewer — will not be generated again",
    STATE_NOT_ASKED: "No question exists for this comparison",
}

_STAGED_LIVE = ("CLASSIFIED", "QUARANTINED_AE")


def cell_state(
    *,
    answered: bool,
    approved: bool,
    pending: bool,
    staged_status: str | None,
) -> str:
    """The single state of one comparison. Pure, so the precedence is testable."""
    if answered:
        return STATE_ANSWERED
    if approved:
        return STATE_APPROVED_NOT_RUN
    if pending or staged_status in _STAGED_LIVE:
        return STATE_IN_REVIEW
    if staged_status == "REJECTED":
        return STATE_DECLINED
    return STATE_NOT_ASKED


async def coverage_funnel(
    db: AsyncSession,
    *,
    brands: list[str] | None = None,
    therapeutic_areas: list[str] | None = None,
    diseases: list[str] | None = None,
    personas: list[str] | None = None,
    limit: int = 100,
) -> dict:
    """How far each comparison actually got: not asked -> in review -> approved -> answered.

    Cells are matched to the bank through ``coverage.covers`` (the same rule the matrix
    uses) and to staged candidates through the cell's own ``dedupe_hash``, which is an exact
    key rather than a text comparison.
    """
    cells = coverage.build_matrix(
        brands=brands, therapeutic_areas=therapeutic_areas,
        diseases=diseases, personas=personas,
    )

    bank = (await db.execute(
        select(
            Question.question_id, Question.question_text,
            Question.persona, Question.approval_status,
        ).where(
            Question.deleted_at.is_(None),
            Question.superseded_by.is_(None),
        )
    )).all()

    staged_status = dict((await db.execute(
        select(HarvestedQuestion.dedupe_hash, HarvestedQuestion.status)
        .where(HarvestedQuestion.source == SOURCE)
    )).all())

    # A question is "answered" once any of its responses carries a score. The join is what
    # separates approved-and-idle from actually monitored.
    answered_ids = set((await db.execute(
        select(Response.question_id)
        .join(ScoringRecord, ScoringRecord.response_id == Response.response_id)
        .distinct()
    )).scalars().all())

    verdicts = await _pair_verdicts(db)

    rows: list[dict] = []
    tally: dict[str, int] = {state: 0 for state in FUNNEL_STATES}
    for cell in cells:
        covering = [
            (qid, status) for qid, text, persona, status in bank
            if coverage.covers(cell, text, persona=persona)
        ]
        state = cell_state(
            answered=any(qid in answered_ids for qid, _ in covering),
            approved=any(status == "APPROVED" for _, status in covering),
            pending=any(status == "PENDING" for _, status in covering),
            staged_status=staged_status.get(cell.dedupe_hash()),
        )
        tally[state] += 1
        rows.append({
            **cell.as_dict(),
            "state": state,
            "state_label": STATE_LABELS[state],
            "questions": len(covering),
            # Persona-agnostic: the scoreboard aggregates a comparison across personas, so
            # this is the verdict for the pairing, not for this cell's persona alone.
            "verdict": verdicts.get(f"{cell.brand}|{cell.competitor}|{cell.disease}"),
        })

    total = len(cells) or 1
    monitored = tally[STATE_ANSWERED]
    # What today's coverage number counts: a question exists and has not been declined.
    counted_covered = total - tally[STATE_NOT_ASKED] - tally[STATE_DECLINED]
    return {
        "scope": {
            "brands": brands or [],
            "therapeutic_areas": therapeutic_areas or [],
            "diseases": diseases or [],
            "personas": list(personas or coverage.DEFAULT_PERSONAS),
        },
        "total_cells": len(cells),
        "states": [
            {"state": s, "label": STATE_LABELS[s], "cells": tally[s]} for s in FUNNEL_STATES
        ],
        "monitored_cells": monitored,
        "monitored_pct": round(100.0 * monitored / total, 1),
        "counted_as_covered": counted_covered,
        # The headline: comparisons that look covered but no model has ever been asked.
        "covered_but_unmonitored": max(0, counted_covered - monitored),
        "cells": rows[:limit],
        "cells_truncated": max(0, len(rows) - limit),
    }


async def _pair_verdicts(db: AsyncSession) -> dict[str, str]:
    """``{pair_key: verdict}`` for answered comparisons. Best-effort by design.

    The funnel's job is to report how far each comparison got; a failure to also colour it
    with a verdict must not take the whole answer down with it.
    """
    try:
        from app.competitive import head_to_head as h2h

        answers = await h2h.load_answers(db)
        buckets, _ = h2h.group_by_pair(answers)
        return {
            key: h2h.summarize_pair(bucket)["verdict"] for key, bucket in buckets.items()
        }
    except Exception as e:  # noqa: BLE001 — a missing verdict is not a missing funnel
        logger.warning("Funnel verdict colouring skipped: %s", e)
        return {}


def _screen(text: str) -> tuple[list[str], bool, str | None]:
    """Run the same guards harvested content faces. Returns (pii, ae, injection)."""
    return scan_for_pii(text), looks_like_ae(text), scan_injection(text)


async def _stage_one(
    db: AsyncSession, cell: Cell, text: str, model_id: str, *, commit: bool
) -> dict:
    """Upsert one generated question, keyed on the CELL rather than the wording.

    Keying on the cell is what makes regeneration idempotent: a second run refreshes the
    proposal for a comparison still awaiting review instead of adding a near-duplicate,
    and leaves a row the reviewer has already decided on completely alone.
    """
    dedupe = cell.dedupe_hash()
    existing = (await db.execute(
        select(HarvestedQuestion).where(
            HarvestedQuestion.dedupe_hash == dedupe,
            HarvestedQuestion.source == SOURCE,
        )
    )).scalars().first()

    if existing is not None and existing.status in ("PROMOTED", "REJECTED"):
        return {"status": "skipped", "cell": cell.key, "question_text": text,
                "reason": f"already {existing.status.lower()}; a decided row is not overwritten"}

    pii, ae, injection = _screen(text)
    if injection:
        return {"status": "rejected", "cell": cell.key, "question_text": text,
                "reason": f"possible prompt-injection content: {injection}"}
    if pii:
        return {"status": "rejected", "cell": cell.key, "question_text": text,
                "reason": f"possible PII: {pii}"}

    # An AE-flagged row is STAGED but quarantined, not dropped: pharmacovigilance has to
    # see it. It cannot be promoted without explicit PV sign-off (harvest_service.promote).
    status = "QUARANTINED_AE" if ae else "CLASSIFIED"
    payload = json.dumps({**cell.as_dict(), "generated_by": model_id})
    result = {"status": "refreshed" if existing else "created", "cell": cell.key,
              "question_text": text, "staged_status": status}
    if not commit:
        return result

    if existing is None:
        db.add(HarvestedQuestion(
            source=SOURCE,
            search_query=cell.key,
            question_text=text,
            dedupe_hash=dedupe,
            persona=cell.persona,
            therapeutic_area=cell.therapeutic_area,
            brand_focus=cell.brand,
            domain=cell.domain,
            intent_type="COMPARE",
            relevance_score=1.0,
            ae_flag=ae,
            status=status,
            evidence_payload=payload,
        ))
    else:
        existing.question_text = text
        existing.persona = cell.persona
        existing.therapeutic_area = cell.therapeutic_area
        existing.brand_focus = cell.brand
        existing.domain = cell.domain
        existing.ae_flag = ae
        existing.status = status
        existing.evidence_payload = payload
        existing.updated_at = utcnow()
    return result


async def generate(
    db: AsyncSession,
    *,
    brands: list[str] | None = None,
    therapeutic_areas: list[str] | None = None,
    diseases: list[str] | None = None,
    personas: list[str] | None = None,
    limit: int = 20,
    commit: bool = False,
) -> dict:
    """Write questions for the top-ranked uncovered comparisons and stage them.

    A dry run reports the cells it would fill and the exact number of model calls, and
    **makes no model call and no write**. Costing a run by performing it would make the
    estimate useless as an estimate.
    """
    limit = max(1, min(int(limit), MAX_CELLS_PER_RUN))
    report = await coverage_report(
        db, brands=brands, therapeutic_areas=therapeutic_areas,
        diseases=diseases, personas=personas, limit=limit,
    )
    targets = [
        Cell(
            disease=g["disease"], brand=g["brand"], competitor=g["competitor"],
            persona=g["persona"], domain=g["domain"],
        )
        for g in report["gaps"]
    ]

    if not commit:
        return {
            "dry_run": True,
            "summary": report["summary"],
            "targets": [c.as_dict() for c in targets],
            "model_calls": estimate_model_calls(len(targets)),
            "staged": [],
        }

    staged: list[dict] = []
    rejected: list[dict] = []
    model_id = ""
    calls = 0
    for start in range(0, len(targets), BATCH_SIZE):
        batch = targets[start:start + BATCH_SIZE]
        try:
            accepted, batch_rejected, model_id = await generator.generate_for_cells(batch)
        except RuntimeError as e:
            rejected.extend(
                {"cell": c.key, "question_text": None, "reason": str(e)} for c in batch
            )
            continue
        calls += 1
        rejected.extend(batch_rejected)
        for cell, text in accepted:
            staged.append(await _stage_one(db, cell, text, model_id, commit=True))

    created = sum(1 for s in staged if s["status"] == "created")
    refreshed = sum(1 for s in staged if s["status"] == "refreshed")
    await write_audit(
        db, role="SYSTEM", event="CURATION_QUESTIONS_STAGED",
        context={
            "created": created, "refreshed": refreshed,
            "rejected": len(rejected), "model_calls": calls, "model": model_id,
            "scope": report["scope"],
        },
        commit=False,
    )
    await db.commit()
    return {
        "dry_run": False,
        "summary": report["summary"],
        "model": model_id,
        "model_calls": calls,
        "created": created,
        "refreshed": refreshed,
        "staged": staged,
        "rejected": rejected,
    }
