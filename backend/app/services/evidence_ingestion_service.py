"""Persist parsed clinical evidence, under the licence's retention rules (Phase 3A).

The missing half of the adapters. ``clinicaltrials.parse`` returns unsaved canonical rows
and says so; this is the "ingestion service" its ``source_payload_id=None`` comment refers
to. Without it the Phase 6 resolver has nothing to resolve against.

Three rules that shape the whole module:

* **Ingestion never verifies.** Rows land ``EXTRACTED``. Whether the extraction is
  *accurate* is a human judgement, and ``EXTRACTED -> VERIFIED`` is not even a legal
  transition — it must pass through ``MAPPED``. A pipeline that marked its own output
  verified would make the entire verification lifecycle decorative, and the resolver's
  refusal to compute on unverified rows meaningless.

* **``MAPPED`` is advanced automatically, because it is a fact rather than a judgement.**
  A study whose every outcome row resolved to a ``canonical_outcome_id`` *has been*
  mapped; asserting that is not the same as asserting the numbers are right. A study with
  unmapped endpoints stays ``EXTRACTED`` and shows up in curation.

* **A decided study is never overwritten.** Re-ingesting a ``VERIFIED`` or ``REJECTED``
  study would silently rewrite facts someone signed for. Those are reported as skipped;
  a genuine correction creates a new version (``version`` / ``superseded_by``), which is
  deliberately out of scope here. Undecided rows are replaced freely — there is no history
  to protect yet.

Retention goes through ``SourcePayload.record``, so the licence matrix cannot be bypassed.
ClinicalTrials.gov is PUBLIC_DOMAIN, so the full registry JSON is retained; that is a
property of the source, not of this module, and a restricted source would silently keep
only the fragment.

**Two entity families, because they version differently.** Studies are superseded when an
extraction is corrected; drug facts are superseded when the *label* changes. Sharing one
rule would force one of them to lie, so ``ingest_drug_fact`` deliberately does not reuse
``ingest_study``'s replace-in-place path — see the section at the foot of this module.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import taxonomy
from app.evidence import lifecycles, treatments
from app.evidence.sources import clinicaltrials as ctg
from app.evidence.sources import openfda_facts as fda
from app.evidence.sources.base import FetchResult
from app.geo.sources.openfda import LabelSeed
from app.models.clinical_study import ClinicalStudy
from app.models.drug_fact import DrugFact
from app.models.source_payload import SourcePayload, checksum_of
from app.utils.audit import write_audit

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IngestionError(ValueError):
    """A request that cannot be ingested at all."""


@dataclass
class StudyOutcome:
    """What happened to one study."""

    study_id: str
    action: str  # INGESTED | UPDATED | SKIPPED
    reason: str | None = None
    verification_status: str | None = None
    arm_count: int = 0
    outcome_count: int = 0
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "study_id": self.study_id,
            "action": self.action,
            "reason": self.reason,
            "verification_status": self.verification_status,
            "arm_count": self.arm_count,
            "outcome_count": self.outcome_count,
            "warnings": list(self.warnings),
        }


@dataclass
class IngestionReport:
    """The outcome of one ingestion run, per study and in aggregate."""

    indication: str
    studies: list[StudyOutcome] = field(default_factory=list)
    discovered: int = 0
    screened_out: int = 0
    fetch_failures: list[tuple[str, str]] = field(default_factory=list)
    unmapped_treatments: dict[str, int] = field(default_factory=dict)
    uninformative_arms: dict[str, int] = field(default_factory=dict)
    class_level_arms: dict[str, int] = field(default_factory=dict)
    # label -> the studies that produced it. Without this the advice attached to the
    # buckets above is unfollowable: "read the source document" needs to name one.
    label_studies: dict[str, list[str]] = field(default_factory=dict)
    # (study_id, reason) for everything screened out. A bare count cannot be audited, and
    # screening is the one step that removes real randomised evidence from the network.
    screened_out_detail: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ingested(self) -> int:
        return sum(1 for s in self.studies if s.action == "INGESTED")

    @property
    def updated(self) -> int:
        return sum(1 for s in self.studies if s.action == "UPDATED")

    @property
    def skipped(self) -> int:
        return sum(1 for s in self.studies if s.action == "SKIPPED")

    def as_dict(self) -> dict:
        return {
            "indication": self.indication,
            "discovered": self.discovered,
            "screened_out": self.screened_out,
            "ingested": self.ingested,
            "updated": self.updated,
            "skipped": self.skipped,
            "fetch_failures": [{"id": i, "reason": r} for i, r in self.fetch_failures],
            # The Phase 0 audit measured 12-20% catalog coverage, so an ingestion run that
            # does NOT surface uncurated labels is the surprising one. Reported as a first
            # class figure rather than buried in per-study warnings.
            "unmapped_treatments": dict(
                sorted(self.unmapped_treatments.items(), key=lambda kv: (-kv[1], kv[0]))
            ),
            # Kept apart from the above because the two need different actions. An
            # uncurated drug is fixed by a catalog entry; an arm the registry labelled
            # "Group A" cannot be fixed here at all, because the record never said what
            # that arm received. Curation has to go back to the source document.
            "uninformative_arms": dict(
                sorted(self.uninformative_arms.items(), key=lambda kv: (-kv[1], kv[0]))
            ),
            # Screened out rather than merely uncurated: these name a drug class or a care
            # strategy, so there is no catalog entry that would fix them.
            "class_level_arms": dict(
                sorted(self.class_level_arms.items(), key=lambda kv: (-kv[1], kv[0]))
            ),
            "label_studies": {k: list(v) for k, v in sorted(self.label_studies.items())},
            "screened_out_detail": [
                {"id": i, "reason": r} for i, r in self.screened_out_detail
            ],
            "studies": [s.as_dict() for s in self.studies],
        }


def _tick(progress: dict | None, **fields) -> None:
    """Publish live counters into a caller-owned dict, if one was supplied.

    The same contract ``harvest.pipeline.harvest`` already uses: the caller allocates the
    dict, reads it from another coroutine while the run is in flight, and a caller that
    passes nothing (every CLI script) gets byte-identical behaviour. Additive on purpose —
    a progress hook that changed what a run *does* would be a second code path to test.
    """
    if progress is not None:
        progress.update(**fields)


def _note_label(
    report: IngestionReport, bucket: dict[str, int], label: str, study_id: str
) -> None:
    """Count one unresolved label and remember which study it came from."""
    bucket[label] = bucket.get(label, 0) + 1
    seen_in = report.label_studies.setdefault(label, [])
    if study_id not in seen_in:
        seen_in.append(study_id)


def class_level_arms(parsed: ctg.ParsedStudy) -> list[str]:
    """Arms naming a drug class or care strategy rather than a molecule.

    A curated agent is checked first, so methotrexate is a molecule here even though it is
    itself a csDMARD — the drug is not its class.
    """
    return [
        arm.treatment for arm in parsed.arms
        if arm.treatment and not arm.is_placebo
        and treatments.is_class_level_node(arm.treatment)
    ]


# =====================================================================================
# One study
# =====================================================================================
async def _existing(db: AsyncSession, study_id: str) -> ClinicalStudy | None:
    return (await db.execute(
        select(ClinicalStudy).where(ClinicalStudy.study_id == study_id)
    )).scalar_one_or_none()


async def _identical_payload(
    db: AsyncSession, source_identifier: str, raw_payload: str | None
) -> SourcePayload | None:
    """An existing payload for this identifier whose content is byte-identical, if any.

    Re-harvesting minted a fresh ``uuid4()`` payload every time, so an unchanged registry
    record was stored again in full on every run and the previous row was left orphaned —
    repointed away from by the study, but never marked ``superseded_by``. That is not
    versioning, it is duplication, and it grew the retained-document table without adding
    a single retrievable fact.

    Reuse is conditional on a checksum being available. A ``None`` checksum means nothing
    was passed to attest to, and SQL ``NULL`` does not compare equal anyway, so those mint
    a new row rather than collapsing every unattested payload for an identifier into one.

    The reused row keeps its original ``retrieved_at``. It states when that exact document
    was fetched, which is still true; what is deliberately not recorded here is that we saw
    it again unchanged, since a ``last_verified_at`` column is a schema change and a
    separate decision.
    """
    digest = checksum_of(raw_payload)
    if digest is None:
        return None
    return (await db.execute(
        select(SourcePayload).where(
            SourcePayload.source_identifier == source_identifier,
            SourcePayload.checksum == digest,
        ).limit(1)
    )).scalar_one_or_none()


def _mapping_complete(parsed: ctg.ParsedStudy) -> bool:
    """True when every outcome row carries a canonical id.

    Deliberately strict: one unmapped endpoint keeps the study ``EXTRACTED``, because
    ``MAPPED`` is read downstream as "the endpoints are known", and a partially mapped
    study would misrepresent itself in a curation queue.
    """
    return bool(parsed.outcomes) and all(o.canonical_outcome_id for o in parsed.outcomes)


async def ingest_study(
    db: AsyncSession,
    parsed: ctg.ParsedStudy,
    *,
    raw_payload: str | None,
    source_type: str = ctg.SOURCE_TYPE,
    url: str | None = None,
    commit: bool = True,
) -> StudyOutcome:
    """Persist one parsed study, its arms and its outcome rows.

    Idempotent on ``study_id`` (the NCT id), so re-running a harvest updates undecided
    rows instead of duplicating them.
    """
    study = parsed.study
    existing = await _existing(db, study.study_id)

    if existing is not None and existing.verification_status in (
        lifecycles.VERIFIED, lifecycles.REJECTED
    ):
        return StudyOutcome(
            study_id=study.study_id,
            action="SKIPPED",
            reason=(
                f"already {existing.verification_status}; re-ingesting would rewrite a "
                "decided row, so a correction must create a new version instead"
            ),
            verification_status=existing.verification_status,
        )

    identifier = study.registry_id or study.study_id
    payload = await _identical_payload(db, identifier, raw_payload)
    if payload is None:
        payload = SourcePayload.record(
            payload_id=f"SP-{uuid4().hex}",
            source_type=source_type,
            source_identifier=identifier,
            raw_payload=raw_payload,
            retained_fragment=None,
            citation=study.title,
            url=url,
        )
        db.add(payload)

    # MAPPED is a statement of fact about the rows, not a judgement about their accuracy.
    status = lifecycles.MAPPED if _mapping_complete(parsed) else lifecycles.EXTRACTED
    study.source_payload_id = payload.payload_id
    study.verification_status = status
    for row in parsed.outcomes:
        row.source_payload_id = payload.payload_id
        row.verification_status = status

    action = "INGESTED"
    if existing is not None:
        # Undecided, so there is no history to protect. Arms and outcomes go with it via
        # the ORM cascade — they are loaded eagerly (``lazy="selectin"``), so deleting the
        # parent is sufficient and a bulk delete would only double-delete the same rows.
        await db.delete(existing)
        await db.flush()
        action = "UPDATED"

    db.add(study)
    for arm in parsed.arms:
        db.add(arm)
    await db.flush()
    for row in parsed.outcomes:
        db.add(row)

    await write_audit(
        db, role="OPERATOR", event="CLINICAL_STUDY_INGESTED",
        context={
            "study_id": study.study_id,
            "action": action,
            "indication": study.indication,
            "payload_id": payload.payload_id,
            "license_class": payload.license_class,
            "retention_policy": payload.retention_policy,
            "full_document_retained": payload.raw_payload is not None,
            "verification_status": status,
            "arm_count": len(parsed.arms),
            "outcome_count": len(parsed.outcomes),
            "extraction_warnings": list(parsed.warnings),
        },
        commit=False,
    )
    if commit:
        await db.commit()

    return StudyOutcome(
        study_id=study.study_id,
        action=action,
        verification_status=status,
        arm_count=len(parsed.arms),
        outcome_count=len(parsed.outcomes),
        warnings=tuple(parsed.warnings),
    )


# =====================================================================================
# One indication
# =====================================================================================
async def ingest_indication(
    db: AsyncSession,
    indication: str,
    *,
    commit: bool = True,
    drugs: list[str],
    page_size: int = 40,
    limit: int | None = None,
    progress: dict | None = None,
) -> IngestionReport:
    """Discover, fetch and persist the randomised trials for one indication.

    Discovery is a search per drug; ingestion then **fetches each record individually**
    rather than reusing the search payload. A search response is not guaranteed to carry a
    complete ``resultsSection``, and storing a truncated payload as the provenance for an
    extracted value would make the checksum attest to something other than the source.

    ``progress``, when given, is mutated in place so a caller running this in the background
    can report *searching -> fetching -> done* while it happens. A full indication is minutes
    of throttled requests, so a surface with no progress is a surface that looks hung.
    """
    report = IngestionReport(indication=indication)
    _tick(
        progress, phase="searching", drugs_total=len(drugs), drugs_done=0, discovered=0,
        studies_total=0, studies_done=0, ingested=0, screened_out=0,
    )
    seen: set[str] = set()
    for index, drug in enumerate(drugs, start=1):
        found = await ctg.search(condition=indication, intervention=drug, page_size=page_size)
        if not found.ok:
            report.fetch_failures.append((f"search:{drug}", found.reason or "unknown"))
            _tick(progress, drugs_done=index)
            continue
        for record in (found.payload or {}).get("studies") or []:
            nct = (
                ((record.get("protocolSection") or {}).get("identificationModule") or {})
                .get("nctId")
            )
            if nct and nct not in seen:
                seen.add(nct)
        _tick(progress, drugs_done=index, discovered=len(seen))

    report.discovered = len(seen)
    ordered = sorted(seen)[: limit if limit else None]
    # ``studies_total`` is the CAPPED list, not ``discovered``: a --limit 3 smoke run that
    # reported "3 of 37" would look stalled at the moment it finished.
    _tick(
        progress, phase="fetching", discovered=len(seen), studies_total=len(ordered),
        studies_done=0,
    )

    for done, nct in enumerate(ordered, start=1):
        _tick(progress, studies_done=done, current_study=nct)
        result = await ctg.fetch(nct)
        if not result.ok:
            report.fetch_failures.append((nct, result.reason or "unknown"))
            continue

        parsed = ctg.parse(result, indication=indication)
        if parsed is None:
            report.fetch_failures.append((nct, "payload could not be parsed as a study record"))
            continue

        study_id = parsed.study.study_id

        # Interventional does not imply randomised. Admitting a single-arm or open-label
        # extension study would invent network edges that no randomisation supports.
        if not parsed.study.is_randomised:
            report.screened_out += 1
            report.screened_out_detail.append((study_id, "not randomised"))
            _tick(progress, screened_out=report.screened_out)
            continue

        # A class-level or strategy arm keeps the whole study out of a molecule network.
        # Screening the study rather than the arm is the point: the comparison the trial
        # actually made is molecule-versus-class, and keeping the drug arms while dropping
        # its comparator would leave edges pointing at a node that is no longer there.
        #
        # Recorded arm by arm first, so a screened study is still visible. Silently losing
        # randomised evidence is exactly what the rest of this module refuses to do.
        class_level = class_level_arms(parsed)
        if class_level:
            for label in class_level:
                _note_label(report, report.class_level_arms, label, study_id)
            report.screened_out += 1
            _tick(progress, screened_out=report.screened_out)
            # The study's OTHER arms are named too. Screening drops them from every bucket,
            # so a drug appearing only in strategy trials would vanish from the census and
            # nobody would learn the catalog is missing it.
            others = sorted({
                arm.treatment for arm in parsed.arms
                if arm.treatment and not arm.is_placebo
                and arm.treatment not in set(class_level)
            })
            reason = f"class-level or strategy arm: {', '.join(sorted(set(class_level))[:3])}"
            if others:
                reason += f" | also dropped: {', '.join(others[:5])}"
            report.screened_out_detail.append((study_id, reason))
            continue

        for arm in parsed.arms:
            if not arm.treatment or arm.is_placebo:
                continue
            if treatments.is_uninformative_label(arm.treatment):
                _note_label(report, report.uninformative_arms, arm.treatment, study_id)
            elif not taxonomy.drug_class_for(arm.treatment):
                _note_label(report, report.unmapped_treatments, arm.treatment, study_id)

        report.studies.append(await ingest_study(
            db, parsed,
            raw_payload=result.raw_text,
            url=result.url,
            commit=False,
        ))
        _tick(progress, ingested=report.ingested, updated=report.updated)

    # The CALLER decides. Committing unconditionally here made the CLI's dry-run
    # `rollback()` a no-op — a rollback after a commit rolls back an empty transaction, so
    # every "DRY RUN - nothing written" run was in fact writing.
    if commit:
        await db.commit()
    _tick(
        progress, phase="done", studies_done=len(ordered), ingested=report.ingested,
        updated=report.updated, skipped=report.skipped, screened_out=report.screened_out,
        current_study=None,
    )
    return report


async def ingest_payload(
    db: AsyncSession, payload: dict, *, indication: str, source_identifier: str = ""
) -> StudyOutcome:
    """Ingest one already-retrieved registry record. The offline path, used by tests."""
    result = FetchResult(
        ok=True,
        source_type=ctg.SOURCE_TYPE,
        source_identifier=source_identifier,
        payload=payload,
        raw_text=json.dumps(payload, default=str),
    )
    parsed = ctg.parse(result, indication=indication)
    if parsed is None:
        raise IngestionError("payload could not be parsed as a study record")
    return await ingest_study(db, parsed, raw_payload=result.raw_text)


# =====================================================================================
# Re-parse — fixing a stale extraction without re-harvesting
# =====================================================================================
async def reparse_study(
    db: AsyncSession, study_id: str, *, commit: bool = True
) -> StudyOutcome:
    """Re-extract one study from its own retained payload. No network call.

    A **stale parse is a defect in our code, not in the source**, and the two need
    different remedies. Re-harvesting to fix a parser bug changes both variables at once:
    a moved estimate could afterwards be either the fix or an updated registry record, and
    nobody could tell which. Re-reading the stored bytes keeps the change attributable.

    Provenance survives because the original ``raw_payload`` is passed through unmodified
    rather than re-serialised — ``_identical_payload`` then matches on checksum and reuses
    the existing row, so re-parsing does not mint a second document for the same fetch.

    This has **no privileged access to a decided row.** It delegates to ``ingest_study``,
    so a VERIFIED or REJECTED study is reported SKIPPED here exactly as a re-harvest would
    be. A maintenance routine does not get to step around the verification lifecycle; the
    only thing that may is a deliberate, audited, out-of-band reset.

    A FRAGMENT_ONLY licence physically retains no document, so those studies cannot be
    re-parsed at all. Reported as skipped with the licence named, because "nothing to
    re-read" and "nothing changed" are different outcomes.
    """
    study = await _existing(db, study_id)
    if study is None:
        return StudyOutcome(study_id=study_id, action="SKIPPED", reason="unknown study")

    indication = study.indication
    status = study.verification_status
    payload: SourcePayload | None = None
    if study.source_payload_id:
        payload = (await db.execute(
            select(SourcePayload).where(
                SourcePayload.payload_id == study.source_payload_id
            )
        )).scalar_one_or_none()

    if payload is None:
        return StudyOutcome(
            study_id=study_id, action="SKIPPED", verification_status=status,
            reason="no source payload on record — re-parse needs the original document",
        )
    if payload.raw_payload is None:
        return StudyOutcome(
            study_id=study_id, action="SKIPPED", verification_status=status,
            reason=(
                f"no document retained ({payload.license_class} / "
                f"{payload.retention_policy}) — this study can only be re-harvested"
            ),
        )
    try:
        data = json.loads(payload.raw_payload)
    except (TypeError, ValueError) as exc:
        return StudyOutcome(
            study_id=study_id, action="SKIPPED", verification_status=status,
            reason=f"retained payload is not valid JSON: {exc}",
        )

    result = FetchResult(
        ok=True,
        source_type=payload.source_type,
        source_identifier=payload.source_identifier,
        payload=data,
        raw_text=payload.raw_payload,
        url=payload.url,
    )
    # The indication is carried over, never re-derived. It is the scope someone chose for
    # this row; a re-parse corrects the extraction, not the question being asked of it.
    parsed = ctg.parse(result, indication=indication)
    if parsed is None:
        return StudyOutcome(
            study_id=study_id, action="SKIPPED", verification_status=status,
            reason="retained payload no longer parses as a study record",
        )

    outcome = await ingest_study(
        db, parsed,
        raw_payload=payload.raw_payload,
        source_type=payload.source_type,
        url=payload.url,
        commit=commit,
    )

    # Screening rules can have tightened since the row was first ingested. Re-parse does
    # not remove studies — deleting evidence is a decision, not a maintenance side effect
    # — but it must not leave a study silently sitting in a corpus that current rules
    # would have refused, which is the same class of defect as the silent orphan skip.
    if outcome.action != "SKIPPED":
        now_screened: list[str] = []
        if not parsed.study.is_randomised:
            now_screened.append("not randomised")
        class_level = class_level_arms(parsed)
        if class_level:
            now_screened.append(
                f"class-level or strategy arm: {', '.join(sorted(set(class_level))[:3])}"
            )
        if now_screened:
            outcome.warnings = outcome.warnings + (
                "current screening rules would reject this study on re-ingest ("
                + "; ".join(now_screened) + ") — kept, but it needs a decision",
            )
    return outcome


async def reparse_studies(
    db: AsyncSession,
    *,
    indication: str | None = None,
    study_ids: list[str] | None = None,
    commit: bool = True,
    progress: dict | None = None,
) -> list[StudyOutcome]:
    """Re-parse every stored study in scope, in a single transaction."""
    query = select(ClinicalStudy.study_id)
    if indication:
        query = query.where(ClinicalStudy.indication == indication)
    if study_ids:
        query = query.where(ClinicalStudy.study_id.in_(list(study_ids)))
    targets = sorted((await db.execute(query)).scalars().all())
    _tick(
        progress, phase="reparsing", studies_total=len(targets), studies_done=0,
        updated=0, skipped=0,
    )

    results: list[StudyOutcome] = []
    for done, sid in enumerate(targets, start=1):
        _tick(progress, studies_done=done, current_study=sid)
        results.append(await reparse_study(db, sid, commit=False))
        _tick(
            progress,
            updated=sum(1 for r in results if r.action in ("INGESTED", "UPDATED")),
            skipped=sum(1 for r in results if r.action == "SKIPPED"),
        )
    if commit:
        await db.commit()
    _tick(progress, phase="done", studies_done=len(targets), current_study=None)
    return results


# =====================================================================================
# Verification — the human step, kept separate on purpose
# =====================================================================================
async def verify_study(
    db: AsyncSession, study_id: str, *, verified_by: str, commit: bool = True
) -> ClinicalStudy:
    """Advance one study to ``VERIFIED``, recording who decided.

    Separate from ingestion by design: this asserts a person checked the extraction against
    the source. ``verified_by`` is **recorded, not authenticated** — there is no RBAC in
    this tree — so it is an audit trail rather than an access control, and that distinction
    matters to anyone reading the resulting evidence.

    ``commit`` exists for the same reason it does on every other write here, and this was the
    last function in the module still missing it. An unconditional commit makes a caller's
    later ``rollback()`` a no-op — it rolls back an empty transaction — so a "dry run" that
    verified anything silently persisted **everything queued before it too**, not just the
    verification. That is exactly the defect already fixed in ``ingest_indication``; it
    survived here because verification is the one step a dry run was not expected to reach.
    """
    if not (verified_by or "").strip():
        raise IngestionError(
            "verified_by is required — an anonymous verification is not auditable"
        )
    study = await _existing(db, study_id)
    if study is None:
        raise IngestionError(f"unknown study {study_id!r}")

    before = study.verification_status
    # EXTRACTED cannot reach VERIFIED directly. Walking the machine rather than assigning
    # the end state keeps the intermediate transition auditable and the rule in one place.
    if before == lifecycles.EXTRACTED:
        lifecycles.assert_transition("verification", before, lifecycles.MAPPED)
        study.verification_status = lifecycles.MAPPED
    lifecycles.assert_transition(
        "verification", study.verification_status, lifecycles.VERIFIED
    )
    study.verification_status = lifecycles.VERIFIED
    study.verified_by = verified_by.strip()
    study.verified_at = utcnow()

    for row in study.outcomes:
        row.verification_status = lifecycles.VERIFIED
        row.verified_by = verified_by.strip()
        row.verified_at = study.verified_at

    await write_audit(
        db, role="CURATOR", event="CLINICAL_STUDY_VERIFIED",
        context={
            "study_id": study_id,
            "from": before,
            "to": lifecycles.VERIFIED,
            "verified_by": study.verified_by,
            "outcome_rows": len(study.outcomes),
            "authentication": "RECORDED_NOT_AUTHENTICATED",
        },
        commit=False,
    )
    if commit:
        await db.commit()
    return study


async def reject_study(
    db: AsyncSession, study_id: str, *, rejected_by: str, reason: str, commit: bool = True
) -> ClinicalStudy:
    """Record that an extraction is wrong and must not be used. Requires a reason.

    The counterpart to ``verify_study`` and, until now, the missing half of the
    lifecycle: ``EXTRACTED -> REJECTED`` and ``MAPPED -> REJECTED`` have always been legal
    transitions with nothing able to make them. A curator who found a bad extraction could
    only leave it unverified, which is indistinguishable from one nobody has looked at.

    **A reason is required** for the same argument that makes it required on a membership
    exclusion: rejection removes evidence, and an unexplained removal leaves the next
    reader unable to tell a considered judgement from an accident.
    """
    if not (rejected_by or "").strip():
        raise IngestionError(
            "rejected_by is required — an anonymous rejection is not auditable"
        )
    if not (reason or "").strip():
        raise IngestionError(
            "a rejection reason is required — rejecting a study removes it from every "
            "network, and an unexplained removal cannot be reviewed"
        )
    study = await _existing(db, study_id)
    if study is None:
        raise IngestionError(f"unknown study {study_id!r}")

    before = study.verification_status
    lifecycles.assert_transition("verification", before, lifecycles.REJECTED)
    study.verification_status = lifecycles.REJECTED
    study.rejection_reason = reason.strip()
    study.verified_by = rejected_by.strip()
    study.verified_at = utcnow()
    for row in study.outcomes:
        row.verification_status = lifecycles.REJECTED
        row.rejection_reason = reason.strip()

    await write_audit(
        db, role="CURATOR", event="CLINICAL_STUDY_REJECTED",
        context={
            "study_id": study_id,
            "from": before,
            "to": lifecycles.REJECTED,
            "rejected_by": study.verified_by,
            "reason": study.rejection_reason,
            "outcome_rows": len(study.outcomes),
            "authentication": "RECORDED_NOT_AUTHENTICATED",
        },
        commit=False,
    )
    if commit:
        await db.commit()
    return study


# =====================================================================================
# Drug facts — the second entity family, versioned by LABEL DATE
# =====================================================================================
# `openfda_facts.parse_label` returned unsaved rows and nothing called it: the adapter was
# imported by its own test and by nothing else, so `drug_facts` was empty in every
# environment. That is the same defect this module was written to fix for
# `clinicaltrials.parse`, reached through the other adapter — and it was quieter, because
# three consumers gate on `verification_status == VERIFIED` and an empty table makes them
# return "no findings" rather than an error.
#
# What it silently disabled: Phase 7's drug-fact question category, Phase 8's
# APPROVAL / SAFETY_WARNING / MECHANISM claim types, and Phase 9's AI_MISINFORMATION_RISK
# — which carries the highest severity in the engine.

# The parser's input is a `LabelSeed`, not the raw SPL document, so that is what the
# payload retains. Storing the seed is what makes a re-derivation meaningful: it re-runs
# the same pure `parse_label` over the same bytes. State the limit plainly — this checks
# the MAPPING, not the FETCH. A curator confirming a fact is confirming that our reading
# of the label reproduces, and the label itself still has to be read at the source URL.
_SEED_FIELDS = tuple(LabelSeed().__dict__.keys())


@dataclass
class DrugFactOutcome:
    """What happened to one brand's label."""

    brand: str
    action: str  # INGESTED | UPDATED | SUPERSEDED | SKIPPED | NOT_FOUND
    fact_id: str | None = None
    reason: str | None = None
    verification_status: str | None = None
    label_updated_at: str | None = None
    supersedes: str | None = None
    flags: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "brand": self.brand,
            "action": self.action,
            "fact_id": self.fact_id,
            "reason": self.reason,
            "verification_status": self.verification_status,
            "label_updated_at": self.label_updated_at,
            "supersedes": self.supersedes,
            "flags": list(self.flags),
        }


