"""Evidence synthesis for one indication (Phase 9, final workstream).

Assembles what the programme already knows into the readout the plan asks for: what the
evidence shows, what changed, where sources agree, how strong it is, what its limitations
are, and what any of it strategically implies.

**Assembled, never inferred.** Every number here is read from a stored row that some earlier
phase already decided: the resolver decided the estimates, the curator decided what is
verified, Phase 7 decided why a gap exists, Phase 8 decided whether a model's answer matched.
This adds no judgement of its own, which is what makes it safe to put on a page — there is
no step here that could be wrong independently of the phase that produced its input.

**Limitations are a first-class output, not a footnote.** A synthesis that reported six
estimates without saying the network is unratified and the studies unverified would read as
settled evidence. On the current corpus the limitations *are* the finding.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.evidence import lifecycles, statuses
from app.evidence import question_generation as qg
from app.models.clinical_study import ClinicalStudy
from app.models.competitor_candidate import ACCEPTED, CompetitorCandidate
from app.models.drug_fact import DrugFact
from app.models.evaluation_claim import EvaluationClaim
from app.models.evidence_network import EvidenceNetwork
from app.models.nma_result import NMAResult
from app.remediation import implications as impl
from app.services import comparison_service
from app.utils.logging import get_logger

logger = get_logger("evidence_synthesis")

DEFAULT_CHANGE_WINDOW_DAYS = 90


def _json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


async def synthesise(
    db: AsyncSession,
    *,
    indication: str,
    network_id: str | None = None,
    change_window_days: int = DEFAULT_CHANGE_WINDOW_DAYS,
) -> dict:
    """The full readout for one indication."""
    network = await _network_for(db, indication, network_id)
    shows, limitations = await _what_the_evidence_shows(db, network)
    return {
        "indication": indication,
        "network_id": network.network_id if network else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "what_the_evidence_shows": shows,
        "what_changed": await _what_changed(db, indication, change_window_days),
        "evidence_strength": await _evidence_strength(db, indication, network),
        "limitations": limitations,
        "competitor_landscape": await _competitors(db, indication),
        "ai_alignment": await _ai_alignment(db, indication),
        "strategic_implications": await _implications(db, indication),
    }


async def _network_for(
    db: AsyncSession, indication: str, network_id: str | None
) -> EvidenceNetwork | None:
    stmt = select(EvidenceNetwork).where(EvidenceNetwork.superseded_by.is_(None))
    stmt = (
        stmt.where(EvidenceNetwork.network_id == network_id)
        if network_id else stmt.where(EvidenceNetwork.indication == indication)
    )
    return (await db.execute(stmt.limit(1))).scalars().first()


async def _what_the_evidence_shows(
    db: AsyncSession, network: EvidenceNetwork | None
) -> tuple[list[dict], list[dict]]:
    """``(releasable comparisons, limitations)`` for the network.

    Only **releasable** comparisons are reported as findings. An exploratory estimate is
    computed but not approved, and putting it in a section headed "what the evidence shows"
    would be precisely the leak the execution modes exist to prevent. Non-releasable pairs
    become limitations instead, which is where they honestly belong.
    """
    if network is None:
        return [], [{
            "kind": "NO_NETWORK",
            "detail": "No evidence network has been built for this indication, so no "
                      "comparison can be reported.",
        }]

    limitations: list[dict] = []
    if network.ratification_status != lifecycles.RATIFIED:
        limitations.append({
            "kind": "NETWORK_NOT_RATIFIED",
            "detail": f"The network is {network.ratification_status}. Nothing below has "
                      "passed both medical and statistical review.",
        })
    if network.is_connected is False:
        limitations.append({
            "kind": "NETWORK_DISCONNECTED",
            "detail": "The network is disconnected: some treatments cannot be compared at all.",
        })
    if not network.has_closed_loops:
        limitations.append({
            "kind": "NO_CLOSED_LOOPS",
            "detail": "The network is a star with no independent loops, so consistency "
                      "between direct and indirect evidence cannot be assessed.",
        })
    routes = json.loads(network.administration_routes or "{}") if network.administration_routes else {}
    if len(set(routes.values())) > 1:
        limitations.append({
            "kind": "ROUTE_MIXING",
            "detail": "The network mixes administration routes, a transitivity threat: "
                      f"{routes}",
        })

    try:
        matrix = await comparison_service.resolve_all_pairs(db, network_id=network.network_id)
    except comparison_service.ComparisonError as exc:
        limitations.append({"kind": "NOT_RESOLVABLE", "detail": str(exc)})
        return [], limitations

    shows: list[dict] = []
    gaps_by_attribution: dict[str, int] = {}
    for answer in matrix["comparisons"]:
        monitorable, _ = qg.is_monitorable_pair(answer["treatment"], answer["comparator"])
        if not monitorable:
            continue
        if statuses.is_releasable(answer["status"]):
            shows.append({
                "treatment": answer["treatment"],
                "comparator": answer["comparator"],
                "statement": qg.describe_estimate(answer),
                "crosses_no_effect": qg.crosses_no_effect(answer),
                "evidence_level": answer.get("evidence_level"),
                "is_direct": answer.get("evidence_level") == 1,
                "is_internal_output": bool(answer.get("is_internal_output")),
                "status": answer["status"],
                "contributing_studies": answer.get("contributing_studies") or [],
                "flags": answer.get("flags") or [],
            })
            continue
        if statuses.is_gap(answer["status"]):
            attribution, _ = qg.attribute_gap(answer["status"], answer.get("scoping"))
            gaps_by_attribution[attribution] = gaps_by_attribution.get(attribution, 0) + 1

    for attribution, count in sorted(gaps_by_attribution.items()):
        limitations.append({
            "kind": f"GAP_{attribution}",
            "detail": _gap_detail(attribution, count),
            "count": count,
        })
    return shows, limitations


def _gap_detail(attribution: str, count: int) -> str:
    """Why these comparisons are missing, in the words that name the right owner."""
    return {
        qg.ATTRIBUTION_CURATION: (
            f"{count} comparison(s) are unavailable because studies we already hold have "
            "not been verified. This is our backlog, not an absence of evidence."
        ),
        qg.ATTRIBUTION_PROTOCOL: (
            f"{count} comparison(s) are unavailable because the approved analysis window "
            "excludes evidence that exists. A statistical-review decision, not a data gap."
        ),
        qg.ATTRIBUTION_EVIDENCE: (
            f"{count} comparison(s) have no in-scope evidence. A genuine evidence gap."
        ),
    }.get(attribution, f"{count} comparison(s) unavailable ({attribution}).")


async def _what_changed(db: AsyncSession, indication: str, window_days: int) -> dict:
    """Studies, labels and syntheses that landed inside the window."""
    since = datetime.now(timezone.utc) - timedelta(days=window_days)

    new_studies = (await db.execute(
        select(ClinicalStudy.study_id, ClinicalStudy.title, ClinicalStudy.verification_status)
        .where(ClinicalStudy.indication == indication, ClinicalStudy.created_at >= since)
        .limit(50)
    )).all()
    new_labels = (await db.execute(
        select(DrugFact.brand, DrugFact.label_updated_at, DrugFact.verification_status)
        .where(DrugFact.updated_at >= since, DrugFact.superseded_by.is_(None))
        .limit(50)
    )).all()
    new_syntheses = (await db.execute(
        select(func.count()).select_from(NMAResult).where(
            NMAResult.indication == indication, NMAResult.created_at >= since
        )
    )).scalar_one_or_none() or 0

    return {
        "window_days": window_days,
        "new_studies": [
            {"study_id": sid, "title": title, "verification_status": status}
            for sid, title, status in new_studies
        ],
        "label_updates": [
            {"brand": brand, "label_updated_at": str(updated) if updated else None,
             "verification_status": status}
            for brand, updated, status in new_labels
        ],
        "new_synthesis_results": int(new_syntheses),
    }


async def _evidence_strength(
    db: AsyncSession, indication: str, network: EvidenceNetwork | None
) -> dict:
    """How much of the corpus has actually been through review.

    The number that matters on this programme today. A study count says how much data was
    harvested; the verified count says how much of it anything is allowed to use.
    """
    rows = (await db.execute(
        select(ClinicalStudy.verification_status, func.count())
        .where(ClinicalStudy.indication == indication)
        .group_by(ClinicalStudy.verification_status)
    )).all()
    by_status = {status: int(count) for status, count in rows}
    total = sum(by_status.values())
    verified = by_status.get(lifecycles.VERIFIED, 0)

    return {
        "studies_total": total,
        "studies_verified": verified,
        "verified_fraction": round(verified / total, 4) if total else None,
        "studies_by_verification_status": by_status,
        "network_ratification_status": network.ratification_status if network else None,
        "network_is_connected": network.is_connected if network else None,
        "network_has_closed_loops": network.has_closed_loops if network else None,
    }


async def _competitors(db: AsyncSession, indication: str) -> dict:
    """Accepted competitor candidates, and the threat each one carries."""
    rows = (await db.execute(
        select(CompetitorCandidate).where(
            CompetitorCandidate.indication == indication,
            CompetitorCandidate.review_status == ACCEPTED,
        )
    )).scalars().all()
    return {
        "accepted_count": len(rows),
        "threats": [
            impl.competitor_threat(
                treatment=row.treatment,
                indication=indication,
                reasons=[str(r) for r in _json_list(row.discovery_reasons)],
                has_posted_results=bool(row.has_posted_results),
            ).as_dict() | {"treatment": row.treatment, "candidate_id": row.candidate_id}
            for row in rows
        ],
    }


async def _ai_alignment(db: AsyncSession, indication: str) -> dict:
    """What monitored models say about this indication, against our evidence (Phase 8).

    The one genuinely cross-source comparison this system holds: our curated evidence on one
    side, what several external models actually say on the other.
    """
    rows = (await db.execute(
        select(
            EvaluationClaim.classification,
            EvaluationClaim.certainty_verdict,
            func.count(),
        )
        .where(EvaluationClaim.indication == indication)
        .group_by(EvaluationClaim.classification, EvaluationClaim.certainty_verdict)
    )).all()

    by_classification: dict[str, int] = {}
    by_certainty: dict[str, int] = {}
    for classification, verdict, count in rows:
        if classification:
            by_classification[classification] = by_classification.get(classification, 0) + int(count)
        if verdict:
            by_certainty[verdict] = by_certainty.get(verdict, 0) + int(count)

    return {
        "claims_evaluated": sum(by_classification.values()),
        "by_classification": by_classification,
        "by_certainty_verdict": by_certainty,
    }


async def _implications(db: AsyncSession, indication: str) -> list[dict]:
    """Strategic implications rolled up, ordered by severity and separated by ownership."""
    rows = (await db.execute(
        select(EvaluationClaim).where(
            EvaluationClaim.indication == indication,
            (EvaluationClaim.is_adverse.is_(True))
            | (EvaluationClaim.certainty_verdict.isnot(None)),
        )
    )).scalars().all()

    counts: dict[str, int] = {}
    for row in rows:
        implication = impl.classify(
            classification=row.classification or "",
            certainty_verdict=row.certainty_verdict,
            claim_type=row.claim_type,
            flags=_json_list(row.flags),
        )
        if implication is not None:
            counts[implication.implication] = counts.get(implication.implication, 0) + 1

    return sorted(
        (
            {
                "implication": name,
                "count": count,
                "owner": impl.OWNER_OF.get(name),
                "severity": impl.SEVERITY_OF.get(name),
                "externally_actionable": name in impl.EXTERNALLY_ACTIONABLE,
            }
            for name, count in counts.items()
        ),
        key=lambda item: (item["severity"] or 0, item["count"]),
        reverse=True,
    )
