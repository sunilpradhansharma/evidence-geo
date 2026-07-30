"""Assemble an ``EvidenceNetwork`` from ingested studies (Phase 3A / Phase 6 bridge).

``EvidenceNetwork`` has carried nullable topology columns since Phase 2 with nothing to
populate them. This is what populates them, and it is the step that turns a pile of
ingested trials into the analysable question the resolver expects.

**The pipeline proposes; a human ratifies.** Memberships are created ``PROPOSED``, never
``INCLUDED``, and the network is created ``DRAFT``, never ``RATIFIED``. Inclusion is a
per-analysis clinical judgement — the same verified study belongs in an ACR50 network and
may not belong in ACR20 — so a builder that decided it would be inventing the review it
exists to prepare for.

**Scope is identity, not filter.** Indication, canonical outcome, treatment phase and
population stratum are all part of what makes this network *this* network. Two of them
differing means two networks, which is why the id is derived from all four.

Topology comes from ``evidence.topology``, the same module the Phase 0 audit and the
Phase 6 engine selector use. A second implementation would eventually disagree with the
audit about whether a network has a closed loop, which would mean promising a comparison
the resolver then refuses to compute.

**Two topologies, one of them narrower.** The network this builder assembles is
endpoint-level: every study reporting the canonical outcome anywhere in that outcome's own
allowed window. A resolve runs under an *approved protocol*, whose window may be narrower,
so a node can be in the network and still be unanswerable. The first live PsA run reported
8 connected nodes including Rinvoq while every protocol-scoped resolve saw 6 without it.
That is the same broken promise a second topology implementation would produce, reached
through the time window instead of through graph code, so ``BuildReport`` now carries a
``ProtocolScope`` alongside its topology. The fix is **disclosure**: the window stays the
protocol's judgement, asked of ``protocols.in_approved_window`` and never restated here.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.evidence import lifecycles, protocols, topology
# Aliased because `treatments` is a local variable throughout this module.
from app.evidence import treatments as treatment_labels
from app.models.clinical_study import ClinicalStudy
from app.models.evidence_network import EvidenceNetwork, NetworkMembership
from app.utils.audit import write_audit

logger = logging.getLogger(__name__)


class NetworkBuildError(ValueError):
    """A network that cannot be assembled as specified."""


@dataclass(frozen=True)
class ProtocolScope:
    """The assembled network re-read through the governing protocol's approved window.

    Reported, never enforced. Memberships and the stored topology stay endpoint-level,
    because the window belongs to a protocol that can be re-approved without re-harvesting
    anything — and because applying it here would put one judgement in two places that can
    disagree, which is exactly the defect this disclosure exists to reveal.

    ``nodes_lost`` is the figure that matters: treatments the network contains and this
    protocol cannot answer on.
    """

    protocol_id: str
    approved_time_window: tuple[float, float]
    topology_summary: dict
    nodes_lost: tuple[str, ...]
    studies_out_of_window: tuple[str, ...]

    @property
    def narrows(self) -> bool:
        """True when the protocol's window costs the network a node or a study."""
        return bool(self.nodes_lost or self.studies_out_of_window)

    def as_dict(self) -> dict:
        return {
            "protocol_id": self.protocol_id,
            "approved_time_window": list(self.approved_time_window),
            "topology": self.topology_summary,
            "nodes_lost_to_window": list(self.nodes_lost),
            "studies_out_of_window": list(self.studies_out_of_window),
            "narrows_the_network": self.narrows,
        }


@dataclass
class BuildReport:
    """What the builder assembled, what it left out, and what a protocol would narrow."""

    network_id: str
    indication: str
    canonical_outcome_id: str
    treatment_phase: str
    population_stratum: str | None
    protocol_id: str | None
    created: bool
    proposed_studies: list[str] = field(default_factory=list)
    excluded: list[tuple[str, str]] = field(default_factory=list)
    # Endpoint-level: the whole of the canonical outcome's allowed window, pre-protocol.
    topology_summary: dict = field(default_factory=dict)
    # ``None`` when no protocol governs the network, which is not the same as "nothing is
    # narrowed" being unknown — with no approved window there is nothing to narrow.
    protocol_scope: ProtocolScope | None = None

    @property
    def overstates_answerable(self) -> bool:
        """True when the topology above promises more than a protocol-scoped resolve gives."""
        return bool(self.protocol_scope and self.protocol_scope.narrows)

    def as_dict(self) -> dict:
        return {
            "network_id": self.network_id,
            "indication": self.indication,
            "canonical_outcome_id": self.canonical_outcome_id,
            "treatment_phase": self.treatment_phase,
            "population_stratum": self.population_stratum,
            "protocol_id": self.protocol_id,
            "created": self.created,
            "proposed_study_count": len(self.proposed_studies),
            "proposed_studies": list(self.proposed_studies),
            "excluded": [{"study_id": s, "reason": r} for s, r in self.excluded],
            "endpoint_topology": self.topology_summary,
            "protocol_scope": self.protocol_scope.as_dict() if self.protocol_scope else None,
            "overstates_answerable": self.overstates_answerable,
        }


