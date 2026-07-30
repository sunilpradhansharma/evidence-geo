"""The taxonomy moved from brands.yaml into SQLite. This is the proof it changed nothing.

The load-bearing test is ``test_a_snapshot_of_the_seeded_rows_equals_the_seed_document``:
a snapshot rebuilt from seeded rows must compare EQUAL to the parsed seed file, not merely
equivalent. Fifty-four modules read this taxonomy — scoring, harvest, evidence networks,
prompt volume, social, GEO and the corpus-wide competitor reads — and several test files
pin its content transitively without ever naming ``taxonomy``. Exact equality is the only
assertion that covers all of them at once.

Equality is stricter than behaviour on purpose. ``drug_index()`` reads
``entry.get("background_therapy", False)`` and ``entry.get("evidence_depth") or "standard"``,
so a row that nulled an absent field would behave identically and compare differently. If
this test fails on a null-versus-absent difference it has done its job: that is the class of
drift that would otherwise be invisible until something downstream read the raw dict.
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import taxonomy
from app.models.brand_taxonomy import STATUS_DRAFT, TaxonomyIndication
from app.models.database import Base
from app.services import brand_taxonomy_service as store


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from app.models import brand_taxonomy  # noqa: F401 — register tables on Base.metadata

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.fixture(autouse=True)
def restore_snapshot():
    """Undo any snapshot a test installs, so the module-level default survives the file.

    Without this a test that hydrates from a database would leave every later test in the
    run reading its taxonomy, which is precisely the global-state failure the snapshot
    design has to be trusted not to cause.
    """
    yield
    taxonomy.install_snapshot(None)


# =====================================================================================
# The migration's proof
# =====================================================================================
async def test_a_snapshot_of_the_seeded_rows_equals_the_seed_document(db_session):
    seed = store.seed_document()
    await store.import_document(db_session, seed)
    assert await store.build_snapshot(db_session) == seed


async def test_seeding_is_idempotent(db_session):
    """The deploy runs the seeder on every boot, like every other seeder in ec2_deploy.sh."""
    assert await store.seed_if_empty(db_session) is True
    first = await store.build_snapshot(db_session)
    assert await store.seed_if_empty(db_session) is False
    assert await store.build_snapshot(db_session) == first


async def test_importing_twice_does_not_accumulate_rows(db_session):
    """Import is a replacement, not a merge: a drug removed from the document is gone."""
    seed = store.seed_document()
    await store.import_document(db_session, seed)
    await store.import_document(db_session, seed)
    assert await store.build_snapshot(db_session) == seed


# =====================================================================================
# Declaration order is data
# =====================================================================================
async def test_declaration_order_survives_the_round_trip(db_session):
    """``coverage.rank()`` orders the gap queue by index into these tuples.

    Order was expressed in the YAML purely as line order, so a store that returned rows in
    insertion or alphabetical order would silently rerank every gap without failing anything
    that only checks membership.
    """
    seed = store.seed_document()
    await store.import_document(db_session, seed)
    snapshot = await store.build_snapshot(db_session)

    assert list(snapshot["indications"]) == list(seed["indications"])
    assert list(snapshot["therapeutic_areas"]) == list(seed["therapeutic_areas"])
    for disease, block in seed["indications"].items():
        assert snapshot["indications"][disease]["competitors"] == block["competitors"]
        assert snapshot["indications"][disease]["brands"] == block["brands"]


# =====================================================================================
# Absent is not null
# =====================================================================================
async def test_an_absent_field_stays_absent(db_session):
    """Methotrexate is the only declared background therapy; nothing else may claim the key.

    ``comparison_agents()`` excludes a background therapy from the scope guard, so a row
    that emitted ``background_therapy: False`` for every other drug would compare unequal
    and, worse, invite a reader to treat the flag as meaningful everywhere.
    """
    await store.import_document(db_session, store.seed_document())
    catalog = (await store.build_snapshot(db_session))["drug_catalog"]

    assert catalog["Methotrexate"]["background_therapy"] is True
    assert "background_therapy" not in catalog["Taltz"]
    # Never carried in the catalog block at all, so absence must round-trip as absence.
    assert "evidence_depth" not in catalog["Taltz"]
    assert "name" not in catalog["Taltz"]


async def test_a_focus_brand_keeps_only_the_fields_it_declared(db_session):
    await store.import_document(db_session, store.seed_document())
    snapshot = await store.build_snapshot(db_session)
    seed = store.seed_document()

    for ta_key, block in seed["therapeutic_areas"].items():
        for kind in ("focus_brands", "competitors"):
            for original, rebuilt in zip(
                block[kind], snapshot["therapeutic_areas"][ta_key][kind]
            ):
                assert set(original) == set(rebuilt), (
                    f"{ta_key}/{kind}/{original.get('name')} changed its key set"
                )


# =====================================================================================
# Installing a snapshot actually swaps what the accessors serve
# =====================================================================================
async def test_hydrate_makes_the_accessors_read_the_database(db_session):
    await store.hydrate(db_session)
    assert taxonomy.competitors_for_disease("Atopic Dermatitis")
    assert taxonomy.validate_config() == []


async def test_a_reload_invalidates_every_cached_accessor(db_session):
    """The likeliest bug in this migration is one accessor left holding the old taxonomy.

    ``clear_caches`` walks the module namespace rather than a hand-written list, so this
    checks a value that is cached several layers deep: ``competitors_for_disease`` is cached
    itself AND reads ``_disease_block``, which is cached, which reads ``_indications``.
    """
    await store.hydrate(db_session)
    assert "Dupixent" in taxonomy.competitors_for_disease("Atopic Dermatitis")

    row = await db_session.get(TaxonomyIndication, "Atopic Dermatitis")
    row.aliases_json = None
    await db_session.commit()

    from sqlalchemy import delete

    from app.models.brand_taxonomy import TaxonomyIndicationDrug

    await db_session.execute(
        delete(TaxonomyIndicationDrug).where(
            TaxonomyIndicationDrug.disease == "Atopic Dermatitis",
            TaxonomyIndicationDrug.drug_name == "Dupixent",
        )
    )
    await db_session.commit()
    await store.hydrate(db_session)

    assert "Dupixent" not in taxonomy.competitors_for_disease("Atopic Dermatitis")


async def test_installing_none_restores_the_seed_fallback(db_session):
    """Tests and offline scripts read the taxonomy with no database at all."""
    await store.hydrate(db_session)
    taxonomy.install_snapshot(None)
    assert taxonomy.config() == store.seed_document()


# =====================================================================================
# The DRAFT fence
# =====================================================================================
async def test_nothing_seeded_from_the_reviewed_baseline_is_a_draft(db_session):
    await store.hydrate(db_session)
    assert taxonomy.draft_diseases() == ()


async def test_a_draft_indication_is_flagged_and_does_not_fail_validation(db_session):
    """A UI-created indication carries model-drafted endpoints, so it is fenced, not fatal.

    Startup validation is what would otherwise refuse to boot: an indication with no
    canonical outcomes is an error for a verified one precisely because it would yield a
    network with no defined endpoint. A draft cannot reach a network at all.
    """
    await store.import_document(db_session, store.seed_document())
    db_session.add(TaxonomyIndication(
        disease="Test Disease",
        area="Dermatology",
        therapeutic_area_key="Dermatology",
        verification_status=STATUS_DRAFT,
        display_order=999,
    ))
    await db_session.commit()
    await store.hydrate(db_session)

    assert taxonomy.draft_diseases() == ("Test Disease",)
    assert taxonomy.is_draft_disease("Test Disease") is True
    assert taxonomy.is_draft_disease("Atopic Dermatitis") is False
    assert taxonomy.validate_config() == []


async def test_a_verified_indication_with_no_outcomes_is_still_an_error(db_session):
    """The DRAFT carve-out must not become a way to skip endpoint review by omission."""
    await store.import_document(db_session, store.seed_document())
    db_session.add(TaxonomyIndication(
        disease="Test Disease",
        area="Dermatology",
        therapeutic_area_key="Dermatology",
        display_order=999,
    ))
    await db_session.commit()
    await store.hydrate(db_session)

    errors = taxonomy.validate_config()
    assert any("declares no canonical_outcomes" in e for e in errors)


# =====================================================================================
# The YAML export — the only reviewable diff left once brands.yaml is retired
# =====================================================================================
async def test_the_export_parses_back_to_exactly_what_is_stored(db_session):
    """An export that cannot be re-imported is a report, not a backup.

    It has to survive the round trip because copying it over the seed file is the documented
    way to make the current state the new reviewed baseline.
    """
    import yaml

    await store.import_document(db_session, store.seed_document())
    rendered = await store.export_yaml(db_session)
    assert yaml.safe_load(rendered) == await store.build_snapshot(db_session)


async def test_the_export_keeps_declaration_order(db_session):
    """Alphabetising would describe a different gap ranking from the one actually applied."""
    import yaml

    seed = store.seed_document()
    await store.import_document(db_session, seed)
    parsed = yaml.safe_load(await store.export_yaml(db_session))

    assert list(parsed["indications"]) == list(seed["indications"])
    assert parsed["indications"]["Psoriatic Arthritis"]["competitors"] == \
        seed["indications"]["Psoriatic Arthritis"]["competitors"]


async def test_the_export_carries_the_curation_notes(db_session):
    """The reasoning lived in YAML comments, which a migration into rows would destroy."""
    import yaml

    from app.models.brand_taxonomy import ROLE_COMPETITOR, TaxonomyIndicationDrug

    await store.import_document(db_session, store.seed_document())
    db_session.add(TaxonomyIndicationDrug(
        disease="Waldenstrom's Macroglobulinemia",
        drug_name="Calquence",
        role=ROLE_COMPETITOR,
        note="Same class is not the same competitive field: holds no WM indication.",
        display_order=9999,
    ))
    await db_session.commit()

    rendered = await store.export_yaml(db_session)
    assert "Same class is not the same competitive field" in rendered
    # Still a parseable document: the notes are comments, so they must not corrupt it.
    assert yaml.safe_load(rendered)["indications"]


# =====================================================================================
# The read API the UI depends on
# =====================================================================================
async def test_the_api_hierarchy_agrees_with_the_accessors(db_session):
    """The frontend used to hardcode this. It must now match what scoring actually reads."""
    from app.api import taxonomy as api

    await store.hydrate(db_session)
    payload = await api.read_taxonomy()

    assert payload["area_options"], "no areas would leave every picker empty"
    assert set(payload["disease_options"]) == set(taxonomy.diseases())
    for disease, brands in payload["disease_brand_map"].items():
        assert brands == list(taxonomy.brands_for_disease(disease))
    # Only focus brands are offered: a competitor is not something we monitor on our behalf.
    assert "Rinvoq" in payload["brand_options"]
    assert "Tremfya" not in payload["brand_options"]


async def test_the_hierarchy_keeps_areas_and_indications_distinct(db_session):
    """The frontend builds its TA picker off this shape.

    An area holding one indication of its own name renders as a plain option; anything else
    renders as a group. Flattening the two levels would either lose Women's Health as a
    heading or lose Endometriosis and Uterine Fibroids as the values actually stored on a
    question, so the distinction has to survive the API.
    """
    from app.api import taxonomy as api

    await store.hydrate(db_session)
    areas = {a["area"]: [i["taKey"] for i in a["indications"]] for a in (await api.read_taxonomy())["areas"]}

    assert areas["Dermatology"] == ["Dermatology"]
    assert areas["Women's Health"] == ["Endometriosis", "Uterine Fibroids"]
    # One indication, but not named after its area — still a group, not a plain option.
    assert areas["Endocrinology"] == ["Central Precocious Puberty"]


async def test_the_coverage_picker_excludes_brands_that_can_have_no_gaps(db_session):
    """Offering a brand with no comparisons makes the filter answer "no gaps" \u2014 which reads
    as fully covered rather than as nothing having been defined.

    This exact bug was fixed once already by extending the disease overlay to Imbruvica,
    Venclexta, Vraylar and Lupron. The Obesity block still declares focus brands with no
    overlay entries, so the two lists must stay distinct.
    """
    from app.api import taxonomy as api

    await store.hydrate(db_session)
    payload = await api.read_taxonomy()

    coverage = set(payload["coverage_brand_options"])
    everything = set(payload["brand_options"])

    assert coverage < everything, "the coverage list must be a strict subset"
    # Declared as focus brands under Obesity, which has no disease overlay.
    assert not coverage & {"Wegovy", "Zepbound", "Ozempic", "Mounjaro", "Saxenda", "Rybelsus"}
    assert {"Rinvoq", "Skyrizi", "Humira", "Imbruvica", "Vraylar"} <= coverage
    # Every offered brand must genuinely appear in at least one indication.
    for brand in coverage:
        assert any(
            brand in taxonomy.brands_for_disease(d) for d in taxonomy.diseases()
        ), f"{brand} is offered on the coverage filter but has no indication"


@pytest.fixture
async def api_client():
    """The taxonomy router on a bare app.

    Mounted directly rather than importing ``app.main`` so the routes are exercised over real
    HTTP without dragging in the whole application's optional dependencies.
    """
    import httpx
    from fastapi import FastAPI
    from httpx import ASGITransport
    from sqlalchemy.pool import StaticPool

    from app.api import taxonomy as api
    from app.models.database import get_db

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    from app.models import brand_taxonomy  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as s:
        await store.hydrate(s)

    async def _get_db():
        async with maker() as s:
            yield s

    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_db] = _get_db
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await engine.dispose()


async def test_the_taxonomy_routes_answer(api_client):
    response = await api_client.get("/taxonomy")
    assert response.status_code == 200
    assert response.json()["area_options"]


async def test_the_export_route_serves_parseable_yaml(api_client):
    import yaml

    response = await api_client.get("/taxonomy/export.yaml")
    assert response.status_code == 200
    parsed = yaml.safe_load(response.text)
    assert set(parsed) == {"therapeutic_areas", "indications", "drug_catalog"}


async def test_the_status_route_reports_a_healthy_taxonomy(api_client):
    """Errors are recomputed live, so this answers 'is it wrong now', not 'was it at boot'."""
    response = await api_client.get("/taxonomy/status")
    assert response.status_code == 200
    body = response.json()
    assert body["errors"] == []
    assert body["indications"] > 0
    assert body["draft_diseases"] == []
    # Trial ingestion is gated on evidence_depth, and adding a brand must not widen it.
    assert set(body["full_depth_drugs"]) == {"Rinvoq", "Skyrizi", "Humira", "Tremfya"}
