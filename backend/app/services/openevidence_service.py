"""OpenEvidence manual-capture bridge.

OpenEvidence is an HCP-gated clinical search tool with no public/automatable API, so
its Provider-persona answers cannot be fetched by the orchestrator. Instead a human
runs the question in the OpenEvidence web app and pastes the answer back here. This
service turns that paste into a normal `open-evidence` Response so it is scored and
folded into Chairman consensus exactly like any other target.

The `open-evidence` target stays `enabled: false` in targets.yaml on purpose: the
automated/scheduled orchestrator must NOT try to call the (stubbed) provider client.
These rows are written out-of-band, only through this bridge.
"""
import json
import uuid
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.consensus import ConsensusRecord
from app.models.database import AsyncSessionLocal
from app.models.question import Question
from app.models.response import Response
from app.models.run import Run, utcnow
from app.models.scoring import ScoringRecord
from app.schemas import OpenEvidenceCapture
from app.source_authority import references
from app.utils.audit import write_audit
from app.utils.logging import get_logger

logger = get_logger("openevidence")

OE_TARGET = "open-evidence"
OE_DEFAULT_VERSION = "open-evidence-web"
PROVIDER_PERSONA = "Provider"
AWAITING_STATUS = "AWAITING_OPENEVIDENCE"


def _domain_of(url: str) -> str | None:
    try:
        netloc = urlparse(url).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else (netloc or None)
    except Exception:  # noqa: BLE001
        return None


async def _latest_scores(db: AsyncSession, response_ids: list[str]) -> dict[str, ScoringRecord]:
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


async def list_runs_with_provider(db: AsyncSession, limit: int = 100) -> list[dict]:
    """Recent runs that asked Provider-persona questions, with OpenEvidence capture progress."""
    runs = list((await db.execute(
        select(Run).order_by(Run.started_at.desc()).limit(limit)
    )).scalars().all())
    if not runs:
        return []
    run_ids = [r.run_id for r in runs]

    prov_rows = (await db.execute(
        select(Response.run_id, func.count(func.distinct(Response.question_id)))
        .where(Response.run_id.in_(run_ids), Response.persona == PROVIDER_PERSONA)
        .group_by(Response.run_id)
    )).all()
    prov_map = {rid: n for rid, n in prov_rows}

    cap_rows = (await db.execute(
        select(Response.run_id, func.count(func.distinct(Response.question_id)))
        .where(
            Response.run_id.in_(run_ids),
            Response.persona == PROVIDER_PERSONA,
            Response.llm_name == OE_TARGET,
        )
        .group_by(Response.run_id)
    )).all()
    cap_map = {rid: n for rid, n in cap_rows}

    out: list[dict] = []
    for r in runs:
        pq = prov_map.get(r.run_id, 0)
        if pq == 0:
            continue  # nothing to capture for this run
        cap = cap_map.get(r.run_id, 0)
        out.append({
            "run_id": r.run_id,
            "trigger": r.trigger,
            "status": r.status,
            "started_at": r.started_at,
            "provider_questions": pq,
            "captured": cap,
            "pending": max(pq - cap, 0),
        })
    # Surface runs paused for OpenEvidence (still pending) first; stable sort keeps the
    # started_at-desc order within each group.
    out.sort(key=lambda r: not (r["status"] == AWAITING_STATUS and r["pending"] > 0))
    return out