def network_id_for(
    *,
    indication: str,
    canonical_outcome_id: str,
    treatment_phase: str = "PRIMARY",
    population_stratum: str | None = None,
) -> str:
    """A deterministic id derived from the full scope.

    Derived rather than random so re-running the builder updates one network instead of
    accumulating near-duplicates, and so two people scoping the same question independently
    arrive at the same id.
    """
    slug = re.sub(r"[^A-Z0-9]+", "-", indication.upper()).strip("-")
    parts = ["NET", slug, canonical_outcome_id.upper()]
    if (treatment_phase or "PRIMARY") != "PRIMARY":
        parts.append(treatment_phase.upper())
    if population_stratum:
        parts.append(population_stratum.upper())
    return "-".join(parts)


def _eligible(
    study: ClinicalStudy,
    *,
    canonical_outcome_id: str,
    treatment_phase: str,
    population_stratum: str | None,
) -> tuple[bool, str | None]:
    """``(eligible, reason)`` for proposing *study* to this network.

    Only structural facts are checked. Clinical suitability is the reviewer's question, and
    an approved protocol's time window is the resolver's — duplicating either here would put
    the same judgement in two places that can disagree.
    """
    if not study.is_randomised:
        return False, "not randomised, so it contributes no randomised comparison"
    if study.verification_status == lifecycles.REJECTED:
        return False, "extraction was rejected"

    rows = [o for o in study.outcomes if o.canonical_outcome_id == canonical_outcome_id]
    if not rows:
        return False, f"reports no results for {canonical_outcome_id}"

    in_phase = [r for r in rows if (r.treatment_phase or "PRIMARY") == treatment_phase]
    if not in_phase:
        found = sorted({(r.treatment_phase or "PRIMARY") for r in rows})
        return False, (
            f"reports {canonical_outcome_id} for the {', '.join(found)} phase, "
            f"not {treatment_phase}"
        )

    wanted = population_stratum or None
    in_stratum = [r for r in in_phase if (r.population_stratum or None) == wanted]
    if not in_stratum:
        return False, (
            f"population stratum does not match {population_stratum or 'unstated'}"
        )

    arms = {o.arm_id for o in in_stratum if o.arm_id}
    if len(arms) < 2:
        return False, (
            "fewer than two arms report this outcome, so it yields no comparison"
        )
    return True, None


def _reporting_arms(
    study: ClinicalStudy, *, canonical_outcome_id: str, treatment_phase: str,
    population_stratum: str | None,
) -> list[tuple[str, float | None]]:
    """``(treatment, timepoint_week)`` for every arm that reports this outcome in scope.

    Not every randomised arm: an arm with no result for the endpoint is not a node of THIS
    network, and including it would draw an edge no data supports.

    The week travels with the treatment so a caller can ask a *protocol* whether the row is
    inside its approved window. No comparison against a window happens here — this only
    reports the timepoint the study posted.
    """
    wanted = population_stratum or None
    by_arm = {a.arm_id: a for a in study.arms}
    reporting: list[tuple[str, float | None]] = []
    for row in study.outcomes:
        if row.canonical_outcome_id != canonical_outcome_id:
            continue
        if (row.treatment_phase or "PRIMARY") != treatment_phase:
            continue
        if (row.population_stratum or None) != wanted:
            continue
        arm = by_arm.get(row.arm_id or "")
        if arm is not None and arm.treatment:
            reporting.append((arm.treatment, row.timepoint_week))
    return reporting


