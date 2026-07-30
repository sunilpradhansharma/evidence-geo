"""Evidence-driven question generation — the impure half (Phase 7).

``evidence.question_generation`` knows how to turn evidence into a question and refuses
when it cannot; this module knows the database, the staging queue and the approval gate.
Same split as ``resolver.py`` / ``comparison_service.py``, for the same reason: the
constructors stay testable with no session.

Three rules the phase exists to enforce:

**Generation never approves.** Every generated question lands in ``harvested_questions``
as ``CLASSIFIED`` — the same staging table a web-harvested question uses, so a reviewer
has one queue rather than one per provenance. Promotion still creates a ``PENDING``
question and Medical Affairs still approves it. The double gate is reused, not reimplemented.

**Associations are materialised at promotion, never before.** ``QuestionEvidence.question_id``
is ``NOT NULL`` and a staged row has no question yet, so the staged row carries a
*proposal* — a different object from the association, exactly as ``HarmonisationProposal``
is a different object from the value it proposes. That is what keeps the association table
free of rows pointing at questions that may never exist.

**An evidence-generated question cannot be approved without verified backing.** Enforced in
``question_service.update_question``, the one choke point every approval path goes through
(UI, copilot tool, CSV importer). Scoped to ``generation_method == "EVIDENCE"``: the
invariant is about questions whose entire justification is the evidence behind them, and
applying it to the manual bank would block every question that already exists.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import taxonomy
from app.evidence import lifecycles, question_generation as qg, statuses
from app.models.clinical_study import ClinicalStudy, OutcomeResult
from app.models.competitor_candidate import ACCEPTED, CompetitorCandidate
from app.models.drug_fact import DrugFact
from app.models.evidence_network import EvidenceNetwork
from app.models.harvested_question import HarvestedQuestion
from app.models.nma_result import NMAResult
from app.models.question import Question
from app.models.question_evidence import QuestionEvidence
from app.services import comparison_service
from app.utils.audit import write_audit
from app.utils.logging import get_logger

logger = get_logger("evidence_question_service")

SOURCE = "evidence"
GENERATION_METHOD = "EVIDENCE"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EvidenceQuestionError(ValueError):
    """A generation request whose scope is incoherent."""


def _json_list(raw: str | None) -> list[str]:
    """A stored JSON list, or an empty list. Never raises on a malformed column.

    Same tolerance the evidence read surface applies to ``mismatch_flags``: a corrupt
    column should not 500 a page, and an empty list makes the generator refuse rather
    than invent.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(x) for x in parsed] if isinstance(parsed, list) else []


