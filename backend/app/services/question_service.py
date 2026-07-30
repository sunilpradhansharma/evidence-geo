"""Question repository service (FR-101..107, SE-002, DM-003)."""
import csv
import io
import json
import uuid
from functools import lru_cache

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import taxonomy
from app.config.analyst_questions import ANALYST_QUESTION_DESIGNATIONS, ANALYST_QUESTIONS
from app.config.taxonomy import area_for
from app.models.harvested_question import HarvestedQuestion
from app.models.prompt_volume import PromptVolumeStaging
from app.models.question import Question, utcnow
from app.models.question_variation import QuestionVariation
from app.prompt_volume import gap as pv_gap
from app.prompt_volume.mapping import map_query
from app.prompt_volume.parser import resolve_text_column
from app.schemas import QuestionCreate, QuestionUpdate
from app.utils.pii_lint import scan_for_pii

# Bulk prompt-import guardrails: drop rows that are too short to be a real question or
# implausibly long (e.g. a mis-mapped answer column) so only clean prompts enter the bank.
_MIN_PROMPT_LEN = 6
_MAX_PROMPT_LEN = 500


def _new_question_id() -> str:
    return f"Q-{uuid.uuid4().hex[:10]}"


def _dump_competitor_focus(value) -> str | None:
    """Serialize a competitor_focus list (or None) to a JSON string for storage."""
    if not value:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value)


async def create_question(db: AsyncSession, data: QuestionCreate) -> Question:
    q = Question(
        question_id=_new_question_id(),
        question_text=data.question_text,
        persona=data.persona,
        therapeutic_area=data.therapeutic_area,
        indication=data.indication,
        disease=data.disease,
        brand_focus=data.brand_focus,
        monitoring_mode=data.monitoring_mode,
        competitor_focus=_dump_competitor_focus(data.competitor_focus),
        domain=data.domain,
        approval_status=data.approval_status,
        approver_name=data.approver_name,
        active=data.active,
        priority_weight=data.priority_weight,
        demand_origin=data.demand_origin,
        version=1,
    )
    db.add(q)
    await db.commit()
    await db.refresh(q)
    return q


def _extract_prompts(content: bytes) -> tuple[str | None, str | None, list[str]]:
    """Decode + parse a CSV, returning ``(column, kind, ordered raw prompt strings)``.

    Reads a SINGLE prompt/question column — the shape of a Profound / AlsoAsked / "People
    Also Ask" export — ignoring every other column (answers, metrics). Row order is
    preserved and nothing is deduped or scanned here; callers decide policy. ``kind`` is
    "prompt" (a real question column) or "query" (a bare keyword column). Returns
    ``(None, None, [])`` when no prompt/question/keyword column is present.
    """
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    col, kind = resolve_text_column(reader.fieldnames or [])
    if not col:
        return None, None, []
    prompts = [(row.get(col) or "").strip() for row in reader]
    return col, kind, [p for p in prompts if p]


def _derive_ta(brand_focus: str, therapeutic_area: str | None, disease: str | None = None) -> str:
    """Therapeutic area for an imported question, when it is not explicitly pinned.

    Resolution order is **disease first, brand second**. A focus brand does NOT map to
    exactly one therapeutic area — Rinvoq spans Dermatology (AD), Gastroenterology
    (UC, CD) and Rheumatology (RA, PsA, AS, nr-axSpA), and Humira spans all three.
    Deriving from the brand alone is what landed the RA/PsA workshop set under
    "Immunology" and required ``scripts/hotfix_rhem_therapeutic_area.py`` to repair it
    after the fact.

    When no disease is known and the brand spans several areas, this returns "Unmapped"
    rather than picking one. ``map_query`` would happily answer — but its answer is
    whichever block brands.yaml declares first, so it would file a Rinvoq question under
    Dermatology purely because Dermatology is at the top of the file. A visible
    "Unmapped" is reviewable; a plausible wrong area is not. Single-area brands
    (Vraylar, Imbruvica, Lupron) are unaffected and still resolve from the brand.
    """
    pinned = (therapeutic_area or "").strip()
    if pinned:
        return pinned

    mapped = map_query(brand_focus or "")
    resolved_disease = (disease or "").strip() or mapped.get("disease")
    by_disease = taxonomy.therapeutic_area_key_for_disease(resolved_disease)
    if by_disease:
        return by_disease

    if len(taxonomy.area_keys_for_brand(mapped.get("brand") or brand_focus)) > 1:
        return "Unmapped"

    return mapped.get("therapeutic_area") or "Unmapped"