@dataclass
class DrugFactReport:
    """The outcome of one drug-fact run, per brand and in aggregate."""

    facts: list[DrugFactOutcome] = field(default_factory=list)

    def _count(self, action: str) -> int:
        return sum(1 for f in self.facts if f.action == action)

    def as_dict(self) -> dict:
        return {
            "requested": len(self.facts),
            "ingested": self._count("INGESTED"),
            "updated": self._count("UPDATED"),
            "superseded": self._count("SUPERSEDED"),
            "skipped": self._count("SKIPPED"),
            "not_found": self._count("NOT_FOUND"),
            # Every fact lands unverified, so this is always the whole set. Reported
            # anyway, because the number a curator plans around is the queue length.
            "awaiting_verification": sum(
                1 for f in self.facts
                if f.verification_status in (lifecycles.EXTRACTED, lifecycles.MAPPED)
            ),
            "facts": [f.as_dict() for f in self.facts],
        }


def fact_id_for(brand: str, label_updated_at: object) -> str:
    """A deterministic id keyed on brand **and label date**.

    Derived from both because a drug fact is superseded when the label changes, not when
    an extraction is corrected. Keying on the brand alone would make a new label overwrite
    the old one in place and destroy the version history the model exists to keep; keying
    on a uuid would re-store an unchanged label on every run.

    An undated label collapses to one row per brand, which is right: with no effective
    date there is nothing to distinguish two retrievals by.
    """
    slug = re.sub(r"[^A-Z0-9]+", "-", (brand or "").upper()).strip("-") or "UNKNOWN"
    stamp = str(label_updated_at) if label_updated_at else "UNDATED"
    return f"DF-{slug}-{stamp}"