# =========================================================================================
# Generation
# =========================================================================================
async def generate_for_network(
    db: AsyncSession,
    *,
    network_id: str,
    commit: bool = False,
) -> dict:
    """Every question the evidence in one network supports, plus every refusal.

    Refusals are returned, not logged. *"3 comparisons could have produced a gap question
    but the gap is our verification backlog"* is the most useful line in the report — it
    names work a curator can do, where a silently shorter list would look like the evidence
    simply being thin.
    """
    network = await comparison_service._network(db, network_id)
    disease = network.indication
    ta_key = taxonomy.therapeutic_area_key_for_disease(disease) or taxonomy.area_for_disease(disease)

    generated: list[qg.GeneratedQuestion] = []
    refused: list[dict] = []

    matrix = await comparison_service.resolve_all_pairs(db, network_id=network_id)
    unmonitorable = 0
    for answer in matrix["comparisons"]:
        scoping = answer.get("scoping") or {}
        pair = f"{answer['treatment']} vs {answer['comparator']}"

        # A resolver node set is not a question set. Screened before the status is even
        # looked at, because a placebo contrast is no more worth asking as a gap than as a
        # result — and screening after would have produced a gap question about placebo.
        monitorable, why_not = qg.is_monitorable_pair(answer["treatment"], answer["comparator"])
        if not monitorable:
            unmonitorable += 1
            refused.append({"pair": pair, "category": None, "reason": why_not})
            continue

        if statuses.is_success(answer["status"]):
            for build in (qg.comparative_question, qg.evidence_quality_question):
                try:
                    generated.append(build(
                        answer,
                        indication=disease,
                        canonical_outcome_id=network.canonical_outcome_id,
                        therapeutic_area=ta_key,
                        network_id=network.network_id,
                    ))
                except qg.GenerationError as exc:
                    refused.append({"pair": pair, "category": build.__name__, "reason": str(exc)})
            continue

        if statuses.is_gap(answer["status"]):
            attribution, reason = qg.attribute_gap(answer["status"], scoping)
            try:
                generated.append(qg.evidence_gap_question(
                    answer,
                    indication=disease,
                    canonical_outcome_id=network.canonical_outcome_id,
                    scoping=scoping,
                    therapeutic_area=ta_key,
                    network_id=network.network_id,
                ))
            except qg.GenerationError as exc:
                refused.append({
                    "pair": pair,
                    "category": qg.EVIDENCE_GAP,
                    "attribution": attribution,
                    "reason": str(exc),
                })
            continue

        refused.append({
            "pair": pair,
            "category": qg.COMPARATIVE_EFFICACY,
            "reason": f"{answer['status']} is neither a success nor an evidence gap",
        })

    generated.extend(await _from_drug_facts(db, disease, ta_key, refused))
    generated.extend(await _from_competitors(db, disease, ta_key, refused))

    staged = await stage(db, generated, commit=commit)
    return {
        "network_id": network_id,
        "indication": disease,
        "therapeutic_area": ta_key,
        "generated_count": len(generated),
        "questions": [q.as_dict() for q in generated],
        "refused": refused,
        "refused_count": len(refused),
        # Counted separately because these two are somebody's queue, not a finding about
        # the evidence: CURATION is a verification backlog, PROTOCOL is issue-1 territory.
        "gaps_attributable_to_curation": sum(
            1 for r in refused if r.get("attribution") == qg.ATTRIBUTION_CURATION
        ),
        "gaps_attributable_to_protocol": sum(
            1 for r in refused if r.get("attribution") == qg.ATTRIBUTION_PROTOCOL
        ),
        # Pairs the network holds that nobody would ask about — placebo contrasts, class
        # nodes. Reported so the difference between "the evidence is thin" and "most of
        # this network is scaffolding" stays visible.
        "pairs_not_monitorable": unmonitorable,
        "committed": commit,
        **staged,
    }


async def _from_drug_facts(
    db: AsyncSession, disease: str, ta_key: str | None, refused: list[dict]
) -> list[qg.GeneratedQuestion]:
    """Approval and boxed-warning questions from **verified** drug facts only.

    Unverified is not a lower confidence here, it is a different kind of claim: *"Is X
    approved for Y?"* answered from an unreviewed label extraction is an assertion about a
    regulatory fact that nobody has checked, and it is the single easiest claim in this
    system to be confidently wrong about.
    """
    rows = (await db.execute(
        select(DrugFact).where(
            DrugFact.verification_status == lifecycles.VERIFIED,
            DrugFact.superseded_by.is_(None),
        )
    )).scalars().all()

    out: list[qg.GeneratedQuestion] = []
    for fact in rows:
        indications = _json_list(fact.approved_indications)
        for build, kwargs in (
            (qg.drug_fact_question, {"approved_indications": indications}),
            (qg.safety_question, {
                "boxed_warnings": _json_list(fact.boxed_warnings),
                "has_boxed_warning": bool(fact.has_boxed_warning),
            }),
        ):
            try:
                out.append(build(
                    brand=fact.brand,
                    indication=disease,
                    fact_id=fact.fact_id,
                    label_updated_at=fact.label_updated_at,
                    therapeutic_area=ta_key,
                    **kwargs,
                ))
            except qg.GenerationError as exc:
                refused.append({
                    "brand": fact.brand, "category": build.__name__, "reason": str(exc)
                })
    return out


async def _from_competitors(
    db: AsyncSession, disease: str, ta_key: str | None, refused: list[dict]
) -> list[qg.GeneratedQuestion]:
    """Landscape questions for **accepted** candidates only.

    A ``NEW`` candidate is a machine's proposal, and the review queue exists precisely to
    judge it. Generating a monitored question from one would let discovery write into the
    corpus by the back door — the same objection that keeps the sweep out of ``brands.yaml``.
    """
    rows = (await db.execute(
        select(CompetitorCandidate).where(
            CompetitorCandidate.indication == disease,
            CompetitorCandidate.review_status == ACCEPTED,
        )
    )).scalars().all()

    out: list[qg.GeneratedQuestion] = []
    for candidate in rows:
        try:
            out.append(qg.competitor_question(
                treatment=candidate.treatment,
                indication=disease,
                candidate_id=candidate.candidate_id,
                discovery_reasons=_json_list(candidate.discovery_reasons),
                has_posted_results=bool(candidate.has_posted_results),
                development_phase=candidate.development_phase,
                therapeutic_area=ta_key,
            ))
        except qg.GenerationError as exc:
            refused.append({
                "treatment": candidate.treatment,
                "category": qg.COMPETITOR_DISCOVERY,
                "reason": str(exc),
            })
    return out


