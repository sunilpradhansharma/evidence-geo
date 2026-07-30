"""Curator-facing drug-fact verification: re-derive from the retained label, then confirm.

The drug-fact half of the curation surface. ``study_curation_service`` had existed for a
while and this did not, which is why ``DrugFact.verification_status`` could never leave
``EXTRACTED``/``MAPPED`` — and three consumers filter on ``VERIFIED``, so the effect was
not an error anywhere but an empty result everywhere:

* Phase 7's drug-fact and safety question categories generated nothing.
* Phase 8's ``APPROVAL_CLAIM``, ``SAFETY_WARNING_CLAIM`` and ``MECHANISM_CLAIM`` resolved
  no authority and graded as evidence-unavailable.
* Phase 9's ``AI_MISINFORMATION_RISK`` — the highest severity the engine carries — was
  unreachable.

**What a clean diff proves here is narrower than for a study, and the difference matters.**
A study's retained payload is the registry's own JSON, so re-deriving exercises the whole
parse. A label's retained payload is the normalised ``LabelSeed`` — the input to
``parse_label`` — because that is what the fetcher returns; the SPL document itself is
never held. So a reproducible drug fact proves **our mapping of the label reproduces**, not
that the label was read correctly in the first place. The response therefore leads with the
prescribing-information URL: that, not the diff, is what a curator actually checks against.

Nothing here re-fetches. Every byte compared is already on disk, so a difference is always
attributable to our mapping and never to the FDA having republished underneath us.
"""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.evidence import lifecycles
from app.evidence.sources import openfda_facts as fda
from app.models.drug_fact import DrugFact
from app.models.source_payload import SourcePayload
from app.services import evidence_ingestion_service as ingestion
# One curation vocabulary, so an API layer maps one error type rather than two that mean
# the same thing.
from app.services.study_curation_service import DIFFERENCE_LIMIT, CurationError, _plain
from app.utils.audit import write_audit

# Every field `parse_label` populates. `extraction_rationale` is included because it is
# derived deterministically from the seed, so a change in it is a real change in what we
# read — not noise.
_FACT_FIELDS = (
    "brand", "generic", "molecule", "manufacturer", "drug_class", "administration_route",
    "dosage_form", "approved_indications", "label_updated_at", "boxed_warnings",
    "has_boxed_warning", "regulatory_source", "prescribing_information",
    "extraction_confidence", "extraction_rationale", "mismatch_flags",
)


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


async def _fact(db: AsyncSession, fact_id: str) -> DrugFact:
    fact = (await db.execute(
        select(DrugFact).where(DrugFact.fact_id == fact_id)
    )).scalar_one_or_none()
    if fact is None:
        raise CurationError(f"unknown drug fact {fact_id!r}")
    return fact


async def _payload(db: AsyncSession, fact: DrugFact) -> SourcePayload | None:
    if not fact.source_payload_id:
        return None
    return (await db.execute(
        select(SourcePayload).where(SourcePayload.payload_id == fact.source_payload_id)
    )).scalar_one_or_none()


def contribution(fact: DrugFact) -> dict:
    """Which claims and questions this fact could answer once verified.

    The drug-fact analogue of the study queue's ``could_contribute``, and it exists for the
    same reason: *"12 unverified facts"* and *"3 that would change an answer"* are the same
    list and completely different tasks.

    **Approval is the one worth reading.** ``openfda_facts`` deliberately does not structure
    the indications prose — it flags ``INDICATIONS_TEXT_NOT_STRUCTURED`` and leaves the list
    empty, because a half-parsed regulatory list is worse than an absent one. So
    ``drug_fact_question`` refuses, and an approval claim has no authority to grade against,
    **even after a curator verifies the row**. Verifying does not fix it; structuring the
    indications does, and that is Phase 3A pipeline work. Saying so here stops a curator
    spending an afternoon on a row that cannot answer the question they were asked about.
    """
    indications = _json_list(fact.approved_indications)
    warnings = _json_list(fact.boxed_warnings)
    contradicts_itself = bool(warnings and not fact.has_boxed_warning)

    answers_approval = bool(indications)
    # `has_boxed_warning` is a stored boolean, so the safety question is answerable either
    # way — "no boxed warning" is as much a claim as "yes".
    answers_safety = not contradicts_itself
    answers_mechanism = bool(fact.drug_class)

    blockers: list[str] = []
    if not answers_approval:
        blockers.append(
            "no approved-indication list was extracted, so an approval claim has nothing "
            "to grade against — this needs the label's indications prose structured, "
            "which verification does not do"
        )
    if contradicts_itself:
        blockers.append(
            "boxed-warning text is present while has_boxed_warning is false; the row "
            "contradicts itself and must be corrected before it can answer a safety claim"
        )
    if not answers_mechanism:
        blockers.append("no drug class, so a mechanism claim has no authority")

    return {
        "answers_approval_claim": answers_approval,
        "answers_safety_claim": answers_safety,
        "answers_mechanism_claim": answers_mechanism,
        "could_contribute": answers_approval or answers_safety or answers_mechanism,
        "blockers": blockers,
    }