def seed_payload(seed: LabelSeed, *, brand: str, generic: str | None) -> str:
    """The retained payload for a label: the parser's input, plus who it was fetched for.

    Brand and generic travel with the seed so a re-derivation is self-contained. Without
    them the curation surface would have to guess which brand a stored seed belonged to,
    and `parse_label` takes both.
    """
    return json.dumps(
        {"brand": brand, "generic": generic, "seed": dict(seed.__dict__)},
        sort_keys=True, default=str,
    )


def seed_from_payload(raw: str) -> tuple[LabelSeed, str, str | None]:
    """``(seed, brand, generic)`` from a retained payload. Raises on anything malformed.

    Unknown keys are dropped rather than passed to the constructor: a payload retained
    before a field was added must still re-derive, and a `TypeError` deep inside a
    curation read would present a schema change as a corrupt document.
    """
    data = json.loads(raw)
    if not isinstance(data, dict) or "seed" not in data:
        raise ValueError("retained payload is not a label record")
    seed_data = data.get("seed") or {}
    known = {k: v for k, v in seed_data.items() if k in _SEED_FIELDS}
    return LabelSeed(**known), data.get("brand") or "", data.get("generic")


def _fact_mapping_complete(fact: DrugFact) -> bool:
    """True when the label resolved onto the curated catalog.

    The drug-fact analogue of every outcome row carrying a canonical id, and a statement
    of fact for the same reason: a brand the catalog knows *has been* mapped onto class
    and route. It says nothing about whether the extracted warnings are right, which is
    what verification is for.
    """
    flags = json.loads(fact.mismatch_flags) if fact.mismatch_flags else []
    if fda.FLAG_NO_CURATED_ENTRY in flags:
        return False
    return bool(fact.drug_class and fact.administration_route)


