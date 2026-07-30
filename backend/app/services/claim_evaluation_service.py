"""Claim-level AI-vs-evidence evaluation — routing and persistence (Phase 8).

``evidence.claims`` decides verdicts and ``evidence.claim_extraction`` talks to the model;
this fetches the authoritative evidence each claim needs and writes the findings down. Same
split as Phase 7, for the same reason: every verdict stays a pure function of stored data,
so re-running the grader on the same rows a year from now gives the same answer.

**Evaluation is opt-in per run.** It costs one extra model call per response on top of
scoring, and a scheduled full-bank run would double the post-run bill without anyone asking
for it. ``run_service`` therefore does not call this; it is triggered explicitly, which also
means a failed extraction can never fail a monitoring run.

**Governed evidence only.** ``resolve_comparison`` is called in ``GOVERNED`` mode and a
non-releasable answer is reported as evidence unavailable rather than used. The execution
mode table says an exploratory result may not affect AI scoring, and an alignment dashboard
is AI scoring — building it on numbers no statistician has approved would be exactly the
leak the mode exists to prevent.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import taxonomy
from app.evidence import claims as cl
from app.evidence import claim_extraction, lifecycles
from app.models.clinical_study import ClinicalStudy, OutcomeResult, StudyArm
from app.models.competitor_candidate import ACCEPTED, CompetitorCandidate
from app.models.drug_fact import DrugFact
from app.models.evaluation_claim import EvaluationClaim
from app.models.evidence_network import EvidenceNetwork
from app.models.nma_result import GOVERNED
from app.models.response import Response
from app.services import comparison_service
from app.utils.audit import write_audit
from app.utils.logging import get_logger

logger = get_logger("claim_evaluation")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(x) for x in parsed] if isinstance(parsed, list) else []


# =========================================================================================
# Resolving each claim's authority
# =========================================================================================
async def _verified_drug_fact(db: AsyncSession, brand: str | None) -> DrugFact | None:
    """The current verified label record for a brand, or ``None``.

    Verified only. An approval or boxed-warning finding drawn from an unreviewed label
    extraction would be a confident assertion about a regulatory fact nobody has checked —
    the same reason Phase 7 refuses to generate those questions from unverified facts.
    """
    if not brand:
        return None
    return (await db.execute(
        select(DrugFact)
        .where(
            func.lower(DrugFact.brand) == brand.strip().lower(),
            DrugFact.verification_status == lifecycles.VERIFIED,
            DrugFact.superseded_by.is_(None),
        )
        .order_by(DrugFact.label_updated_at.desc().nullslast())
        .limit(1)
    )).scalars().first()


async def _network_for(
    db: AsyncSession, *, indication: str | None, treatment: str, comparator: str | None
) -> EvidenceNetwork | None:
    """A network holding both treatments for this indication, or ``None``.

    Node membership is matched through ``treatments.canonical_treatment`` rather than by
    string, so a response saying "upadacitinib" reaches the Rinvoq node instead of silently
    missing every network and reporting a coverage gap we do not have.
    """
    if not comparator:
        return None
    from app.evidence import treatments

    wanted = {
        treatments.canonical_treatment(treatment)[0].lower(),
        treatments.canonical_treatment(comparator)[0].lower(),
    }
    stmt = select(EvidenceNetwork).where(EvidenceNetwork.superseded_by.is_(None))
    if indication:
        stmt = stmt.where(EvidenceNetwork.indication == indication)

    for network in (await db.execute(stmt)).scalars().all():
        nodes = {
            treatments.canonical_treatment(n)[0].lower()
            for n in _json_list(network.treatment_nodes)
        }
        if wanted <= nodes:
            return network
    return None


async def _resolve_identifiers(
    db: AsyncSession, identifiers: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(resolvable, unresolvable)`` for the trial identifiers a claim cited."""
    if not identifiers:
        return (), ()
    rows = (await db.execute(
        select(ClinicalStudy.study_id, ClinicalStudy.registry_id).where(
            ClinicalStudy.study_id.in_(identifiers)
            | ClinicalStudy.registry_id.in_(identifiers)
        )
    )).all()
    known = {value for row in rows for value in row if value}
    resolvable = tuple(i for i in identifiers if i in known)
    return resolvable, tuple(i for i in identifiers if i not in known)