async def rederivation_diff(db: AsyncSession, fact_id: str) -> dict:
    """Compare the stored fact against a fresh parse of the retained label seed. Read-only.

    Deliberately not a re-fetch: comparing against openFDA today would conflate "our stored
    row drifted" with "the FDA republished the label", and only the first is a curation
    problem. A republished label arrives through ingestion as a new version.
    """
    fact = await _fact(db, fact_id)
    payload = await _payload(db, fact)

    base = {
        "fact_id": fact.fact_id,
        "brand": fact.brand,
        "generic": fact.generic,
        "label_updated_at": _plain(fact.label_updated_at),
        "version": fact.version,
        "superseded_by": fact.superseded_by,
        "verification_status": fact.verification_status,
        "verified_by": fact.verified_by,
        "verified_at": _plain(fact.verified_at),
        # The URL is the headline, not the diff. This is the thing a curator opens.
        "prescribing_information": fact.prescribing_information,
        "mismatch_flags": _json_list(fact.mismatch_flags),
        "source": None,
        "checkable": False,
        "reproducible": False,
        "difference_count": 0,
        "differences": [],
        "differences_omitted": 0,
        **contribution(fact),
        "checks": "our mapping of the retained label seed",
        "does_not_check": "that the label itself was read correctly — open the source URL",
    }

    if payload is None:
        return {**base, "blocked_reason": (
            "no source payload on record, so there is nothing to re-derive from — this "
            "label can only be checked by hand"
        )}
    base["source"] = {
        "payload_id": payload.payload_id,
        "source_type": payload.source_type,
        "source_identifier": payload.source_identifier,
        "url": payload.url,
        "retrieved_at": _plain(payload.retrieved_at),
        "checksum": payload.checksum,
        "license_class": payload.license_class,
        "retention_policy": payload.retention_policy,
    }

    if payload.raw_payload is None:
        return {**base, "blocked_reason": (
            f"no document retained ({payload.license_class} / {payload.retention_policy})"
        )}
    try:
        seed, brand, generic = ingestion.seed_from_payload(payload.raw_payload)
    except (TypeError, ValueError) as exc:
        return {**base, "blocked_reason": f"retained payload is not a label record: {exc}"}

    rederived = fda.parse_label(
        seed, brand=brand or fact.brand, fact_id=fact.fact_id, generic=generic
    )

    differences: list[dict] = []
    for name in _FACT_FIELDS:
        stored = _plain(getattr(fact, name, None))
        source = _plain(getattr(rederived, name, None))
        if stored != source:
            differences.append({
                "kind": "drug_fact", "id": fact.fact_id, "field": name,
                "stored": stored, "source": source,
            })

    return {
        **base,
        "checkable": True,
        "reproducible": not differences,
        "difference_count": len(differences),
        "differences": differences[:DIFFERENCE_LIMIT],
        "differences_omitted": max(0, len(differences) - DIFFERENCE_LIMIT),
        "blocked_reason": None,
    }