async def _current_fact(db: AsyncSession, brand: str) -> DrugFact | None:
    """The live (unsuperseded) fact for a brand, if any."""
    return (await db.execute(
        select(DrugFact).where(
            DrugFact.brand == brand,
            DrugFact.superseded_by.is_(None),
        ).order_by(DrugFact.version.desc()).limit(1)
    )).scalar_one_or_none()


async def ingest_drug_fact(
    db: AsyncSession,
    seed: LabelSeed,
    *,
    brand: str,
    generic: str | None = None,
    commit: bool = True,
) -> DrugFactOutcome:
    """Persist one brand's label as a ``DrugFact``. Never verifies its own output.

    Idempotent on ``(brand, label date)``. A **new label date supersedes** the previous
    row rather than replacing it, because the old row remains a true statement about what
    the label said then, and a claim graded against it last quarter must stay explicable.
    An **older** label never supersedes a newer one — an out-of-order retrieval is
    reported and dropped, not applied.
    """
    brand = (brand or "").strip()
    if not brand:
        raise IngestionError("brand is required to ingest a drug fact")

    label_on = fda.label_date(seed)
    fact_id = fact_id_for(brand, label_on)

    existing = (await db.execute(
        select(DrugFact).where(DrugFact.fact_id == fact_id)
    )).scalar_one_or_none()
    if existing is not None and existing.verification_status in (
        lifecycles.VERIFIED, lifecycles.REJECTED
    ):
        return DrugFactOutcome(
            brand=brand, fact_id=fact_id, action="SKIPPED",
            reason=(
                f"already {existing.verification_status}; re-ingesting would rewrite a "
                "decided row, so a label change must arrive as a new version instead"
            ),
            verification_status=existing.verification_status,
        )

    current = await _current_fact(db, brand)
    if (
        current is not None
        and current.fact_id != fact_id
        and current.label_updated_at
        and label_on
        and current.label_updated_at > label_on
    ):
        return DrugFactOutcome(
            brand=brand, fact_id=fact_id, action="SKIPPED",
            reason=(
                f"a newer label ({current.label_updated_at}) is already on record, so "
                f"this one ({label_on}) would move the brand backwards"
            ),
        )

    identifier = f"openfda:{seed.set_id or brand.lower()}"
    raw = seed_payload(seed, brand=brand, generic=generic)
    payload = await _identical_payload(db, identifier, raw)
    if payload is None:
        payload = SourcePayload.record(
            payload_id=f"SP-{uuid4().hex}",
            source_type=fda.SOURCE_TYPE,
            source_identifier=identifier,
            raw_payload=raw,
            citation=f"FDA label for {brand}" + (f" ({label_on})" if label_on else ""),
            url=seed.prescribing_information,
        )
        db.add(payload)

    fact = fda.parse_label(seed, brand=brand, fact_id=fact_id, generic=generic)
    fact.source_payload_id = payload.payload_id
    fact.verification_status = (
        lifecycles.MAPPED if _fact_mapping_complete(fact) else lifecycles.EXTRACTED
    )
    flags = tuple(json.loads(fact.mismatch_flags) if fact.mismatch_flags else [])

    action, supersedes = "INGESTED", None
    if existing is not None:
        # Same label date and undecided, so there is no history to protect — this is a
        # refresh of the same version, not a new one.
        fact.version = existing.version
        await db.delete(existing)
        await db.flush()
        action = "UPDATED"
    elif current is not None:
        # A genuinely new label. The old row stays, marked superseded.
        current.superseded_by = fact_id
        fact.version = (current.version or 1) + 1
        action, supersedes = "SUPERSEDED", current.fact_id

    db.add(fact)

    await write_audit(
        db, role="OPERATOR", event="DRUG_FACT_INGESTED",
        context={
            "fact_id": fact_id,
            "brand": brand,
            "action": action,
            "supersedes": supersedes,
            "label_updated_at": str(label_on) if label_on else None,
            "payload_id": payload.payload_id,
            "license_class": payload.license_class,
            "full_document_retained": payload.raw_payload is not None,
            "verification_status": fact.verification_status,
            "mismatch_flags": list(flags),
            # Stated because the retained payload is the normalised seed, not the SPL.
            "retained_input_is": "normalised label seed, not the source SPL document",
        },
        commit=False,
    )
    if commit:
        await db.commit()

    return DrugFactOutcome(
        brand=brand,
        action=action,
        fact_id=fact_id,
        verification_status=fact.verification_status,
        label_updated_at=str(label_on) if label_on else None,
        supersedes=supersedes,
        flags=flags,
    )