def _derive_disease(question_text: str, disease: str | None = None) -> str | None:
    """Indication named by a question, preferring an explicitly supplied value.

    Per-question rather than per-batch: a bulk import shares one brand and persona but
    its prompts routinely span indications, and the disease is what makes a historical
    response indication-comparable.
    """
    explicit = taxonomy.canonical_disease(disease)
    if explicit:
        return explicit
    return map_query(question_text or "").get("disease")


def _classify_prompts(raw_prompts: list[str], seen: set[str]) -> tuple[list[str], int, list[dict]]:
    """Split raw prompts into ``(new, duplicate_count, skipped)`` applying the import policy.

    Policy per prompt: length guard -> normalized dedupe (within this list AND against the
    ``seen`` set of already-known normalized texts, which is mutated) -> PII scan. Skipped
    rows carry a human reason so the UI can show WHY a prompt was dropped rather than
    silently losing it. ``new`` preserves first-seen order.
    """
    new: list[str] = []
    duplicates = 0
    skipped: list[dict] = []
    for raw in raw_prompts:
        raw = (raw or "").strip()
        if not raw:
            continue
        if len(raw) < _MIN_PROMPT_LEN or len(raw) > _MAX_PROMPT_LEN:
            skipped.append({"text": raw[:120], "reason": "too short / too long"})
            continue
        norm = pv_gap.normalize(raw)
        if not norm:
            skipped.append({"text": raw[:120], "reason": "no meaningful terms"})
            continue
        if norm in seen:
            duplicates += 1
            continue
        seen.add(norm)
        pii = scan_for_pii(raw)
        if pii:
            skipped.append({"text": raw[:120], "reason": f"PII: {', '.join(pii)}"})
            continue
        new.append(raw)
    return new, duplicates, skipped


async def preview_prompts(
    db: AsyncSession,
    *,
    content: bytes,
    persona: str,
    brand_focus: str,
    domain: str = "General",
    therapeutic_area: str | None = None,
) -> dict:
    """DRY RUN: extract the distinct, importable questions from a CSV WITHOUT persisting.

    Powers the preview step of the bulk importer — the analyst sees exactly which questions
    would be added (deduped against the file and the existing bank), how many duplicates were
    collapsed, and what was skipped for PII, before committing anything. Raises ``ValueError``
    when no prompt/question/keyword column can be found.
    """
    col, kind, raw_prompts = _extract_prompts(content)
    if not col:
        raise ValueError(
            "No question/prompt column found. Expected a column such as "
            "'prompt', 'question', or 'keyword'."
        )
    existing = await list_questions(db, persona=persona, brand_focus=brand_focus, limit=10000)
    seen: set[str] = {pv_gap.normalize(q.question_text) for q in existing}
    new, duplicates, skipped = _classify_prompts(raw_prompts, seen)
    return {
        "questions": new,                         # distinct, clean, not-already-in-bank
        "duplicates": duplicates,                 # collapsed (within file + already in bank)
        "skipped": skipped,                       # dropped for PII / length, with reasons
        "total_rows": len(raw_prompts),
        "persona": persona,
        "brand_focus": brand_focus,
        "therapeutic_area": _derive_ta(brand_focus, therapeutic_area),
        "domain": domain,
        # PROMPT = real question column ("Real"); KEYWORD = bare keyword column.
        "demand_origin": "PROMPT" if kind == "prompt" else "KEYWORD",
        "prompt_column": col,
    }