async def record_curator_check(
    db: AsyncSession,
    *,
    fact_id: str,
    verified_by: str,
    note: str | None = None,
    commit: bool = True,
) -> dict:
    """Mark a drug fact ``VERIFIED`` once its mapping reproduces from the retained label.

    Refused while a difference is outstanding, for the same reason a study is: a decided
    row is skipped by ingestion, so certifying a stale mapping puts it beyond the reach of
    the ordinary re-ingest.
    """
    if not (verified_by or "").strip():
        raise CurationError(
            "verified_by is required — an anonymous check is not an audit trail"
        )

    diff = await rederivation_diff(db, fact_id)
    if diff["blocked_reason"]:
        raise CurationError(
            f"cannot confirm {fact_id!r} against its source: {diff['blocked_reason']}"
        )
    if not diff["reproducible"]:
        fields = ", ".join(sorted({d["field"] for d in diff["differences"]})[:5])
        raise CurationError(
            f"{fact_id!r} does not reproduce from its retained label "
            f"({diff['difference_count']} difference(s): {fields}). Re-ingest this brand "
            "first, then confirm."
        )

    fact = await ingestion.verify_drug_fact(
        db, fact_id, verified_by=verified_by, commit=False
    )
    await write_audit(
        db, role="CURATOR", event="DRUG_FACT_CURATION_CHECK_RECORDED",
        context={
            "fact_id": fact_id,
            "brand": fact.brand,
            "checked_by_recorded_not_authenticated": verified_by.strip(),
            "payload_id": (diff["source"] or {}).get("payload_id"),
            "payload_checksum": (diff["source"] or {}).get("checksum"),
            "difference_count": 0,
            "note": note,
            "check_is": "our mapping of the retained label seed reproduces",
            "check_is_not": "a reading of the source SPL, nor a clinical review",
        },
        commit=False,
    )
    if commit:
        await db.commit()

    return {
        "fact_id": fact_id,
        "brand": fact.brand,
        "verification_status": fact.verification_status,
        "verified_by": fact.verified_by,
        "verified_at": _plain(fact.verified_at),
    }


async def curation_queue(
    db: AsyncSession,
    *,
    brand: str | None = None,
    verification_status: str | None = None,
    include_superseded: bool = False,
    limit: int = 200,
) -> dict:
    """Drug facts awaiting a curator, ranked by whether verifying one changes an answer.

    Superseded versions are excluded by default and cannot be verified at all: certifying
    a label version that is no longer current would put a stale claim behind a verified
    flag. They remain readable, because the claim graded against one last quarter still
    has to be explicable.
    """
    stmt = select(DrugFact)
    if brand:
        stmt = stmt.where(DrugFact.brand == brand)
    if verification_status:
        stmt = stmt.where(DrugFact.verification_status == verification_status)
    if not include_superseded:
        stmt = stmt.where(DrugFact.superseded_by.is_(None))

    rows = list((await db.execute(stmt.limit(limit))).scalars().all())

    by_status: dict[str, int] = {}
    facts = []
    for fact in rows:
        by_status[fact.verification_status] = by_status.get(fact.verification_status, 0) + 1
        facts.append({
            "fact_id": fact.fact_id,
            "brand": fact.brand,
            "generic": fact.generic,
            "label_updated_at": _plain(fact.label_updated_at),
            "version": fact.version,
            "superseded_by": fact.superseded_by,
            "verification_status": fact.verification_status,
            "verified_by": fact.verified_by,
            "has_boxed_warning": bool(fact.has_boxed_warning),
            "mismatch_flags": _json_list(fact.mismatch_flags),
            "prescribing_information": fact.prescribing_information,
            **contribution(fact),
        })

    facts.sort(key=lambda f: (
        not f["could_contribute"],
        f["verification_status"] == lifecycles.VERIFIED,
        f["brand"],
    ))

    blocking = sum(
        1 for f in facts if f["verification_status"] != lifecycles.VERIFIED
    )
    worth_verifying = sum(
        1 for f in facts
        if f["could_contribute"] and f["verification_status"] != lifecycles.VERIFIED
        and not f["superseded_by"]
    )
    # Not curation work: no amount of checking makes these answer an approval claim.
    approval_blocked = [
        f["fact_id"] for f in facts if not f["answers_approval_claim"]
    ]

    return {
        "total": len(facts),
        "blocking": blocking,
        "worth_verifying": worth_verifying,
        "approval_blocked": approval_blocked,
        "by_verification_status": by_status,
        "facts": facts,
        "note": (
            f"{blocking} of {len(facts)} drug facts are not yet VERIFIED. Question "
            "generation and every approval, safety and mechanism claim read verified "
            "labels only, so these are what stands between the store and a graded claim."
            + (
                f" {len(approval_blocked)} carry no extracted indication list and cannot "
                "answer an approval claim however carefully they are verified — that "
                "needs the label's indications prose structured, not a curator."
                if approval_blocked else ""
            )
        ),
    }
