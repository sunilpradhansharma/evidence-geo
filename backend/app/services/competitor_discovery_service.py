"""Competitor discovery sweep + review queue (Phase 5, Tier A).

Assembles ``TreatmentObservation`` rows from evidence already ingested, asks
``evidence.discovery`` which of them are candidates, and stores the answer as a review queue.
The rules live in the pure module; this file only reads rows and persists decisions.

**Nothing here writes ``brands.yaml``.** Accepting a candidate records a decision and makes it
eligible for ``config_proposal``, which renders a YAML fragment for a human to commit. The
whole justification for the curated class/route table is that a curated table is a reviewable
artefact — a queue that edited the file directly would turn it into the inferred kind, which
is explicitly out of scope.

**A decided candidate is never overwritten**, matching ingestion's rule for a decided study. A
re-sweep refreshes signals on ``NEW``/``DEFERRED`` rows and leaves ``ACCEPTED``/``REJECTED``
alone, so a rejection is remembered instead of being re-proposed every run.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import taxonomy
from app.evidence import discovery
from app.models.clinical_study import ClinicalStudy
from app.models.competitor_candidate import (
    ACCEPTED,
    DECIDED_STATES,
    NEW,
    REVIEW_STATES,
    CompetitorCandidate,
)
from app.models.drug_fact import DrugFact
from app.models.nma_result import PUBLISHED, NMAResult
from app.services import published_synthesis_service as synthesis
from app.utils.audit import write_audit

logger = logging.getLogger(__name__)

# Most advanced first. Used only to report a phase, never to gate anything — a treatment's
# development stage is context for a reviewer, not a computation input.
_PHASE_RANK = (
    "PHASE4", "PHASE3", "PHASE2_PHASE3", "PHASE2", "PHASE1_PHASE2", "PHASE1",
    "EARLY_PHASE1",
)


class DiscoveryError(ValueError):
    """A discovery request that cannot be carried out as specified."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def candidate_id_for(indication: str, treatment: str) -> str:
    """Deterministic id, so a re-sweep updates one row instead of accumulating duplicates.

    Same reasoning as ``network_builder_service.network_id_for``.
    """
    def slug(value: str) -> str:
        return re.sub(r"[^A-Z0-9]+", "-", (value or "").upper()).strip("-")

    return f"CC-{slug(indication)}-{slug(treatment)}"


def _json(value) -> str | None:
    return json.dumps(list(value)) if value else None


def _most_advanced_phase(phases: set[str]) -> str | None:
    for phase in _PHASE_RANK:
        if phase in phases:
            return phase
    return next(iter(sorted(phases)), None)


def _monitored_for(indication: str) -> frozenset[str]:
    """Everything we already track for this indication, lowercased.

    Disease-level names first, because that is the Phase 1 fix — the parent area's competitor
    list mixes indications together. The parent block is then unioned in, deliberately: a drug
    curated at area level *is* tracked, just not indication-scoped, and treating it as a fresh
    discovery would bury the genuine ones under drugs we already watch.

    The unmet case that leaves — curated for the area but absent from the indication overlay —
    is config tidiness rather than a discovered competitor, and mixing the two into one queue
    would make neither actionable.
    """
    names = set(taxonomy.brands_for_disease(indication))
    names |= set(taxonomy.competitors_for_disease(indication))
    ta_key = taxonomy.therapeutic_area_key_for_disease(indication)
    if ta_key:
        names |= set(taxonomy.focus_brands_for_key(ta_key))
        names |= set(taxonomy.competitors_for_key(ta_key))
    return discovery.normalise_names(names)


async def _published_nma_nodes(
    db: AsyncSession, indication: str
) -> tuple[dict[str, int], int]:
    """``({lowercased treatment: syntheses containing it}, syntheses scanned)``.

    The node set is read back through ``published_synthesis_service`` rather than parsed here,
    so there is one interpretation of the stored league-table shape. A second reading would
    eventually disagree about which treatments a paper covered.
    """
    rows = list((await db.execute(
        select(NMAResult).where(
            NMAResult.source == PUBLISHED, NMAResult.indication == indication
        )
    )).scalars().all())
    counts: dict[str, int] = {}
    for row in rows:
        for treatment in synthesis.treatments_in(row):
            key = treatment.strip().lower()
            if key:
                counts[key] = counts.get(key, 0) + 1
    return counts, len(rows)


