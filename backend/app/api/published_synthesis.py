"""Published synthesis API — governed upload and the Level-2 answer (Phase 4).

Restricted sources are **never scraped**. Cochrane full text, HTA reports and society
abstracts arrive through ``POST /published-syntheses``, and what the platform keeps is
decided by the source's licence class, not by the uploader. A paywalled PDF submitted here
yields the extracted values, the citation, the checksum and the page provenance — and no
document. The response reports exactly what was dropped, so a reviewer can see that the
absence was a licence decision rather than a failed upload.

``GET /published-syntheses/assess`` is the Level-2 gate. It returns the *reasons* a paper
does not fit, not just a verdict, so a consumer falling through to Level 3 can still say
"a synthesis exists, here is why it was not used".
"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.evidence import suitability
from app.models.database import get_db
from app.services import published_synthesis_service as svc

router = APIRouter(prefix="/published-syntheses", tags=["published-syntheses"])


class SynthesisUploadIn(BaseModel):
    """One extracted published synthesis, plus what the uploader is allowed to send."""

    extraction: dict = Field(
        description="Normalised extraction: indication, endpoint, league_table, "
                    "included_studies, SUCRA/P-score, GRADE, heterogeneity, citation."
    )
    uploaded_by: str
    raw_document: str | None = Field(
        default=None,
        description="Full source text. RETAINED ONLY where the licence permits — for a "
                    "restricted source this is dropped, deliberately and audibly.",
    )
    retained_fragment: str | None = Field(
        default=None,
        description="The specific passage the values came from. Retained at every tier.",
    )
    page_provenance: str | None = Field(
        default=None, description="Where in the document, e.g. 'Table 3, p. 14'."
    )
    url: str | None = None
    license_class: str | None = Field(
        default=None,
        description="Override the source-type default, e.g. a PMC article known to be in "
                    "the OA subset. An unrecognised value still resolves to FRAGMENT_ONLY.",
    )


@router.post("")
async def upload_synthesis(body: SynthesisUploadIn, db: AsyncSession = Depends(get_db)):
    """Store one published synthesis under its licence's retention rules.

    Rejects an extraction with *problems* (no indication, no effect estimates) as 400.
    Stores one with *flags* — a missing included-study list is a reason the Level-2 gate
    will refuse the paper later, not a reason to discard its citation now.
    """
    try:
        result, parsed, payload = await svc.record_synthesis(
            db, body.extraction,
            uploaded_by=body.uploaded_by,
            raw_document=body.raw_document,
            retained_fragment=body.retained_fragment,
            page_provenance=body.page_provenance,
            url=body.url,
            license_class=body.license_class,
        )
    except svc.UploadRejected as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return {
        "result_id": result.result_id,
        "status": result.status,
        "indication": result.indication,
        "canonical_outcome_id": result.canonical_outcome_id,
        "treatments": list(parsed.treatments),
        "extraction_flags": list(parsed.flags),
        "included_studies_recoverable": result.included_studies_recoverable,
        "retention": {
            "license_class": payload.license_class,
            "retention_policy": payload.retention_policy,
            "full_document_retained": payload.raw_payload is not None,
            "dropped_fields": payload.dropped_fields,
            "checksum": payload.checksum,
            "page_provenance": payload.page_provenance,
            "expires_at": payload.expires_at,
        },
    }


@router.get("")
async def list_syntheses(
    indication: str | None = None, limit: int = 100, db: AsyncSession = Depends(get_db)
):
    """Stored published syntheses, most recently published first."""
    rows = await svc.list_syntheses(db, indication=indication, limit=limit)
    return [
        {
            "result_id": r.result_id,
            "status": r.status,
            "indication": r.indication,
            "canonical_outcome_id": r.canonical_outcome_id,
            "endpoint": r.endpoint,
            "timepoint_week": r.timepoint_week,
            "treatment_phase": r.treatment_phase,
            "citation": r.citation,
            "publication_date": r.publication_date,
            "grade_certainty": r.grade_certainty,
            "funding_source": r.funding_source,
            "included_studies_recoverable": r.included_studies_recoverable,
            "source_is_citable": r.source_is_citable,
            "claim_is_approved_for_external_use": r.claim_is_approved_for_external_use,
        }
        for r in rows
    ]


@router.get("/assess")
async def assess(
    indication: str,
    treatment_a: str,
    treatment_b: str,
    canonical_outcome_id: str | None = None,
    population_stratum: str | None = None,
    treatment_phase: str = "PRIMARY",
    protocol_id: str | None = None,
    requested_dose: str | None = None,
    max_age_years: int | None = Query(
        default=suitability.DEFAULT_MAX_AGE_YEARS,
        description="Omit or pass null to disable the recency gate. Age is a proxy for "
                    "'does this include the current evidence base'; the real check is "
                    "study overlap against an internal network.",
    ),
    as_of: date | None = None,
    db: AsyncSession = Depends(get_db),
):
    """The Level-2 answer for one comparison, with every failed dimension listed.

    A synthesis containing both treatments can still be unsuitable — that is the normal
    case, not an edge case, so the reasons are always returned.
    """
    return await svc.assess_for_question(
        db,
        indication=indication,
        treatment_a=treatment_a,
        treatment_b=treatment_b,
        canonical_outcome_id=canonical_outcome_id,
        population_stratum=population_stratum,
        treatment_phase=treatment_phase,
        protocol_id=protocol_id,
        requested_dose=requested_dose,
        max_age_years=max_age_years,
        as_of=as_of,
    )
