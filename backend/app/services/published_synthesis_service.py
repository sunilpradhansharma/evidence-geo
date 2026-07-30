"""Governed manual-upload path for published syntheses (Phase 4).

Restricted sources — Cochrane full text, HTA reports, society abstracts — must never be
scraped. They arrive by governed upload, and this service is that path.

**Retention is enforced, not requested.** Every upload routes through
``SourcePayload.record``, which applies ``licensing.enforce``. A reviewer uploading a
paywalled Cochrane PDF therefore ends up with the extracted values, the citation, the
checksum and the page provenance — and no document. That is not a policy this service
implements; it is one it cannot bypass, because the retention decision happens inside the
model's constructor.

**A fresh upload is NOT marked ``PUBLISHED_RESULT_AVAILABLE``.** That status means a
synthesis *passed suitability* for a question, and at upload time no question has been
asked and no reviewer has checked the extraction. Uploads are stored as
``MEDICAL_REVIEW_REQUIRED`` instead, which is the accurate statement: an unreviewed
extraction of a real paper. Suitability is then judged **per question, on demand**, by
``evidence.suitability`` — never cached onto the row, for the same reason network
membership is scoped per network-and-protocol rather than being a column on the study.

Promoting a row past ``MEDICAL_REVIEW_REQUIRED`` belongs to the curation queue still owed
by Phase 3A. This service deliberately does not build a second review surface.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.evidence import statuses, suitability
from app.evidence.sources import published_nma
from app.models.nma_result import PUBLISHED, NMAResult
from app.models.source_payload import SourcePayload, checksum_of
from app.utils.audit import write_audit


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UploadRejected(ValueError):
    """The extraction is not coherent enough to store as evidence."""


async def _existing_for_checksum(
    db: AsyncSession, checksum: str | None, source_identifier: str | None
) -> NMAResult | None:
    """A prior upload of the same document, if any.

    Manual upload is a human action with a submit button, so double submission is a
    realistic failure mode. Deduplicating on the content checksum keeps a re-upload from
    creating a second, divergent copy of the same paper.
    """
    if not checksum:
        return None
    payload = (await db.execute(
        select(SourcePayload).where(
            SourcePayload.checksum == checksum,
            SourcePayload.source_identifier == (source_identifier or ""),
        )
    )).scalars().first()
    if payload is None:
        return None
    return (await db.execute(
        select(NMAResult).where(NMAResult.source_payload_id == payload.payload_id)
    )).scalars().first()


async def record_synthesis(
    db: AsyncSession,
    record: dict,
    *,
    uploaded_by: str,
    raw_document: str | None = None,
    retained_fragment: str | None = None,
    page_provenance: str | None = None,
    url: str | None = None,
    license_class: str | None = None,
) -> tuple[NMAResult, published_nma.ParsedSynthesis, SourcePayload]:
    """Store one extracted published synthesis under its licence's retention rules.

    Raises ``UploadRejected`` when the extraction has *problems* — a record with no
    indication or no effect estimates carries no evidence. Records with *flags* are stored:
    a missing included-study list is a reason the Level-2 gate will refuse the paper later,
    not a reason to throw away its citation now.
    """
    if not (uploaded_by or "").strip():
        raise UploadRejected("uploaded_by is required — an anonymous upload is not auditable")

    parsed = published_nma.parse(record)
    if parsed.problems:
        raise UploadRejected(
            "extraction cannot be stored as evidence: " + "; ".join(parsed.problems)
        )

    checksum = checksum_of(raw_document if raw_document is not None else retained_fragment)
    existing = await _existing_for_checksum(db, checksum, parsed.source_identifier)
    if existing is not None:
        payload = (await db.execute(
            select(SourcePayload).where(SourcePayload.payload_id == existing.source_payload_id)
        )).scalar_one()
        return existing, parsed, payload

    payload = SourcePayload.record(
        payload_id=f"SP-{uuid4().hex}",
        source_type=parsed.source_type,
        source_identifier=parsed.source_identifier or "",
        raw_payload=raw_document,
        retained_fragment=retained_fragment or json.dumps(record, default=str),
        license_class=license_class,
        citation=parsed.citation,
        url=url,
        page_provenance=page_provenance,
        uploaded_by=uploaded_by.strip(),
    )
    db.add(payload)

    result = published_nma.to_nma_result(
        parsed,
        result_id=f"PNMA-{uuid4().hex}",
        # Accurate at upload time: a real paper, an unreviewed reading of it, no question
        # asked yet. See the module docstring for why this is not PUBLISHED_RESULT_AVAILABLE.
        status=statuses.MEDICAL_REVIEW_REQUIRED,
        source_payload_id=payload.payload_id,
    )
    db.add(result)

    await write_audit(
        db, role="CURATOR", event="PUBLISHED_SYNTHESIS_UPLOADED",
        context={
            "result_id": result.result_id,
            "payload_id": payload.payload_id,
            "source_type": parsed.source_type,
            "license_class": payload.license_class,
            "retention_policy": payload.retention_policy,
            "full_document_retained": payload.raw_payload is not None,
            "dropped_fields": payload.dropped_fields,
            "indication": parsed.indication,
            "canonical_outcome_id": parsed.canonical_outcome_id,
            "treatments": list(parsed.treatments),
            "extraction_flags": list(parsed.flags),
            "uploaded_by": uploaded_by.strip(),
        },
        commit=False,
    )
    await db.commit()
    return result, parsed, payload


def _parsed_from_row(
    row: NMAResult, *, source_type: str = "MANUAL_UPLOAD"
) -> published_nma.ParsedSynthesis:
    """Rebuild a ``ParsedSynthesis`` from a stored row, for suitability assessment.

    Reads back through the same normalisation the adapter applied, so a stored row is
    judged by exactly the rules a fresh upload would be. *source_type* is passed in from
    the row's ``SourcePayload`` rather than guessed — ``NMAResult`` has no such column, and
    inventing one from whether a citation happens to be present would be a fabrication.
    """
    estimates = json.loads(row.estimates) if row.estimates else []
    scores = json.loads(row.sucra) if row.sucra else {}
    rankings = json.loads(row.rankings) if row.rankings else {}
    studies = tuple(json.loads(row.included_studies)) if row.included_studies else ()

    contrasts = tuple(
        published_nma.Contrast(
            treatment=e.get("treatment", ""),
            comparator=e.get("comparator", ""),
            effect_measure=e.get("effect_measure"),
            estimate=e.get("estimate"),
            interval_lower=e.get("interval_lower"),
            interval_upper=e.get("interval_upper"),
            interval_type=e.get("interval_type"),
            treatment_label=e.get("treatment_label"),
            comparator_label=e.get("comparator_label"),
            flags=tuple(e.get("flags") or ()),
        )
        for e in estimates
    )
    nodes = {c.treatment for c in contrasts} | {c.comparator for c in contrasts}
    nodes.discard("")

    return published_nma.ParsedSynthesis(
        source_type=source_type,
        source_identifier=row.source_payload_id,
        citation=row.citation,
        publication_date=row.publication_date,
        funding_source=row.funding_source,
        indication=row.indication,
        endpoint=row.endpoint,
        canonical_outcome_id=row.canonical_outcome_id,
        timepoint_week=row.timepoint_week,
        population_stratum=row.population_stratum,
        treatment_phase=row.treatment_phase,
        treatments=tuple(sorted(nodes)),
        contrasts=contrasts,
        effect_measure=row.effect_measure,
        model_type=row.model_type,
        ranking_metric=rankings.get("metric"),
        ranking_scores=scores,
        tau_squared=row.tau_squared,
        q_statistic=row.q_statistic,
        degrees_freedom=row.degrees_freedom,
        heterogeneity_note=row.heterogeneity_note,
        inconsistency=json.loads(row.inconsistency) if row.inconsistency else None,
        grade_certainty=row.grade_certainty,
        included_studies=studies,
        included_studies_recoverable=bool(row.included_studies_recoverable),
    )


def treatments_in(row: NMAResult) -> tuple[str, ...]:
    """The treatment nodes of one stored published synthesis.

    Public so competitor discovery can ask which treatments a paper covered without parsing
    ``estimates`` itself. One interpretation of the stored league-table shape: a second
    reading would eventually disagree about a paper's node set, and discovery would then
    propose a competitor no synthesis actually contains.
    """
    return _parsed_from_row(row).treatments


async def _source_types_for(db: AsyncSession, rows: list[NMAResult]) -> dict[str, str]:
    """``{payload_id: source_type}`` for these rows, in one query rather than N."""
    ids = [r.source_payload_id for r in rows if r.source_payload_id]
    if not ids:
        return {}
    payloads = (await db.execute(
        select(SourcePayload.payload_id, SourcePayload.source_type).where(
            SourcePayload.payload_id.in_(ids)
        )
    )).all()
    return {payload_id: source_type for payload_id, source_type in payloads}


async def list_syntheses(
    db: AsyncSession, *, indication: str | None = None, limit: int = 100
) -> list[NMAResult]:
    """Stored published syntheses, most recently published first."""
    query = select(NMAResult).where(NMAResult.source == PUBLISHED)
    if indication:
        query = query.where(NMAResult.indication == indication)
    query = query.order_by(NMAResult.publication_date.desc().nullslast()).limit(limit)
    return list((await db.execute(query)).scalars().all())


async def assess_for_question(
    db: AsyncSession,
    *,
    indication: str,
    treatment_a: str,
    treatment_b: str,
    canonical_outcome_id: str | None = None,
    population_stratum: str | None = None,
    treatment_phase: str = "PRIMARY",
    protocol_id: str | None = None,
    requested_dose: str | None = None,
    max_age_years: int | None = suitability.DEFAULT_MAX_AGE_YEARS,
    as_of: date | None = None,
) -> dict:
    """The Level-2 answer for one comparison, with the reasons behind it.

    Returns the closest miss and its failed dimensions when nothing is suitable, so a
    consumer falling through to Level 3 can still surface *"a paper exists, here is why it
    does not fit"* rather than implying none was found.
    """
    request = suitability.ComparisonRequest(
        indication=indication,
        treatment_a=treatment_a,
        treatment_b=treatment_b,
        canonical_outcome_id=canonical_outcome_id,
        population_stratum=population_stratum,
        treatment_phase=treatment_phase,
        protocol_id=protocol_id,
        requested_dose=requested_dose,
        as_of=as_of,
    )
    rows = await list_syntheses(db, indication=indication, limit=500)
    source_types = await _source_types_for(db, rows)
    pairs = [
        (_parsed_from_row(row, source_type=source_types.get(row.source_payload_id or "",
                                                           "MANUAL_UPLOAD")), row)
        for row in rows
    ]

    chosen, decision = suitability.best_of(
        [p for p, _ in pairs], request, max_age_years=max_age_years
    )
    # Matched by identity, not by citation: two papers can both have a null citation, and
    # a dict keyed on that would silently return the wrong row.
    row = next((r for p, r in pairs if p is chosen), None) if chosen is not None else None

    return {
        "status": decision.status,
        "suitable": decision.suitable,
        "reason": decision.reason_text,
        "failed_dimensions": list(decision.failed_dimensions),
        "candidates_considered": len(pairs),
        "result_id": row.result_id if row is not None else None,
        "citation": chosen.citation if chosen is not None else None,
        "publication_date": chosen.publication_date if chosen is not None else None,
        "grade_certainty": chosen.grade_certainty if chosen is not None else None,
        "estimate": (
            decision.matched_contrast.as_dict()
            if decision.suitable and decision.matched_contrast is not None
            else None
        ),
        "describes": statuses.describe(decision.status),
    }