async def _label_indications(db: AsyncSession) -> dict[str, set[str]]:
    """``{lowercased brand or generic: approved indications}`` from stored drug facts."""
    facts = list((await db.execute(
        select(DrugFact).where(DrugFact.superseded_by.is_(None))
    )).scalars().all())
    index: dict[str, set[str]] = {}
    for fact in facts:
        try:
            approved = set(json.loads(fact.approved_indications or "[]"))
        except (TypeError, ValueError):
            approved = set()
        for name in (fact.brand, fact.generic):
            if name:
                index.setdefault(name.strip().lower(), set()).update(approved)
    return index


async def _observations(
    db: AsyncSession, indication: str
) -> tuple[list[discovery.TreatmentObservation], dict[str, int], set[str], frozenset[str]]:
    """Assemble one observation per treatment seen in this indication's trials.

    Returns the observations plus a small tally the sweep reports, so a run can say *"34
    treatments observed, 27 already tracked"* rather than only naming what it found.
    """
    studies = list((await db.execute(
        select(ClinicalStudy).where(
            ClinicalStudy.indication == indication,
            ClinicalStudy.is_randomised.is_(True),
        )
    )).scalars().all())

    nma_nodes, synthesis_count = await _published_nma_nodes(db, indication)
    labels = await _label_indications(db)
    monitored = _monitored_for(indication)
    recent_cutoff = date.today() - timedelta(days=discovery.NEWLY_ACTIVE_DAYS)

    # Per treatment: the studies it appears in and who it was randomised against.
    seen: dict[str, dict] = {}
    # Comparators our own monitored brands were tested against, in this indication. This is
    # the shared-anchor set — usually placebo, sometimes Humira or Stelara.
    our_comparators: set[str] = set()
    # Reported, not silently dropped: a sweep that read 37 studies and used 34 has to say
    # which number it means.
    strategy_trials: list[str] = []

    for study in studies:
        arm_treatments = sorted({a.treatment for a in study.arms if a.treatment})
        if len(arm_treatments) < 2:
            continue
        # The whole study, not the offending arm. Dropping only the class-level arm would
        # leave the rest looking like a head-to-head between the molecules beside it, which
        # is exactly the comparison a strategy trial did not run.
        if discovery.is_strategy_trial(arm_treatments):
            strategy_trials.append(study.study_id)
            continue
        for treatment in arm_treatments:
            others = [t for t in arm_treatments if t != treatment]
            if treatment.strip().lower() in monitored:
                our_comparators.update(others)
            bucket = seen.setdefault(treatment, {
                "studies": set(), "others": set(), "phases": set(),
                "posted": False, "recent": False, "latest": None, "sponsor": None,
            })
            bucket["studies"].add(study.study_id)
            bucket["others"].update(others)
            if study.phase:
                bucket["phases"].add(study.phase.upper())
            if study.results_first_posted:
                bucket["posted"] = True
            for when in (study.results_first_posted, study.start_date):
                if when and when >= recent_cutoff:
                    bucket["recent"] = True
                if when and (bucket["latest"] is None or when > bucket["latest"]):
                    bucket["latest"] = when
            bucket["sponsor"] = bucket["sponsor"] or study.sponsor

    observations: list[discovery.TreatmentObservation] = []
    already_tracked = 0
    for treatment, bucket in seen.items():
        key = treatment.strip().lower()
        if key in monitored:
            already_tracked += 1
        approved = labels.get(key, set())
        observations.append(discovery.TreatmentObservation(
            treatment=treatment,
            indication=indication,
            study_ids=tuple(sorted(bucket["studies"])),
            # The same set answers two different questions: checked against `monitored` it
            # means "randomised head to head with ours", and against `our_comparators` it
            # means "shares an anchor with ours".
            co_arm_treatments=tuple(sorted(bucket["others"])),
            comparators=tuple(sorted(bucket["others"])),
            published_nma_count=nma_nodes.get(key, 0),
            latest_evidence_date=bucket["latest"],
            development_phase=_most_advanced_phase(bucket["phases"]),
            has_posted_results=bucket["posted"],
            started_recently=bucket["recent"],
            label_names_indication=indication in approved,
            sponsor=bucket["sponsor"],
            # Curated annotation is copied, never inferred. An uncurated molecule keeps
            # NULLs, which is what tells a reviewer it needs characterising.
            generic=None,
            drug_class=taxonomy.drug_class_for(treatment),
            administration_route=taxonomy.administration_route_for(treatment),
            is_curated_drug=bool(taxonomy.drug_index().get(key)),
        ))

    tally = {
        "treatments_observed": len(seen),
        "already_tracked": already_tracked,
        "studies_scanned": len(studies),
        "strategy_trials_screened": len(strategy_trials),
        "published_syntheses_scanned": synthesis_count,
    }
    return observations, tally, our_comparators, monitored


