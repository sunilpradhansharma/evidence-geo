"""Curator-facing study verification: re-derive from the retained source, then confirm.

``VERIFIED`` asserts that **a person checked the extraction against the source** — see
``evidence_ingestion_service.verify_study``. That is a data-accuracy judgement, not a
clinical one, which is why its audit entry is written as ``CURATOR`` while protocol
approval and network ratification are written as ``REVIEWER``. Those are different jobs
needing different people, and conflating them is how a programme waits on a physician for
work a careful curator can do today. This module gives the curator somewhere to do it.

**What a clean diff proves, and what it does not.** Re-deriving a study from its retained
payload and finding the stored rows identical proves the extraction is *reproducible*:
there is no drift between what is stored and what today's parser reads out of the same
bytes. It does **not** prove either is *correct*. A parser that misreads a denominator
misreads it the same way twice and the diff stays silent. What sends a curator to the
registry record itself is the source URL and the mismatch flags; the diff only rules out
the stored rows being separately stale.

**Verification is refused while a difference is outstanding.** ``ingest_study`` skips a
``VERIFIED`` row, so verifying a stale extraction freezes it — the ordinary re-parse can
no longer correct it and only a deliberate out-of-band reset can. Re-parse first, then
verify. Being unable to fix a row you have just certified is a worse failure than being
asked to run one more command.

Nothing here re-harvests. Every byte compared is one already on disk, so a difference is
always attributable to our parser and never to the registry having moved underneath us.
"""
from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.evidence import lifecycles, protocols
from app.evidence.sources import clinicaltrials as ctg
from app.evidence.sources.base import FetchResult
from app.models.clinical_study import ClinicalStudy
from app.models.evidence_network import EvidenceNetwork
from app.models.source_payload import SourcePayload
from app.services import comparison_service as comparisons
from app.services import evidence_ingestion_service as ingestion
from app.utils.audit import write_audit

# Differences are listed, not just counted, so a curator can see what moved. The cap keeps
# a pathological study from returning thousands of rows; the remainder is always reported
# alongside, because a count above a shorter list reads as the whole list.
DIFFERENCE_LIMIT = 200

_STUDY_FIELDS = (
    "title", "acronym", "phase", "study_design", "is_randomised", "enrollment",
    "treatment_phase", "sponsor", "start_date", "completion_date", "results_first_posted",
)
_ARM_FIELDS = (
    "treatment", "label", "is_placebo", "dose_value", "dose_unit", "dose_frequency",
    "administration_route", "sample_size",
)
_OUTCOME_FIELDS = (
    "canonical_outcome_id", "endpoint", "timepoint_week", "arm_id", "outcome_type",
    "events", "sample_size", "mean", "standard_deviation", "effect_estimate",
)


class CurationError(ValueError):
    """A curation request that cannot be answered as asked."""


