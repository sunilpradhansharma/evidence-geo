"""The Immunology -> Dermatology / Gastroenterology split, and the backfill that migrates it.

Two things are pinned here:

* the **seed banks** stay internally consistent — every seeded row's disease must resolve
  to the therapeutic area that row claims, and its brand must actually hold that
  indication. A drifted seed bank is a demo that contradicts the taxonomy it ships with.
* the **backfill resolver** resolves disease-first and refuses to answer from a
  multi-area brand. That refusal is the whole point: Humira, Skyrizi and Rinvoq each
  live in three blocks, so a brand-derived answer is decided by brands.yaml ordering —
  the bug ``scripts/hotfix_rhem_therapeutic_area.py`` had to repair after the fact.
"""
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import taxonomy
from app.models.database import Base
from app.models.question import Question
from app.models.response import Response
from app.models.response_citation import ResponseCitation
from scripts.backfill_therapeutic_area_split import (
    LEGACY_AREA,
    TABLES,
    TableSpec,
    _resolve,
    _scan_table,
)
from scripts.seed_dermatology_questions import DERM_QUESTIONS
from scripts.seed_gastroenterology_questions import GI_QUESTIONS

_SPECS = {spec.label: spec for spec in TABLES}


@pytest.fixture
async def session():
    engine_ = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine_.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine_, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine_.dispose()


# --- seed banks ------------------------------------------------------------------
@pytest.mark.parametrize(
    "rows,area",
    [(DERM_QUESTIONS, "Dermatology"), (GI_QUESTIONS, "Gastroenterology")],
)
def test_seed_bank_rows_agree_with_the_taxonomy(rows, area):
    assert rows, "seed bank must not be empty"
    for _persona, _domain, brand, disease, text in rows:
        assert taxonomy.therapeutic_area_key_for_disease(disease) == area, (
            f"{disease!r} does not belong to {area}: {text!r}"
        )
        assert brand in taxonomy.brands_for_disease(disease), (
            f"{brand} holds no {disease} indication in brands.yaml: {text!r}"
        )


@pytest.mark.parametrize("rows", [DERM_QUESTIONS, GI_QUESTIONS])
def test_seed_bank_covers_every_persona(rows):
    assert {r[0] for r in rows} == {"Prospect", "Patient", "Provider"}


def test_seed_banks_cover_every_indication_of_their_area():
    assert {r[3] for r in DERM_QUESTIONS} == set(taxonomy.diseases_for_key("Dermatology"))
    assert {r[3] for r in GI_QUESTIONS} == set(taxonomy.diseases_for_key("Gastroenterology"))


# --- backfill resolver -----------------------------------------------------------
def _question(**kw) -> Question:
    return Question(
        question_id=kw.pop("question_id", "Q-TEST"),
        question_text=kw.pop("question_text", ""),
        therapeutic_area=kw.pop("therapeutic_area", LEGACY_AREA),
        persona="Patient", domain="Safety", version=1, **kw,
    )


def test_resolver_prefers_an_explicit_disease_over_everything_else():
    spec = _SPECS["questions"]
    row = _question(
        disease="Ulcerative Colitis",
        question_text="Is Skyrizi good for plaque psoriasis?",   # contradicts the column
        brand_focus="Skyrizi",
    )
    area, how, _detail = _resolve(row, spec)
    assert (area, how) == ("Gastroenterology", "disease")


def test_resolver_falls_back_to_the_indication_column():
    spec = _SPECS["questions"]
    area, how, _ = _resolve(_question(indication="Atopic Dermatitis"), spec)
    assert (area, how) == ("Dermatology", "indication")


def test_resolver_reads_the_disease_out_of_the_question_text():
    spec = _SPECS["questions"]
    row = _question(question_text="How well does Humira work for Crohn's disease?",
                    brand_focus="Humira")
    area, how, detail = _resolve(row, spec)
    assert area == "Gastroenterology"
    assert how == "text:question_text"
    assert detail == "Crohn's Disease"


def test_resolver_refuses_to_answer_from_a_multi_area_brand():
    """The load-bearing case: no disease anywhere, and Humira spans three blocks."""
    spec = _SPECS["questions"]
    row = _question(question_text="How much does Humira cost?", brand_focus="Humira")
    area, how, _ = _resolve(row, spec)
    assert area is None and how == ""


def test_resolver_still_uses_a_single_area_brand():
    spec = _SPECS["questions"]
    row = _question(question_text="Any tips for taking this?", brand_focus="Vraylar")
    area, how, _ = _resolve(row, spec)
    assert (area, how) == ("Neuroscience", "brand")


def test_resolver_routes_to_rheumatology_when_that_is_what_the_disease_says():
    """A legacy Immunology row naming RA was always mis-filed; it does not become
    Dermatology just because Dermatology is one of the two successor areas."""
    spec = _SPECS["questions"]
    row = _question(disease="Rheumatoid Arthritis", brand_focus="Humira")
    assert _resolve(row, spec)[0] == "Rheumatology"