async def discover(
    db: AsyncSession,
    *,
    indication: str | None = None,
    commit: bool = True,
) -> dict:
    """Run a Tier A sweep over one indication or every indication with trials.

    ``commit=False`` reports what it would store and rolls nothing forward, mirroring the
    ingestion and builder runners so a sweep can be inspected before it lands.
    """
    if indication:
        indications = [indication]
    else:
        indications = [
            row for row in (await db.execute(
                select(ClinicalStudy.indication).distinct()
            )).scalars().all() if row
        ]

    report: dict = {
        "indications": [],
        "created": 0,
        "updated": 0,
        "skipped_decided": 0,
        "candidates": [],
    }

    for name in sorted(indications):
        observations, tally, our_comparators, monitored = await _observations(db, name)
        comparator_set = discovery.normalise_names(our_comparators)

        found: list[discovery.Candidate] = []
        for observation in observations:
            candidate = discovery.assess(
                observation, monitored=monitored, our_comparators=comparator_set
            )
            if candidate is not None:
                found.append(candidate)
        found.sort(key=lambda c: (-c.discovery_confidence, c.treatment))

        existing = {
            row.candidate_id: row for row in (await db.execute(
                select(CompetitorCandidate).where(
                    CompetitorCandidate.indication == name
                )
            )).scalars().all()
        }

        for candidate in found:
            candidate_id = candidate_id_for(name, candidate.treatment)
            row = existing.get(candidate_id)
            if row is not None and row.review_status in DECIDED_STATES:
                # A decision is a judgement about this molecule, and a re-sweep is not new
                # information about it. Refreshing the row would quietly resurrect a
                # rejection or reopen an acceptance.
                report["skipped_decided"] += 1
                continue
            if row is None:
                row = CompetitorCandidate(
                    candidate_id=candidate_id,
                    treatment=candidate.treatment,
                    indication=name,
                    review_status=NEW,
                )
                db.add(row)
                report["created"] += 1
            else:
                report["updated"] += 1

            row.therapeutic_area = taxonomy.therapeutic_area_key_for_disease(name)
            row.generic = candidate.generic
            row.sponsor = candidate.sponsor
            row.drug_class = candidate.drug_class
            row.administration_route = candidate.administration_route
            row.is_curated_drug = candidate.is_curated_drug
            row.discovery_reasons = json.dumps(list(candidate.reasons))
            row.evidence_count = candidate.evidence_count
            row.direct_comparison_count = candidate.direct_comparison_count
            row.compared_with = _json(candidate.compared_with)
            row.shared_comparators = _json(candidate.shared_comparators)
            row.published_nma_count = candidate.published_nma_count
            row.development_phase = candidate.development_phase
            row.has_posted_results = candidate.has_posted_results
            row.latest_evidence_date = candidate.latest_evidence_date
            row.source_study_ids = _json(candidate.source_study_ids)
            row.discovery_confidence = candidate.discovery_confidence

        report["indications"].append({
            "indication": name,
            **tally,
            "candidates_found": len(found),
        })
        report["candidates"].extend(
            {
                "candidate_id": candidate_id_for(name, c.treatment),
                "indication": name,
                "treatment": c.treatment,
                "reasons": list(c.reasons),
                "reason_labels": list(c.reason_labels),
                "discovery_confidence": c.discovery_confidence,
                "evidence_count": c.evidence_count,
                "compared_with": list(c.compared_with),
                "is_curated_drug": c.is_curated_drug,
            }
            for c in found
        )

    await write_audit(
        db, role="OPERATOR", event="COMPETITOR_DISCOVERY_SWEEP",
        context={
            "indications": [i["indication"] for i in report["indications"]],
            "created": report["created"],
            "updated": report["updated"],
            "skipped_decided": report["skipped_decided"],
            "tier": "A",
            # Recorded so an audit reader knows the sweep never touched the taxonomy.
            "config_written": False,
        },
        commit=False,
    )
    if commit:
        await db.commit()
    return report