async def commit_prompts(
    db: AsyncSession,
    *,
    questions: list[str],
    persona: str,
    brand_focus: str,
    domain: str = "General",
    therapeutic_area: str | None = None,
    demand_origin: str = "PROMPT",
) -> dict:
    """Persist the analyst-approved subset of extracted questions into the bank as PENDING.

    The second step of the importer: takes the exact list the analyst kept in the preview and
    applies the shared persona / brand / theme. Each question lands **PENDING** with its
    demand-origin label so Medical Affairs still reviews it before any run. Dedupe + PII are
    re-checked defensively (the bank may have changed since preview); skips are reported.
    """
    ta = _derive_ta(brand_focus, therapeutic_area)
    origin = demand_origin if demand_origin in ("PROMPT", "KEYWORD") else "PROMPT"
    existing = await list_questions(db, persona=persona, brand_focus=brand_focus, limit=10000)
    seen: set[str] = {pv_gap.normalize(q.question_text) for q in existing}
    new, duplicates, skipped = _classify_prompts(list(questions or []), seen)

    # A batch shares one brand and persona but its prompts routinely span indications, so
    # the disease — and therefore the therapeutic area — is resolved PER ROW. An explicitly
    # pinned therapeutic_area still wins for every row.
    area_counts: dict[str, int] = {}
    for raw in new:
        row_disease = _derive_disease(raw)
        row_ta = _derive_ta(brand_focus, therapeutic_area, disease=row_disease)
        area_counts[row_ta] = area_counts.get(row_ta, 0) + 1
        db.add(Question(
            question_id=_new_question_id(),
            question_text=raw,
            persona=persona,
            therapeutic_area=row_ta,
            disease=row_disease,
            brand_focus=brand_focus,
            monitoring_mode="BRAND",
            domain=domain,
            approval_status="PENDING",
            demand_origin=origin,
            priority_weight=1.0,
            version=1,
        ))
    await db.commit()
    return {
        "imported": len(new),
        "duplicates": duplicates,
        "skipped": skipped,
        "persona": persona,
        "brand_focus": brand_focus,
        "therapeutic_area": ta,
        # Additive: the actual per-row split, so a mixed batch is visible rather than
        # hidden behind one batch-level label.
        "therapeutic_areas": area_counts,
        "demand_origin": origin,
    }


@lru_cache(maxsize=1)
def _analyst_norms() -> frozenset[str]:
    """Normalized texts of the curated analyst question set (Rhem.csv).

    Uses the same normalization as prompt ingestion so matching is insensitive to
    case, punctuation, apostrophe style, and surrounding whitespace.
    """
    return frozenset(n for n in (pv_gap.normalize(p) for p in ANALYST_QUESTIONS) if n)


@lru_cache(maxsize=1)
def _analyst_designations() -> dict[str, str]:
    """Normalized workshop prompt text -> designation (e.g. "Patient RA", "HCP PsA").

    Same normalization as the Workshop Questions filter so the mapping is insensitive
    to case / punctuation / apostrophe / whitespace. Derived from Rhem.csv's
    Persona + TA columns; the one HCP/"Both" row is labelled "HCP RA & PsA".
    """
    out: dict[str, str] = {}
    for prompt, designation in ANALYST_QUESTION_DESIGNATIONS:
        norm = pv_gap.normalize(prompt)
        if norm:
            out[norm] = designation
    return out


