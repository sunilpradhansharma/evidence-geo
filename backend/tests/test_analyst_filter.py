"""Questions-tab "Analyst" filter tests.

The curated analyst set (Rhem.csv) is already in PROD with no stored marker, so the
filter matches on NORMALIZED question text. These tests prove that:
  * a base question is matched despite case / punctuation / apostrophe / whitespace drift
  * unrelated bank questions are excluded
  * variation rows are excluded (they surface via each base's expand-dropdown), even
    when their text happens to equal a curated prompt
  * the default (analyst=False) path is unchanged
"""
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config.analyst_questions import ANALYST_QUESTION_DESIGNATIONS, ANALYST_QUESTIONS
from app.models.database import Base
from app.models.question import Question
from app.models.response import Response
from app.prompt_volume import gap as pv_gap
from app.services import export_service
from app.services import question_service as svc
from app.services import response_service as rsvc


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from app.models import alert, question, response, scoring  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _add(session, qid, text, **kw) -> Question:
    q = Question(
        question_id=qid,
        question_text=text,
        persona=kw.get("persona", "Patient"),
        therapeutic_area=kw.get("therapeutic_area", "Rheumatology"),
        brand_focus=kw.get("brand_focus", "Rinvoq"),
        domain=kw.get("domain", "General"),
        monitoring_mode="BRAND",
        approval_status=kw.get("approval_status", "APPROVED"),
        active=kw.get("active", True),
        is_variation=kw.get("is_variation", False),
        variation_of=kw.get("variation_of"),
        variation_group_id=kw.get("variation_group_id"),
    )
    session.add(q)
    await session.commit()
    await session.refresh(q)
    return q


async def _add_response(session, rid, qid, text, **kw) -> Response:
    r = Response(
        response_id=rid,
        run_id=kw.get("run_id", "run-1"),
        llm_name=kw.get("llm_name", "Claude"),
        persona=kw.get("persona", "Patient"),
        question_id=qid,
        question_text=text,
        therapeutic_area=kw.get("therapeutic_area", "Rheumatology"),
        domain=kw.get("domain", "General"),
        response_text="...",
        status="SUCCESS",
    )
    session.add(r)
    await session.commit()
    return r


def test_analyst_norms_cover_every_curated_prompt():
    norms = svc._analyst_norms()
    assert norms                                   # non-empty
    for prompt in ANALYST_QUESTIONS:
        assert pv_gap.normalize(prompt) in norms


async def test_analyst_filter_matches_despite_text_drift(session):
    # Exact match.
    await _add(session, "Q-EXACT", "Who is a good candidate for RINVOQ?")
    # Case / straight-apostrophe / extra punctuation drift — still normalizes equal.
    await _add(session, "Q-DRIFT", "what's the difference between rinvoq and xeljanz???")
    # Leading/trailing whitespace + case drift.
    await _add(session, "Q-SPACE", "   why does RINVOQ have a boxed warning?  ")
    # Unrelated question — must NOT match.
    await _add(session, "Q-OTHER", "How much does Humira cost per month?")

    rows = await svc.list_questions(session, analyst=True, limit=500)
    ids = {q.question_id for q in rows}
    assert ids == {"Q-EXACT", "Q-DRIFT", "Q-SPACE"}


async def test_analyst_filter_excludes_variation_rows(session):
    base = await _add(session, "Q-BASE", "Who is a good candidate for RINVOQ?")
    # A promoted variation whose text ALSO equals a curated prompt: the is_variation
    # guard must still keep it out of the top-level analyst list.
    await _add(
        session, "Q-VAR", "Who is a good candidate for RINVOQ?",
        is_variation=True, variation_of=base.question_id,
        variation_group_id=base.question_id,
    )

    rows = await svc.list_questions(session, analyst=True, limit=500)
    ids = {q.question_id for q in rows}
    assert ids == {"Q-BASE"}
    assert all(not q.is_variation for q in rows)


async def test_analyst_flag_off_returns_everything(session):
    await _add(session, "Q-EXACT", "Who is a good candidate for RINVOQ?")
    await _add(session, "Q-OTHER", "How much does Humira cost per month?")

    rows = await svc.list_questions(session, limit=500)  # analyst defaults False
    ids = {q.question_id for q in rows}
    assert ids == {"Q-EXACT", "Q-OTHER"}


async def test_analyst_filter_composes_with_persona(session):
    await _add(session, "Q-PAT", "Who is a good candidate for RINVOQ?", persona="Patient")
    await _add(session, "Q-PROV", "What is the best treatment for rheumatoid arthritis?", persona="Provider")

    rows = await svc.list_questions(session, analyst=True, persona="Patient", limit=500)
    ids = {q.question_id for q in rows}
    assert ids == {"Q-PAT"}


async def test_analyst_question_ids_includes_base_and_variations(session):
    base = await _add(session, "Q-BASE", "Who is a good candidate for RINVOQ?")
    await _add(
        session, "Q-VAR", "Is RINVOQ right for me?",
        is_variation=True, variation_of=base.question_id,
        variation_group_id=base.question_id,
    )
    await _add(session, "Q-OTHER", "How much does Humira cost per month?")

    ids = await svc.analyst_question_ids(session)
    assert ids == {"Q-BASE", "Q-VAR"}