async def worklist(db: AsyncSession, run_id: str) -> dict:
    """Provider-persona questions in a run + their OpenEvidence capture/score status."""
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(404, f"Run {run_id} not found")

    rows = list((await db.execute(
        select(Response)
        .where(Response.run_id == run_id, Response.persona == PROVIDER_PERSONA)
        .order_by(Response.timestamp_utc.asc())
    )).scalars().all())

    rep: dict[str, Response] = {}   # representative (non-OE) row per question
    oe: dict[str, Response] = {}    # captured OpenEvidence row per question
    for r in rows:
        if r.llm_name == OE_TARGET:
            oe[r.question_id] = r
        prev = rep.get(r.question_id)
        if prev is None or (prev.llm_name == OE_TARGET and r.llm_name != OE_TARGET):
            rep[r.question_id] = r

    scores = await _latest_scores(db, [r.response_id for r in oe.values()])

    items: list[dict] = []
    for qid, r in rep.items():
        oer = oe.get(qid)
        score = scores.get(oer.response_id) if oer else None
        items.append({
            "question_id": qid,
            "question_text": r.question_text,
            "brand_focus": r.brand_focus,
            "therapeutic_area": r.therapeutic_area,
            "domain": r.domain,
            "intent_type": r.intent_type,
            "captured": oer is not None,
            "response_id": oer.response_id if oer else None,
            "status": oer.status if oer else None,
            "scored": score is not None,
            "sentiment_score": score.sentiment_score if score else None,
            "competitive_position": score.competitive_position if score else None,
        })

    # pending first, then deterministic by question id
    items.sort(key=lambda it: (it["captured"], it["question_id"]))
    captured = sum(1 for it in items if it["captured"])
    return {
        "run_id": run_id,
        "status": run.status,
        "provider_questions": len(items),
        "captured": captured,
        "pending": len(items) - captured,
        "items": items,
    }


async def capture(db: AsyncSession, payload: OpenEvidenceCapture) -> dict:
    """Persist a manually-captured OpenEvidence answer as an immutable Response row.

    Scoring + consensus refresh happen afterwards in finalize_capture (background).
    """
    run = await db.get(Run, payload.run_id)
    if run is None:
        raise HTTPException(404, f"Run {payload.run_id} not found")

    rep = (await db.execute(
        select(Response)
        .where(
            Response.run_id == payload.run_id,
            Response.question_id == payload.question_id,
            Response.persona == PROVIDER_PERSONA,
        )
        .order_by(Response.timestamp_utc.asc())
        .limit(1)
    )).scalars().first()
    if rep is None:
        raise HTTPException(
            404,
            f"No Provider-persona question '{payload.question_id}' found in run {payload.run_id}",
        )

    existing = (await db.execute(
        select(Response).where(
            Response.run_id == payload.run_id,
            Response.question_id == payload.question_id,
            Response.llm_name == OE_TARGET,
        )
    )).scalars().first()
    if existing is not None:
        raise HTTPException(
            409,
            f"OpenEvidence answer already captured for this question (response "
            f"{existing.response_id}); responses are immutable.",
        )

    sources_json = None
    clean_sources = [s for s in payload.sources if s.url and s.url.strip()]
    if clean_sources:
        sources_json = json.dumps([
            {
                "url": s.url.strip(),
                "title": s.title,
                "domain": _domain_of(s.url.strip()),
                "redirect_url": None,
                "snippet": None,
                "origin": "GROUNDED",
            }
            for s in clean_sources
        ])
    else:
        # No citations pasted: recover provenance from the answer's own '### References' list
        # so OpenEvidence still appears in the Source Authority dashboards.
        derived = references.parse_reference_sources(payload.answer_text or "")
        if derived:
            sources_json = json.dumps(derived)

    resp = Response(
        response_id=str(uuid.uuid4()),
        run_id=payload.run_id,
        llm_name=OE_TARGET,
        llm_model_version=(payload.model_version or OE_DEFAULT_VERSION).strip() or OE_DEFAULT_VERSION,
        persona=rep.persona,
        question_id=payload.question_id,
        question_text=rep.question_text,
        therapeutic_area=rep.therapeutic_area,
        indication=rep.indication,
        disease=rep.disease,
        brand_focus=rep.brand_focus,
        domain=rep.domain,
        intent_type=rep.intent_type,
        response_text=payload.answer_text,
        prompt_tokens=0,
        response_tokens=0,
        sources=sources_json,
        finish_reason="stop",
        status="SUCCESS",
    )
    db.add(resp)
    await db.commit()

    await write_audit(
        db, role="OPERATOR", event="OPEN_EVIDENCE_CAPTURE",
        run_id=payload.run_id, question_id=payload.question_id, llm_target=OE_TARGET,
        context={"chars": len(payload.answer_text), "sources": len(clean_sources)},
    )
    logger.info("Captured OpenEvidence answer for run=%s question=%s", payload.run_id, payload.question_id)
    return {
        "response_id": resp.response_id,
        "run_id": payload.run_id,
        "question_id": payload.question_id,
        "captured": True,
    }