async def attach_designation(db: AsyncSession, questions: list[Question]) -> None:
    """Populate QuestionOut.designation (computed, not stored) for the given rows.

    The workshop set (Rhem.csv) carries a Persona + indication designation
    (Patient RA / Patient PsA / HCP RA / HCP PsA / HCP RA & PsA). Base questions are
    matched on normalized TEXT; variations inherit their base question's designation
    (variation_group_id == base question_id). Non-workshop questions get None.

    Transient attribute (no column, no migration), same pattern as attach_question_source.
    """
    if not questions:
        return
    desig = _analyst_designations()
    for q in questions:
        q.designation = None

    # Variations inherit their base question's designation — resolve the base text.
    group_ids = {q.variation_group_id for q in questions if q.is_variation and q.variation_group_id}
    base_text_by_gid: dict[str, str] = {}
    if group_ids:
        rows = (await db.execute(
            select(Question.question_id, Question.question_text).where(
                Question.question_id.in_(group_ids),
                Question.deleted_at.is_(None),
                Question.superseded_by.is_(None),
            )
        )).all()
        base_text_by_gid = {qid: text for qid, text in rows}

    for q in questions:
        if q.is_variation:
            base_text = base_text_by_gid.get(q.variation_group_id)
            if base_text:
                q.designation = desig.get(pv_gap.normalize(base_text))
        else:
            q.designation = desig.get(pv_gap.normalize(q.question_text))


async def analyst_question_ids(db: AsyncSession) -> set[str]:
    """Stable question_ids of the curated analyst/workshop set (Rhem.csv): base questions
    matched on normalized text PLUS their phrasing variations.

    Used to scope OTHER lists (e.g. the AI Response Review) to the workshop set. Matching
    on question_id — not text — means it survives edits/versioning and lets response rows
    (which store the run-time question text) be filtered by their stable id.
    """
    norms = _analyst_norms()
    rows = (await db.execute(
        select(Question.question_id, Question.question_text).where(
            Question.is_variation.is_(False),
            Question.deleted_at.is_(None),
            Question.superseded_by.is_(None),
        )
    )).all()
    base_ids = {qid for qid, text in rows if pv_gap.normalize(text) in norms}
    if not base_ids:
        return set()
    # Their variations share variation_group_id == the base question_id.
    var_ids = {
        qid for (qid,) in (await db.execute(
            select(Question.question_id).where(Question.variation_group_id.in_(base_ids))
        )).all()
    }
    return base_ids | var_ids


async def analyst_designation_map(db: AsyncSession) -> dict[str, str]:
    """question_id -> designation for the whole workshop set (base questions + variations).

    Keys are exactly the ids returned by ``analyst_question_ids`` (base questions matched
    on normalized text PLUS their variations, which inherit the base's designation). Used
    to tag response rows / the CSV export with a Persona + indication designation when the
    Workshop Questions filter is on. Returns {} when nothing in the bank matches.
    """
    desig = _analyst_designations()
    rows = (await db.execute(
        select(Question.question_id, Question.question_text).where(
            Question.is_variation.is_(False),
            Question.deleted_at.is_(None),
            Question.superseded_by.is_(None),
        )
    )).all()
    base = {qid: desig[n] for qid, text in rows if (n := pv_gap.normalize(text)) in desig}
    if not base:
        return {}
    # Variations inherit their base's designation (variation_group_id == base question_id).
    var_rows = (await db.execute(
        select(Question.question_id, Question.variation_group_id).where(
            Question.variation_group_id.in_(base.keys())
        )
    )).all()
    out = dict(base)
    for qid, gid in var_rows:
        if gid in base:
            out[qid] = base[gid]
    return out


def _scope(value: "str | list[str] | None") -> list[str]:
    """Normalise a filter that accepts either one value or several. Blanks are dropped.

    Several values on one field mean OR (persona in Patient, Provider); different fields
    still intersect. A caller passing a bare string keeps its old exact-match behaviour.
    """
    if value is None:
        return []
    values = [value] if isinstance(value, str) else list(value)
    return [v.strip() for v in values if isinstance(v, str) and v.strip()]