# --- queue ------------------------------------------------------------------------------
def _candidate_out(row: CompetitorCandidate) -> dict:
    reasons = json.loads(row.discovery_reasons) if row.discovery_reasons else []
    return {
        "candidate_id": row.candidate_id,
        "treatment": row.treatment,
        "generic": row.generic,
        "sponsor": row.sponsor,
        "indication": row.indication,
        "therapeutic_area": row.therapeutic_area,
        "drug_class": row.drug_class,
        "administration_route": row.administration_route,
        "is_curated_drug": row.is_curated_drug,
        "discovery_reasons": reasons,
        "reason_labels": [
            discovery.REASON_LABELS[r] for r in reasons if r in discovery.REASON_LABELS
        ],
        "evidence_count": row.evidence_count,
        "direct_comparison_count": row.direct_comparison_count,
        "compared_with": json.loads(row.compared_with) if row.compared_with else [],
        "shared_comparators": (
            json.loads(row.shared_comparators) if row.shared_comparators else []
        ),
        "published_nma_count": row.published_nma_count,
        "development_phase": row.development_phase,
        "has_posted_results": row.has_posted_results,
        "latest_evidence_date": (
            row.latest_evidence_date.isoformat() if row.latest_evidence_date else None
        ),
        "source_study_ids": (
            json.loads(row.source_study_ids) if row.source_study_ids else []
        ),
        "discovery_confidence": row.discovery_confidence,
        "review_status": row.review_status,
        "reviewed_by": row.reviewed_by,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "review_note": row.review_note,
        "config_applied": row.config_applied,
        "first_seen_at": row.first_seen_at.isoformat() if row.first_seen_at else None,
    }


async def list_candidates(
    db: AsyncSession,
    *,
    indication: str | None = None,
    review_status: str | None = None,
    limit: int = 200,
) -> dict:
    """The review queue, strongest signal first."""
    stmt = select(CompetitorCandidate)
    if indication:
        stmt = stmt.where(CompetitorCandidate.indication == indication)
    if review_status:
        stmt = stmt.where(CompetitorCandidate.review_status == review_status)
    total = (await db.execute(
        select(func.count()).select_from(stmt.subquery())
    )).scalar_one()
    rows = list((await db.execute(
        stmt.order_by(
            CompetitorCandidate.discovery_confidence.desc(),
            CompetitorCandidate.treatment,
        ).limit(max(1, min(limit, 500)))
    )).scalars().all())

    by_status = {
        str(status): int(n) for status, n in (await db.execute(
            select(CompetitorCandidate.review_status, func.count())
            .group_by(CompetitorCandidate.review_status)
        )).all()
    }
    return {
        "total": int(total),
        "candidates": [_candidate_out(r) for r in rows],
        "counts_by_status": by_status,
        "review_states": list(REVIEW_STATES),
        "reasons": [
            {"code": code, "label": discovery.REASON_LABELS[code],
             "weight": discovery.REASON_WEIGHTS[code]}
            for code in discovery.DISCOVERY_REASONS
        ],
    }