@dataclass
class _Screened:
    """One pass over an indication's studies, shared by the builder and read-only callers.

    Extracted so the protocol-scope disclosure can be recomputed for an existing network
    without a second copy of the eligibility screen. Two copies would drift, and a read
    surface that screened studies differently from the builder would report a scope for a
    network nobody assembled.
    """

    study_arms: dict[str, list[str]] = field(default_factory=dict)
    reporting_arms: dict[str, list[tuple[str, float | None]]] = field(default_factory=dict)
    routes: dict[str, str] = field(default_factory=dict)
    excluded: list[tuple[str, str]] = field(default_factory=list)


def _screen(
    studies: Sequence[ClinicalStudy],
    *,
    canonical_outcome_id: str,
    treatment_phase: str,
    population_stratum: str | None,
) -> _Screened:
    """Which studies can be nodes of this network, and what they contribute.

    Pure and synchronous: every judgement here is structural, so it needs no session and
    can be re-run on read.
    """
    screened = _Screened()
    for study in studies:
        eligible, reason = _eligible(
            study,
            canonical_outcome_id=canonical_outcome_id,
            treatment_phase=treatment_phase,
            population_stratum=population_stratum,
        )
        if not eligible:
            screened.excluded.append((study.study_id, reason or "not eligible"))
            continue

        reporting = _reporting_arms(
            study,
            canonical_outcome_id=canonical_outcome_id,
            treatment_phase=treatment_phase,
            population_stratum=population_stratum,
        )
        treatments = [treatment for treatment, _week in reporting]
        # A node is identified by its label, so an arm the registry called "A" would pool
        # with every other study's "A" into one node. Two unrelated trials then share a
        # comparator that does not exist, and the fabricated edge closes a loop the evidence
        # never contained. This is the "Standard Care" problem with nothing left to inspect:
        # the study is kept on record, but it cannot join a network until curation reads the
        # source and says what the arm received.
        unidentifiable = sorted(
            {t for t in treatments if treatment_labels.is_uninformative_label(t)}
        )
        if unidentifiable:
            screened.excluded.append((
                study.study_id,
                "an arm label names no treatment "
                f"({', '.join(unidentifiable[:3])}), so it cannot be a node",
            ))
            continue

        # Ingestion screens these too, but the builder cannot rely on that: it reads every
        # study for the indication, including ones ingested BEFORE the screen existed. The
        # four PsA strategy trials are exactly that case — already in the table, and
        # "Standard Care" would otherwise pool with every other trial's "Standard Care" on
        # nothing but a shared label.
        class_level = sorted({t for t in treatments if treatment_labels.is_class_level_node(t)})
        if class_level:
            screened.excluded.append((
                study.study_id,
                f"a class-level or strategy arm ({', '.join(class_level[:3])}) cannot be a "
                "molecule node without assuming class equivalence",
            ))
            continue

        if len(set(treatments)) < 2:
            screened.excluded.append((
                study.study_id,
                "its reporting arms collapse to a single treatment, so there is no contrast",
            ))
            continue

        screened.study_arms[study.study_id] = treatments
        screened.reporting_arms[study.study_id] = reporting
        for arm in study.arms:
            if arm.treatment and arm.administration_route:
                screened.routes[arm.treatment] = arm.administration_route
    return screened


def _protocol_scope(
    *,
    protocol_id: str | None,
    endpoint_topology: topology.Topology,
    reporting: dict[str, list[tuple[str, float | None]]],
) -> ProtocolScope | None:
    """The proposed evidence re-read through *protocol_id*'s approved time window.

    ``None`` when no protocol governs the network, or when the one recorded on it is no
    longer defined: in both cases the resolver applies no window either, so the
    endpoint-level topology above is already what it will see. Fabricating an empty scoped
    graph would report that nothing is answerable.

    **The window judgement is asked, not repeated.** ``protocols.in_approved_window`` is the
    one implementation of it — the same one extraction screening and the resolver's scoping
    consult — and a second copy here is precisely how the builder and the resolver would
    come to disagree about what a week means.
    """
    window = protocols.approved_time_window(protocol_id)
    if not protocol_id or window is None:
        return None

    scoped = {
        study_id: [
            treatment for treatment, week in arms
            if protocols.in_approved_window(protocol_id, week)
        ]
        for study_id, arms in reporting.items()
    }
    # `topology.build` drops any study left with fewer than two distinct treatments, so a
    # study whose in-window rows no longer contrast anything falls out here rather than
    # contributing a node on its own.
    graph = topology.build(scoped)
    surviving = set(graph.nodes)
    return ProtocolScope(
        protocol_id=protocol_id,
        approved_time_window=window,
        topology_summary=graph.summary(),
        nodes_lost=tuple(n for n in endpoint_topology.nodes if n not in surviving),
        studies_out_of_window=tuple(sorted(set(reporting) - set(graph.study_arms))),
    )