async def ingest_drug_facts(
    db: AsyncSession, brands: list[str], *, commit: bool = True,
    progress: dict | None = None,
) -> DrugFactReport:
    """Fetch and persist labels for several brands. Never raises on a source failure.

    ``fetch_label`` is the proven never-raises contract, so a brand openFDA has nothing
    for is reported ``NOT_FOUND`` and the run continues. One unavailable label must not
    cost the other three.
    """
    report = DrugFactReport()
    _tick(progress, phase="fetching", brands_total=len(brands), brands_done=0)
    for index, brand in enumerate(brands, start=1):
        brand = (brand or "").strip()
        if not brand:
            _tick(progress, brands_done=index)
            continue
        _tick(progress, current_brand=brand)
        generic = taxonomy.generic_for(brand)
        seed = await fda.fetch_label(brand, generic)
        if seed is None:
            report.facts.append(DrugFactOutcome(
                brand=brand, action="NOT_FOUND",
                reason="openFDA returned no usable label for this brand or its generic",
            ))
            _tick(progress, brands_done=index)
            continue
        report.facts.append(
            await ingest_drug_fact(db, seed, brand=brand, generic=generic, commit=False)
        )
        _tick(progress, brands_done=index)
    if commit:
        await db.commit()
    _tick(
        progress, phase="done", brands_done=len(brands), current_brand=None,
    )
    return report