# =========================================================================================
# Staging
# =========================================================================================
async def stage(
    db: AsyncSession, questions: list[qg.GeneratedQuestion], *, commit: bool
) -> dict:
    """Write generated questions into the shared review queue as ``CLASSIFIED``.

    Keyed on ``dedupe_key``, so re-generating refreshes the proposal on an undecided row
    and leaves a ``PROMOTED`` or ``REJECTED`` one alone. That is the rule ingestion and
    competitor discovery both keep: a decision already made is not new information.

    A dry run **adds nothing and rolls nothing back.** Deciding what would happen and then
    undoing it would make this function's caller unable to not write — the inverse of the
    unconditional commit that turned a reported dry run into a durable one during issue 4,
    and just as invisible.
    """
    created, refreshed, skipped = 0, 0, []
    for question in questions:
        existing = (await db.execute(
            select(HarvestedQuestion).where(
                HarvestedQuestion.dedupe_hash == question.dedupe_key,
                HarvestedQuestion.source == SOURCE,
            )
        )).scalars().first()

        if existing is not None and existing.status in ("PROMOTED", "REJECTED"):
            skipped.append({
                "question_text": question.question_text,
                "reason": f"already {existing.status.lower()}; a decided row is not overwritten",
            })
            continue

        if existing is None:
            created += 1
        else:
            refreshed += 1
        if not commit:
            continue

        payload = json.dumps(question.as_dict())
        if existing is None:
            db.add(HarvestedQuestion(
                source=SOURCE,
                question_text=question.question_text,
                dedupe_hash=question.dedupe_key,
                persona=question.persona,
                therapeutic_area=question.therapeutic_area,
                brand_focus=question.brand,
                domain=question.domain,
                relevance_score=question.confidence,
                search_query=question.category,
                status="CLASSIFIED",
                evidence_payload=payload,
            ))
        else:
            existing.question_text = question.question_text
            existing.persona = question.persona
            existing.therapeutic_area = question.therapeutic_area
            existing.brand_focus = question.brand
            existing.domain = question.domain
            existing.relevance_score = question.confidence
            existing.search_query = question.category
            existing.evidence_payload = payload
            existing.updated_at = utcnow()

    if commit:
        await write_audit(
            db, role="SYSTEM", event="EVIDENCE_QUESTIONS_STAGED",
            context={"created": created, "refreshed": refreshed, "skipped": len(skipped)},
            commit=False,
        )
        await db.commit()

    return {"staged_created": created, "staged_refreshed": refreshed, "staged_skipped": skipped}


