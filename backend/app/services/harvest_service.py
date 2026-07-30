"""Discovery feature service — list / promote / reject staged harvested questions.

Promotion is the bridge to the existing Question Repository: it creates a PENDING
Question (via question_service) that still must clear Medical-Affairs approval before
it can ever be used in a monitoring run. A final PII scan and an adverse-event guard
run at promotion time as defense in depth.
"""
import json

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import taxonomy
from app.guardrails.injection import scan_injection
from app.models.harvested_question import HarvestedQuestion, utcnow
from app.models.question import Question
from app.schemas import HarvestPromote, QuestionCreate
from app.services import question_service
from app.utils.pii_lint import scan_for_pii

VALID_PERSONAS = {"Prospect", "Provider", "Patient"}
VALID_DOMAINS = {"Efficacy", "Safety", "Access", "Comparative", "General"}


def serialize(hq: HarvestedQuestion) -> dict:
    return {
        "id": hq.id,
        "source": hq.source,
        "source_url": hq.source_url,
        "source_domain": hq.source_domain,
        "source_title": hq.source_title,
        "search_query": hq.search_query,
        "raw_excerpt": hq.raw_excerpt,
        "question_text": hq.question_text,
        "persona": hq.persona,
        "therapeutic_area": hq.therapeutic_area,
        "brand_focus": hq.brand_focus,
        "domain": hq.domain,
        "intent_type": hq.intent_type,
        "relevance_score": hq.relevance_score,
        "search_persona": hq.search_persona,
        "pii_flags": json.loads(hq.pii_flags) if hq.pii_flags else [],
        "ae_flag": hq.ae_flag,
        "status": hq.status,
        "promoted_question_id": hq.promoted_question_id,
        "review_note": hq.review_note,
        "harvested_at": hq.harvested_at.isoformat() if hq.harvested_at else None,
        # Phase 7: present only on evidence-generated rows. Carries the expected answer,
        # the evidence behind it and any flags those rows are already carrying, so the
        # reviewer judges the question against the evidence rather than on its wording.
        "evidence": json.loads(hq.evidence_payload) if hq.evidence_payload else None,
    }


