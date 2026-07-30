"""Evidence-alignment gaps for the recommendation engine (Phase 9 step 1).

Sits **alongside** ``gaps.find_gaps`` rather than replacing it, because the two find
genuinely different things and the plan is explicit that the engine is extended, not
rebuilt:

* ``gaps.find_gaps`` finds a **positioning** gap — the brand was scored ``SECOND_LINE`` or
  worse. The evidence is not consulted; the finding is about how the answer *reads*.
* This finds an **alignment** gap — a specific claim the model made that our evidence
  contradicts, cannot support, or supports more strongly than the model was willing to say.
  The finding is about whether the answer is *right*.

A response can carry both, one, or neither. They are emitted as separate records with the
same dict shape, so ``engine._build_row`` consumes either without knowing which finder
produced it, and ``source_type`` on the resulting row says which did.

Both a claim's finding and the network's gap attribution are read from stored rows. Nothing
here re-decides anything Phase 7 or Phase 8 already decided — this assembles.
"""
from __future__ import annotations

import json

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.taxonomy import keys_for_area
from app.evidence import claims as cl
from app.evidence import question_generation as qg
from app.models.evaluation_claim import EvaluationClaim
from app.models.evidence_network import EvidenceNetwork
from app.models.response import Response
from app.models.scoring import ScoringRecord
from app.remediation import implications as impl
from app.utils.logging import get_logger

logger = get_logger("remediation.evidence_gaps")

SOURCE_EVIDENCE_GAP = "EVIDENCE_GAP"
SOURCE_POSITIONING_GAP = "POSITIONING_GAP"

# A response whose position was never scored still yields alignment findings — the two
# passes are independent. Recorded rather than left blank so nothing reads it as a good
# position that simply happened to have no score.
POSITION_NOT_ASSESSED = "NOT_ASSESSED"


def _loads(raw: str | None) -> list:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


async def _network_attribution(
    db: AsyncSession, claim: EvaluationClaim
) -> tuple[str | None, str | None]:
    """``(gap_attribution, required_evidence)`` for an unsupported comparative claim.

    The single most important lookup in Phase 9. Phase 7 established that a resolver gap is
    not automatically an *evidence* gap — it is often our own verification backlog — and a
    recommendation that confused the two would tell a brand team to publish a comparison
    table when the fix is a curator verifying studies we already hold.

    Returns ``(None, None)`` when no network covers the pair, which is a coverage gap rather
    than an attribution: we cannot say why a comparison is missing from a network that does
    not exist.
    """
    if not claim.comparator:
        return None, None
    network = (await db.execute(
        select(EvidenceNetwork).where(
            EvidenceNetwork.indication == claim.indication,
            EvidenceNetwork.superseded_by.is_(None),
        ).limit(1)
    )).scalars().first()
    if network is None:
        return None, None

    # Re-resolve rather than trust a stale cache: the attribution depends on verification
    # states that a curator may have changed since the claim was graded, and reporting a
    # curation gap that has since been closed would send someone to do work already done.
    from app.services import comparison_service

    try:
        answer = await comparison_service.resolve_comparison(
            db, network_id=network.network_id,
            treatment_a=claim.subject, treatment_b=claim.comparator,
        )
    except Exception as exc:  # noqa: BLE001 — attribution is context, never blocks the gap
        logger.warning("attribution skipped for claim %s: %s", claim.claim_id, exc)
        return None, None

    attribution, _ = qg.attribute_gap(answer.get("status") or "", answer.get("scoping"))
    required = qg.required_evidence_for(
        answer.get("status") or "", claim.subject, claim.comparator
    )
    return attribution, required