async def finalize_capture(response_id: str) -> None:
    """Background: score the captured answer, then re-run Chairman consensus for its
    question so OpenEvidence is folded into consensus_level / final_answer / aggregates.

    Best-effort: a failure here never corrupts the captured response. Runs in its own
    session because it executes after the HTTP response is sent.
    """
    # Late imports avoid a circular dependency (scorer/chairman import the registry).
    from app.scoring.scorer import (
        _compute_response_diff,
        aggregate_consensus_scores,
        score_response,
    )

    async with AsyncSessionLocal() as db:
        oe_resp = await db.get(Response, response_id)
        if oe_resp is None or oe_resp.llm_name != OE_TARGET:
            return
        run_id = oe_resp.run_id
        question_id = oe_resp.question_id

        # 1) Score the captured answer (versioned record + alerts + diff vs prior OE answer).
        try:
            rec = await score_response(db, oe_resp, commit=False)
            if rec is not None:
                await _compute_response_diff(db, oe_resp)
            await db.commit()
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            logger.warning("Scoring failed for captured OpenEvidence response %s: %s", response_id, e)

        # 1b) Classify the captured answer's cited sources into the Source Authority tables
        #     so OpenEvidence appears in the citation dashboards (top-cited-per-model, etc).
        #     The run-level classify_run pass already ran during the original run, before this
        #     out-of-band capture existed, so without this the OpenEvidence citations would only
        #     surface after a manual /source-authority/classify/sweep. Best-effort.
        try:
            from app.source_authority import service as source_authority_service

            await source_authority_service.classify_response(db, oe_resp, commit=True)
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            logger.warning(
                "Source authority classification failed for OpenEvidence response %s: %s",
                response_id, e,
            )

        # 2) Re-run consensus for this question, now including the OpenEvidence answer,
        #    then auto-complete the run if every Provider question now has an OE answer.
        try:
            await _arbitrate_question(db, run_id, question_id)
            await db.commit()
            await _refresh_run_consensus_counters(db, run_id)
            await aggregate_consensus_scores(db, run_id)  # commits internally
            await _maybe_complete_run(db, run_id)
            logger.info("Refreshed consensus for run=%s question=%s after OpenEvidence capture",
                        run_id, question_id)
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            logger.warning("Consensus refresh failed after OpenEvidence capture %s: %s", response_id, e)


async def _refresh_run_consensus_counters(db: AsyncSession, run_id: str) -> None:
    """Rebuild this run's consensus tallies after a capture re-arbitrates a question.

    Delegates to the shared rule so the orchestrator and this service cannot drift on how
    the run-level numbers are derived. Late import: chairman pulls in the provider registry.
    """
    from app.agent.chairman import refresh_run_consensus_counters

    await refresh_run_consensus_counters(db, run_id)


async def _arbitrate_question(db: AsyncSession, run_id: str, question_id: str):
    """Run Chairman consensus for one question over whatever responses currently exist
    and stamp consensus_level on every response. Returns the result (None if no rows)."""
    from app.agent.chairman import arbitrate  # late import (registry cycle)

    responses = list((await db.execute(
        select(Response).where(
            Response.run_id == run_id,
            Response.question_id == question_id,
        )
    )).scalars().all())
    if not responses:
        return None

    question = (await db.execute(
        select(Question).where(
            Question.question_id == question_id,
            Question.superseded_by.is_(None),
            Question.deleted_at.is_(None),
        ).limit(1)
    )).scalars().first()
    rep = responses[0]
    if question is None:
        # Question re-versioned/deleted — synthesize a transient one (not session-added).
        question = Question(
            question_id=question_id,
            question_text=rep.question_text,
            persona=rep.persona,
            therapeutic_area=rep.therapeutic_area,
            brand_focus=rep.brand_focus,
            domain=rep.domain,
        )

    intent = next((r.intent_type for r in responses if r.intent_type), None) or "CLINICAL"
    result = await arbitrate(db, run_id, question, responses, intent)
    for r in responses:
        r.consensus_level = result.consensus_level
    return result