async def materialise_associations(
    db: AsyncSession, *, question_id: str, evidence_payload: str | None
) -> int:
    """Create ``QuestionEvidence`` rows for a just-promoted evidence question.

    Called from ``harvest_service.promote`` rather than wrapped around it, so a reviewer
    promoting from the ordinary Discover screen cannot produce an evidence question with
    no associations. The enforcement cannot be forgotten because nobody performs it.

    Never commits: it runs inside the promotion's transaction, so a failure to associate
    cannot leave an approved question standing over evidence that was not recorded.
    """
    try:
        payload = json.loads(evidence_payload or "")
    except (TypeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0

    category = payload.get("category")
    written = 0
    for ref in payload.get("evidence") or []:
        if not isinstance(ref, dict):
            continue
        try:
            checked = qg.EvidenceRef(
                evidence_type=ref.get("evidence_type"),
                evidence_id=ref.get("evidence_id"),
                relationship_role=ref.get("relationship_role"),
                evidence_priority=ref.get("evidence_priority"),
            )
        except qg.GenerationError:
            logger.warning("skipping malformed evidence reference on %s: %r", question_id, ref)
            continue
        db.add(QuestionEvidence(
            question_id=question_id,
            evidence_type=checked.evidence_type,
            evidence_id=checked.evidence_id,
            relationship_role=checked.relationship_role,
            evidence_priority=checked.evidence_priority,
            category=category,
            verification_state_at_link=await _verification_state(db, checked),
        ))
        written += 1
    return written


# =========================================================================================
# The approval invariant
# =========================================================================================
async def _verification_state(db: AsyncSession, ref: qg.EvidenceRef) -> str | None:
    """The referenced row's own verification state, or ``None`` when it does not exist.

    Each evidence family answers "is this checked?" in its own vocabulary and there is no
    honest way to collapse them: a study is ``VERIFIED``, a computed result is *releasable*,
    a discovered competitor is ``ACCEPTED``, a network is ``RATIFIED``. Translating them all
    into one word would hide which review actually happened.
    """
    kind = ref.evidence_type
    if kind == qg.CLINICAL_STUDY:
        row = await db.get(ClinicalStudy, ref.evidence_id)
        return row.verification_status if row else None
    if kind == qg.OUTCOME_RESULT:
        row = await db.get(OutcomeResult, ref.evidence_id)
        return row.verification_status if row else None
    if kind == qg.DRUG_FACT_EVIDENCE:
        row = await db.get(DrugFact, ref.evidence_id)
        return row.verification_status if row else None
    if kind == qg.NMA_RESULT:
        row = await db.get(NMAResult, ref.evidence_id)
        return row.status if row else None
    if kind == qg.COMPETITOR_CANDIDATE:
        row = await db.get(CompetitorCandidate, ref.evidence_id)
        return row.review_status if row else None
    if kind == qg.EVIDENCE_NETWORK:
        row = (await db.execute(
            select(EvidenceNetwork).where(EvidenceNetwork.network_id == ref.evidence_id)
        )).scalar_one_or_none()
        return row.ratification_status if row else None
    return None


def _is_verified(evidence_type: str, state: str | None) -> bool:
    """Whether *state* counts as review having happened for that evidence family."""
    if state is None:
        return False
    if evidence_type == qg.NMA_RESULT:
        # A computed result that is not releasable is exploratory, and an exploratory
        # result explicitly cannot back an approved question.
        return statuses.is_releasable(state)
    return state in {
        qg.CLINICAL_STUDY: lifecycles.VERIFIED,
        qg.OUTCOME_RESULT: lifecycles.VERIFIED,
        qg.DRUG_FACT_EVIDENCE: lifecycles.VERIFIED,
        qg.COMPETITOR_CANDIDATE: ACCEPTED,
        # An absence claim rests on the evidence set being a faithful picture, and network
        # ratification is exactly the review that says so. A gap question backed by a DRAFT
        # network asserts "nothing shows this" from a set nobody has signed off as complete.
        qg.EVIDENCE_NETWORK: lifecycles.RATIFIED,
    }.get(evidence_type)


async def associations(db: AsyncSession, question_id: str) -> list[dict]:
    """Every association behind a question, each with its evidence's live review state."""
    rows = (await db.execute(
        select(QuestionEvidence)
        .where(QuestionEvidence.question_id == question_id)
        .order_by(QuestionEvidence.id)
    )).scalars().all()

    out: list[dict] = []
    for row in rows:
        ref = qg.EvidenceRef(
            evidence_type=row.evidence_type,
            evidence_id=row.evidence_id,
            relationship_role=row.relationship_role,
            evidence_priority=row.evidence_priority,
        )
        state = await _verification_state(db, ref)
        out.append({
            "id": row.id,
            "evidence_type": row.evidence_type,
            "evidence_id": row.evidence_id,
            "relationship_role": row.relationship_role,
            "evidence_priority": row.evidence_priority,
            "category": row.category,
            "verification_state_at_link": row.verification_state_at_link,
            "verification_state_now": state,
            "exists": state is not None,
            "is_verified": _is_verified(row.evidence_type, state),
        })
    return out


async def approval_blockers(db: AsyncSession, question: Question) -> list[str]:
    """Why this question may not be approved. Empty list means it may.

    Returns ``[]`` for anything the evidence programme did not generate. The invariant is
    that an evidence-backed question is backed by evidence somebody checked; it is not a
    new rule for the manual bank, and applying it there would block every question that
    already exists without improving a single one of them.
    """
    if (question.generation_method or "") != GENERATION_METHOD:
        return []

    links = await associations(db, question.question_id)
    if not links:
        return [
            "no QuestionEvidence associations: an evidence-generated question with no "
            "recorded evidence cannot be approved"
        ]
    if any(link["is_verified"] for link in links):
        return []

    unverified = [
        f"{link['evidence_type']} {link['evidence_id']} is "
        + (
            f"{link['verification_state_now']}"
            if link["exists"] else "missing from the evidence store"
        )
        for link in links
    ]
    return [
        "no verified evidence association: "
        + "; ".join(sorted(unverified)[:5])
        + ("; …" if len(unverified) > 5 else "")
    ]