async def build_network(
    db: AsyncSession,
    *,
    indication: str,
    canonical_outcome_id: str,
    treatment_phase: str = "PRIMARY",
    population_stratum: str | None = None,
    protocol_id: str | None = None,
    label: str | None = None,
    commit: bool = True,
    progress: dict | None = None,
) -> BuildReport:
    """Create or refresh one network from the studies already ingested.

    Refreshing recomputes topology and proposes newly eligible studies. It **never
    downgrades an existing membership decision**: a study a reviewer marked INCLUDED or
    EXCLUDED keeps that status, because the builder re-running is not new information about
    a judgement someone already made.

    ``commit=False`` lets a caller inspect the topology it would build and then roll back.
    The report is fully populated either way, so a dry run shows the real graph.

    The report carries **two** topologies: the endpoint-level one it assembles and stores,
    and ``protocol_scope`` — the same evidence re-read through the governing protocol's
    approved time window. The window is never applied to memberships or to the stored
    topology; it is disclosed so a run cannot announce a node the resolver will refuse.

    ``progress``, when given, is mutated in place — the same contract ``ingest_indication``
    uses, so a background caller can say *building* rather than leaving a bar that has
    finished fetching sitting at 100% through the graph assembly.
    """
    if progress is not None:
        progress.update(phase="building", network_id=None, node_count=0)
    if protocol_id and not protocols.is_defined(protocol_id):
        raise NetworkBuildError(
            f"protocol {protocol_id!r} is not defined in analysis_protocols.yaml"
        )

    network_id = network_id_for(
        indication=indication,
        canonical_outcome_id=canonical_outcome_id,
        treatment_phase=treatment_phase,
        population_stratum=population_stratum,
    )

    studies = list((await db.execute(
        select(ClinicalStudy).where(ClinicalStudy.indication == indication)
    )).scalars().all())

    report = BuildReport(
        network_id=network_id,
        indication=indication,
        canonical_outcome_id=canonical_outcome_id,
        treatment_phase=treatment_phase,
        population_stratum=population_stratum,
        protocol_id=protocol_id,
        created=False,
    )

    screened = _screen(
        studies,
        canonical_outcome_id=canonical_outcome_id,
        treatment_phase=treatment_phase,
        population_stratum=population_stratum,
    )
    report.excluded.extend(screened.excluded)
    report.proposed_studies.extend(screened.study_arms)
    routes = screened.routes

    graph = topology.build(screened.study_arms)
    report.topology_summary = graph.summary()
    if progress is not None:
        progress.update(
            network_id=network_id, node_count=len(graph.nodes),
            proposed_studies=len(report.proposed_studies),
            excluded_studies=len(report.excluded),
        )

    network = (await db.execute(
        select(EvidenceNetwork).where(EvidenceNetwork.network_id == network_id)
    )).scalar_one_or_none()

    if network is None:
        network = EvidenceNetwork(
            network_id=network_id,
            indication=indication,
            canonical_outcome_id=canonical_outcome_id,
            treatment_phase=treatment_phase,
            population_stratum=population_stratum,
            ratification_status=lifecycles.DRAFT,
        )
        db.add(network)
        report.created = True
    elif lifecycles.is_frozen_for_edit(network.ratification_status):
        # Silently mutating the evidence set under a review — concluded OR in progress —
        # invalidates that review while leaving the network looking reviewed. This used to
        # refuse only on RATIFIED, which let a rebuild rewrite the graph under a reviewer
        # who was mid-way through reading it.
        status = network.ratification_status
        raise NetworkBuildError(
            f"network {network_id!r} is {status}; rebuilding it would change the evidence "
            f"set — {lifecycles.frozen_explanation(status)}. Reopen it to DRAFT, recording "
            "why, before rebuilding"
        )

    network.label = label or network.label or (
        f"{indication} — {canonical_outcome_id} ({treatment_phase})"
    )
    network.protocol_id = protocol_id or network.protocol_id
    network.treatment_nodes = json.dumps(list(graph.nodes))
    network.comparator_edges = json.dumps([[a, b, n] for a, b, n in graph.edges])
    network.administration_routes = json.dumps(
        {n: routes[n] for n in graph.nodes if n in routes}
    )
    network.is_connected = graph.is_connected
    network.has_closed_loops = graph.has_closed_loops
    network.has_multi_arm_studies = graph.has_multi_arm_studies

    # Scoped to the protocol that will actually govern a resolve, which is the one just
    # written above: a rebuild that names no protocol does not un-govern the network.
    #
    # Deliberately NOT persisted. The stored columns are the network's own shape; a window
    # is one protocol's judgement and can be re-approved without re-harvesting anything, so
    # a stored scoped topology would be a second truth that goes stale silently. The
    # resolver derives its own scope from the protocol every time it runs.
    report.protocol_scope = _protocol_scope(
        protocol_id=network.protocol_id,
        endpoint_topology=graph,
        reporting=screened.reporting_arms,
    )
    if report.overstates_answerable:
        scope = report.protocol_scope
        logger.warning(
            "network %s is endpoint-level: protocol %s (weeks %g-%g) leaves %d of %d "
            "nodes answerable%s",
            network_id, scope.protocol_id, *scope.approved_time_window,
            scope.topology_summary.get("node_count", 0), len(graph.nodes),
            f", losing {', '.join(scope.nodes_lost)}" if scope.nodes_lost else "",
        )

    existing = {
        m.study_id: m for m in (await db.execute(
            select(NetworkMembership).where(NetworkMembership.network_id == network_id)
        )).scalars().all()
    }
    for study_id in report.proposed_studies:
        if study_id in existing:
            continue
        db.add(NetworkMembership(
            membership_id=f"NM-{network_id}-{study_id}",
            network_id=network_id,
            study_id=study_id,
            protocol_id=protocol_id,
            membership_status=lifecycles.PROPOSED,
            proposal_rationale=(
                f"reports {canonical_outcome_id} for at least two arms in the "
                f"{treatment_phase} phase"
            ),
        ))

    await write_audit(
        db, role="OPERATOR", event="EVIDENCE_NETWORK_BUILT",
        context={
            "network_id": network_id,
            "created": report.created,
            "indication": indication,
            "canonical_outcome_id": canonical_outcome_id,
            "treatment_phase": treatment_phase,
            "population_stratum": population_stratum,
            "protocol_id": protocol_id,
            "proposed_study_count": len(report.proposed_studies),
            "excluded_count": len(report.excluded),
            "endpoint_topology": graph.summary(),
            "protocol_scope": (
                report.protocol_scope.as_dict() if report.protocol_scope else None
            ),
            "overstates_answerable": report.overstates_answerable,
            "membership_status": lifecycles.PROPOSED,
            "ratification_status": network.ratification_status,
        },
        commit=False,
    )
    # The caller decides, for the same reason ingestion does: committing here made the
    # CLI's dry-run rollback a no-op.
    if commit:
        await db.commit()
    return report


async def protocol_scope_for(
    db: AsyncSession, network: EvidenceNetwork
) -> ProtocolScope | None:
    """The protocol-scope disclosure for a network that already exists. Reads only.

    Exists so a read surface can show *"the stored graph has 8 nodes, the governing window
    leaves 6"* without going through ``build_network``, which mutates the session, refuses a
    RATIFIED network and writes an audit entry — none of which a GET should do.

    Derived on every call and never stored, for the reason recorded above: a window is one
    protocol's judgement and can be re-approved without re-harvesting, so a persisted scope
    would be a second truth that goes stale in silence.
    """
    if not network.protocol_id:
        return None

    studies = list((await db.execute(
        select(ClinicalStudy).where(ClinicalStudy.indication == network.indication)
    )).scalars().all())
    screened = _screen(
        studies,
        canonical_outcome_id=network.canonical_outcome_id,
        treatment_phase=network.treatment_phase or "PRIMARY",
        population_stratum=network.population_stratum,
    )
    return _protocol_scope(
        protocol_id=network.protocol_id,
        endpoint_topology=topology.build(screened.study_arms),
        reporting=screened.reporting_arms,
    )
