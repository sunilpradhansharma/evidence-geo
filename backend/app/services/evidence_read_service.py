"""Read-only projections over the evidence store (X2).

Phases 2-6 built networks, studies, arms, outcome rows and drug facts, and shipped three
routers on top: ``/comparisons``, ``/evidence-review`` and ``/published-syntheses``. Two of
those three require a ``network_id``, and **nothing exposed one** — networks are created by
``scripts/ingest_evidence.py`` and could only be reached by someone who already knew the id.
This module is the missing entry point.

Three rules it keeps:

* **It writes nothing.** Verification, membership and ratification transitions belong to
  ``evidence_ingestion_service`` and ``evidence_review_service``. A read surface that could
  also decide would put a lifecycle transition one accidental click away.
* **It judges nothing.** Every status, flag and exclusion reason is reported as stored. The
  one derived figure is the protocol scope, and that is *asked of* the builder rather than
  recomputed here.
* **It hides no mismatch.** ``mismatch_flags`` travel with every row, because a UI that
  shows a clean number for a row flagged ``EVENTS_DERIVED_FROM_PERCENTAGE`` is worse than no
  UI: it launders a caveat the extraction was careful to record.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.evidence import lifecycles
from app.models.clinical_study import ClinicalStudy, OutcomeResult, StudyArm
from app.models.drug_fact import DrugFact
from app.models.evidence_network import EvidenceNetwork, NetworkMembership
from app.services import network_builder_service as builder


class EvidenceNotFound(LookupError):
    """A named network, study or drug that does not exist."""


def _json_list(raw: str | None) -> list:
    """Parse a JSON list column, tolerating null and malformed text.

    Tolerant because a display surface must not 500 on one bad row — the alternative is a
    whole network page failing because a single flags column was written by an older
    parser.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _json_obj(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


# --- overview ---------------------------------------------------------------------------
async def overview(db: AsyncSession) -> dict[str, Any]:
    """Headline counts for the section landing page.

    It leads with **canonical endpoint coverage** deliberately. That single ratio is the
    difference between a store that looks full and a store that can answer anything: an
    outcome row with no ``canonical_outcome_id`` is invisible to every network, and the live
    corpus sits near 5%. A landing page reporting only "6342 results ingested" would be
    true and thoroughly misleading.
    """
    async def _grouped(column, model) -> dict[str, int]:
        rows = (await db.execute(
            select(column, func.count()).select_from(model).group_by(column)
        )).all()
        return {str(k): int(n) for k, n in rows}

    outcome_total = (await db.execute(
        select(func.count()).select_from(OutcomeResult)
    )).scalar_one()
    outcome_canonical = (await db.execute(
        select(func.count()).select_from(OutcomeResult)
        .where(OutcomeResult.canonical_outcome_id.is_not(None))
    )).scalar_one()
    networks_connected = (await db.execute(
        select(func.count()).select_from(EvidenceNetwork)
        .where(EvidenceNetwork.is_connected.is_(True))
    )).scalar_one()

    return {
        "studies": {
            "total": int((await db.execute(
                select(func.count()).select_from(ClinicalStudy)
            )).scalar_one()),
            "by_verification_status": await _grouped(
                ClinicalStudy.verification_status, ClinicalStudy
            ),
            "by_indication": await _grouped(ClinicalStudy.indication, ClinicalStudy),
        },
        "outcome_results": {
            "total": int(outcome_total),
            "with_canonical_outcome": int(outcome_canonical),
            "canonical_coverage_pct": (
                round(100.0 * outcome_canonical / outcome_total, 1) if outcome_total else 0.0
            ),
        },
        "networks": {
            "total": int((await db.execute(
                select(func.count()).select_from(EvidenceNetwork)
            )).scalar_one()),
            "by_ratification_status": await _grouped(
                EvidenceNetwork.ratification_status, EvidenceNetwork
            ),
            "connected": int(networks_connected),
        },
        "drug_facts": {
            "total": int((await db.execute(
                select(func.count()).select_from(DrugFact)
                .where(DrugFact.superseded_by.is_(None))
            )).scalar_one()),
            "by_verification_status": await _grouped(
                DrugFact.verification_status, DrugFact
            ),
        },
    }


# --- networks ---------------------------------------------------------------------------
def _network_row(network: EvidenceNetwork, counts: dict[str, int]) -> dict[str, Any]:
    """List-shaped view. Enough to choose a network, not enough to analyse one."""
    nodes = _json_list(network.treatment_nodes)
    edges = _json_list(network.comparator_edges)
    return {
        "network_id": network.network_id,
        "label": network.label,
        "indication": network.indication,
        "canonical_outcome_id": network.canonical_outcome_id,
        "population_stratum": network.population_stratum,
        "treatment_phase": network.treatment_phase,
        "protocol_id": network.protocol_id,
        "ratification_status": network.ratification_status,
        "is_connected": network.is_connected,
        "has_closed_loops": network.has_closed_loops,
        "has_multi_arm_studies": network.has_multi_arm_studies,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "membership_counts": counts,
        "version": network.version,
        "superseded_by": network.superseded_by,
        "updated_at": _iso(network.updated_at),
    }


async def _membership_counts(db: AsyncSession, network_ids: list[str]) -> dict[str, dict[str, int]]:
    """``{network_id: {status: count}}`` in one query rather than one per network."""
    if not network_ids:
        return {}
    rows = (await db.execute(
        select(
            NetworkMembership.network_id,
            NetworkMembership.membership_status,
            func.count().label("n"),
        )
        .where(NetworkMembership.network_id.in_(network_ids))
        .group_by(NetworkMembership.network_id, NetworkMembership.membership_status)
    )).all()
    counts: dict[str, dict[str, int]] = {nid: {} for nid in network_ids}
    for network_id, status, n in rows:
        counts.setdefault(network_id, {})[status] = int(n)
    return counts


async def list_networks(
    db: AsyncSession,
    *,
    indication: str | None = None,
    ratification_status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Every network, newest first. The entry point ``/comparisons`` never had."""
    stmt = select(EvidenceNetwork)
    if indication:
        stmt = stmt.where(EvidenceNetwork.indication == indication)
    if ratification_status:
        stmt = stmt.where(EvidenceNetwork.ratification_status == ratification_status)
    total = (await db.execute(
        select(func.count()).select_from(stmt.subquery())
    )).scalar_one()
    networks = list((await db.execute(
        stmt.order_by(EvidenceNetwork.updated_at.desc()).limit(max(1, min(limit, 500)))
    )).scalars().all())

    counts = await _membership_counts(db, [n.network_id for n in networks])
    return {
        "total": int(total),
        "networks": [_network_row(n, counts.get(n.network_id, {})) for n in networks],
        "ratification_states": list(lifecycles.RATIFICATION_STATES),
        "membership_states": list(lifecycles.MEMBERSHIP_STATES),
    }


async def get_network(db: AsyncSession, network_id: str) -> dict[str, Any]:
    """One network: stored topology, memberships, and what a protocol window narrows.

    The two topologies are reported side by side and labelled, because the stored one is
    endpoint-level and a protocol-scoped resolve can legitimately see fewer nodes. Showing
    only the stored graph is how a surface ends up promising a comparison the resolver
    refuses — the defect the ``ProtocolScope`` disclosure exists to make visible.
    """
    network = (await db.execute(
        select(EvidenceNetwork).where(EvidenceNetwork.network_id == network_id)
    )).scalar_one_or_none()
    if network is None:
        raise EvidenceNotFound(f"no network {network_id!r}")

    memberships = list((await db.execute(
        select(NetworkMembership)
        .where(NetworkMembership.network_id == network_id)
        .order_by(NetworkMembership.study_id)
    )).scalars().all())

    study_ids = [m.study_id for m in memberships]
    titles: dict[str, ClinicalStudy] = {}
    if study_ids:
        titles = {
            s.study_id: s for s in (await db.execute(
                select(ClinicalStudy).where(ClinicalStudy.study_id.in_(study_ids))
            )).scalars().all()
        }

    scope = await builder.protocol_scope_for(db, network)
    counts: dict[str, int] = {}
    for m in memberships:
        counts[m.membership_status] = counts.get(m.membership_status, 0) + 1

    return {
        **_network_row(network, counts),
        "endpoint_topology": {
            "nodes": _json_list(network.treatment_nodes),
            "edges": _json_list(network.comparator_edges),
            "administration_routes": _json_obj(network.administration_routes),
        },
        # None means no protocol governs this network, so there is no window to narrow —
        # not that nothing is answerable.
        "protocol_scope": scope.as_dict() if scope else None,
        "overstates_answerable": bool(scope and scope.narrows),
        "ratification": {
            "status": network.ratification_status,
            "medical_reviewer": network.medical_reviewer,
            "medical_reviewed_at": _iso(network.medical_reviewed_at),
            "medical_review_note": network.medical_review_note,
            "statistical_reviewer": network.statistical_reviewer,
            "statistical_reviewed_at": _iso(network.statistical_reviewed_at),
            "statistical_review_note": network.statistical_review_note,
            "rejection_reason": network.rejection_reason,
        },
        "memberships": [
            {
                "membership_id": m.membership_id,
                "study_id": m.study_id,
                "protocol_id": m.protocol_id,
                "membership_status": m.membership_status,
                "exclusion_reason": m.exclusion_reason,
                "proposal_rationale": m.proposal_rationale,
                "review_note": m.review_note,
                "mismatch_flags": _json_list(m.mismatch_flags),
                "decided_by": m.decided_by,
                "decided_at": _iso(m.decided_at),
                "registry_id": getattr(titles.get(m.study_id), "registry_id", None),
                "acronym": getattr(titles.get(m.study_id), "acronym", None),
                "title": getattr(titles.get(m.study_id), "title", None),
                "verification_status": getattr(
                    titles.get(m.study_id), "verification_status", None
                ),
            }
            for m in memberships
        ],
    }


# --- studies ----------------------------------------------------------------------------
def _study_row(study: ClinicalStudy) -> dict[str, Any]:
    return {
        "study_id": study.study_id,
        "registry_id": study.registry_id,
        "acronym": study.acronym,
        "title": study.title,
        "indication": study.indication,
        "phase": study.phase,
        "treatment_phase": study.treatment_phase,
        "is_randomised": study.is_randomised,
        "population_stratum": study.population_stratum,
        "enrollment": study.enrollment,
        "sponsor": study.sponsor,
        "results_first_posted": _iso(study.results_first_posted),
        "risk_of_bias": study.risk_of_bias,
        "verification_status": study.verification_status,
        "verified_by": study.verified_by,
        "mismatch_flags": _json_list(study.mismatch_flags),
        "source_is_citable": study.source_is_citable,
        "claim_is_approved_for_external_use": study.claim_is_approved_for_external_use,
        "version": study.version,
        "superseded_by": study.superseded_by,
        "updated_at": _iso(study.updated_at),
    }


async def list_studies(
    db: AsyncSession,
    *,
    indication: str | None = None,
    verification_status: str | None = None,
    treatment: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Ingested studies with arm and outcome counts.

    ``treatment`` filters through ``StudyArm``, so "which trials have a Rinvoq arm?" is one
    request. It is the question a curator asks before anything else.
    """
    stmt = select(ClinicalStudy)
    if indication:
        stmt = stmt.where(ClinicalStudy.indication == indication)
    if verification_status:
        stmt = stmt.where(ClinicalStudy.verification_status == verification_status)
    if treatment:
        stmt = stmt.where(
            ClinicalStudy.study_id.in_(
                select(StudyArm.study_id).where(StudyArm.treatment == treatment)
            )
        )
    total = (await db.execute(
        select(func.count()).select_from(stmt.subquery())
    )).scalar_one()
    studies = list((await db.execute(
        stmt.order_by(ClinicalStudy.updated_at.desc()).limit(max(1, min(limit, 500)))
    )).scalars().all())

    rows = []
    for study in studies:
        # `arms` and `outcomes` are lazy="selectin", so these are already loaded.
        canonical = {o.canonical_outcome_id for o in study.outcomes if o.canonical_outcome_id}
        rows.append({
            **_study_row(study),
            "arm_count": len(study.arms),
            "outcome_count": len(study.outcomes),
            # The gap Issue 2 measures: how many rows carry an endpoint a network can use.
            "canonical_outcome_count": sum(
                1 for o in study.outcomes if o.canonical_outcome_id
            ),
            "canonical_outcome_ids": sorted(canonical),
            "treatments": sorted({a.treatment for a in study.arms if a.treatment}),
        })
    return {
        "total": int(total),
        "studies": rows,
        "verification_states": list(lifecycles.VERIFICATION_STATES),
    }


async def get_study(db: AsyncSession, study_id: str) -> dict[str, Any]:
    """One study with its arms and every outcome row, flags included."""
    study = (await db.execute(
        select(ClinicalStudy).where(ClinicalStudy.study_id == study_id)
    )).scalar_one_or_none()
    if study is None:
        raise EvidenceNotFound(f"no study {study_id!r}")

    arm_labels = {a.arm_id: a.treatment for a in study.arms}
    return {
        **_study_row(study),
        "study_design": study.study_design,
        "population_description": study.population_description,
        "prior_treatment_status": study.prior_treatment_status,
        "risk_of_bias_rationale": study.risk_of_bias_rationale,
        "start_date": _iso(study.start_date),
        "completion_date": _iso(study.completion_date),
        "source_payload_id": study.source_payload_id,
        "extraction_confidence": study.extraction_confidence,
        "extraction_rationale": study.extraction_rationale,
        "rejection_reason": study.rejection_reason,
        "arms": [
            {
                "arm_id": a.arm_id,
                "label": a.label,
                "treatment": a.treatment,
                "is_placebo": a.is_placebo,
                "drug_class": a.drug_class,
                "administration_route": a.administration_route,
                "dose_value": a.dose_value,
                "dose_unit": a.dose_unit,
                "dose_frequency": a.dose_frequency,
                "dose_description": a.dose_description,
                "sample_size": a.sample_size,
            }
            for a in sorted(study.arms, key=lambda a: (a.treatment or "", a.arm_id))
        ],
        "outcomes": [
            {
                "result_id": o.result_id,
                "arm_id": o.arm_id,
                "arm_treatment": arm_labels.get(o.arm_id or ""),
                "canonical_outcome_id": o.canonical_outcome_id,
                "endpoint": o.endpoint,
                "timepoint_week": o.timepoint_week,
                "population_stratum": o.population_stratum,
                "treatment_phase": o.treatment_phase,
                "outcome_type": o.outcome_type,
                "events": o.events,
                "sample_size": o.sample_size,
                "mean": o.mean,
                "standard_deviation": o.standard_deviation,
                "comparator_treatment": o.comparator_treatment,
                "effect_estimate": o.effect_estimate,
                "effect_measure": o.effect_measure,
                "ci_lower": o.ci_lower,
                "ci_upper": o.ci_upper,
                "is_significant": o.is_significant,
                "is_safety_outcome": o.is_safety_outcome,
                "mismatch_flags": _json_list(o.mismatch_flags),
                "verification_status": o.verification_status,
            }
            for o in sorted(
                study.outcomes,
                key=lambda o: (o.endpoint or "", o.timepoint_week or 0, o.result_id),
            )
        ],
    }


# --- drug facts -------------------------------------------------------------------------
def _fact_row(fact: DrugFact) -> dict[str, Any]:
    return {
        "fact_id": fact.fact_id,
        "brand": fact.brand,
        "generic": fact.generic,
        "molecule": fact.molecule,
        "manufacturer": fact.manufacturer,
        "mechanism_of_action": fact.mechanism_of_action,
        "drug_class": fact.drug_class,
        "administration_route": fact.administration_route,
        "dosage_form": fact.dosage_form,
        "approved_indications": _json_list(fact.approved_indications),
        "approval_date": _iso(fact.approval_date),
        "label_updated_at": _iso(fact.label_updated_at),
        "contraindications": _json_list(fact.contraindications),
        "boxed_warnings": _json_list(fact.boxed_warnings),
        "common_adverse_events": _json_list(fact.common_adverse_events),
        "serious_adverse_events": _json_list(fact.serious_adverse_events),
        "has_boxed_warning": fact.has_boxed_warning,
        "regulatory_source": fact.regulatory_source,
        "prescribing_information": fact.prescribing_information,
        "extraction_confidence": fact.extraction_confidence,
        "mismatch_flags": _json_list(fact.mismatch_flags),
        "verification_status": fact.verification_status,
        "verified_by": fact.verified_by,
        # Reported as the two independent properties they are. A published label is citable
        # the moment it exists; our extraction of it is not approved for external use until
        # MLR says so, and collapsing the two is how unreviewed wording reaches an audience.
        "source_is_citable": fact.source_is_citable,
        "claim_is_approved_for_external_use": fact.claim_is_approved_for_external_use,
        "version": fact.version,
        "superseded_by": fact.superseded_by,
        "updated_at": _iso(fact.updated_at),
    }


async def list_drug_facts(
    db: AsyncSession,
    *,
    brand: str | None = None,
    verification_status: str | None = None,
    current_only: bool = True,
    limit: int = 200,
) -> dict[str, Any]:
    """Label-derived facts. ``current_only`` drops superseded label versions."""
    stmt = select(DrugFact)
    if brand:
        stmt = stmt.where(DrugFact.brand == brand)
    if verification_status:
        stmt = stmt.where(DrugFact.verification_status == verification_status)
    if current_only:
        stmt = stmt.where(DrugFact.superseded_by.is_(None))
    total = (await db.execute(
        select(func.count()).select_from(stmt.subquery())
    )).scalar_one()
    facts = list((await db.execute(
        stmt.order_by(DrugFact.brand, DrugFact.version.desc())
        .limit(max(1, min(limit, 500)))
    )).scalars().all())
    return {"total": int(total), "drug_facts": [_fact_row(f) for f in facts]}


async def get_drug_fact(db: AsyncSession, brand: str) -> dict[str, Any]:
    """The current label version for *brand*, plus its superseded history.

    History is returned rather than discarded because a drug fact is superseded when the
    label changes, and "what did the label say when we scored that response?" is a real
    question with an auditable answer.
    """
    facts = list((await db.execute(
        select(DrugFact)
        .where(func.lower(DrugFact.brand) == brand.strip().lower())
        .order_by(DrugFact.version.desc())
    )).scalars().all())
    if not facts:
        raise EvidenceNotFound(f"no drug facts for {brand!r}")

    current = next((f for f in facts if f.superseded_by is None), facts[0])
    return {
        "brand": current.brand,
        "current": _fact_row(current),
        "history": [_fact_row(f) for f in facts if f.fact_id != current.fact_id],
    }