async def list_questions(
    db: AsyncSession,
    *,
    persona: "str | list[str] | None" = None,
    therapeutic_area: "str | list[str] | None" = None,
    indication: "str | list[str] | None" = None,
    disease: "str | list[str] | None" = None,
    brand_focus: "str | list[str] | None" = None,
    domain: "str | list[str] | None" = None,
    approval_status: "str | list[str] | None" = None,
    active: bool | None = None,
    analyst: bool = False,
    include_deleted: bool = False,
    only_current: bool = True,
    limit: int = 500,
    offset: int = 0,
) -> list[Question]:
    stmt = select(Question)
    for column, raw in (
        (Question.persona, persona),
        (Question.therapeutic_area, therapeutic_area),
        (Question.indication, indication),
        (Question.disease, disease),
        (Question.brand_focus, brand_focus),
        (Question.domain, domain),
        (Question.approval_status, approval_status),
    ):
        wanted = _scope(raw)
        if wanted:
            stmt = stmt.where(column.in_(wanted))
    if active is not None:
        stmt = stmt.where(Question.active == active)
    if not include_deleted:
        stmt = stmt.where(Question.deleted_at.is_(None))
    if only_current:
        stmt = stmt.where(Question.superseded_by.is_(None))
    stmt = stmt.order_by(Question.created_at.desc())
    if analyst:
        # The curated analyst set (Rhem.csv) has no stored marker, so it is matched on
        # normalized question TEXT — which SQL can't express. Pull the current bank
        # (already narrowed by any other active filters), keep base questions whose
        # normalized text is in the set, then page in Python. The set is tiny (~21),
        # so the in-memory pass is cheap. Variations are reached via each base
        # question's expand-dropdown, so only base rows are returned here.
        norms = _analyst_norms()
        rows = (await db.execute(stmt.limit(10000))).scalars().all()
        matched = [
            q for q in rows
            if not q.is_variation and pv_gap.normalize(q.question_text) in norms
        ]
        return matched[offset:offset + limit] if limit else matched[offset:]
    stmt = stmt.limit(limit).offset(offset)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def attach_variation_lineage(db: AsyncSession, questions: list[Question]) -> None:
    """Populate QuestionOut's computed lineage fields on the given rows, both directions.

    Forward (variation -> original): ``variation_of_text`` = the CURRENT text of the source
    question. Reverse (original -> variations): ``variation_count`` = how many variations were
    created from it (ALL staged statuses: draft + approved + rejected), read from
    ``question_variations`` (the full-history source of truth).

    Sets transient attributes on the ORM instances (no columns, no migration). Defaults are
    written on every row so ``QuestionOut.from_attributes`` always finds them.
    """
    if not questions:
        return
    for q in questions:
        q.variation_of_text = None
        q.variation_count = 0

    # Forward: resolve each variation's source question_id -> current (non-deleted) text.
    source_ids = {q.variation_of for q in questions if q.is_variation and q.variation_of}
    if source_ids:
        rows = (await db.execute(
            select(Question.question_id, Question.question_text).where(
                Question.question_id.in_(source_ids),
                Question.deleted_at.is_(None),
                Question.superseded_by.is_(None),
            )
        )).all()
        text_by_qid = {qid: text for qid, text in rows}
        for q in questions:
            if q.is_variation and q.variation_of:
                q.variation_of_text = text_by_qid.get(q.variation_of)

    # Reverse: count staged variations per original (grouped by variation_group_id == base id).
    base_ids = {q.question_id for q in questions if not q.is_variation}
    if base_ids:
        counts = (await db.execute(
            select(QuestionVariation.variation_group_id, func.count())
            .where(QuestionVariation.variation_group_id.in_(base_ids))
            .group_by(QuestionVariation.variation_group_id)
        )).all()
        count_by_gid = {gid: n for gid, n in counts}
        for q in questions:
            if not q.is_variation:
                q.variation_count = count_by_gid.get(q.question_id, 0)