# =========================================================================================
# Grading one claim
# =========================================================================================
async def grade_claim(
    db: AsyncSession, claim: cl.ExtractedClaim, *, indication: str | None
) -> cl.Finding:
    """Route *claim* to its authoritative evidence and grade it there.

    Every branch calls a grader that asserts its own routing first, so a claim type added
    without a policy entry raises rather than falling through to a default. There is no
    ``else: return ALIGNED`` here by design — an unhandled claim type must be a loud failure,
    not a silent pass.
    """
    scope = claim.indication or indication

    if claim.claim_type == cl.APPROVAL_CLAIM:
        fact = await _verified_drug_fact(db, claim.subject)
        return cl.grade_approval(
            claim,
            fact_id=fact.fact_id if fact else None,
            approved_indications=_json_list(fact.approved_indications) if fact else [],
            label_updated_at=fact.label_updated_at if fact else None,
        )

    if claim.claim_type == cl.SAFETY_WARNING_CLAIM:
        fact = await _verified_drug_fact(db, claim.subject)
        return cl.grade_safety(
            claim,
            fact_id=fact.fact_id if fact else None,
            boxed_warnings=_json_list(fact.boxed_warnings) if fact else [],
            has_boxed_warning=bool(fact.has_boxed_warning) if fact else None,
            label_updated_at=fact.label_updated_at if fact else None,
        )

    if claim.claim_type in (cl.DIRECT_COMPARISON_CLAIM, cl.RANKING_CLAIM, cl.CERTAINTY_CLAIM):
        return await _grade_comparative(db, claim, scope)

    if claim.claim_type == cl.MECHANISM_CLAIM:
        # The verified label's own class first, the curated table second. A label we have
        # reviewed outranks a config entry nobody re-checks when a label changes.
        fact = await _verified_drug_fact(db, claim.subject)
        return cl.grade_mechanism(
            claim,
            fact_id=fact.fact_id if fact else None,
            drug_class=(fact.drug_class if fact else None)
            or taxonomy.drug_class_for(claim.subject),
        )

    if claim.claim_type == cl.PIPELINE_CLAIM:
        return await _grade_pipeline(db, claim, scope)

    if claim.claim_type == cl.TRIAL_RESULT_CLAIM:
        return await _grade_trial_result(db, claim)

    raise cl.ClaimError(f"no grader is wired for claim type {claim.claim_type!r}")


async def _grade_comparative(
    db: AsyncSession, claim: cl.ExtractedClaim, indication: str | None
) -> cl.Finding:
    """A comparative, ranking or certainty claim, resolved through the evidence network."""
    network = await _network_for(
        db, indication=indication, treatment=claim.subject, comparator=claim.comparator
    )
    if network is None:
        return cl.evidence_unavailable(
            claim,
            f"no evidence network holds both {claim.subject} and "
            f"{claim.comparator or '(no comparator named)'}"
            + (f" for {indication}" if indication else ""),
        )
    try:
        answer = await comparison_service.resolve_comparison(
            db,
            network_id=network.network_id,
            treatment_a=claim.subject,
            treatment_b=claim.comparator,
            execution_mode=GOVERNED,
        )
    except comparison_service.ComparisonError as exc:
        return cl.evidence_unavailable(claim, f"the comparison could not be scoped: {exc}")

    return cl.grade_comparison(
        claim,
        answer=answer,
        canonical_outcome_id=network.canonical_outcome_id,
        network_id=network.network_id,
    )


async def _grade_pipeline(
    db: AsyncSession, claim: cl.ExtractedClaim, indication: str | None
) -> cl.Finding:
    # Matched on ARMS, not the title. A registry title often names only the sponsor's own
    # agent, so a title search misses every trial where the subject was the comparator.
    study = (await db.execute(
        select(ClinicalStudy)
        .join(StudyArm, StudyArm.study_id == ClinicalStudy.study_id)
        .where(func.lower(StudyArm.treatment) == claim.subject.strip().lower())
        .limit(1)
    )).scalars().first() if claim.subject else None

    candidate = (await db.execute(
        select(CompetitorCandidate).where(
            func.lower(CompetitorCandidate.treatment) == claim.subject.strip().lower(),
            CompetitorCandidate.review_status == ACCEPTED,
        ).limit(1)
    )).scalars().first() if claim.subject else None

    return cl.grade_pipeline(
        claim,
        study_id=study.study_id if study else None,
        development_phase=(
            (candidate.development_phase if candidate else None)
            or (study.phase if study else None)
        ),
        candidate_id=candidate.candidate_id if candidate else None,
    )