async def verify_drug_fact(
    db: AsyncSession, fact_id: str, *, verified_by: str, commit: bool = True
) -> DrugFact:
    """Advance one drug fact to ``VERIFIED``, recording who decided.

    The gate three phases wait on. Until this existed, ``DrugFact.verification_status``
    could never leave ``EXTRACTED``/``MAPPED``, so every consumer that filters on
    ``VERIFIED`` returned nothing and read as "no findings" rather than "not wired".

    ``verified_by`` is **recorded, not authenticated**, exactly as for a study.
    """
    if not (verified_by or "").strip():
        raise IngestionError(
            "verified_by is required — an anonymous verification is not auditable"
        )
    fact = (await db.execute(
        select(DrugFact).where(DrugFact.fact_id == fact_id)
    )).scalar_one_or_none()
    if fact is None:
        raise IngestionError(f"unknown drug fact {fact_id!r}")
    if fact.superseded_by:
        raise IngestionError(
            f"{fact_id!r} was superseded by {fact.superseded_by!r}; verifying a label "
            "version that is no longer current would put a stale claim behind a verified "
            "flag. Verify the current version instead"
        )

    before = fact.verification_status
    if before == lifecycles.EXTRACTED:
        lifecycles.assert_transition("verification", before, lifecycles.MAPPED)
        fact.verification_status = lifecycles.MAPPED
    lifecycles.assert_transition(
        "verification", fact.verification_status, lifecycles.VERIFIED
    )
    fact.verification_status = lifecycles.VERIFIED
    fact.verified_by = verified_by.strip()
    fact.verified_at = utcnow()

    await write_audit(
        db, role="CURATOR", event="DRUG_FACT_VERIFIED",
        context={
            "fact_id": fact_id,
            "brand": fact.brand,
            "from": before,
            "to": lifecycles.VERIFIED,
            "verified_by": fact.verified_by,
            "label_updated_at": str(fact.label_updated_at) if fact.label_updated_at else None,
            "authentication": "RECORDED_NOT_AUTHENTICATED",
        },
        commit=False,
    )
    if commit:
        await db.commit()
    return fact