async def attach_question_source(db: AsyncSession, questions: list[Question]) -> None:
    """Populate QuestionOut.source (computed, not stored) for the given rows.

    Derived with precedence VARIATION > PROMPT_VOLUME(demand_origin) > DISCOVER >
    PROMPT_VOLUME(exact staged match) > MANUAL:
      * VARIATION      — an AI-generated paraphrase (is_variation)
      * PROMPT_VOLUME  — added via the prompt/keyword importer (demand_origin set), OR whose
                         text was ingested VERBATIM as a Prompt Volume search (an exact
                         staged match, score 1.0). The exact-match fallback recovers
                         prompt-volume questions created before demand_origin was persisted;
                         sub-1.0 (merely similar) coverage matches are NOT counted so genuine
                         manual questions aren't mislabeled.
      * DISCOVER       — harvested from the web then promoted (question_id matches a
                         harvested_questions.promoted_question_id)
      * MANUAL         — everything else (New Question modal / generic CSV import)

    Transient attribute (no column, no migration), same pattern as attach_variation_lineage.
    """
    if not questions:
        return
    for q in questions:
        q.source = None

    qids = [q.question_id for q in questions]
    promoted: set[str] = set()
    pv_matched: set[str] = set()
    if qids:
        # Discover: which of these question_ids were promoted from a harvested question.
        promoted = {pid for (pid,) in (await db.execute(
            select(HarvestedQuestion.promoted_question_id).where(
                HarvestedQuestion.promoted_question_id.in_(qids)
            )
        )).all() if pid}
        # Prompt Volume (inferred): question text ingested verbatim as a staged search
        # (exact match only, score 1.0) — not sub-threshold coverage similarity.
        pv_matched = {qid for (qid,) in (await db.execute(
            select(PromptVolumeStaging.matched_question_id).where(
                PromptVolumeStaging.matched_question_id.in_(qids),
                PromptVolumeStaging.match_score >= 1.0,
            )
        )).all() if qid}

    for q in questions:
        if q.is_variation:
            q.source = "VARIATION"
        elif q.demand_origin:
            q.source = "PROMPT_VOLUME"
        elif q.question_id in promoted:
            q.source = "DISCOVER"
        elif q.question_id in pv_matched:
            q.source = "PROMPT_VOLUME"
        else:
            q.source = "MANUAL"


async def get_question(db: AsyncSession, row_id: int) -> Question | None:
    return await db.get(Question, row_id)


async def approval_blockers(db: AsyncSession, question: Question) -> list[str]:
    """Why *question* may not be approved. Empty for everything but Phase-7 questions.

    Delegates rather than restating the rule, so there is one opinion about what backs an
    evidence question. Lives here because ``update_question`` is the choke point every
    approval path goes through — the UI, the copilot tool and the CSV importer all arrive
    via it, and a check placed in any one of them would be a check the others skip.
    """
    from app.services import evidence_question_service  # local import avoids a cycle

    return await evidence_question_service.approval_blockers(db, question)


class QuestionApprovalBlocked(ValueError):
    """An approval refused because its evidence has not been verified."""

    def __init__(self, blockers: list[str]) -> None:
        self.blockers = blockers
        super().__init__("; ".join(blockers))


async def update_question(db: AsyncSession, row_id: int, data: QuestionUpdate) -> Question | None:
    """Edits create a NEW version row; the old row is marked superseded (FR-103)."""
    current = await db.get(Question, row_id)
    if current is None or current.deleted_at is not None:
        return None

    payload = data.model_dump(exclude_none=True)

    # Phase 7 invariant. An evidence-generated question may not reach APPROVED with zero
    # VERIFIED evidence associations. Checked on the transition rather than on every save,
    # so editing a blocked question's wording still works — what is refused is asserting it
    # is fit to run over evidence nobody has checked.
    if (
        payload.get("approval_status") == "APPROVED"
        and current.approval_status != "APPROVED"
    ):
        blockers = await approval_blockers(db, current)
        if blockers:
            raise QuestionApprovalBlocked(blockers)

    competitor_focus = (
        _dump_competitor_focus(payload["competitor_focus"])
        if "competitor_focus" in payload
        else current.competitor_focus
    )
    new_q = Question(
        question_id=current.question_id,  # stable logical id
        question_text=payload.get("question_text", current.question_text),
        persona=payload.get("persona", current.persona),
        therapeutic_area=payload.get("therapeutic_area", current.therapeutic_area),
        indication=payload.get("indication", current.indication),
        disease=payload.get("disease", current.disease),
        brand_focus=payload.get("brand_focus", current.brand_focus),
        monitoring_mode=payload.get("monitoring_mode", current.monitoring_mode),
        competitor_focus=competitor_focus,
        domain=payload.get("domain", current.domain),
        approval_status=payload.get("approval_status", current.approval_status),
        approver_name=payload.get("approver_name", current.approver_name),
        active=payload.get("active", current.active),
        priority_weight=payload.get("priority_weight", current.priority_weight),
        # Carry provenance/lineage forward so an edit (new version) keeps its tags/traceability.
        demand_origin=current.demand_origin,
        variation_group_id=current.variation_group_id,
        variation_of=current.variation_of,
        is_variation=current.is_variation,
        generation_method=current.generation_method,
        version=current.version + 1,
    )
    db.add(new_q)
    await db.flush()  # get new_q.id
    current.superseded_by = new_q.id
    await db.commit()
    await db.refresh(new_q)
    return new_q