async def list_items(
    db: AsyncSession,
    *,
    status: str | None = None,
    source: str | None = None,
    persona: str | None = None,
    therapeutic_area: str | None = None,
    domain: str | None = None,
    ae_only: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    stmt = select(HarvestedQuestion)
    if status:
        stmt = stmt.where(HarvestedQuestion.status == status)
    if source:
        stmt = stmt.where(HarvestedQuestion.source == source)
    if persona:
        stmt = stmt.where(HarvestedQuestion.persona == persona)
    if therapeutic_area:
        stmt = stmt.where(HarvestedQuestion.therapeutic_area == therapeutic_area)
    if domain:
        stmt = stmt.where(HarvestedQuestion.domain == domain)
    if ae_only:
        stmt = stmt.where(HarvestedQuestion.ae_flag.is_(True))
    stmt = (
        stmt.order_by(
            HarvestedQuestion.relevance_score.desc().nullslast(),
            HarvestedQuestion.harvested_at.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [serialize(r) for r in rows]


async def promote(db: AsyncSession, item_id: int, data: HarvestPromote, *, approve: bool = False):
    hq = await db.get(HarvestedQuestion, item_id)
    if hq is None:
        raise HTTPException(404, "Harvested question not found")
    if hq.status == "PROMOTED":
        raise HTTPException(409, "Already promoted")
    if hq.ae_flag and not data.override_ae:
        raise HTTPException(
            409,
            "Adverse-event content is quarantined; route to safety/pharmacovigilance review. "
            "Set override_ae=true only after PV sign-off.",
        )

    persona = data.persona or hq.persona
    ta = data.therapeutic_area or hq.therapeutic_area
    brand = data.brand_focus or hq.brand_focus
    domain = data.domain or hq.domain

    if persona not in VALID_PERSONAS:
        raise HTTPException(422, f"persona must be one of {sorted(VALID_PERSONAS)}")
    if domain not in VALID_DOMAINS:
        raise HTTPException(422, f"domain must be one of {sorted(VALID_DOMAINS)}")
    if not ta or not brand:
        raise HTTPException(422, "therapeutic_area and brand_focus are required to promote")

    pii = scan_for_pii(hq.question_text)
    if pii:
        raise HTTPException(422, f"Cannot promote — possible PII detected: {pii}")

    # G3: hard block prompt-injection/jailbreak payloads from entering the question bank.
    inj = scan_injection(hq.question_text)
    if inj:
        raise HTTPException(
            422,
            f"Cannot promote — possible prompt-injection content detected: {inj}. "
            "Reject this item or sanitize the question before promotion.",
        )

    # Discover "Run to Pipeline" auto-approves so the question can run immediately; the
    # default review path still lands it as PENDING for Medical-Affairs sign-off.
    approval_status = "APPROVED" if approve else "PENDING"
    approver_name = data.reviewer_name or ("Harvest run-to-pipeline" if approve else None)

    # Phase 7: an evidence-generated row carries the indication it was scoped to, which is
    # what lets the resulting question be graded against the same network later.
    proposal = _evidence_proposal(hq)
    disease = (proposal.get("indication") or None) if proposal else None

    # A coverage cell carries the same two facts under its own names. Without them the
    # comparison the question was written FOR is unrecoverable from the bank, and the
    # scorer falls back to the therapeutic-area competitor block (which flattens every
    # indication into one list) instead of the indication's real competitive field.
    cell = _curation_cell(hq)
    competitor_focus = None
    if cell:
        disease = disease or cell["disease"]
        competitor_focus = [cell["competitor"]]

    q = await question_service.create_question(
        db,
        QuestionCreate(
            question_text=hq.question_text,
            persona=persona,
            therapeutic_area=ta,
            disease=disease,
            brand_focus=brand,
            competitor_focus=competitor_focus,
            domain=domain,
            approval_status=approval_status,
            approver_name=approver_name,
            active=True,
        ),
    )
    if proposal:
        await _attach_evidence(db, q, hq, approve=approve)
    hq.status = "PROMOTED"
    hq.promoted_question_id = q.question_id
    hq.persona, hq.therapeutic_area, hq.brand_focus, hq.domain = persona, ta, brand, domain
    hq.updated_at = utcnow()
    await db.commit()
    return q


async def reject(db: AsyncSession, item_id: int, reason: str) -> dict:
    hq = await db.get(HarvestedQuestion, item_id)
    if hq is None:
        raise HTTPException(404, "Harvested question not found")
    hq.status = "REJECTED"
    hq.review_note = reason or None
    hq.updated_at = utcnow()
    await db.commit()
    return serialize(hq)


def _evidence_proposal(hq: HarvestedQuestion) -> dict | None:
    """The Phase-7 evidence proposal on a staged row, or ``None`` for every other row.

    Keyed on the STAGING SOURCE, not on the column being non-empty. ``evidence_payload`` is
    shared staging space: curation writes the coverage cell it filled into the same column,
    and a coverage cell is not evidence. Reading any dict there as a proposal stamped every
    promoted curation candidate ``generation_method="EVIDENCE"`` with zero associations, and
    the Phase-7 invariant then refused to approve it forever — a gate with nothing behind it
    that could ever be verified, so Approve simply stopped working on those questions.
    """
    from app.services import evidence_question_service as eqs  # local import avoids a cycle

    if hq.source != eqs.SOURCE:
        return None
    if not getattr(hq, "evidence_payload", None):
        return None
    try:
        parsed = json.loads(hq.evidence_payload)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _curation_cell(hq: HarvestedQuestion) -> dict | None:
    """The coverage cell on a curation-staged row, or ``None`` for every other row.

    Source-keyed for the same reason ``_evidence_proposal`` is: ``evidence_payload`` is
    shared staging space and reading whatever dict happens to sit there is what broke
    promotion once already.

    Returns a cell only when BOTH facts survive validation — the comparator has to be a
    declared competitor of the indication, checked against the same taxonomy accessor the
    matrix was built from. A payload written before a brands.yaml edit can name a pairing
    that no longer exists, and writing that onto a question would put a comparison in the
    scoreboard that the scorer would never grade the same way.
    """
    from app.curation import service as curation_service  # local import avoids a cycle

    if hq.source != curation_service.SOURCE:
        return None
    if not getattr(hq, "evidence_payload", None):
        return None
    try:
        parsed = json.loads(hq.evidence_payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None

    disease = taxonomy.canonical_disease(parsed.get("disease"))
    competitor = (parsed.get("competitor") or "").strip()
    if not disease or not competitor:
        return None
    declared = {c.strip().lower() for c in taxonomy.competitors_for_disease(disease)}
    if competitor.lower() not in declared:
        return None
    return {"disease": disease, "competitor": competitor}


async def _attach_evidence(
    db: AsyncSession, q: Question, hq: HarvestedQuestion, *, approve: bool
) -> None:
    """Materialise a staged evidence proposal into ``QuestionEvidence`` rows.

    Inside ``promote`` rather than wrapped around it, so a reviewer acting from the
    ordinary Discover screen cannot create an evidence question with no associations —
    the enforcement cannot be forgotten because nobody performs it.

    **Auto-approval is refused when the evidence is not verified.** The Run-to-Pipeline
    shortcut bypasses ``update_question``, where the approval invariant lives, so without
    this an evidence question could reach APPROVED over evidence nobody has checked by
    taking a different route to the same state. The shortcut still works once the evidence
    IS verified; what it may not do is skip the check.
    """
    from app.services import evidence_question_service as eqs  # local import avoids a cycle

    q.generation_method = eqs.GENERATION_METHOD
    await eqs.materialise_associations(
        db, question_id=q.question_id, evidence_payload=hq.evidence_payload
    )
    await db.flush()
    if not approve:
        return
    blockers = await eqs.approval_blockers(db, q)
    if blockers:
        await db.rollback()
        raise HTTPException(
            422,
            "Cannot auto-approve an evidence-generated question: " + "; ".join(blockers),
        )


async def _ensure_approved(
    db: AsyncSession, question_id: str, reviewer_name: str | None
) -> str | None:
    """Return the current (non-deleted) question_id for an already-promoted item, flipping it
    to APPROVED if needed. Returns None when the question no longer exists (e.g. deleted)."""
    stmt = select(Question).where(
        Question.question_id == question_id,
        Question.deleted_at.is_(None),
        Question.superseded_by.is_(None),
    )
    q = (await db.execute(stmt)).scalars().first()
    if q is None:
        return None
    if q.approval_status != "APPROVED":
        # Same refusal as `_attach_evidence`, reached by the other route: this shortcut sets
        # the column directly rather than going through `update_question`, so without the
        # check an already-promoted evidence question could be flipped to APPROVED over
        # unverified evidence simply by being run a second time.
        blockers = await question_service.approval_blockers(db, q)
        if blockers:
            raise HTTPException(
                422,
                "Cannot approve an evidence-generated question: " + "; ".join(blockers),
            )
        q.approval_status = "APPROVED"
        if reviewer_name:
            q.approver_name = reviewer_name
        q.updated_at = utcnow()
        await db.commit()
    return q.question_id


async def promote_and_approve_batch(
    db: AsyncSession, item_ids: list[int], *, reviewer_name: str | None = None
) -> dict:
    """Promote + APPROVE selected harvested items so they can run immediately (Discover-page
    "Run to Pipeline").

    Each item is promoted via ``promote(approve=True)`` which runs the AE / PII / injection
    guards; unsafe or incomplete items are SKIPPED with a human-readable reason (the batch
    never hard-fails) so the caller can still run whatever qualified. Adverse-event items are
    never auto-run (``override_ae=False``) and must clear pharmacovigilance first. Already-
    promoted items reuse their existing question (approving it if needed) so the action is
    idempotent and works from the PROMOTED filter too.

    Returns ``{question_ids, promoted, skipped}`` where ``promoted`` items are
    ``{id, question_id, question_text}`` and ``skipped`` items are
    ``{id, question_text, reason}``.
    """
    question_ids: list[str] = []
    promoted: list[dict] = []
    skipped: list[dict] = []
    seen: set[str] = set()

    for item_id in item_ids:
        hq = await db.get(HarvestedQuestion, item_id)
        if hq is None:
            skipped.append({"id": item_id, "question_text": None,
                            "reason": "Harvested item not found."})
            continue
        qtext = hq.question_text
        reuse_qid = hq.promoted_question_id if hq.status == "PROMOTED" else None

        # Idempotent: reuse an already-promoted question instead of duplicating it.
        if reuse_qid:
            qid = await _ensure_approved(db, reuse_qid, reviewer_name)
            if qid is None:
                skipped.append({"id": item_id, "question_text": qtext,
                                "reason": "Already promoted, but its question was deleted."})
            elif qid not in seen:
                seen.add(qid)
                question_ids.append(qid)
                promoted.append({"id": item_id, "question_id": qid, "question_text": qtext})
            continue

        data = HarvestPromote(
            persona=hq.persona,
            therapeutic_area=hq.therapeutic_area,
            brand_focus=hq.brand_focus,
            domain=hq.domain or "General",
            reviewer_name=reviewer_name,
            override_ae=False,
        )
        try:
            q = await promote(db, item_id, data, approve=True)
        except Exception as exc:  # noqa: BLE001 - HTTPException guards / validation
            await db.rollback()
            skipped.append({"id": item_id, "question_text": qtext,
                            "reason": str(getattr(exc, "detail", exc))})
            continue
        if q.question_id not in seen:
            seen.add(q.question_id)
            question_ids.append(q.question_id)
            promoted.append({"id": item_id, "question_id": q.question_id, "question_text": qtext})

    return {"question_ids": question_ids, "promoted": promoted, "skipped": skipped}
