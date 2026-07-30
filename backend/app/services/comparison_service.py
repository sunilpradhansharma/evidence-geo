"""Resolve one comparison against a network, end to end (Phase 6).

The impure half of the resolver. ``evidence.resolver`` is pure and knows the hierarchy;
this module knows the database, the governance gate and the sidecar. Keeping them apart is
what lets the hierarchy be tested without a session, exactly as ``approvals.py`` is.

**Scoping happens in the query, and every rejection is kept.** A result at week 12 when the
protocol approved weeks 14-18 is not silently dropped: if its study randomised both
requested treatments it becomes ``unsuitable_direct``, which is how the answer can say
*"a head-to-head trial exists but reports week 12"* instead of *"no head-to-head trial"*.
Those are different findings and only one of them sends a reviewer to look at a paper.

**Only ratified evidence reaches a governed run.** Studies must be VERIFIED and their
membership INCLUDED. In EXPLORATORY mode the same filter still applies to *verification* —
computing on unverified extractions would produce a number whose inputs nobody has checked,
which is not exploratory, it is wrong.

**GOVERNED is asked for, never assumed.** The caller states the mode; the gate decides
whether it is permitted, and a refusal downgrades to EXPLORATORY with the blocking status
recorded rather than raising.

**No value is decided by row order.** Two in-scope rows can describe one arm — the registry
posts a result twice, or a window admits two timepoints — and if they disagree the arm is
withheld as ``AMBIGUOUS_ARM_DATA`` rather than settled by whichever was read last. Identical
duplicates collapse, because that case has no choice in it to get wrong.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.evidence import lifecycles, protocols, resolver, statuses
from app.evidence.engines import netmeta
from app.evidence.engines.pairwise import (
    BinaryArm,
    ContinuousArm,
    StudyContrast,
    binary_contrast,
    continuous_contrast,
    outcome_type_for,
    zero_event_policy_of,
)
from app.evidence.resolver import ComparisonRequest, EvidenceSet
from app.evidence.sources.published_nma import ParsedSynthesis
from app.evidence.treatments import canonical_treatment
from app.models.clinical_study import BINARY, CONTINUOUS, ClinicalStudy, OutcomeResult, StudyArm
from app.models.evidence_network import EvidenceNetwork, NetworkMembership
from app.models.nma_result import COMPUTED, EXPLORATORY, GOVERNED, NMAResult
from app.services import evidence_review_service, published_synthesis_service
from app.utils.audit import write_audit

logger = logging.getLogger(__name__)


class ComparisonError(ValueError):
    """A request that cannot be resolved because its scope is incoherent."""


async def _network(db: AsyncSession, network_id: str) -> EvidenceNetwork:
    network = (await db.execute(
        select(EvidenceNetwork).where(EvidenceNetwork.network_id == network_id)
    )).scalar_one_or_none()
    if network is None:
        raise ComparisonError(f"network {network_id!r} does not exist")
    return network


async def membership_filter(db: AsyncSession, network: EvidenceNetwork) -> set[str] | None:
    """The INCLUDED study ids for this network, or ``None`` if membership narrows nothing.

    Membership is scoped to network AND protocol because inclusion is a per-analysis
    judgement — the same verified study can belong in an ACR50 network and be excluded from
    ACR20 at the same instant.

    **An empty INCLUDED set is not an empty corpus.** `network_builder_service` proposes
    every membership as PROPOSED and nothing in the system promotes one yet, so *every*
    built network currently has an empty INCLUDED set. Reading that as "exclude everything"
    would make every resolve return nothing at all. It is read as "nobody has narrowed this
    network by hand, so consider the whole indication" instead.

    Returned as ``None`` rather than an empty set because the two callers of this rule must
    not be able to disagree about which it is: the falsy empty set silently did the right
    thing here and the wrong thing in the curation queue, which reported that verifying
    these studies would change nothing when it is in fact the only thing that would.
    """
    rows = (await db.execute(
        select(NetworkMembership.study_id).where(
            NetworkMembership.network_id == network.network_id,
            NetworkMembership.membership_status == lifecycles.INCLUDED,
        )
    )).scalars().all()
    return set(rows) or None


def outcome_in_scope(
    row: OutcomeResult, network: EvidenceNetwork, window: tuple[float, float] | None
) -> tuple[bool, str | None]:
    """``(in_scope, rejection_reason)`` for one outcome row against the network's scope.

    Public because the curation queue must be able to ask *exactly* this question when it
    reports which studies could contribute. A study with no in-scope row cannot change what
    the network resolves however carefully it is verified, and a queue that cannot tell the
    difference sends a curator to check trials that were never going to count.
    """
    if row.canonical_outcome_id != network.canonical_outcome_id:
        return False, (
            f"measures {row.canonical_outcome_id or row.endpoint!r}, "
            f"not {network.canonical_outcome_id}"
        )
    if (row.treatment_phase or "PRIMARY") != (network.treatment_phase or "PRIMARY"):
        return False, (
            f"reports the {row.treatment_phase} phase, not {network.treatment_phase}"
        )
    if (row.population_stratum or None) != (network.population_stratum or None):
        return False, (
            f"population {row.population_stratum or 'unstated'} does not match "
            f"{network.population_stratum or 'unstated'}"
        )
    if window is not None:
        if row.timepoint_week is None:
            return False, "has no parsed timepoint, so it cannot be placed in the window"
        if not (window[0] <= float(row.timepoint_week) <= window[1]):
            return False, (
                f"reports week {row.timepoint_week:g}, outside the approved window "
                f"[{window[0]:g}, {window[1]:g}]"
            )
    return True, None


def _arm_payload(
    arm: StudyArm, row: OutcomeResult, *, expected_type: str
) -> tuple[BinaryArm | ContinuousArm | None, str | None]:
    """``(arm_payload, rejection_reason)`` for one outcome row.

    *expected_type* is derived from the protocol's effect measure. A row of the other shape
    is **refused with a reason**, never converted and never passed through: handing a binary
    row to the mean-difference engine raises deep inside the arithmetic, which would surface
    as a 500 rather than as the scoping decision it actually is.
    """
    if row.outcome_type != expected_type:
        return None, (
            f"arm {arm.treatment} reports {row.outcome_type} data, but this protocol's "
            f"effect measure requires {expected_type}"
        )

    n = row.sample_size or arm.sample_size
    if expected_type == BINARY:
        if row.events is None or not n:
            return None, f"arm {arm.treatment} has no events/denominator posted"
        return BinaryArm(
            treatment=arm.treatment, events=int(row.events), sample_size=int(n)
        ), None
    if row.mean is None or row.standard_deviation is None or not n:
        return None, f"arm {arm.treatment} has no mean/SD/denominator posted"
    return ContinuousArm(
        treatment=arm.treatment, mean=float(row.mean),
        standard_deviation=float(row.standard_deviation), sample_size=int(n),
    ), None


def _arm_value(payload: BinaryArm | ContinuousArm) -> str:
    """The payload as a reviewer would read it, for a refusal that names the conflict."""
    if isinstance(payload, BinaryArm):
        return f"{payload.events}/{payload.sample_size}"
    return (
        f"mean {payload.mean:g} (SD {payload.standard_deviation:g}, "
        f"n={payload.sample_size})"
    )


def _select_arm_payload(
    arm: StudyArm,
    rows: list[tuple[OutcomeResult, BinaryArm | ContinuousArm]],
    *,
    canonical_outcome_id: str | None,
) -> tuple[BinaryArm | ContinuousArm | None, str | None]:
    """``(payload, refusal)`` for one arm's in-scope rows. Never decided by row order.

    An arm can be described by more than one in-scope row two different ways: the registry
    posts one result twice — once as its own measure and once inside a combined by-visit
    measure — and a widened window admits two timepoints at once. Both arrive here.

    **Agreement collapses, disagreement is refused.** Duplicates carrying identical numbers
    are one fact stated twice, so withholding them would discard correct evidence to solve a
    problem that case does not have. Rows that differ are two faithful readings of two
    analysis populations, and choosing between them is a curation judgement about which
    population the analysis is of — not something a resolver may settle, and *emphatically*
    not something the order rows come back from the database may settle.

    The refusal names every candidate value so the choice can be made where it belongs.
    """
    distinct: list[tuple[OutcomeResult, BinaryArm | ContinuousArm]] = []
    for row, payload in rows:
        if all(payload != seen for _row, seen in distinct):
            distinct.append((row, payload))
    if not distinct:
        return None, None
    if len(distinct) == 1:
        return distinct[0][1], None

    # Sorted so the same conflict always reads the same way, whatever order the rows arrived
    # in. A message that varied by row order would reintroduce the defect in the reporting.
    described = "; ".join(
        f"week {row.timepoint_week:g}: {_arm_value(payload)}"
        if row.timepoint_week is not None
        else _arm_value(payload)
        for row, payload in sorted(
            distinct,
            key=lambda pair: (pair[0].timepoint_week or 0.0, _arm_value(pair[1])),
        )
    )
    return None, (
        f"arm {arm.treatment} has {len(distinct)} contradictory in-scope results for "
        f"{canonical_outcome_id or 'this outcome'} ({described}); choosing between analysis "
        "populations is a curation decision, so the arm contributed no value"
    )


def _contrast_for(
    study_id: str, treatment_arm, comparator_arm, *, measure: str, zero_event_policy: str
):
    if isinstance(treatment_arm, BinaryArm):
        return binary_contrast(
            study_id, treatment_arm, comparator_arm,
            measure=measure, zero_event_policy=zero_event_policy,
        )
    return continuous_contrast(study_id, treatment_arm, comparator_arm, measure=measure)


async def gather_evidence(
    db: AsyncSession,
    network: EvidenceNetwork,
    request: ComparisonRequest,
    *,
    require_verified: bool = True,
) -> tuple[EvidenceSet, dict]:
    """Assemble everything the resolver may consider, plus a scoping report.

    The report is returned rather than logged because *what was excluded and why* is part
    of the answer's provenance, not diagnostic noise.
    """
    protocol_id = network.protocol_id
    window = protocols.approved_time_window(protocol_id)
    measure = (protocols.protocol(protocol_id) or {}).get("effect_measure") or "risk_ratio"
    zero_policy = zero_event_policy_of(protocol_id)
    # The protocol decides the arm-data shape, not the stored rows. Raises on an
    # unrecognised measure, which is a misconfigured protocol and should stop the run.
    outcome_type = outcome_type_for(measure)

    included = await membership_filter(db, network)
    wanted = set(request.nodes)

    studies = list((await db.execute(
        select(ClinicalStudy).where(ClinicalStudy.indication == network.indication)
    )).scalars().all())

    contrasts: list[StudyContrast] = []
    study_arms: dict[str, frozenset[str]] = {}
    sidecar_studies: dict[str, list[netmeta.ArmPayload]] = {}
    routes: dict[str, str] = {}
    unsuitable_direct: list[tuple[str, str]] = []
    insufficient: list[tuple[str, str]] = []
    ambiguous: list[tuple[str, str]] = []
    shared_nodes: list[tuple[str, str]] = []
    skipped: list[dict] = []
    # {treatment: [(study_id, reason)]} for a REQUESTED treatment a trial randomises whose
    # every row this scope rejected. Not derivable from `unsuitable_direct`, which is only
    # filled for studies holding both requested treatments — so for a pair never randomised
    # together (Rinvoq vs Tremfya) the reason would otherwise be lost entirely.
    excluded_nodes: dict[str, list[tuple[str, str]]] = {}

    for study in studies:
        arm_treatments = {a.treatment for a in study.arms}
        has_both = wanted <= arm_treatments

        if included is not None and study.study_id not in included:
            skipped.append({"study_id": study.study_id, "reason": "not INCLUDED in this network"})
            if has_both:
                unsuitable_direct.append(
                    (study.study_id, "is not an included member of this network")
                )
            continue
        if require_verified and study.verification_status != lifecycles.VERIFIED:
            skipped.append({
                "study_id": study.study_id,
                "reason": f"verification_status is {study.verification_status}",
            })
            if has_both:
                unsuitable_direct.append(
                    (study.study_id,
                     f"has not been verified (status {study.verification_status})")
                )
            continue

        by_arm = {a.arm_id: a for a in study.arms}
        # Collected per ARM, then reduced to one value each. Assigning straight into a
        # treatment-keyed dict here is what let a second in-scope row for the same arm
        # overwrite the first, making the number a function of row order and recording
        # nowhere that a choice had been made.
        per_arm: dict[str, list[tuple[OutcomeResult, BinaryArm | ContinuousArm]]] = {}
        rejections: set[str] = set()
        # The same rejections, attributed to the treatment whose arm the row described, and
        # narrowed to rows that ARE this endpoint. The study-wide set above cannot answer
        # *"why is THIS node absent"*, and a disconnected-network refusal has to answer that
        # without guessing which arm to blame.
        #
        # A row measuring something else entirely is dropped rather than reported: every
        # trial carries dozens of secondary endpoints, so "measures HAQ-DI, not ACR50" is
        # true of almost every row in the corpus and says nothing about why this drug is
        # missing. What remains is the informative case — the endpoint IS here and the
        # protocol's window, phase or stratum refused it, which is a decision somebody made
        # rather than data nobody collected.
        by_treatment: dict[str, set[str]] = {}

        for row in study.outcomes:
            if row.arm_id is None or row.arm_id not in by_arm:
                continue
            treatment = by_arm[row.arm_id].treatment
            is_this_endpoint = row.canonical_outcome_id == network.canonical_outcome_id
            in_scope, reason = outcome_in_scope(row, network, window)
            if not in_scope:
                if reason:
                    rejections.add(reason)
                    if is_this_endpoint:
                        by_treatment.setdefault(treatment, set()).add(reason)
                continue
            arm = by_arm[row.arm_id]
            payload, payload_reason = _arm_payload(arm, row, expected_type=outcome_type)
            if payload is None:
                if payload_reason:
                    rejections.add(payload_reason)
                    by_treatment.setdefault(treatment, set()).add(payload_reason)
                continue
            per_arm.setdefault(row.arm_id, []).append((row, payload))

        usable: dict[str, object] = {}
        claimed: dict[str, StudyArm] = {}
        # Sorted by arm id so what this study contributes never depends on the order the
        # rows came back from the database.
        for arm_id, rows in sorted(per_arm.items()):
            arm = by_arm[arm_id]
            payload, conflict = _select_arm_payload(
                arm, rows, canonical_outcome_id=network.canonical_outcome_id
            )
            if payload is None:
                if conflict:
                    rejections.add(conflict)
                    ambiguous.append((study.study_id, conflict))
                continue
            # Two arms of one study can resolve to one node: SELECT-PsA 1's 15 mg and 30 mg
            # upadacitinib arms are both `Rinvoq`, because dose is stripped from node names
            # by design. Every protocol declares `dose_policy: SEPARATE_BY_APPROVED_DOSE`
            # and nothing reads that field, so one arm's numbers stand in for the treatment.
            # **Reported, not resolved** — separating doses renames nodes in every stored
            # network and pooling them contradicts the approved protocol, so the choice is
            # not this function's to make. It is only made visible.
            if arm.treatment in claimed:
                previous = claimed[arm.treatment]
                shared_nodes.append((study.study_id, (
                    f"arms {previous.label or previous.arm_id!r} and "
                    f"{arm.label or arm.arm_id!r} both resolve to {arm.treatment}; only the "
                    "latter contributes a value, because dose_policy is not applied here"
                )))
            claimed[arm.treatment] = arm
            usable[arm.treatment] = payload
            if arm.administration_route:
                routes[arm.treatment] = arm.administration_route

        # Collected before the sufficiency check below, because both of its outcomes can
        # strand a node: a study that contributes nothing obviously can, and so can one that
        # contributes two arms while a third, requested treatment had every row refused.
        for stranded in sorted((wanted & arm_treatments) - set(usable)):
            for reason in sorted(by_treatment.get(stranded, ())):
                excluded_nodes.setdefault(stranded, []).append((study.study_id, reason))

        if len(usable) < 2:
            if has_both and rejections:
                unsuitable_direct.extend((study.study_id, r) for r in sorted(rejections))
            elif has_both:
                insufficient.append(
                    (study.study_id, "fewer than two arms report this outcome in scope")
                )
            elif rejections:
                skipped.append({
                    "study_id": study.study_id, "reason": "; ".join(sorted(rejections))
                })
            continue

        study_arms[study.study_id] = frozenset(usable)
        sidecar_studies[study.study_id] = [
            netmeta.ArmPayload(
                treatment=t,
                sample_size=getattr(p, "sample_size", None),
                events=getattr(p, "events", None),
                mean=getattr(p, "mean", None),
                standard_deviation=getattr(p, "standard_deviation", None),
                administration_route=routes.get(t),
            )
            for t, p in sorted(usable.items())
        ]

        # Every pair within the study. Multi-arm correlation is preserved for the sidecar
        # via `sidecar_studies`; Bucher's own within-study handling is the reason the
        # protocol routes multi-arm networks to netmeta instead.
        names = sorted(usable)
        for i, first in enumerate(names):
            for second in names[i + 1:]:
                contrast, reason = _contrast_for(
                    study.study_id, usable[first], usable[second],
                    measure=measure, zero_event_policy=zero_policy,
                )
                if contrast is not None:
                    contrasts.append(contrast)
                elif reason:
                    if {first, second} == wanted:
                        insufficient.append((study.study_id, reason))
                    else:
                        skipped.append({
                            "study_id": study.study_id,
                            "reason": f"{first} vs {second}: {reason}",
                        })

    # A treatment that reached the graph from some other study was not excluded from it, so
    # whatever a given trial refused about it is not the reason it is missing — it is not
    # missing. Keeping those entries would have the field assert an exclusion the network
    # itself contradicts, which is the failure it exists to prevent.
    in_network = {t for arms in study_arms.values() for t in arms}
    excluded_nodes = {t: rows for t, rows in excluded_nodes.items() if t not in in_network}

    syntheses = await _published_syntheses(db, network)

    evidence = EvidenceSet(
        contrasts=tuple(contrasts),
        study_arms=study_arms,
        syntheses=syntheses,
        administration_routes=routes,
        unsuitable_direct=tuple(dict.fromkeys(unsuitable_direct)),
        insufficient_data=tuple(dict.fromkeys(insufficient)),
        ambiguous_arms=tuple(dict.fromkeys(ambiguous)),
        excluded_nodes={
            t: tuple(dict.fromkeys(rows)) for t, rows in sorted(excluded_nodes.items())
        },
    )
    report = {
        "effect_measure": measure,
        "outcome_type": outcome_type,
        "approved_time_window": list(window) if window else None,
        "zero_event_policy": zero_policy,
        "included_study_count": len(study_arms),
        "candidate_study_count": len(studies),
        "published_synthesis_count": len(syntheses),
        # A first-class figure rather than a line buried in `skipped`: an arm withheld for
        # contradicting itself is a defect in the store, and a count of them is the only way
        # anyone would notice the corpus had acquired more.
        "ambiguous_arms": [{"study_id": s, "reason": r} for s, r in dict.fromkeys(ambiguous)],
        # Disclosure only — see the note at the assignment. Left in the report rather than
        # in the EvidenceSet because it changes no answer; it names one the reader should
        # not yet trust to be about the dose they think it is.
        "arms_sharing_a_node": [
            {"study_id": s, "reason": r} for s, r in dict.fromkeys(shared_nodes)
        ],
        # Machine-readable alongside the prose the resolver builds from it, so a caller can
        # tell "the protocol excluded this trial" from "no such trial exists" without parsing
        # a sentence. Empty for a treatment nobody studied, which is the honest difference.
        "excluded_nodes": {
            t: [{"study_id": s, "reason": r} for s, r in dict.fromkeys(rows)]
            for t, rows in sorted(excluded_nodes.items())
        },
        "skipped": skipped,
        "sidecar_studies": sidecar_studies,
    }
    return evidence, report


async def _published_syntheses(
    db: AsyncSession, network: EvidenceNetwork
) -> tuple[ParsedSynthesis, ...]:
    """Stored published syntheses for this indication, read back through the adapter."""
    rows = await published_synthesis_service.list_syntheses(
        db, indication=network.indication, limit=500
    )
    source_types = await published_synthesis_service._source_types_for(db, rows)
    return tuple(
        published_synthesis_service._parsed_from_row(
            row, source_type=source_types.get(row.source_payload_id or "", "MANUAL_UPLOAD")
        )
        for row in rows
    )


async def _netmeta_response(
    request_payload: netmeta.NetmetaRequest, *, independent_loop_count: int
) -> netmeta.NetmetaResponse:
    """Consult the sidecar, or report plainly that it is not deployed."""
    settings = get_settings()
    if not settings.nma_sidecar_url:
        logger.info(
            "netmeta required but nma_sidecar_url is blank; returning %s",
            statuses.NMA_SERVICE_UNAVAILABLE,
        )
        return netmeta.NetmetaResponse.unavailable(
            "the NMA sidecar is not configured (nma_sidecar_url is blank)"
        )
    return await netmeta.run(
        request_payload,
        base_url=settings.nma_sidecar_url,
        independent_loop_count=independent_loop_count,
        timeout=settings.nma_sidecar_timeout_seconds,
    )


async def resolve_comparison(
    db: AsyncSession,
    *,
    network_id: str,
    treatment_a: str,
    treatment_b: str,
    execution_mode: str = EXPLORATORY,
    requested_dose: str | None = None,
    max_published_age_years: int | None = None,
    persist: bool = False,
    as_of: date | None = None,
) -> dict:
    """Resolve *treatment_a* vs *treatment_b* against a network. Always returns a status.

    When *execution_mode* is GOVERNED the governance gate decides whether that is permitted.
    A refusal **downgrades to EXPLORATORY** and records the blocking status, because the
    honest report is "computed, but not releasable, and here is what is missing" rather than
    an error that hides the fact a number was obtainable.
    """
    network = await _network(db, network_id)
    definition = protocols.protocol(network.protocol_id) or {}

    request = ComparisonRequest(
        indication=network.indication,
        treatment_a=treatment_a,
        treatment_b=treatment_b,
        canonical_outcome_id=network.canonical_outcome_id,
        population_stratum=network.population_stratum,
        treatment_phase=network.treatment_phase,
        protocol_id=network.protocol_id,
        requested_dose=requested_dose,
        as_of=as_of,
    )

    gate: dict | None = None
    may_compute_governed = False
    if execution_mode == GOVERNED:
        gate = await evidence_review_service.governance_gate(db, network_id=network_id)
        may_compute_governed = bool(gate["may_compute_governed"])

    evidence, report = await gather_evidence(db, network, request)
    graph = evidence.topology()

    # The sidecar is consulted only when the protocol's rule actually selects it, so a
    # missing sidecar never degrades a comparison that Bucher could have answered.
    response: netmeta.NetmetaResponse | None = None
    engine = netmeta.select_engine(definition.get("model_selection_rule"), graph)
    if engine == netmeta.ENGINE and report["sidecar_studies"]:
        response = await _netmeta_response(
            netmeta.build_request(
                report["sidecar_studies"],
                outcome_type=report["outcome_type"],
                effect_measure=report["effect_measure"],
                model="random" if definition.get("heterogeneity_rule") == "RANDOM_EFFECTS_ALWAYS"
                else "fixed",
                reference_treatment=_reference_of(graph),
                zero_event_policy=report["zero_event_policy"],
                inconsistency_rule=definition.get("inconsistency_rule"),
                protocol_id=network.protocol_id,
                protocol_hash=protocols.content_hash(network.protocol_id),
            ),
            independent_loop_count=graph.independent_loop_count,
        )

    answer = resolver.resolve(
        evidence, request,
        model_selection_rule=definition.get("model_selection_rule"),
        heterogeneity_rule=definition.get("heterogeneity_rule"),
        effect_measure=report["effect_measure"],
        may_compute_governed=may_compute_governed,
        netmeta_response=response,
        max_published_age_years=max_published_age_years,
        # Passed, not just recorded. Three of the four protocols select
        # SENSITIVITY_REQUIRED, and until this argument existed the policy was written
        # onto every stored result while nothing ran a second analysis — a result
        # asserting a disclosure that had not happened.
        placebo_response_policy=definition.get("placebo_response_policy"),
    )

    payload = answer.as_dict()
    payload["network"] = {
        "network_id": network.network_id,
        "indication": network.indication,
        "canonical_outcome_id": network.canonical_outcome_id,
        "population_stratum": network.population_stratum,
        "treatment_phase": network.treatment_phase,
        "protocol_id": network.protocol_id,
        "ratification_status": network.ratification_status,
        "version": network.version,
    }
    payload["topology"] = graph.summary()
    payload["scoping"] = {k: v for k, v in report.items() if k != "sidecar_studies"}
    payload["execution_mode"] = GOVERNED if may_compute_governed else EXPLORATORY
    payload["requested_execution_mode"] = execution_mode
    payload["governance"] = gate
    # Recorded on the RESULT, not just the network, so the transitivity threat travels with
    # the number wherever it is quoted.
    payload["administration_routes"] = dict(sorted(evidence.administration_routes.items()))
    payload["is_route_mixed"] = len(set(evidence.administration_routes.values())) > 1

    if persist and answer.evidence_level == resolver.LEVEL_COMPUTED and answer.is_success:
        payload["result_id"] = await _persist(db, network, answer, payload, definition)

    return payload


def _reference_of(graph) -> str:
    """The reference node for a league table: placebo when present, else the hub.

    A reference is only a parameterisation — every contrast is recoverable from any of
    them — but choosing placebo keeps the printed table readable to a clinician.
    """
    for node in graph.nodes:
        if canonical_treatment(node)[1]:
            return node
    if not graph.nodes:
        return "Placebo"
    return max(graph.nodes, key=lambda n: len(graph.neighbours(n)))


async def _persist(
    db: AsyncSession,
    network: EvidenceNetwork,
    answer: resolver.ComparisonAnswer,
    payload: dict,
    definition: dict,
) -> str:
    """Store a computed result with the provenance that makes it reviewable.

    ``protocol_hash`` is derived, never accepted, so a stored row always names the exact
    methodology it ran under. Editing the protocol later changes the hash and the row
    stops matching — which is how a result produced under retired rules stays identifiable.
    """
    result = NMAResult(
        result_id=f"CMP-{uuid4().hex}",
        source=COMPUTED,
        indication=network.indication,
        canonical_outcome_id=network.canonical_outcome_id,
        endpoint=definition.get("canonical_outcome_id"),
        timepoint_week=None,
        population_stratum=network.population_stratum,
        treatment_phase=network.treatment_phase,
        network_id=network.network_id,
        network_version=network.version,
        engine=answer.engine,
        engine_version=(
            netmeta.ENGINE_VERSION if answer.engine == netmeta.ENGINE else "1.0.0"
        ),
        package_version=payload.get("package_version"),
        protocol_id=network.protocol_id,
        protocol_hash=protocols.content_hash(network.protocol_id),
        execution_mode=payload["execution_mode"],
        status=answer.status,
        model_type=answer.model,
        effect_measure=answer.effect_measure,
        estimates=json.dumps([{
            "treatment": answer.treatment,
            "comparator": answer.comparator,
            "estimate": answer.estimate,
            "ci_lower": answer.ci_lower,
            "ci_upper": answer.ci_upper,
            "interval_type": answer.interval_type,
            "anchor": answer.anchor,
            "flags": list(answer.flags),
        }]),
        heterogeneity_note=json.dumps(answer.heterogeneity) if answer.heterogeneity else None,
        administration_routes=(
            json.dumps(payload["administration_routes"])
            if payload.get("administration_routes") else None
        ),
        is_route_mixed=bool(payload["is_route_mixed"]),
        placebo_response_policy=definition.get("placebo_response_policy"),
        # The columns have existed since Phase 2 and were never populated. A policy
        # recorded beside an empty analysis reads as though the analysis was done.
        sensitivity_analysis=(
            json.dumps(answer.sensitivity.as_dict()) if answer.sensitivity else None
        ),
        sensitivity_divergence_note=(
            answer.sensitivity.reason if answer.sensitivity else None
        ),
        # A computed result is internal analytical output. Never citable, and never
        # approved for external use by the act of computing it.
        source_is_citable=False,
        claim_is_approved_for_external_use=False,
    )
    db.add(result)

    await write_audit(
        db, role="SYSTEM", event="COMPARISON_RESOLVED",
        context={
            "result_id": result.result_id,
            "network_id": network.network_id,
            "treatment": answer.treatment,
            "comparator": answer.comparator,
            "status": answer.status,
            "evidence_level": answer.evidence_level,
            "engine": answer.engine,
            "execution_mode": payload["execution_mode"],
            "protocol_id": network.protocol_id,
            "protocol_hash": result.protocol_hash,
            "flags": list(answer.flags),
        },
        commit=False,
    )
    await db.commit()
    return result.result_id


async def resolve_all_pairs(
    db: AsyncSession, *, network_id: str, execution_mode: str = EXPLORATORY
) -> dict:
    """Every pair in the network's own node set, resolved.

    Useful for a coverage view: which comparisons this network can actually support, and
    which are gaps with named reasons.
    """
    network = await _network(db, network_id)
    probe = ComparisonRequest(
        indication=network.indication, treatment_a="", treatment_b="",
        canonical_outcome_id=network.canonical_outcome_id,
        population_stratum=network.population_stratum,
        treatment_phase=network.treatment_phase,
        protocol_id=network.protocol_id,
    )
    evidence, _ = await gather_evidence(db, network, probe)
    nodes = evidence.topology().nodes

    answers = []
    for i, first in enumerate(nodes):
        for second in nodes[i + 1:]:
            answers.append(await resolve_comparison(
                db, network_id=network_id, treatment_a=first, treatment_b=second,
                execution_mode=execution_mode,
            ))
    return {
        "network_id": network_id,
        "node_count": len(nodes),
        "pair_count": len(answers),
        "releasable_count": sum(1 for a in answers if a["is_releasable"]),
        "gap_count": sum(1 for a in answers if statuses.is_gap(a["status"])),
        "comparisons": answers,
    }
