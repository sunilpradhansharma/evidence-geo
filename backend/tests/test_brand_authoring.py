"""Adding a brand from the UI.

The write path is the part of this feature that can do real damage, so the tests are weighted
towards what it must REFUSE. Adding a brand does not only create new coverage cells — since
the mentions rollup resolves every scored answer through ``drug_index()``, it also changes how
answers already in the corpus are attributed. A bad write is therefore not confined to the new
row; it rewrites the reading of history.
"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import taxonomy
from app.models import brand_taxonomy  # noqa: F401 — registers the tables
from app.models.audit_log import AuditLog
from app.models.brand_taxonomy import (
    ROLE_COMPETITOR,
    STATUS_DRAFT,
    TaxonomyIndication,
    TaxonomyIndicationDrug,
)
from app.models.database import Base
from app.services import brand_authoring_service as authoring
from app.services import brand_taxonomy_service as store

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await store.hydrate(session)
        yield session

    # Put the seed-file fallback back so a test cannot leak its taxonomy into the next one.
    taxonomy.install_snapshot(None)
    await engine.dispose()


def _addition(**overrides) -> dict:
    """A realistic addition: a real JAK1 inhibitor used in UC that is not curated today.

    Deliberately a drug the seed baseline has never heard of under any alias. Using one it
    already knows would make every write test pass for the wrong reason — rejected as a
    duplicate rather than exercising the write.
    """
    payload = {
        "name": "Jyseleca",
        "generic": "filgotinib",
        "company": "Galapagos",
        "drug_class": "JAK1 inhibitor",
        "administration_route": "ORAL",
        "aliases": ["filgotinib"],
        "therapeutic_area_key": "Gastroenterology",
        "diseases": [{"disease": "Ulcerative Colitis", "competitors": []}],
    }
    payload.update(overrides)
    return payload


# =====================================================================================
# Step 1 — resolving a typed name
# =====================================================================================
async def test_an_existing_brand_is_reported_as_an_exact_match(db_session):
    result = authoring.resolve("Rinvoq")
    assert result["status"] == "exact_match"
    assert result["canonical"] == "Rinvoq"
    assert result["matched_alias"] is False


async def test_typing_a_generic_reports_the_brand_it_belongs_to(db_session):
    """Typing an alias is not a duplicate — the analyst has to see which brand it resolved to."""
    result = authoring.resolve("upadacitinib")
    assert result["status"] == "exact_match"
    assert result["canonical"] == "Rinvoq"
    assert result["matched_alias"] is True


async def test_a_typo_is_caught_without_a_model_call(db_session):
    """A near miss must be deterministic. Depending on model judgement to notice a typo of a
    curated brand would make duplicate creation a matter of chance."""
    result = authoring.resolve("Rinvok")
    assert result["status"] == "near_matches"
    assert "Rinvoq" in [m["name"] for m in result["near_matches"]]


async def test_a_genuinely_new_name_is_novel(db_session):
    assert authoring.resolve("Jyseleca")["status"] == "novel"


async def test_a_blank_name_is_rejected_not_treated_as_novel(db_session):
    assert authoring.resolve("   ")["status"] == "invalid"


async def test_resolve_itself_never_calls_a_model(db_session):
    """``resolve`` has to stay pure. It backs the duplicate check, and a duplicate decision
    that varied with model availability would be worse than no check."""
    import app.services.brand_draft_llm as drafts

    called = False

    async def _fail(*a, **k):  # pragma: no cover — asserted not to run
        nonlocal called
        called = True
        return {}

    original = drafts.chat_json
    drafts.chat_json = _fail
    try:
        authoring.resolve("Rinvoq")
        authoring.resolve("Jyseleca")
    finally:
        drafts.chat_json = original
    assert not called


# =====================================================================================
# The model spelling pass — the second half of step 1
# =====================================================================================
async def test_the_model_check_runs_only_when_string_matching_found_nothing(db_session):
    """The blind spot this covers, and the boundary it must not cross.

    ``difflib`` compares against curated drugs, so it cannot see a typo of a drug that is not
    curated: "Mavyre" is close to nothing because Mavyret is absent too. The model pass exists
    for exactly that. It must NOT run when string matching already answered, or a curated
    brand's duplicate check would start depending on model availability.
    """
    from app.api import taxonomy as api

    calls: list[str] = []

    async def _fake(name: str) -> dict:
        calls.append(name)
        return {"checked": True, "verdict": "misspelling", "corrected": "Mavyret",
                "generic": "glecaprevir/pibrentasvir", "company": "AbbVie", "note": "x"}

    original = api.drafts.check_spelling
    api.drafts.check_spelling = _fake
    try:
        exact = await api.resolve_brand(api.BrandResolveRequest(name="Rinvoq"))
        near = await api.resolve_brand(api.BrandResolveRequest(name="Rinvok"))
        novel = await api.resolve_brand(api.BrandResolveRequest(name="Mavyre"))
    finally:
        api.drafts.check_spelling = original

    assert calls == ["Mavyre"], "the model must be consulted on the novel path only"
    assert "spelling" not in exact and "spelling" not in near
    assert novel["spelling"]["corrected"] == "Mavyret"
    # Advisory: the status stays novel, so the analyst can keep their spelling.
    assert novel["status"] == "novel"


# =====================================================================================
# What the write must refuse
# =====================================================================================
async def test_an_alias_already_owned_by_another_drug_is_rejected(db_session):
    """The single most damaging write this feature can make.

    ``drug_index()`` is built with ``setdefault``, so a colliding alias does not raise — it
    silently loses. The new brand would then be attributed to the existing owner across every
    answer already scored, with nothing anywhere reporting a problem.
    """
    payload = _addition(aliases=["upadacitinib"])
    with pytest.raises(authoring.BrandRejected) as excinfo:
        await authoring.add_brand(db_session, payload, reviewer="tester")

    assert any("Rinvoq" in r for r in excinfo.value.reasons)
    assert "Jyseleca" not in taxonomy.comparison_agents(), "nothing may be written on rejection"


async def test_adding_a_brand_that_already_exists_is_rejected(db_session):
    with pytest.raises(authoring.BrandRejected) as excinfo:
        await authoring.add_brand(db_session, _addition(name="Rinvoq"), reviewer="tester")
    assert any("already curated" in r for r in excinfo.value.reasons)


async def test_a_brand_cannot_enrol_itself_in_trial_ingestion(db_session):
    """``full`` depth starts trial ingestion for the drug. That is a reviewed decision, and a
    modal has had none of that review."""
    before = set(taxonomy.full_depth_drugs())
    with pytest.raises(authoring.BrandRejected) as excinfo:
        await authoring.add_brand(
            db_session, _addition(evidence_depth="full"), reviewer="tester"
        )
    assert any("evidence_depth" in r for r in excinfo.value.reasons)
    assert set(taxonomy.full_depth_drugs()) == before


async def test_an_unknown_therapeutic_area_is_rejected(db_session):
    with pytest.raises(authoring.BrandRejected) as excinfo:
        await authoring.add_brand(
            db_session, _addition(therapeutic_area_key="Cardiology"), reviewer="tester"
        )
    assert any("does not exist" in r for r in excinfo.value.reasons)


async def test_a_brand_with_no_indication_is_rejected(db_session):
    """It would sit in the picker producing no comparisons — the "no gaps" failure again."""
    with pytest.raises(authoring.BrandRejected) as excinfo:
        await authoring.add_brand(db_session, _addition(diseases=[]), reviewer="tester")
    assert any("at least one indication" in r for r in excinfo.value.reasons)


async def test_an_off_vocabulary_route_is_rejected(db_session):
    with pytest.raises(authoring.BrandRejected):
        await authoring.add_brand(
            db_session, _addition(administration_route="INHALED"), reviewer="tester"
        )


async def test_a_new_indication_with_an_undefined_endpoint_is_rejected(db_session):
    """Caught by the startup validation running against the would-be taxonomy, not by a
    duplicate rule here. An endpoint reference that resolves to nothing would produce an
    evidence network with no defined outcome."""
    payload = _addition(diseases=[{
        "disease": "Test Colitis",
        "area": "Gastroenterology",
        "therapeutic_area_key": "Gastroenterology",
        "canonical_outcomes": ["NOT_A_REAL_OUTCOME"],
        "competitors": [],
    }])
    with pytest.raises(authoring.BrandRejected) as excinfo:
        await authoring.add_brand(db_session, payload, reviewer="tester")

    assert any("not defined in canonical_outcomes.yaml" in r for r in excinfo.value.reasons)
    assert "Test Colitis" not in taxonomy.diseases(), "the rollback must be complete"


async def test_a_rejected_write_leaves_the_previous_taxonomy_installed(db_session):
    """The rollback has to restore the snapshot too, not just the rows. A half-reverted write
    would leave the process serving a taxonomy that is in no database."""
    before = taxonomy.diseases()
    payload = _addition(diseases=[{
        "disease": "Test Colitis",
        "area": "Gastroenterology",
        "therapeutic_area_key": "Gastroenterology",
        "canonical_outcomes": ["NOT_A_REAL_OUTCOME"],
        "competitors": [],
    }])
    with pytest.raises(authoring.BrandRejected):
        await authoring.add_brand(db_session, payload, reviewer="tester")

    assert taxonomy.diseases() == before
    assert taxonomy.validate_config() == []


# =====================================================================================
# Creating a therapeutic area
# =====================================================================================
def _new_area_addition(**overrides) -> dict:
    """Mavyret: a real AbbVie brand the taxonomy has no area for.

    Hepatitis C is not gastroenterology, rheumatology or anything else declared, which is the
    situation this path exists for — filing it under the nearest existing area would stamp the
    wrong ``therapeutic_area`` on every question generated for it.
    """
    payload = {
        "name": "Mavyret",
        "generic": "glecaprevir/pibrentasvir",
        "company": "AbbVie",
        "drug_class": "NS3/4A protease + NS5A inhibitor",
        "administration_route": "ORAL",
        "aliases": ["glecaprevir/pibrentasvir"],
        "therapeutic_area_key": "Hepatology",
        "new_therapeutic_area": {"ta_key": "Hepatology", "area": "Hepatology"},
        "diseases": [{
            "disease": "Chronic Hepatitis C",
            "area": "Hepatology",
            "therapeutic_area_key": "Hepatology",
            "canonical_outcomes": [],
            "competitors": [],
        }],
    }
    payload.update(overrides)
    return payload


async def test_a_brand_can_create_the_area_it_needs(db_session):
    await authoring.add_brand(db_session, _new_area_addition(), reviewer="tester")

    assert "Hepatology" in (taxonomy.config().get("therapeutic_areas") or {})
    assert taxonomy.area_for("Hepatology") == "Hepatology"
    assert "Mavyret" in taxonomy.focus_brands_for_key("Hepatology")
    assert "Chronic Hepatitis C" in taxonomy.diseases_for_key("Hepatology")
    # The question a brand in a brand-new area would carry.
    assert taxonomy.therapeutic_area_key_for_disease("Chronic Hepatitis C") == "Hepatology"
    assert taxonomy.validate_config() == []


async def test_a_created_area_is_never_empty(db_session):
    """An area with no brands is a selectable filter option with nothing behind it, which
    reads as "no results" rather than "nothing defined". Creating one is only possible as
    part of filing a brand into it, so the empty state cannot be reached."""
    await authoring.add_brand(db_session, _new_area_addition(), reviewer="tester")
    assert taxonomy.focus_brands_for_key("Hepatology"), "a new area must arrive populated"


async def test_creating_an_area_does_not_reorder_the_existing_ones(db_session):
    """Grouped pickers and `coverage.rank()` read declaration order, so a new block appends."""
    before = list(taxonomy.config().get("therapeutic_areas") or {})
    await authoring.add_brand(db_session, _new_area_addition(), reviewer="tester")
    after = list(taxonomy.config().get("therapeutic_areas") or {})

    assert after[: len(before)] == before
    assert after[-1] == "Hepatology"


async def test_creating_an_area_that_already_exists_is_rejected(db_session):
    with pytest.raises(authoring.BrandRejected) as excinfo:
        await authoring.add_brand(
            db_session,
            _new_area_addition(
                therapeutic_area_key="Oncology",
                new_therapeutic_area={"ta_key": "Oncology", "area": "Oncology"},
            ),
            reviewer="tester",
        )
    assert any("already exists" in r for r in excinfo.value.reasons)


async def test_a_form_that_disagrees_with_itself_is_rejected(db_session):
    """Filing the brand under one area while declaring another is ambiguous. Guessing which
    half was meant would silently file the brand somewhere nobody chose."""
    with pytest.raises(authoring.BrandRejected) as excinfo:
        await authoring.add_brand(
            db_session, _new_area_addition(therapeutic_area_key="Oncology"), reviewer="tester"
        )
    assert any("must match" in r for r in excinfo.value.reasons)


async def test_a_rejected_write_leaves_no_orphan_area(db_session):
    """The area is inserted before the brand, so a later failure must take it back out with
    everything else — an orphan block is exactly the empty filter option this guards against."""
    payload = _new_area_addition(diseases=[{
        "disease": "Chronic Hepatitis C",
        "area": "Hepatology",
        "therapeutic_area_key": "Hepatology",
        "canonical_outcomes": ["NOT_A_REAL_OUTCOME"],
        "competitors": [],
    }])
    with pytest.raises(authoring.BrandRejected):
        await authoring.add_brand(db_session, payload, reviewer="tester")

    assert "Hepatology" not in (taxonomy.config().get("therapeutic_areas") or {})
    assert taxonomy.validate_config() == []


async def test_a_created_area_survives_the_yaml_export(db_session):
    import yaml

    await authoring.add_brand(db_session, _new_area_addition(), reviewer="tester")
    parsed = yaml.safe_load(await store.export_yaml(db_session))

    assert parsed["therapeutic_areas"]["Hepatology"]["area"] == "Hepatology"
    assert [b["name"] for b in parsed["therapeutic_areas"]["Hepatology"]["focus_brands"]] \
        == ["Mavyret"]


# =====================================================================================
# The successful write
# =====================================================================================
async def test_adding_a_brand_makes_it_visible_to_every_reader(db_session):
    """One write, and the accessors that scoring, coverage and attribution read all agree."""
    await authoring.add_brand(db_session, _addition(), reviewer="tester")

    assert "Jyseleca" in taxonomy.brands_for_disease("Ulcerative Colitis")
    assert "Jyseleca" in taxonomy.focus_brands_for_key("Gastroenterology")
    assert "Gastroenterology" in taxonomy.area_keys_for_brand("Jyseleca")
    # Resolvable by its generic, which is what corpus-wide attribution reads.
    assert taxonomy.drug_index()["filgotinib"]["canonical"] == "Jyseleca"
    assert taxonomy.company_for("Jyseleca") == "Galapagos"
    assert taxonomy.validate_config() == []


async def test_a_new_brand_defaults_to_standard_depth(db_session):
    await authoring.add_brand(db_session, _addition(), reviewer="tester")
    assert taxonomy.evidence_depth_for("Jyseleca") == "standard"
    assert "Jyseleca" not in taxonomy.full_depth_drugs()


async def test_ticked_competitors_are_saved_with_their_reason(db_session):
    """The reason is why the competitive field stays reviewable. It is recorded and never
    read by a rule, which is exactly why nothing else would notice it going missing."""
    payload = _addition(diseases=[{
        "disease": "Ulcerative Colitis",
        "competitors": [
            {"name": "Rinvoq", "note": "Same JAK1 class, both approved in UC."},
        ],
    }])
    await authoring.add_brand(db_session, payload, reviewer="tester")

    assert "Rinvoq" in taxonomy.competitors_for_disease("Ulcerative Colitis")
    row = (await db_session.execute(
        TaxonomyIndicationDrug.__table__.select().where(
            (TaxonomyIndicationDrug.drug_name == "Rinvoq")
            & (TaxonomyIndicationDrug.role == ROLE_COMPETITOR)
        )
    )).first()
    assert row is not None and "Same JAK1 class" in row.note


async def test_an_unticked_competitor_is_not_saved(db_session):
    """Suggestion is not approval. Only what the analyst ticked reaches the payload, and
    nothing else may appear in the taxonomy."""
    before = set(taxonomy.competitors_for_disease("Ulcerative Colitis"))
    await authoring.add_brand(db_session, _addition(), reviewer="tester")
    assert set(taxonomy.competitors_for_disease("Ulcerative Colitis")) == before


async def test_a_new_indication_is_stored_draft_and_fenced_out(db_session):
    """The endpoints came from a model, so the evidence programme must refuse the indication
    while comparison coverage still works with it today."""
    payload = _addition(diseases=[{
        "disease": "Pouchitis",
        "area": "Gastroenterology",
        "therapeutic_area_key": "Gastroenterology",
        # Deliberately empty: the DRAFT carve-out allows an indication with no verified
        # endpoints yet, which is the whole point of the fence.
        "canonical_outcomes": [],
        "competitors": [],
    }])
    await authoring.add_brand(db_session, payload, reviewer="tester")

    assert taxonomy.is_draft_disease("Pouchitis")
    assert "Pouchitis" in taxonomy.draft_diseases()
    # Usable for coverage regardless — the fence is on the evidence programme, not the UI.
    assert "Jyseleca" in taxonomy.brands_for_disease("Pouchitis")
    assert taxonomy.validate_config() == []

    row = await db_session.get(TaxonomyIndication, "Pouchitis")
    assert row.verification_status == STATUS_DRAFT
    assert row.created_by == "tester"


async def test_an_existing_indication_is_not_downgraded_to_draft(db_session):
    """Adding a brand to a reviewed indication must not quietly un-verify its endpoints."""
    await authoring.add_brand(db_session, _addition(), reviewer="tester")
    row = await db_session.get(TaxonomyIndication, "Ulcerative Colitis")
    assert row.verification_status != STATUS_DRAFT
    assert not taxonomy.is_draft_disease("Ulcerative Colitis")


async def test_the_write_is_audited(db_session):
    """No RBAC in this tree, so the audit row is the whole account of who changed what."""
    await authoring.add_brand(db_session, _addition(), reviewer="a.analyst")
    entry = (await db_session.execute(
        AuditLog.__table__.select().where(AuditLog.event == "TAXONOMY_BRAND_ADDED")
    )).first()
    assert entry is not None
    assert "a.analyst" in entry.context and "Jyseleca" in entry.context


async def test_adding_a_brand_does_not_rerank_the_existing_gap_queue(db_session):
    """``coverage.rank()`` reads declaration order as the curated tiering, so an insert has to
    append. Renumbering would silently reprioritise every existing gap."""
    before = taxonomy.diseases()
    before_competitors = taxonomy.competitors_for_disease("Ulcerative Colitis")

    await authoring.add_brand(db_session, _addition(), reviewer="tester")

    assert taxonomy.diseases() == before
    assert taxonomy.competitors_for_disease("Ulcerative Colitis") == before_competitors


async def test_the_addition_survives_a_reload_from_the_database(db_session):
    """Proves the brand was persisted, not merely installed into the in-process snapshot."""
    await authoring.add_brand(db_session, _addition(), reviewer="tester")

    taxonomy.install_snapshot(None)
    assert "Jyseleca" not in taxonomy.brands_for_disease("Ulcerative Colitis")

    await store.hydrate(db_session)
    assert "Jyseleca" in taxonomy.brands_for_disease("Ulcerative Colitis")


async def test_the_addition_round_trips_through_the_yaml_export(db_session):
    """The export is the only reviewable diff once brands.yaml is retired, so a UI-added brand
    that did not appear in it would be invisible to review."""
    import yaml

    await authoring.add_brand(db_session, _addition(), reviewer="tester")
    parsed = yaml.safe_load(await store.export_yaml(db_session))

    names = [b["name"] for b in parsed["therapeutic_areas"]["Gastroenterology"]["focus_brands"]]
    assert "Jyseleca" in names
    assert "Jyseleca" in parsed["indications"]["Ulcerative Colitis"]["brands"]