async def test_query_responses_analyst_scopes_to_workshop_and_variations(session):
    # The AI Response Review reuses the same set: responses to a workshop base question
    # OR one of its variations are kept; responses to unrelated questions are dropped.
    base = await _add(session, "Q-BASE", "Who is a good candidate for RINVOQ?")
    await _add(
        session, "Q-VAR", "Is RINVOQ right for me?",
        is_variation=True, variation_of=base.question_id,
        variation_group_id=base.question_id,
    )
    await _add(session, "Q-OTHER", "How much does Humira cost per month?")
    await _add_response(session, "r-base", "Q-BASE", "Who is a good candidate for RINVOQ?")
    await _add_response(session, "r-var", "Q-VAR", "Is RINVOQ right for me?")
    await _add_response(session, "r-other", "Q-OTHER", "How much does Humira cost per month?")

    scoped = await rsvc.query_responses(session, analyst=True, limit=100)
    assert scoped["total"] == 2
    assert {i["question_id"] for i in scoped["items"]} == {"Q-BASE", "Q-VAR"}

    everything = await rsvc.query_responses(session, limit=100)  # analyst defaults False
    assert everything["total"] == 3
    assert {i["question_id"] for i in everything["items"]} == {"Q-BASE", "Q-VAR", "Q-OTHER"}


async def test_query_responses_analyst_empty_set_returns_no_rows(session):
    # No workshop questions in the bank -> the scoped response list is empty (not "all").
    await _add(session, "Q-OTHER", "How much does Humira cost per month?")
    await _add_response(session, "r-other", "Q-OTHER", "How much does Humira cost per month?")

    scoped = await rsvc.query_responses(session, analyst=True, limit=100)
    assert scoped == {"total": 0, "count": 0, "items": []}


# --------------------------------------------------------------------------- #
#  Designation (Persona + indication from Rhem.csv) — Patient/HCP × RA/PsA     #
# --------------------------------------------------------------------------- #

def test_analyst_designations_cover_every_prompt_and_are_valid():
    desig = svc._analyst_designations()
    valid = {"Patient RA", "Patient PsA", "HCP RA", "HCP PsA", "HCP RA & PsA"}
    # Every curated prompt has a designation drawn from the allowed set.
    for prompt in ANALYST_QUESTIONS:
        norm = pv_gap.normalize(prompt)
        assert norm in desig
        assert desig[norm] in valid
    # Every pair in the config resolves through normalization too.
    assert len(desig) == len({pv_gap.normalize(p) for p, _ in ANALYST_QUESTION_DESIGNATIONS})


def test_analyst_designations_specific_mappings():
    desig = svc._analyst_designations()
    assert desig[pv_gap.normalize("Who is a good candidate for RINVOQ?")] == "Patient RA"
    assert desig[pv_gap.normalize("Which PsA drug is best by disease domain?")] == "HCP PsA"
    assert desig[pv_gap.normalize("Why does RINVOQ have a boxed warning?")] == "Patient PsA"
    assert desig[pv_gap.normalize(
        "Based on the black box warning for JAK inhibitors, how should I weigh "
        "cardiovascular risk in a 55-year-old RA patient versus switching to a second anti-TNF?"
    )] == "HCP RA"
    # The one HCP/"Both" row is labelled "HCP RA & PsA".
    assert desig[pv_gap.normalize(
        "Are there particular patient sub-types or biomarkers at higher risk for VTE, "
        "CV events, etc.? Beyond known risk factors (diabetes, smoking), are there "
        "biomarkers we could measure to identify risk?"
    )] == "HCP RA & PsA"


async def test_attach_designation_tags_base_and_inherits_to_variations(session):
    base = await _add(session, "Q-BASE", "Who is a good candidate for RINVOQ?")   # Patient RA
    var = await _add(
        session, "Q-VAR", "Is RINVOQ right for me?",
        is_variation=True, variation_of=base.question_id,
        variation_group_id=base.question_id,
    )
    other = await _add(session, "Q-OTHER", "How much does Humira cost per month?")

    await svc.attach_designation(session, [base, var, other])
    assert base.designation == "Patient RA"
    assert var.designation == "Patient RA"   # inherited from its base
    assert other.designation is None         # not in the workshop set


async def test_analyst_designation_map_covers_base_and_variations(session):
    base = await _add(session, "Q-BASE", "Which PsA drug is best by disease domain?")  # HCP PsA
    await _add(
        session, "Q-VAR", "What's the top PsA medication by domain?",
        is_variation=True, variation_of=base.question_id,
        variation_group_id=base.question_id,
    )
    await _add(session, "Q-OTHER", "How much does Humira cost per month?")

    dmap = await svc.analyst_designation_map(session)
    assert dmap == {"Q-BASE": "HCP PsA", "Q-VAR": "HCP PsA"}