async def _pending_provider_count(db: AsyncSession, run_id: str) -> int:
    """Distinct Provider questions in a run that still lack an OpenEvidence answer."""
    total = (await db.execute(
        select(func.count(func.distinct(Response.question_id)))
        .where(Response.run_id == run_id, Response.persona == PROVIDER_PERSONA)
    )).scalar() or 0
    captured = (await db.execute(
        select(func.count(func.distinct(Response.question_id)))
        .where(
            Response.run_id == run_id,
            Response.persona == PROVIDER_PERSONA,
            Response.llm_name == OE_TARGET,
        )
    )).scalar() or 0
    return max(total - captured, 0)


async def _maybe_complete_run(db: AsyncSession, run_id: str) -> None:
    """Flip an AWAITING_OPENEVIDENCE run to COMPLETED once every Provider question has an
    OpenEvidence answer (so all Provider consensus now reflects OpenEvidence)."""
    run = await db.get(Run, run_id)
    if run is None or run.status != AWAITING_STATUS:
        return
    if await _pending_provider_count(db, run_id) > 0:
        return
    run.status = "COMPLETED"
    run.ended_at = utcnow()
    run.notes = "Completed — all Provider questions answered by OpenEvidence."
    await db.commit()
    await write_audit(db, role="SYSTEM", event="RUN_COMPLETE_AFTER_OE", run_id=run_id,
                      context={"trigger": run.trigger})
    logger.info("Run %s completed after OpenEvidence capture", run_id)


async def finalize_without_oe(db: AsyncSession, run_id: str) -> dict:
    """Escape hatch: close an AWAITING_OPENEVIDENCE run WITHOUT OpenEvidence.

    Computes Provider consensus from the automated targets only for every still-pending
    question, then marks the run COMPLETED. Lets an operator finish a run when
    OpenEvidence answers won't be captured.
    """
    from app.scoring.scorer import aggregate_consensus_scores  # late import (registry cycle)

    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(404, f"Run {run_id} not found")
    if run.status != AWAITING_STATUS:
        raise HTTPException(409, f"Run is not awaiting OpenEvidence (status={run.status})")

    # Provider questions with no consensus record yet = the deferred/pending ones.
    decided = {
        qid for (qid,) in (await db.execute(
            select(ConsensusRecord.question_id).where(ConsensusRecord.run_id == run_id)
        )).all()
    }
    provider_qids = {
        qid for (qid,) in (await db.execute(
            select(func.distinct(Response.question_id))
            .where(Response.run_id == run_id, Response.persona == PROVIDER_PERSONA)
        )).all()
    }
    pending = sorted(provider_qids - decided)

    for qid in pending:
        await _arbitrate_question(db, run_id, qid)
    await db.commit()

    await _refresh_run_consensus_counters(db, run_id)
    await aggregate_consensus_scores(db, run_id)

    run = await db.get(Run, run_id)
    run.status = "COMPLETED"
    run.ended_at = utcnow()
    run.notes = "Finalized without OpenEvidence — Provider consensus computed from automated targets only."
    await db.commit()
    await write_audit(db, role="OPERATOR", event="RUN_FINALIZED_WITHOUT_OE", run_id=run_id,
                      context={"questions_finalized": len(pending)})
    logger.info("Run %s finalized without OpenEvidence (%d question(s))", run_id, len(pending))
    return {"run_id": run_id, "status": run.status, "questions_finalized": len(pending)}
