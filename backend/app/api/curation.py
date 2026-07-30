"""Curation API — measure comparison coverage and generate the questions that are missing.

`GET /curation/coverage` is read-only and free. `POST /curation/generate` defaults to a
dry run that reports the exact number of model calls a real run would make; passing
``commit=true`` is the only thing that spends money or writes rows.

Generated candidates land in the shared review queue (`/harvest/items`) and are promoted
through the existing `/harvest/promote` and `/harvest/run-to-pipeline` routes. This router
deliberately exposes no promotion path of its own.
"""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.curation import service as svc
from app.models.database import get_db
from app.utils.logging import get_logger

logger = get_logger("api.curation")

router = APIRouter(prefix="/curation", tags=["curation"])


class CurationScope(BaseModel):
    """Which slice of the comparison matrix to work on.

    Every field is a LIST because a brand is not single-area: scoping Rinvoq to
    Rheumatology, Dermatology and Gastroenterology in one request is the normal case,
    not an edge case.
    """

    brands: list[str] | None = None
    therapeutic_areas: list[str] | None = None
    diseases: list[str] | None = None
    personas: list[str] | None = None


class CurationGenerate(CurationScope):
    limit: int = Field(20, ge=1, le=svc.MAX_CELLS_PER_RUN)
    # Explicit, and false by default: a request that does not say "commit" must not bill.
    commit: bool = False


@router.get("/coverage")
async def coverage(
    brand: list[str] | None = Query(None, description="Focus brand(s) to scope to"),
    therapeutic_area: list[str] | None = Query(None, description="Stored TA key(s) or area name(s)"),
    disease: list[str] | None = Query(None, description="Indication(s) to scope to"),
    persona: list[str] | None = Query(None, description="Prospect / Provider / Patient"),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Comparison coverage for a scope, with the ranked list of gaps."""
    return await svc.coverage_report(
        db, brands=brand, therapeutic_areas=therapeutic_area,
        diseases=disease, personas=persona, limit=limit,
    )


@router.get("/funnel")
async def funnel(
    brand: list[str] | None = Query(None, description="Focus brand(s) to scope to"),
    therapeutic_area: list[str] | None = Query(None, description="Stored TA key(s) or area name(s)"),
    disease: list[str] | None = Query(None, description="Indication(s) to scope to"),
    persona: list[str] | None = Query(None, description="Prospect / Provider / Patient"),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """How far each comparison actually got, not merely whether a question exists.

    Sits beside ``/curation/coverage`` rather than on the competitive router because it takes
    the identical scope parameters, so the frontend reuses one query builder instead of
    growing a second one that could drift from it.
    """
    return await svc.coverage_funnel(
        db, brands=brand, therapeutic_areas=therapeutic_area,
        diseases=disease, personas=persona, limit=limit,
    )


@router.post("/generate")
async def generate(data: CurationGenerate, db: AsyncSession = Depends(get_db)):
    """Generate questions for the top-ranked gaps. Dry run unless ``commit`` is true."""
    return await svc.generate(
        db,
        brands=data.brands,
        therapeutic_areas=data.therapeutic_areas,
        diseases=data.diseases,
        personas=data.personas,
        limit=data.limit,
        commit=data.commit,
    )
