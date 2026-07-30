"""Read the brand taxonomy: the hierarchy the UI renders, its health, and a YAML export.

The taxonomy moved out of ``brands.yaml`` and into SQLite. Two consequences this router
exists to serve:

* **The frontend had its own hardcoded copy.** ``frontend/src/lib/taxonomy.ts`` restated the
  areas, brands, diseases and competitors by hand, so adding a brand meant editing two
  places and the two could disagree — and did. ``GET /taxonomy`` serves the shapes that file
  used to hardcode, from the one source.
* **Retiring the file removed the reviewable diff.** ``GET /taxonomy/export.yaml`` renders
  the live taxonomy so it can be diffed against the seed baseline and committed back.

The one write path is ``POST /taxonomy/brands``, which adds a brand from the UI. It is
guarded so that a modal cannot do what a reviewed commit could: the model only suggests, the
analyst ticks, and the resulting configuration has to pass the same validation the
application runs at startup or nothing is written at all.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import taxonomy as tx
from app.models.database import get_db
from app.services import brand_authoring_service as authoring
from app.services import brand_draft_llm as drafts
from app.services import brand_taxonomy_service as store

router = APIRouter(prefix="/taxonomy", tags=["taxonomy"])


class BrandResolveRequest(BaseModel):
    name: str


class CompetitorDraftRequest(BaseModel):
    brand: str
    disease: str


class OutcomeDraftRequest(BaseModel):
    disease: str


class CompetitorPick(BaseModel):
    """A competitor the analyst ticked. Nothing arrives here unconfirmed."""

    name: str
    # The model's stated reason, kept so a competitive field curated by inclusion stays
    # reviewable. Recorded on the membership row, never read by any rule.
    note: str | None = None


class DiseasePick(BaseModel):
    """One indication the brand is being added to.

    ``area``, ``therapeutic_area_key`` and ``canonical_outcomes`` are only read when the
    disease does not already exist; for an existing one the stored values win.
    """

    disease: str
    competitors: list[CompetitorPick] = Field(default_factory=list)
    area: str | None = None
    therapeutic_area_key: str | None = None
    canonical_outcomes: list[str] = Field(default_factory=list)


class NewTherapeuticArea(BaseModel):
    """A therapeutic-area block this addition creates.

    Only ever accepted alongside the brand being filed into it. A block created on its own
    would show up as a selectable option in every therapeutic-area filter in the application
    with nothing behind it, which reads as "no data" rather than "nothing here yet".
    """

    # The value stored on ``Question.therapeutic_area`` and filtered on.
    ta_key: str
    # The broader display rollup. Equal to ``ta_key`` unless several keys share one heading,
    # the way Endometriosis and Uterine Fibroids both sit under Women's Health.
    area: str


class BrandCreateRequest(BaseModel):
    name: str
    # Optional because ``new_therapeutic_area`` supplies it when an area is being created.
    # One of the two must be present; the write refuses an addition with neither.
    therapeutic_area_key: str = ""
    new_therapeutic_area: NewTherapeuticArea | None = None
    diseases: list[DiseasePick] = Field(default_factory=list)

    generic: str | None = None
    company: str | None = None
    drug_class: str | None = None
    administration_route: str | None = None
    aliases: list[str] = Field(default_factory=list)

    # Accepted so a client can be explicit, but only "standard" passes validation — a brand
    # added through the modal cannot enrol itself in trial ingestion.
    evidence_depth: str | None = None

    # Recorded, not authenticated. There is no RBAC in this tree; the audit row says who
    # claimed to be making the change, which is the same caveat protocol approval carries.
    reviewer: str = "unknown"


# Taxonomy problems found by the startup check, written by ``main._validate_configuration``.
# Lives here rather than in ``app.main`` so this router does not have to import the
# application that imports it; the dependency runs one way.
STARTUP_ERRORS: list[str] = []


def record_startup_errors(errors: list[str]) -> None:
    """Publish the startup validation result. Empty is the healthy state."""
    STARTUP_ERRORS.clear()
    STARTUP_ERRORS.extend(errors)


def _hierarchy() -> list[dict]:
    """Area -> indication -> {diseases, brands, competitors}, in declaration order.

    Three genuinely distinct levels, which is why this cannot be flattened:
    ``area`` is the display rollup, ``indication`` is the stored ``therapeutic_area`` value
    a question actually carries, and ``diseases`` are the granular conditions treated under
    it. Several areas hold one indication of the same name (Dermatology); Women's Health
    holds two with different names.
    """
    cfg = tx.config().get("therapeutic_areas") or {}
    by_area: dict[str, list[dict]] = {}
    order: list[str] = []

    for ta_key, block in cfg.items():
        block = block or {}
        area = block.get("area") or ta_key
        if area not in by_area:
            by_area[area] = []
            order.append(area)

        diseases: list[str] = []
        for brand in block.get("focus_brands") or []:
            for disease in (brand or {}).get("indications") or []:
                if disease not in diseases:
                    diseases.append(disease)

        by_area[area].append({
            "label": ta_key,
            "taKey": ta_key,
            "diseases": diseases,
            "brands": [
                b["name"] for b in block.get("focus_brands") or [] if b.get("name")
            ],
            "competitors": [
                c["name"] for c in block.get("competitors") or [] if c.get("name")
            ],
        })

    return [{"area": area, "indications": by_area[area]} for area in order]


@router.get("")
async def read_taxonomy():
    """Everything the UI needs to render its pickers, from the single source of truth."""
    areas = _hierarchy()
    diseases = list(tx.diseases())
    return {
        "areas": areas,
        "area_options": [a["area"] for a in areas],
        # Every monitored brand. Correct for filtering stored ``brand_focus`` values, because
        # a run may exist for any of them.
        "brand_options": sorted({
            brand for a in areas for i in a["indications"] for brand in i["brands"]
        }),
        # Brands that can actually produce a comparison cell — i.e. those named by an entry
        # in the disease overlay, which is what ``curation.coverage.build_matrix`` iterates.
        #
        # Deliberately NOT the same list. A therapeutic area with no overlay entries (Obesity)
        # declares focus brands that no comparison is defined for, and offering them on the
        # coverage filter reproduces a bug this taxonomy already fixed once: a brand sitting
        # in the picker returning "no gaps", which reads as full coverage rather than as no
        # coverage having been defined.
        "coverage_brand_options": sorted({
            brand for d in diseases for brand in tx.brands_for_disease(d)
        }),
        "disease_options": diseases,
        # Which focus brands are actually indicated in each disease. This is what stops the
        # matrix inventing a comparison the taxonomy never asserted.
        "disease_brand_map": {d: list(tx.brands_for_disease(d)) for d in diseases},
        "disease_competitor_map": {d: list(tx.competitors_for_disease(d)) for d in diseases},
        # Endpoints are model-drafted and unverified for these, so the evidence programme
        # refuses them. Surfaced so the UI can say so rather than showing them as equal.
        "draft_diseases": list(tx.draft_diseases()),
        # The closed vocabularies, served rather than restated in the client. A picker
        # offering a value this rejects is the same drift that made the frontend's hardcoded
        # copy wrong; there is no reason to reintroduce it for a six-item list.
        "administration_routes": list(tx.ADMINISTRATION_ROUTES),
        "evidence_depths": list(tx.EVIDENCE_DEPTHS),
    }


@router.get("/status")
async def taxonomy_status():
    """Counts, draft indications, and any configuration problems.

    Problems are reported rather than fatal. The taxonomy is writable at runtime now, so a
    bad row that refused to boot would also take down the surface needed to fix it.
    """
    cfg = tx.config()
    areas = cfg.get("therapeutic_areas") or {}
    return {
        "therapeutic_areas": len(areas),
        "indications": len(cfg.get("indications") or {}),
        "drug_catalog": len(cfg.get("drug_catalog") or {}),
        "curated_drugs": len({r["canonical"] for r in tx.drug_index().values()}),
        "full_depth_drugs": list(tx.full_depth_drugs()),
        "draft_diseases": list(tx.draft_diseases()),
        # Recomputed live rather than read from the startup snapshot, so this reflects the
        # taxonomy as it stands now including any edit made since boot.
        "errors": tx.validate_config(),
        "errors_at_startup": list(STARTUP_ERRORS),
    }


@router.get("/export.yaml", response_class=PlainTextResponse)
async def export_yaml(db: AsyncSession = Depends(get_db)):
    """The live taxonomy as a YAML document, for diffing against the seed baseline."""
    return PlainTextResponse(
        await store.export_yaml(db),
        media_type="text/yaml; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="brands_export.yaml"'},
    )


# ── Add Brand ─────────────────────────────────────────────────────────────────────────
# Four steps, three of them read-only. The model suggests; the analyst decides; only the
# last call writes anything.

@router.get("/areas")
async def taxonomy_areas(db: AsyncSession = Depends(get_db)):
    """Therapeutic-area blocks a new brand can be filed under, with their indications."""
    return {"areas": await authoring.area_choices(db)}


@router.post("/brands/resolve")
async def resolve_brand(body: BrandResolveRequest):
    """Step 1. Is this name already curated, a near miss, or genuinely new?

    Two passes, and the order matters. The deterministic one runs first and settles every
    case it can: an exact hit or a close match against ``drug_index()`` costs no model call
    and cannot be swayed by model judgement.

    The model runs only on ``novel``, because that verdict has a blind spot. ``difflib``
    compares against drugs the taxonomy already carries, so a typo of a drug it has never
    heard of matches nothing — "Mavyre" is not close to anything curated, since Mavyret is
    not curated either. Only a second pass with knowledge beyond the taxonomy can catch that,
    and it is advisory: it suggests a correction, it does not apply one.
    """
    result = authoring.resolve(body.name)
    if result.get("status") == "novel":
        result["spelling"] = await drafts.check_spelling(body.name)
    return result


@router.post("/brands/draft")
async def draft_brand(body: BrandResolveRequest):
    """Step 2. Model-drafted identity for a novel brand. Every field is editable."""
    return await drafts.draft_identity(body.name)


@router.post("/brands/competitors")
async def draft_competitors(body: CompetitorDraftRequest):
    """Step 3. Suggested competitors for one indication, each unticked, each with a reason."""
    return {"competitors": await drafts.suggest_competitors(body.brand, body.disease)}


@router.post("/brands/outcomes")
async def draft_outcomes(body: OutcomeDraftRequest):
    """Step 3b. Endpoints for a brand-new indication, chosen from the defined vocabulary.

    An indication saved with these is stored ``DRAFT`` and stays out of the evidence
    programme until a human verifies it.
    """
    return await drafts.draft_outcomes(body.disease)


@router.post("/brands", status_code=201)
async def create_brand(body: BrandCreateRequest, db: AsyncSession = Depends(get_db)):
    """Step 4. Validate, insert, reload the snapshot, audit.

    A 400 means nothing was written: the addition is rejected whole, either by the write-time
    checks or because the resulting configuration would not pass the startup validation.
    """
    try:
        result = await authoring.add_brand(
            db, body.model_dump(exclude={"reviewer"}), reviewer=body.reviewer,
        )
    except authoring.BrandRejected as e:
        raise HTTPException(status_code=400, detail={"errors": e.reasons}) from e
    return result