async def find_evidence_gaps(
    db: AsyncSession,
    *,
    persona: str | None = None,
    therapeutic_area: str | None = None,
    indication: str | None = None,
    brand: str | None = None,
    llm_name: str | None = None,
    run_id: str | None = None,
    response_ids: list[str] | None = None,
    limit: int = 25,
) -> list[dict]:
    """Alignment gaps from stored Phase-8 claims, each with its strategic implication.

    Ordered by severity so the caller's ``limit`` keeps the findings that matter rather than
    the most recent ones — a safety contradiction must not fall off the bottom of a page
    because twelve calibration nits were graded after it.
    """
    stmt = select(EvaluationClaim, Response).join(
        Response, Response.response_id == EvaluationClaim.response_id
    )
    if response_ids:
        stmt = stmt.where(EvaluationClaim.response_id.in_(list(response_ids)))
    if run_id:
        stmt = stmt.where(EvaluationClaim.run_id == run_id)
    if persona:
        stmt = stmt.where(Response.persona == persona)
    if therapeutic_area:
        child_keys = keys_for_area(therapeutic_area)
        if child_keys:
            stmt = stmt.where(Response.therapeutic_area.in_(child_keys))
        else:
            stmt = stmt.where(Response.therapeutic_area == therapeutic_area)
    if indication:
        stmt = stmt.where(Response.indication == indication)
    if brand:
        stmt = stmt.where(Response.brand_focus == brand)
    if llm_name:
        stmt = stmt.where(EvaluationClaim.llm_name == llm_name)

    # Only findings that can carry an implication. An ALIGNED, CALIBRATED claim is the
    # system working, and turning it into a queue item would make the engine always find
    # something — which is the same as measuring nothing.
    stmt = stmt.where(
        (EvaluationClaim.is_adverse.is_(True))
        | (EvaluationClaim.certainty_verdict.in_((cl.OVERCLAIMED, cl.UNDERCLAIMED)))
    )
    rows = (await db.execute(stmt)).all()

    positions = await _latest_positions(db, [claim.response_id for claim, _ in rows])

    gaps: list[dict] = []
    for claim, response in rows:
        attribution, required = (None, None)
        if claim.classification == cl.UNSUPPORTED:
            attribution, required = await _network_attribution(db, claim)

        implication = impl.classify(
            classification=claim.classification or cl.EVIDENCE_UNAVAILABLE,
            certainty_verdict=claim.certainty_verdict,
            claim_type=claim.claim_type,
            flags=_loads(claim.flags),
            gap_attribution=attribution,
            required_evidence=required,
            verification_states=_verification_states(claim),
        )
        if implication is None:
            continue

        gaps.append({
            "source_type": SOURCE_EVIDENCE_GAP,
            "source_response_id": claim.response_id,
            "question_id": claim.question_id,
            "run_id": claim.run_id,
            "persona": response.persona,
            "therapeutic_area": response.therapeutic_area,
            "indication": claim.indication or response.indication or response.disease,
            "brand_focus": claim.subject or response.brand_focus,
            "llm_name": claim.llm_name or response.llm_name,
            "question_text": response.question_text,
            # Carried so the two finders rank on one scale; the position pass may not have
            # run, which is a different fact from a good position.
            "competitive_position": positions.get(claim.response_id, POSITION_NOT_ASSESSED),
            "gap_severity": implication.severity,
            # The alignment finding itself
            "claim_id": claim.claim_id,
            "claim_text": claim.claim_text,
            "claim_type": claim.claim_type,
            "classification": claim.classification,
            "certainty_verdict": claim.certainty_verdict,
            "finding_reason": claim.reason,
            "gap_attribution": attribution,
            "required_evidence": required,
            "outperforming_competitor": claim.comparator,
            "competitor_domain": None,
            "missing_citations": [],
            "competitor_citation_count": 0,
            # The strategic layer
            "strategic_implication": implication.implication,
            "implication_reason": implication.reason,
            "implication_owner": implication.owner,
            "externally_actionable": implication.externally_actionable,
            "evidence_action": implication.evidence_action,
            "confidence": implication.confidence,
        })

    gaps.sort(key=lambda g: (g["gap_severity"], g["confidence"]), reverse=True)
    return gaps[:limit]


def _verification_states(claim: EvaluationClaim) -> list[str | None]:
    """The review states of the evidence a finding rested on, as recorded at grading time.

    Read from the frozen ``evidence_links`` rather than re-queried: confidence should
    describe the evidence the verdict was actually reached on. Re-reading would let a
    finding's confidence drift without the finding itself changing, which is worse than
    slightly stale.
    """
    links = _loads(claim.evidence_links)
    states: list[str | None] = []
    for link in links:
        if not isinstance(link, dict):
            continue
        role = link.get("relationship_role")
        # A supporting or contradicting link was, by construction, resolved against a
        # verified row: the graders refuse to reach those verdicts otherwise.
        if role in (qg.SUPPORTS_EXPECTED_ANSWER, qg.CONTRADICTS_EXPECTED_ANSWER):
            states.append("VERIFIED")
        else:
            states.append(None)
    return states


async def _latest_positions(db: AsyncSession, response_ids: list[str]) -> dict[str, str]:
    """The most recent scored competitive position per response, where one exists."""
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
    rows = (await db.execute(
        select(ScoringRecord.response_id, ScoringRecord.competitive_position).join(
            subq,
            and_(
                ScoringRecord.response_id == subq.c.response_id,
                ScoringRecord.score_version == subq.c.maxv,
            ),
        )
    )).all()
    return {rid: position for rid, position in rows if position}