async def review_candidate(
    db: AsyncSession,
    candidate_id: str,
    *,
    decision: str,
    reviewer: str,
    note: str | None = None,
) -> dict:
    """Record a review decision. Changes no configuration.

    Accepting makes the candidate eligible for ``config_proposal``; it does not add the drug
    to any competitor list. ``reviewer`` is **recorded, not authenticated** — RBAC is absent
    from this tree, so this is a governance record rather than a security control, the same
    caveat that applies to protocol approval.
    """
    if decision not in REVIEW_STATES:
        raise DiscoveryError(
            f"decision must be one of {', '.join(REVIEW_STATES)}"
        )
    if decision != NEW and not (reviewer or "").strip():
        raise DiscoveryError("a review decision needs a named reviewer")

    row = (await db.execute(
        select(CompetitorCandidate).where(
            CompetitorCandidate.candidate_id == candidate_id
        )
    )).scalar_one_or_none()
    if row is None:
        raise DiscoveryError(f"no candidate {candidate_id!r}")

    row.review_status = decision
    row.reviewed_by = reviewer.strip() or None
    row.reviewed_at = utcnow()
    row.review_note = (note or "").strip() or None

    await write_audit(
        db, role="REVIEWER", event="COMPETITOR_CANDIDATE_REVIEWED",
        context={
            "candidate_id": candidate_id,
            "treatment": row.treatment,
            "indication": row.indication,
            "decision": decision,
            "reviewer_recorded_not_authenticated": reviewer.strip(),
            "config_written": False,
        },
        commit=False,
    )
    await db.commit()
    return _candidate_out(row)


# --- config proposal --------------------------------------------------------------------
def _yaml_list(values: list[str]) -> str:
    return "[" + ", ".join(values) + "]"


async def config_proposal(db: AsyncSession, *, indication: str | None = None) -> dict:
    """A ``brands.yaml`` fragment for accepted candidates a human has not yet committed.

    Rendered rather than written, on purpose. ``brands.yaml`` is reviewed in git like any
    other config, and an approval path that edited it would make the curated table an
    unreviewed artefact — the exact distinction Tier B1 is in scope and B2 is not.

    Molecules with no curated class or route are emitted **commented out**, with the missing
    fields named. A pasteable placeholder would let ``drug_class: REVIEW_REQUIRED`` ship as a
    real value; a comment cannot.
    """
    stmt = select(CompetitorCandidate).where(
        CompetitorCandidate.review_status == ACCEPTED,
        CompetitorCandidate.config_applied.is_(False),
    )
    if indication:
        stmt = stmt.where(CompetitorCandidate.indication == indication)
    rows = list((await db.execute(
        stmt.order_by(CompetitorCandidate.indication, CompetitorCandidate.treatment)
    )).scalars().all())

    by_indication: dict[str, list[str]] = {}
    needs_characterising: list[CompetitorCandidate] = []
    for row in rows:
        by_indication.setdefault(row.indication, []).append(row.treatment)
        if not row.is_curated_drug or not row.drug_class or not row.administration_route:
            needs_characterising.append(row)

    lines: list[str] = [
        "# Proposed by Tier A competitor discovery. Review every line before committing.",
        "# Apply these to the taxonomy by hand — discovery never edits it. The live store is",
        "# SQLite; backend/app/config/seed/brands_seed.yaml is the reviewed baseline it seeds",
        "# from, so a change belongs in both to survive a fresh database.",
    ]
    if by_indication:
        lines.append("")
        lines.append("indications:")
        for name in sorted(by_indication):
            lines.append(f"  {name}:")
            lines.append(
                f"    competitors: {_yaml_list(sorted(by_indication[name]))}"
                "   # append to the existing list"
            )
    if needs_characterising:
        lines.extend([
            "",
            "# These molecules have no curated class or route. Fill them in from the label,",
            "# then uncomment. Class is never inferred here — an inferred label is an",
            "# unreviewed assertion, and open-set class inference is out of scope.",
            "# drug_catalog:",
        ])
        for row in sorted(needs_characterising, key=lambda r: r.treatment):
            missing = []
            if not row.drug_class:
                missing.append("drug_class")
            if not row.administration_route:
                missing.append("administration_route")
            lines.append(
                f"#   {row.treatment}: {{ generic: ?, company: "
                f"{row.sponsor or '?'}, drug_class: ?, administration_route: ? }}"
                f"   # missing: {', '.join(missing) or 'nothing'}"
            )

    return {
        "accepted_pending_commit": len(rows),
        "indications": sorted(by_indication),
        "needs_characterising": sorted(r.treatment for r in needs_characterising),
        "yaml": "\n".join(lines) if rows else "",
        "note": (
            "Discovery proposes; a human commits. Accepting a candidate records a decision "
            "and never edits brands.yaml."
        ),
    }