async def reject_drug_fact(
    db: AsyncSession, fact_id: str, *, rejected_by: str, reason: str, commit: bool = True
) -> DrugFact:
    """Record that a label extraction is wrong and must not be used. Requires a reason."""
    if not (rejected_by or "").strip():
        raise IngestionError(
            "rejected_by is required — an anonymous rejection is not auditable"
        )
    if not (reason or "").strip():
        raise IngestionError(
            "a rejection reason is required — a rejected label silently stops answering "
            "approval and safety claims, and that must be explicable"
        )
    fact = (await db.execute(
        select(DrugFact).where(DrugFact.fact_id == fact_id)
    )).scalar_one_or_none()
    if fact is None:
        raise IngestionError(f"unknown drug fact {fact_id!r}")

    before = fact.verification_status
    lifecycles.assert_transition("verification", before, lifecycles.REJECTED)
    fact.verification_status = lifecycles.REJECTED
    fact.rejection_reason = reason.strip()
    fact.verified_by = rejected_by.strip()
    fact.verified_at = utcnow()

    await write_audit(
        db, role="CURATOR", event="DRUG_FACT_REJECTED",
        context={
            "fact_id": fact_id,
            "brand": fact.brand,
            "from": before,
            "to": lifecycles.REJECTED,
            "rejected_by": fact.verified_by,
            "reason": fact.rejection_reason,
            "authentication": "RECORDED_NOT_AUTHENTICATED",
        },
        commit=False,
    )
    if commit:
        await db.commit()
    return fact
