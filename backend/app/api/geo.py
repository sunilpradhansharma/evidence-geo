"""GEO Schema Data API — serves llms.txt and JSON-LD brand schemas."""
from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from app.geo import builder, loader
from app.geo.loader import get_brand_schema, get_llms_txt, list_available_brands

router = APIRouter(prefix="/geo", tags=["geo"])


@router.get("/llms.txt")
async def serve_llms_txt():
    """Serve the machine-readable llms.txt content."""
    content = get_llms_txt()
    if not content:
        raise HTTPException(404, "llms.txt not found")
    return PlainTextResponse(content, media_type="text/plain")


@router.get("/schema/{brand}")
async def serve_brand_schema(brand: str):
    """Serve JSON-LD schema for a specific brand."""
    schema = get_brand_schema(brand)
    if schema is None:
        raise HTTPException(404, f"No GEO schema found for brand: {brand}")
    return schema


@router.get("/brands")
async def list_brands():
    """List all brands with available GEO schema data."""
    return {"brands": list_available_brands()}


@router.post("/refresh")
async def refresh_geo(seed: bool = True, brand: str | None = None):
    """Regenerate the corpus from curated YAML (optionally seeding label fields from
    openFDA), then reload the in-memory cache so the change is live without a restart."""
    report, _docs = await builder.generate(seed=seed, only_brand=brand)
    loader.reload()
    return {
        "ok": report.ok,
        "seeded": seed,
        "brand": brand,
        "llms_txt_written": report.llms_txt_written,
        "available_brands": list_available_brands(),
        "brands": [
            {
                "brand": b.brand,
                "file": b.file,
                "valid": b.valid,
                "seeded_fields": b.seeded_fields,
                "label_source": b.label_source,
                "error": b.error,
            }
            for b in report.brands
        ],
    }