async def mark_config_applied(
    db: AsyncSession, candidate_ids: list[str], *, applied_by: str
) -> dict:
    """Record that a human has committed the config for these accepted candidates.

    Separate from acceptance because they are different facts: one is a judgement that the
    molecule belongs on the list, the other is that the list now says so. Collapsing them
    would make the queue claim a config change nobody made.
    """
    if not (applied_by or "").strip():
        raise DiscoveryError("recording a config change needs a named person")
    rows = list((await db.execute(
        select(CompetitorCandidate).where(
            CompetitorCandidate.candidate_id.in_(candidate_ids)
        )
    )).scalars().all())
    not_accepted = [r.candidate_id for r in rows if r.review_status != ACCEPTED]
    if not_accepted:
        raise DiscoveryError(
            "only accepted candidates can be marked applied: "
            + ", ".join(sorted(not_accepted))
        )
    for row in rows:
        row.config_applied = True
    await write_audit(
        db, role="OPERATOR", event="COMPETITOR_CANDIDATE_CONFIG_APPLIED",
        context={
            "candidate_ids": sorted(r.candidate_id for r in rows),
            "applied_by": applied_by.strip(),
        },
        commit=False,
    )
    await db.commit()
    return {"applied": sorted(r.candidate_id for r in rows), "missing": sorted(
        set(candidate_ids) - {r.candidate_id for r in rows}
    )}


# --- cross-class map (B1 presentation) --------------------------------------------------
async def class_map(db: AsyncSession, *, indication: str) -> dict:
    """Treatments in this indication's trials grouped by curated pharmacological class.

    The ``uncharacterised`` list is the point as much as the groups are. Phase 0 measured
    curated coverage at 12-26% of nodes, so an ``IL-23 vs JAK vs TNF`` view that silently
    dropped every uncurated molecule would look authoritative while hiding most of the
    network.
    """
    studies = list((await db.execute(
        select(ClinicalStudy).where(
            ClinicalStudy.indication == indication,
            ClinicalStudy.is_randomised.is_(True),
        )
    )).scalars().all())

    monitored = _monitored_for(indication)
    entries: list[tuple[str, str | None, str | None]] = []
    seen: set[str] = set()
    for study in studies:
        for arm in study.arms:
            name = (arm.treatment or "").strip()
            if not name or name in seen or not discovery.is_discoverable(name):
                continue
            seen.add(name)
            entries.append((
                name,
                # The arm snapshot first: a stored result must stay explicable even if the
                # curated table has since been edited. Curation is the fallback for arms
                # ingested before route/class were snapshotted.
                arm.drug_class or taxonomy.drug_class_for(name),
                arm.administration_route or taxonomy.administration_route_for(name),
            ))

    groups, uncharacterised = discovery.group_by_class(entries)
    routes_present = sorted({
        route for _t, _c, route in entries if route
    })
    return {
        "indication": indication,
        "treatment_count": len(entries),
        "classes": [
            {
                "drug_class": g.drug_class,
                "treatments": g.treatments,
                "routes": g.routes,
                "monitored": [
                    t for t in g.treatments if t.strip().lower() in monitored
                ],
            }
            for g in groups
        ],
        "uncharacterised": uncharacterised,
        "characterised_pct": (
            round(100.0 * (len(entries) - len(uncharacterised)) / len(entries), 1)
            if entries else 0.0
        ),
        # A network holding both oral and injectable agents carries a transitivity threat.
        # Reported here because it is visible from the class map before any resolve runs.
        "is_route_mixed": len(routes_present) > 1,
        "routes_present": routes_present,
    }