async def soft_delete_question(db: AsyncSession, row_id: int, reason: str) -> Question | None:
    """Soft delete (DM-003) — never physically removed."""
    current = await db.get(Question, row_id)
    if current is None:
        return None
    current.deleted_at = utcnow()
    current.delete_reason = reason
    current.active = False
    await db.commit()
    await db.refresh(current)
    return current


async def coverage_report(db: AsyncSession) -> dict:
    """Coverage by persona, therapeutic area, and indication (FR-107, supports AC-02 evidence).

    ``by_therapeutic_area`` is the granular indication-level breakdown (the stored
    join key); ``by_area`` rolls those up to the parent therapeutic area via
    brands.yaml (e.g. Endometriosis + Uterine Fibroids -> Women's Health).
    """
    questions = await list_questions(db, active=True, approval_status="APPROVED", limit=10000)
    by_persona: dict[str, int] = {}
    by_ta: dict[str, int] = {}
    by_area: dict[str, int] = {}
    by_domain: dict[str, int] = {}
    for q in questions:
        by_persona[q.persona] = by_persona.get(q.persona, 0) + 1
        by_ta[q.therapeutic_area] = by_ta.get(q.therapeutic_area, 0) + 1
        area = area_for(q.therapeutic_area)
        by_area[area] = by_area.get(area, 0) + 1
        by_domain[q.domain] = by_domain.get(q.domain, 0) + 1
    return {
        "total_active_approved": len(questions),
        "by_persona": by_persona,
        "by_area": by_area,
        "by_therapeutic_area": by_ta,
        "by_domain": by_domain,
    }


def brand_matrix() -> dict:
    """Therapeutic area → indication → diseases → brands, straight from the taxonomy.

    The taxonomy (the single source of truth, SE-007) stores each monitored entry
    under an indication-level key with a parent ``area``. We emit ONE row per
    indication with its diseases (the union of the indications declared by that
    block's focus brands), the AbbVie focus brands, and the competitor brands kept
    in a separate list so the UI can render them in their own column.
    """
    areas_cfg = taxonomy.config().get("therapeutic_areas", {}) or {}

    def _brands(items: list[dict]) -> list[dict]:
        return [
            {"brand": b.get("name"), "company": b.get("company")}
            for b in items or []
            if b.get("name")
        ]

    rows: list[dict] = []
    for indication, block in areas_cfg.items():
        focus_brands = block.get("focus_brands", []) or []

        # Diseases for this indication = union of all focus-brand indications,
        # preserving first-seen order.
        diseases: list[str] = []
        seen: set[str] = set()
        for b in focus_brands:
            for ind in b.get("indications", []) or []:
                if ind not in seen:
                    seen.add(ind)
                    diseases.append(ind)

        rows.append({
            "area": block.get("area") or indication,
            "indication": indication,
            "diseases": diseases,
            "focus_brands": _brands(focus_brands),
            "competitors": _brands(block.get("competitors", [])),
        })

    rows.sort(key=lambda r: (r["area"], r["indication"]))
    return {"rows": rows}