async def test_query_responses_analyst_tags_designation(session):
    base = await _add(session, "Q-BASE", "Who is a good candidate for RINVOQ?")   # Patient RA
    await _add(
        session, "Q-VAR", "Is RINVOQ right for me?",
        is_variation=True, variation_of=base.question_id,
        variation_group_id=base.question_id,
    )
    await _add_response(session, "r-base", "Q-BASE", "Who is a good candidate for RINVOQ?")
    await _add_response(session, "r-var", "Q-VAR", "Is RINVOQ right for me?")

    scoped = await rsvc.query_responses(session, analyst=True, limit=100)
    by_qid = {i["question_id"]: i for i in scoped["items"]}
    assert by_qid["Q-BASE"]["designation"] == "Patient RA"
    assert by_qid["Q-VAR"]["designation"] == "Patient RA"


async def test_query_responses_without_analyst_has_no_designation(session):
    await _add(session, "Q-BASE", "Who is a good candidate for RINVOQ?")
    await _add_response(session, "r-base", "Q-BASE", "Who is a good candidate for RINVOQ?")

    everything = await rsvc.query_responses(session, limit=100)  # analyst defaults False
    assert all("designation" not in i for i in everything["items"])


def test_to_csv_includes_designation_only_when_requested():
    items = [{
        "response_id": "r1", "run_id": "run-1", "llm_name": "Claude",
        "persona": "Patient", "designation": "Patient RA",
        "question_id": "Q-BASE", "question_text": "Who is a good candidate for RINVOQ?",
        "therapeutic_area": "Rheumatology", "brand_focus": "Rinvoq",
        "status": "SUCCESS", "response_text": "...",
    }]

    # Workshop export: Designation column present, immediately after persona, populated.
    with_col = export_service.to_csv(items, include_designation=True)
    header = with_col.splitlines()[0].split(",")
    assert "designation" in header
    assert header[header.index("persona") + 1] == "designation"
    assert "Patient RA" in with_col

    # Default export: unchanged — no Designation column at all.
    without_col = export_service.to_csv(items)
    assert "designation" not in without_col.splitlines()[0].split(",")
    assert "Patient RA" not in without_col


# --------------------------------------------------------------------------- #
#  Multi-select download filters — therapeutic areas + designations           #
# --------------------------------------------------------------------------- #
async def test_query_responses_filters_by_multiple_therapeutic_areas(session):
    # Multi-select TA scopes the list/download to the UNION of the chosen areas.
    await _add_response(session, "r-imm", "Q1", "q one", therapeutic_area="Immunology")
    await _add_response(session, "r-onc", "Q2", "q two", therapeutic_area="Oncology")
    await _add_response(session, "r-rhe", "Q3", "q three", therapeutic_area="Rheumatology")

    scoped = await rsvc.query_responses(
        session, therapeutic_areas=["Immunology", "Oncology"], limit=100
    )
    assert scoped["total"] == 2
    assert {i["therapeutic_area"] for i in scoped["items"]} == {"Immunology", "Oncology"}

    # The single-value form still works (and is superseded by the plural when both given).
    single = await rsvc.query_responses(session, therapeutic_area="Rheumatology", limit=100)
    assert {i["therapeutic_area"] for i in single["items"]} == {"Rheumatology"}


async def test_query_responses_designations_scope_to_selected_labels_and_tag(session):
    # Two workshop base questions with different designations + one unrelated question.
    await _add(session, "Q-RA", "Who is a good candidate for RINVOQ?")         # Patient RA
    await _add(session, "Q-PSA", "Which PsA drug is best by disease domain?")  # HCP PsA
    await _add(session, "Q-OTHER", "How much does Humira cost per month?")     # not workshop
    await _add_response(session, "r-ra", "Q-RA", "Who is a good candidate for RINVOQ?")
    await _add_response(session, "r-psa", "Q-PSA", "Which PsA drug is best by disease domain?")
    await _add_response(session, "r-other", "Q-OTHER", "How much does Humira cost per month?")

    # A single designation narrows to just that label AND tags the row.
    ra_only = await rsvc.query_responses(session, designations=["Patient RA"], limit=100)
    assert ra_only["total"] == 1
    assert ra_only["items"][0]["question_id"] == "Q-RA"
    assert ra_only["items"][0]["designation"] == "Patient RA"

    # Multiple designations -> union of the selected workshop questions.
    both = await rsvc.query_responses(
        session, designations=["Patient RA", "HCP PsA"], limit=100
    )
    assert {i["question_id"] for i in both["items"]} == {"Q-RA", "Q-PSA"}
    assert {i["designation"] for i in both["items"]} == {"Patient RA", "HCP PsA"}


async def test_query_responses_designation_with_no_matching_bank_question_returns_no_rows(session):
    # The bank only carries a "Patient RA" question; asking for "HCP PsA" yields nothing.
    await _add(session, "Q-RA", "Who is a good candidate for RINVOQ?")  # Patient RA
    await _add_response(session, "r-ra", "Q-RA", "Who is a good candidate for RINVOQ?")

    scoped = await rsvc.query_responses(session, designations=["HCP PsA"], limit=100)
    assert scoped == {"total": 0, "count": 0, "items": []}