async def _grade_trial_result(db: AsyncSession, claim: cl.ExtractedClaim) -> cl.Finding:
    """A stated number against a **verified** outcome row.

    Verification is required, not preferred: telling a brand team a model has the numbers
    wrong, on the strength of an extraction nobody has checked, is the most embarrassing
    output this system could produce.
    """
    if not claim.cited_identifiers:
        return cl.evidence_unavailable(
            claim,
            "the response cites no trial identifier, so no single stored result can be "
            "matched to the number it states",
        )
    resolvable, _ = await _resolve_identifiers(db, claim.cited_identifiers)
    if not resolvable:
        return cl.evidence_unavailable(
            claim, f"none of the cited identifiers are in our corpus: "
                   f"{', '.join(claim.cited_identifiers)}"
        )

    row = (await db.execute(
        select(OutcomeResult)
        .join(ClinicalStudy, ClinicalStudy.study_id == OutcomeResult.study_id)
        .where(
            (ClinicalStudy.study_id.in_(resolvable))
            | (ClinicalStudy.registry_id.in_(resolvable)),
            OutcomeResult.verification_status == lifecycles.VERIFIED,
        )
        .limit(1)
    )).scalars().first()
    if row is None:
        return cl.evidence_unavailable(
            claim,
            f"the cited trial is in our corpus but holds no VERIFIED result for "
            f"{claim.outcome or 'this endpoint'}",
        )

    # A response quoting "45% of patients" is quoting a RATE, and the stored row holds
    # events and a denominator. Comparing 45 against a raw event count would report a
    # contradiction on every correctly-quoted percentage.
    stored, unit = _stored_value(row, claim)
    return cl.grade_trial_result(
        claim, study_id=row.result_id, stored_value=stored, stored_unit=unit
    )


def _stored_value(row: OutcomeResult, claim: cl.ExtractedClaim) -> tuple[float | None, str | None]:
    """The stored result on the same scale the claim stated it."""
    if (claim.magnitude_unit or "").strip() == "%":
        if row.events is not None and row.sample_size:
            return round(100.0 * row.events / row.sample_size, 2), "%"
        return None, None
    if row.events is not None:
        return float(row.events), " events"
    return (float(row.mean), None) if row.mean is not None else (None, None)


# =========================================================================================
# Evaluating a response
# =========================================================================================
async def evaluate_response(
    db: AsyncSession, response: Response, *, commit: bool = True
) -> dict:
    """Extract, route, grade and persist every claim in one response."""
    extraction = await claim_extraction.extract(
        response.question_text, response.response_text,
        indication=response.disease or response.indication,
    )
    if not extraction["ok"]:
        return {
            "response_id": response.response_id, "ok": False,
            "error": extraction.get("error"), "claim_count": 0,
        }

    findings: list[cl.Finding] = []
    rows: list[EvaluationClaim] = []
    for index, claim in enumerate(extraction["claims"]):
        try:
            finding = await grade_claim(
                db, claim, indication=response.disease or response.indication
            )
        except cl.CategoryError:
            # A routing bug, not a finding. Re-raised so a mis-wired grader fails a test
            # rather than filing a verdict against evidence that cannot answer it.
            raise
        findings.append(finding)
        rows.append(_row_for(response, claim, finding, index, extraction["model_id"]))

        resolvable, unresolvable = await _resolve_identifiers(db, claim.cited_identifiers)
        citation_finding = cl.grade_citations(
            claim, resolvable=resolvable, unresolvable=unresolvable
        )
        if citation_finding is not None:
            findings.append(citation_finding)
            rows.append(_row_for(
                response, claim, citation_finding, index, extraction["model_id"],
                suffix="-cite",
            ))

    if commit:
        for row in rows:
            db.add(row)
        await write_audit(
            db, role="ORCHESTRATOR", event="CLAIMS_EVALUATED",
            run_id=response.run_id, question_id=response.question_id,
            llm_target=response.llm_name,
            context={"claims": len(rows), "adverse": sum(1 for f in findings if f.is_adverse)},
            commit=False,
        )
        await db.commit()

    return {
        "response_id": response.response_id,
        "ok": True,
        "claim_count": len(extraction["claims"]),
        "rejected": extraction["rejected"],
        "findings": [f.as_dict() for f in findings],
        "summary": cl.roll_up(findings),
        "committed": commit,
    }