def test_resolver_accepts_a_disease_alias():
    spec = _SPECS["questions"]
    assert _resolve(_question(disease="acne inversa"), spec)[0] == "Dermatology"
    assert _resolve(_question(disease="PsO"), spec)[0] == "Dermatology"


def test_only_the_cached_mapper_column_is_remapped():
    """``prompt_volume_staging.matched_therapeutic_area`` is a cache of map_query and may be
    refreshed. A curated column on any other table must never be."""
    assert _SPECS["prompt_volume_staging"].remap_with_mapper is True
    assert not any(
        spec.remap_with_mapper for label, spec in _SPECS.items()
        if label != "prompt_volume_staging"
    )


def test_citations_inherit_their_parent_response_rather_than_re_inferring():
    spec = _SPECS["response_citations"]
    row = ResponseCitation(
        citation_id="C1", response_id="R1", domain_id="D1",
        authority_domain="example.org", therapeutic_area=LEGACY_AREA,
    )
    assert _resolve(row, spec, {}) [0] is None            # nothing to inherit yet
    area, how, _ = _resolve(row, spec, {"R1": "Gastroenterology"})
    assert (area, how) == ("Gastroenterology", "parent:response_id")


# --- backfill scan ---------------------------------------------------------------
async def test_scan_reports_resolved_and_unresolved_rows(session):
    session.add_all([
        _question(question_id="Q-1", disease="Plaque Psoriasis", brand_focus="Skyrizi"),
        _question(question_id="Q-2", disease="Crohn's Disease", brand_focus="Humira"),
        # No disease and a three-area brand: must be left alone, not guessed at.
        _question(question_id="Q-3", question_text="Is Humira expensive?", brand_focus="Humira"),
        # Already migrated — must not be picked up at all.
        _question(question_id="Q-4", therapeutic_area="Dermatology", disease="Atopic Dermatitis"),
    ])
    await session.commit()

    result = await _scan_table(session, _SPECS["questions"])
    assert result.scanned == 3                                   # Q-4 is not on the legacy area
    assert {c.row_id: c.new for c in result.changes} == {
        "Q-1": "Dermatology", "Q-2": "Gastroenterology",
    }
    assert len(result.unresolved) == 1 and "Q-3" in result.unresolved[0]


async def test_scan_is_idempotent_once_migrated(session):
    session.add(_question(question_id="Q-1", therapeutic_area="Dermatology",
                          disease="Plaque Psoriasis"))
    await session.commit()
    result = await _scan_table(session, _SPECS["questions"])
    assert result.scanned == 0 and result.changes == []


async def test_citations_follow_their_response_through_the_scan(session):
    session.add(Response(
        response_id="R1", run_id="RUN1", llm_name="gpt-4o", persona="Patient",
        question_id="Q-1", question_text="Does Skyrizi help ulcerative colitis?",
        response_text="Skyrizi is approved for moderately to severely active UC.",
        therapeutic_area=LEGACY_AREA, disease="Ulcerative Colitis",
        brand_focus="Skyrizi", domain="Efficacy", status="SUCCESS",
    ))
    session.add(ResponseCitation(
        citation_id="C1", response_id="R1", domain_id="D1",
        authority_domain="example.org", therapeutic_area=LEGACY_AREA,
    ))
    await session.commit()

    responses = await _scan_table(session, _SPECS["responses"])
    inherited = {c.row_id: c.new for c in responses.changes}
    assert inherited == {"R1": "Gastroenterology"}

    citations = await _scan_table(session, _SPECS["response_citations"], inherited)
    assert [(c.row_id, c.new) for c in citations.changes] == [("C1", "Gastroenterology")]


# --- table coverage --------------------------------------------------------------
def test_every_table_carrying_a_therapeutic_area_is_covered():
    """A new table with a therapeutic_area column that nobody added here would silently
    keep serving a retired area."""
    covered = {spec.model.__tablename__ for spec in TABLES}
    known_area_level = {"preferred_sources", "social_briefs", "preferred_source_observations"}

    with_ta = {
        mapper.class_.__tablename__
        for mapper in Base.registry.mappers
        if any(c.key in ("therapeutic_area", "matched_therapeutic_area")
               for c in mapper.columns)
    }
    missing = with_ta - covered - known_area_level
    assert not missing, (
        f"tables carry a therapeutic_area but are not handled by the backfill: {sorted(missing)}"
    )


def test_table_specs_reference_real_columns():
    for spec in TABLES:
        assert isinstance(spec, TableSpec)
        columns = {c.key for c in spec.model.__table__.columns}
        attrs = [spec.ta_attr, spec.id_attr, spec.disease_attr, spec.indication_attr,
                 spec.brand_attr, spec.parent_attr, *spec.text_attrs]
        for attr in [a for a in attrs if a]:
            assert attr in columns, f"{spec.label}: {attr!r} is not a column"