def _plain(value: object) -> object:
    """JSON-safe scalar for a wire diff, without changing what the comparison means."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


async def _study(db: AsyncSession, study_id: str) -> ClinicalStudy:
    study = (await db.execute(
        select(ClinicalStudy).where(ClinicalStudy.study_id == study_id)
    )).scalar_one_or_none()
    if study is None:
        raise CurationError(f"unknown study {study_id!r}")
    return study


async def _payload(db: AsyncSession, study: ClinicalStudy) -> SourcePayload | None:
    if not study.source_payload_id:
        return None
    return (await db.execute(
        select(SourcePayload).where(SourcePayload.payload_id == study.source_payload_id)
    )).scalar_one_or_none()


def _row_differences(stored_rows, source_rows, fields, *, kind: str, id_attr: str) -> list[dict]:
    """Field-level differences between two sets of rows sharing deterministic ids.

    ``arm_id`` and ``result_id`` are derived from payload content, so re-parsing the same
    bytes reproduces them exactly. That makes an id present on only one side a genuine
    added or dropped row rather than an artefact of ordering.
    """
    stored_by = {getattr(r, id_attr): r for r in stored_rows}
    source_by = {getattr(r, id_attr): r for r in source_rows}

    out: list[dict] = []
    for row_id in sorted(set(stored_by) | set(source_by)):
        stored, source = stored_by.get(row_id), source_by.get(row_id)
        if stored is None:
            out.append({
                "kind": kind, "id": row_id, "field": "(whole row)",
                "stored": None, "source": "present",
                "note": "the source yields a row the store does not hold",
            })
            continue
        if source is None:
            out.append({
                "kind": kind, "id": row_id, "field": "(whole row)",
                "stored": "present", "source": None,
                "note": "the store holds a row the source no longer yields",
            })
            continue
        for name in fields:
            a, b = _plain(getattr(stored, name, None)), _plain(getattr(source, name, None))
            if a != b:
                out.append({
                    "kind": kind, "id": row_id, "field": name, "stored": a, "source": b,
                })
    return out


async def rederivation_diff(db: AsyncSession, study_id: str) -> dict:
    """Compare the stored rows against a fresh parse of the retained payload. Read-only.

    Deliberately not ``reparse_study``, which delegates to ``ingest_study`` and therefore
    deletes and rewrites the rows it re-derives. A curator has to be able to look before
    anything moves.
    """
    study = await _study(db, study_id)
    payload = await _payload(db, study)

    base = {
        "study_id": study.study_id,
        "registry_id": study.registry_id,
        "acronym": study.acronym,
        "title": study.title,
        "indication": study.indication,
        "verification_status": study.verification_status,
        "verified_by": study.verified_by,
        "verified_at": _plain(study.verified_at),
        "source": None,
        "checkable": False,
        "reproducible": False,
        "difference_count": 0,
        "differences": [],
        "differences_omitted": 0,
        "counts": {},
        "source_warnings": [],
        "flag_counts": {},
    }

    if payload is None:
        return {**base, "blocked_reason": (
            "no source payload on record, so there is nothing to re-derive from — this "
            "study can only be checked against the registry by hand"
        )}
    source_meta = {
        "payload_id": payload.payload_id,
        "source_type": payload.source_type,
        "source_identifier": payload.source_identifier,
        "url": payload.url,
        "retrieved_at": _plain(payload.retrieved_at),
        "checksum": payload.checksum,
        "license_class": payload.license_class,
        "retention_policy": payload.retention_policy,
    }
    base["source"] = source_meta

    if payload.raw_payload is None:
        return {**base, "blocked_reason": (
            f"no document retained ({payload.license_class} / {payload.retention_policy}) "
            "— the licence forbids keeping one, so this study can only be checked against "
            "the registry by hand"
        )}
    try:
        data = json.loads(payload.raw_payload)
    except (TypeError, ValueError) as exc:
        return {**base, "blocked_reason": f"retained payload is not valid JSON: {exc}"}

    parsed = ctg.parse(
        FetchResult(
            ok=True,
            source_type=payload.source_type,
            source_identifier=payload.source_identifier,
            payload=data,
            raw_text=payload.raw_payload,
            url=payload.url,
        ),
        # Carried over, never re-derived: the indication is the scope someone chose for
        # this row, and a re-derivation checks the extraction rather than that choice.
        indication=study.indication,
    )
    if parsed is None:
        return {**base, "blocked_reason": (
            "the retained payload no longer parses as a study record"
        )}

    differences: list[dict] = []
    for name in _STUDY_FIELDS:
        a, b = _plain(getattr(study, name, None)), _plain(getattr(parsed.study, name, None))
        if a != b:
            differences.append({
                "kind": "study", "id": study.study_id, "field": name,
                "stored": a, "source": b,
            })
    differences += _row_differences(
        study.arms, parsed.arms, _ARM_FIELDS, kind="arm", id_attr="arm_id"
    )
    differences += _row_differences(
        study.outcomes, parsed.outcomes, _OUTCOME_FIELDS,
        kind="outcome", id_attr="result_id",
    )

    stored_canonical = sum(1 for o in study.outcomes if o.canonical_outcome_id)
    source_canonical = sum(1 for o in parsed.outcomes if o.canonical_outcome_id)

    return {
        **base,
        "checkable": True,
        "reproducible": not differences,
        "difference_count": len(differences),
        "differences": differences[:DIFFERENCE_LIMIT],
        "differences_omitted": max(0, len(differences) - DIFFERENCE_LIMIT),
        "counts": {
            "arms": {"stored": len(study.arms), "source": len(parsed.arms)},
            "outcomes": {"stored": len(study.outcomes), "source": len(parsed.outcomes)},
            "canonical_outcomes": {
                "stored": stored_canonical, "source": source_canonical,
            },
        },
        # The parser's own account of what it was unsure about. This, not the diff, is
        # what tells a curator which numbers to check against the registry by eye.
        "source_warnings": list(parsed.warnings),
        "flag_counts": parsed.flag_counts,
        "blocked_reason": None,
    }


async def record_curator_check(
    db: AsyncSession,
    *,
    study_id: str,
    verified_by: str,
    note: str | None = None,
    commit: bool = True,
) -> dict:
    """Mark a study ``VERIFIED`` once its extraction reproduces from the retained source.

    The reproducibility gate is not a formality. A ``VERIFIED`` row is skipped by
    ``ingest_study``, so certifying a stale extraction puts it beyond the reach of the
    ordinary re-parse and leaves an out-of-band reset as the only remedy.
    """
    if not (verified_by or "").strip():
        raise CurationError(
            "verified_by is required — an anonymous check is not an audit trail"
        )

    diff = await rederivation_diff(db, study_id)
    if diff["blocked_reason"]:
        raise CurationError(
            f"cannot confirm {study_id!r} against its source: {diff['blocked_reason']}"
        )
    if not diff["reproducible"]:
        fields = ", ".join(
            sorted({f"{d['kind']}.{d['field']}" for d in diff["differences"]})[:5]
        )
        raise CurationError(
            f"{study_id!r} does not reproduce from its retained source "
            f"({diff['difference_count']} difference(s): {fields}). The stored rows are "
            "stale, so verifying now would freeze them beyond the reach of a re-parse. "
            "Re-parse this study first, then confirm."
        )

    study = await ingestion.verify_study(
        db, study_id, verified_by=verified_by, commit=False
    )
    # Separate from verify_study's own entry because it records something different: not
    # that a status changed, but what the check actually consisted of. "Verified" without
    # the document and checksum it was verified against is not a reviewable claim.
    await write_audit(
        db, role="CURATOR", event="STUDY_CURATION_CHECK_RECORDED",
        context={
            "study_id": study_id,
            "checked_by_recorded_not_authenticated": verified_by.strip(),
            "payload_id": (diff["source"] or {}).get("payload_id"),
            "payload_checksum": (diff["source"] or {}).get("checksum"),
            "difference_count": 0,
            "note": note,
            # Stated on the record so nobody later reads this as a clinical sign-off.
            "check_is": "extraction reproduces from the retained source document",
            "check_is_not": "a clinical or statistical review",
        },
        commit=False,
    )
    if commit:
        await db.commit()

    return {
        "study_id": study_id,
        "verification_status": study.verification_status,
        "verified_by": study.verified_by,
        "verified_at": _plain(study.verified_at),
    }


def _contribution(
    study: ClinicalStudy,
    network: EvidenceNetwork,
    window: tuple[float, float] | None,
) -> dict:
    """Could verifying this study change what the network resolves?

    Asks ``comparison_service.outcome_in_scope`` — the resolver's own rule — rather than a
    second opinion about scope. A contrast needs in-scope data on at least two arms, so a
    study with one is counted honestly as unable to contribute on its own.

    This is deliberately about *scope*, not quality: it says the row would be consulted,
    not that it is right.

    **Only refusals of rows that measure this network's outcome are reported.** A trial
    reports dozens of endpoints and almost all of them are something else; listing
    "measures HAQ-DI, not PSA_ACR50_W16" thirty times buries the one refusal a human needs
    to see. Worse, those reasons sort alphabetically ahead of "reports week 12, outside the
    approved window [14, 18]", so a truncated list showed only the noise and hid issue 1
    exactly where it bites.

    A row that measures the right outcome and is still refused is a **protocol casualty**,
    not a data error: no amount of curation fixes it, and it is a reviewer's decision.
    """
    by_arm = {a.arm_id for a in study.arms}
    in_scope_arms: set[str] = set()
    withheld_rows = 0
    reasons: set[str] = set()
    for row in study.outcomes:
        if row.arm_id is None or row.arm_id not in by_arm:
            continue
        ok, reason = comparisons.outcome_in_scope(row, network, window)
        if ok:
            in_scope_arms.add(row.arm_id)
            continue
        if row.canonical_outcome_id != network.canonical_outcome_id:
            continue
        withheld_rows += 1
        if reason:
            reasons.add(reason)
    return {
        "in_scope_arm_count": len(in_scope_arms),
        "could_contribute": len(in_scope_arms) >= 2,
        # Rows measuring exactly this outcome that were refused anyway. Non-zero here with
        # could_contribute false is the signature of a protocol problem, not a curation one.
        "withheld_row_count": withheld_rows,
        "withheld_reasons": sorted(reasons)[:3],
    }


async def curation_queue(
    db: AsyncSession,
    *,
    network_id: str | None = None,
    indication: str | None = None,
    verification_status: str | None = None,
    limit: int = 200,
) -> dict:
    """Studies awaiting a curator, optionally scoped to the ones a network is waiting on.

    ``network_id`` is the useful form: it answers *"which studies block this network"*
    rather than *"which studies exist"*, which is the difference between a finite task and
    an open-ended one.

    Scoping delegates to ``comparison_service.membership_filter`` so this queue cannot
    disagree with the resolver about which studies a resolve would consult. It did once:
    reading the empty INCLUDED set as an empty corpus, it reported that verifying these
    studies would change nothing, when verification is in fact the only thing standing
    between that network and a number.

    No diff is computed here — that re-parses a document per study and belongs on the
    per-study view, not on a list.
    """
    stmt = select(ClinicalStudy)
    scope_note = ""
    network = None
    if network_id:
        network = (await db.execute(
            select(EvidenceNetwork).where(EvidenceNetwork.network_id == network_id)
        )).scalar_one_or_none()
        if network is None:
            raise CurationError(f"unknown network {network_id!r}")

        member_ids = await comparisons.membership_filter(db, network)
        if member_ids is None:
            # Not "nothing to do". The builder proposes memberships as PROPOSED and nothing
            # promotes them, so an empty INCLUDED set is the normal state of every network
            # here. `gather_evidence` reads it as "membership narrows nothing" and consults
            # the whole indication, so that is exactly the queue a curator needs to see.
            stmt = stmt.where(ClinicalStudy.indication == network.indication)
            scope_note = (
                f"No membership has been marked INCLUDED on {network_id!r}, so a resolve "
                f"consults every {network.indication} study. This queue is that corpus."
            )
        else:
            stmt = stmt.where(ClinicalStudy.study_id.in_(member_ids))
            scope_note = (
                f"Scoped to the {len(member_ids)} INCLUDED members of {network_id!r}."
            )
    if indication:
        stmt = stmt.where(ClinicalStudy.indication == indication)
    if verification_status:
        stmt = stmt.where(ClinicalStudy.verification_status == verification_status)

    rows = list((await db.execute(stmt.limit(limit))).scalars().all())

    window = (
        protocols.approved_time_window(network.protocol_id) if network else None
    )

    by_status: dict[str, int] = {}
    studies = []
    for study in rows:
        by_status[study.verification_status] = by_status.get(study.verification_status, 0) + 1
        studies.append({
            "study_id": study.study_id,
            "registry_id": study.registry_id,
            "acronym": study.acronym,
            "title": study.title,
            "indication": study.indication,
            "verification_status": study.verification_status,
            "verified_by": study.verified_by,
            "arm_count": len(study.arms),
            "canonical_outcome_count": sum(
                1 for o in study.outcomes if o.canonical_outcome_id
            ),
            # A study with no retained document cannot be machine-checked at all, and a
            # queue that does not say so sends a curator to a screen with nothing on it.
            "has_retained_document": bool(study.source_payload_id),
            **(_contribution(study, network, window) if network else {}),
        })

    # Studies that could change the answer first, then the rest; unverified before verified
    # within each. Ordering is the whole point of a queue — "37 studies" and "11 studies that
    # could matter, then 26 that cannot" are the same list and completely different tasks.
    studies.sort(key=lambda s: (
        not s.get("could_contribute", False),
        s["verification_status"] == lifecycles.VERIFIED,
        s["study_id"],
    ))

    blocking = sum(
        1 for s in studies if s["verification_status"] != lifecycles.VERIFIED
    )
    worth_verifying = sum(
        1 for s in studies
        if s.get("could_contribute") and s["verification_status"] != lifecycles.VERIFIED
    ) if network is not None else None

    # A study holding the right outcome that still cannot contribute has been refused by
    # the protocol, not by its data. Counting these separately keeps a reviewer's decision
    # from being filed as curation backlog.
    protocol_blocked = [
        s for s in studies
        if not s.get("could_contribute") and s.get("withheld_row_count")
    ] if network is not None else []

    contribution_note = ""
    if network is not None:
        contribution_note = (
            f"Of those, {worth_verifying} report a {network.canonical_outcome_id} row "
            "in scope and could change what resolves. The remainder cannot, however "
            "carefully they are checked, so they are listed last."
        )
        if protocol_blocked:
            names = ", ".join(s["acronym"] or s["study_id"] for s in protocol_blocked[:4])
            contribution_note += (
                f" {len(protocol_blocked)} of them ({names}) do report "
                f"{network.canonical_outcome_id} but are refused on scope — see "
                "withheld_reasons. That is a protocol decision, not curation work."
            )

    return {
        "network_id": network_id,
        "total": len(studies),
        "blocking": blocking,
        # The number a curator should actually plan around. `blocking` counts everything
        # unverified; this counts only what verifying would change.
        "worth_verifying": worth_verifying,
        # Not curation work at all — these need a reviewer to rule on the protocol.
        "protocol_blocked": [s["study_id"] for s in protocol_blocked],
        "by_status": dict(sorted(by_status.items())),
        "studies": studies,
        "note": " ".join(filter(None, (
            scope_note,
            f"{blocking} of {len(studies)} studies are not yet VERIFIED. Evidence "
            "gathering skips unverified studies even in EXPLORATORY mode, so these are "
            "what stands between this network and a number.",
            contribution_note,
        ))),
    }