def _row_for(
    response: Response,
    claim: cl.ExtractedClaim,
    finding: cl.Finding,
    index: int,
    model_id: str | None,
    *,
    suffix: str = "",
) -> EvaluationClaim:
    return EvaluationClaim(
        claim_id=f"EC-{uuid.uuid4().hex[:12]}{suffix}",
        response_id=response.response_id,
        run_id=response.run_id,
        question_id=response.question_id,
        llm_name=response.llm_name,
        claim_text=claim.claim_text,
        claim_type=claim.claim_type,
        subject=claim.subject,
        comparator=claim.comparator,
        indication=claim.indication,
        outcome=claim.outcome,
        direction=claim.direction,
        polarity=claim.polarity,
        certainty=claim.certainty,
        magnitude=claim.magnitude,
        magnitude_unit=claim.magnitude_unit,
        cited_identifiers=json.dumps(list(claim.cited_identifiers)),
        expected_evidence_policy=json.dumps(
            list(cl.authoritative_evidence_for(claim.claim_type))
        ),
        classification=finding.classification,
        reason=finding.reason,
        dimensions=json.dumps(list(finding.dimensions)),
        evidence_links=json.dumps(finding.as_dict()["evidence"]),
        certainty_verdict=finding.certainty_verdict,
        flags=json.dumps(list(finding.flags)),
        is_adverse=finding.is_adverse,
        extracted_by=model_id,
        extraction_version=claim_extraction.EXTRACTION_VERSION,
        claim_index=index,
    )


async def evaluate_run(db: AsyncSession, run_id: str, *, limit: int = 200) -> dict:
    """Evaluate every successful response in a run.

    Sequential rather than fanned out: extraction is one model call per response and the
    provider rate limits are shared with the monitoring run itself. A burst here would
    throttle the thing this exists to measure.
    """
    responses = (await db.execute(
        select(Response)
        .where(Response.run_id == run_id, Response.status.in_(("SUCCESS", "TRUNCATED")))
        .limit(limit)
    )).scalars().all()

    evaluated, failed, all_findings = 0, 0, []
    for response in responses:
        result = await evaluate_response(db, response, commit=True)
        if result["ok"]:
            evaluated += 1
            all_findings.extend(result["findings"])
        else:
            failed += 1

    return {
        "run_id": run_id,
        "responses": len(responses),
        "evaluated": evaluated,
        "failed": failed,
        "finding_count": len(all_findings),
    }


# =========================================================================================
# Alignment dashboard
# =========================================================================================
async def alignment_report(
    db: AsyncSession,
    *,
    run_id: str | None = None,
    llm_name: str | None = None,
    indication: str | None = None,
) -> dict:
    """Alignment across stored claims, by model and by dimension.

    Reports ``coverage`` beside every score. A model that looks 100% aligned on four
    checkable claims out of thirty is not aligned, it is unmeasured, and a dashboard showing
    only the score would say the opposite of the truth.
    """
    stmt = select(EvaluationClaim)
    if run_id:
        stmt = stmt.where(EvaluationClaim.run_id == run_id)
    if llm_name:
        stmt = stmt.where(EvaluationClaim.llm_name == llm_name)
    if indication:
        stmt = stmt.where(EvaluationClaim.indication == indication)
    rows = (await db.execute(stmt)).scalars().all()

    findings = [
        cl.Finding(
            classification=row.classification or cl.EVIDENCE_UNAVAILABLE,
            reason=row.reason or "",
            dimensions=tuple(_json_list(row.dimensions)),
            certainty_verdict=row.certainty_verdict,
            flags=tuple(_json_list(row.flags)),
        )
        for row in rows
    ]

    by_model: dict[str, list[cl.Finding]] = {}
    for row, finding in zip(rows, findings):
        by_model.setdefault(row.llm_name or "unknown", []).append(finding)

    return {
        "filters": {"run_id": run_id, "llm_name": llm_name, "indication": indication},
        "overall": cl.roll_up(findings),
        "by_model": {name: cl.roll_up(items) for name, items in sorted(by_model.items())},
        "by_claim_type": {
            claim_type: cl.roll_up([
                f for row, f in zip(rows, findings) if row.claim_type == claim_type
            ])
            for claim_type in sorted({row.claim_type for row in rows})
        },
        "adverse_examples": [
            {
                "claim_id": row.claim_id,
                "llm_name": row.llm_name,
                "claim_text": row.claim_text,
                "claim_type": row.claim_type,
                "classification": row.classification,
                "reason": row.reason,
            }
            for row in rows if row.is_adverse
        ][:25],
    }
