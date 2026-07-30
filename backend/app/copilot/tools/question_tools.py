"""Question-bank + harvest promotion tools (confirmed writes; governance-aware).

Governance tools require a reviewer/approver name (enforced generically by the
executor before confirmation). Created/promoted questions land as PENDING, so
the copilot can never shortcut the Medical-Affairs approval gate; approval is a
separate explicit, reviewer-named action. Adverse-event promotion stays
hard-blocked unless PV sign-off is explicitly confirmed (enforced by the
service's 409).
"""
from __future__ import annotations

import asyncio

from app.copilot.tools.schemas import ToolInput, ToolResultData, ToolSpec
from app.models.database import AsyncSessionLocal


class RunHarvestInput(ToolInput):
    persona: str | None = None
    therapeutic_area: str | None = None
    max_queries: int | None = None
    max_items: int | None = None


async def run_harvest(payload: RunHarvestInput) -> ToolResultData:
    from app.api import harvest as harvest_api

    if harvest_api._HARVEST.get("running"):
        return ToolResultData(tool_name="run_harvest", ok=False, summary="A discovery run is already in progress.", error="already_running")
    # Mark running synchronously (before the task is scheduled) so completion
    # polling can't read a previous run's stale finished_at/last_result.
    harvest_api._HARVEST.update(running=True, finished_at=None, error=None, last_result=None)
    asyncio.create_task(harvest_api._harvest_task(
        payload.max_queries, payload.max_items,
        persona=payload.persona, therapeutic_area=payload.therapeutic_area,
    ))
    scope = ", ".join(s for s in (payload.persona, payload.therapeutic_area) if s)
    summary = (
        f"Started discovering new questions ({scope})." if scope
        else "Started discovering new questions from public communities."
    )
    return ToolResultData(
        tool_name="run_harvest",
        ok=True,
        summary=summary,
        data={"persona": payload.persona, "therapeutic_area": payload.therapeutic_area},
        nav_target="/harvest",
        job={"kind": "harvest"},
    )


class CreateQuestionInput(ToolInput):
    question_text: str
    persona: str
    therapeutic_area: str
    brand_focus: str
    domain: str
    approver_name: str


async def create_question(payload: CreateQuestionInput) -> ToolResultData:
    from app.schemas import QuestionCreate, QuestionOut
    from app.services import question_service
    from app.utils.pii_lint import scan_for_pii

    pii = scan_for_pii(payload.question_text)
    if pii:
        return ToolResultData(tool_name="create_question", ok=False, summary="Rejected: possible PII detected.", error=f"PII: {pii}")
    try:
        data = QuestionCreate(
            question_text=payload.question_text,
            persona=payload.persona,
            therapeutic_area=payload.therapeutic_area,
            brand_focus=payload.brand_focus,
            domain=payload.domain,
            approval_status="PENDING",
            approver_name=payload.approver_name,
        )
    except Exception as exc:  # noqa: BLE001 — pattern validation
        return ToolResultData(tool_name="create_question", ok=False, summary="Invalid question fields.", error=str(exc))
    async with AsyncSessionLocal() as db:
        q = await question_service.create_question(db, data)
        out = QuestionOut.model_validate(q).model_dump(mode="json")
    return ToolResultData(
        tool_name="create_question",
        ok=True,
        summary=f"Created question {out['question_id']} (PENDING approval).",
        data={"question": out},
        nav_target="/questions",
    )


class UpdateQuestionInput(ToolInput):
    row_id: int
    question_text: str | None = None
    persona: str | None = None
    therapeutic_area: str | None = None
    brand_focus: str | None = None
    domain: str | None = None
    active: bool | None = None


async def update_question(payload: UpdateQuestionInput) -> ToolResultData:
    from app.schemas import QuestionOut, QuestionUpdate
    from app.services import question_service
    from app.utils.pii_lint import scan_for_pii

    if payload.question_text:
        pii = scan_for_pii(payload.question_text)
        if pii:
            return ToolResultData(tool_name="update_question", ok=False, summary="Rejected: possible PII detected.", error=f"PII: {pii}")
    # Note: approval_status is intentionally NOT editable here — use set_question_approval.
    data = QuestionUpdate(
        question_text=payload.question_text,
        persona=payload.persona,
        therapeutic_area=payload.therapeutic_area,
        brand_focus=payload.brand_focus,
        domain=payload.domain,
        active=payload.active,
    )
    async with AsyncSessionLocal() as db:
        q = await question_service.update_question(db, payload.row_id, data)
        if q is None:
            return ToolResultData(tool_name="update_question", ok=False, summary="Question not found.", error=f"No question id {payload.row_id}")
        out = QuestionOut.model_validate(q).model_dump(mode="json")
    return ToolResultData(
        tool_name="update_question",
        ok=True,
        summary=f"Updated question {out['question_id']} (now v{out['version']}).",
        data={"question": out},
        nav_target="/questions",
    )


class SetApprovalInput(ToolInput):
    row_id: int
    approval_status: str
    approver_name: str


async def set_question_approval(payload: SetApprovalInput) -> ToolResultData:
    from app.schemas import QuestionOut, QuestionUpdate
    from app.services import question_service

    status = (payload.approval_status or "").upper()
    if status not in {"APPROVED", "REJECTED", "PENDING"}:
        return ToolResultData(tool_name="set_question_approval", ok=False, summary="Invalid approval status.", error="approval_status must be APPROVED, REJECTED, or PENDING")
    async with AsyncSessionLocal() as db:
        try:
            q = await question_service.update_question(
                db, payload.row_id, QuestionUpdate(approval_status=status, approver_name=payload.approver_name)
            )
        except question_service.QuestionApprovalBlocked as e:
            # Phase 7: the copilot reaches approval through the same choke point as the UI,
            # so it inherits the invariant and must report it rather than raise into the run.
            return ToolResultData(
                tool_name="set_question_approval",
                ok=False,
                summary="This question's evidence has not been verified, so it cannot be approved.",
                error="; ".join(e.blockers),
            )
        if q is None:
            return ToolResultData(tool_name="set_question_approval", ok=False, summary="Question not found.", error=f"No question id {payload.row_id}")
        out = QuestionOut.model_validate(q).model_dump(mode="json")
    return ToolResultData(
        tool_name="set_question_approval",
        ok=True,
        summary=f"Question {out['question_id']} set to {status} by {payload.approver_name}.",
        data={"question": out},
        nav_target="/questions",
    )


class DeleteQuestionInput(ToolInput):
    row_id: int
    reason: str


async def delete_question(payload: DeleteQuestionInput) -> ToolResultData:
    from app.services import question_service

    async with AsyncSessionLocal() as db:
        q = await question_service.soft_delete_question(db, payload.row_id, payload.reason)
        if q is None:
            return ToolResultData(tool_name="delete_question", ok=False, summary="Question not found.", error=f"No question id {payload.row_id}")
    return ToolResultData(
        tool_name="delete_question",
        ok=True,
        summary=f"Soft-deleted question {q.question_id}.",
        data={"question_id": q.question_id, "reason": payload.reason},
        nav_target="/questions",
    )


class PromoteHarvestedInput(ToolInput):
    item_id: int
    persona: str | None = None
    therapeutic_area: str | None = None
    brand_focus: str | None = None
    domain: str | None = None
    reviewer_name: str
    override_ae: bool = False


async def promote_harvested(payload: PromoteHarvestedInput) -> ToolResultData:
    from app.schemas import HarvestPromote, QuestionOut
    from app.services import harvest_service

    data = HarvestPromote(
        persona=payload.persona,
        therapeutic_area=payload.therapeutic_area,
        brand_focus=payload.brand_focus,
        domain=payload.domain,
        reviewer_name=payload.reviewer_name,
        override_ae=payload.override_ae,
    )
    async with AsyncSessionLocal() as db:
        try:
            q = await harvest_service.promote(db, payload.item_id, data)
        except Exception as exc:  # noqa: BLE001 — HTTPException (AE/PII/injection guards)
            return ToolResultData(
                tool_name="promote_harvested",
                ok=False,
                summary="Could not promote item.",
                error=str(getattr(exc, "detail", exc)),
            )
        out = QuestionOut.model_validate(q).model_dump(mode="json")
    return ToolResultData(
        tool_name="promote_harvested",
        ok=True,
        summary=f"Promoted item {payload.item_id} to question {out['question_id']} (PENDING).",
        data={"question": out},
        nav_target="/questions",
    )


class RejectHarvestedInput(ToolInput):
    item_id: int
    reason: str = ""


async def reject_harvested(payload: RejectHarvestedInput) -> ToolResultData:
    from app.services import harvest_service

    async with AsyncSessionLocal() as db:
        try:
            data = await harvest_service.reject(db, payload.item_id, payload.reason)
        except Exception as exc:  # noqa: BLE001
            return ToolResultData(tool_name="reject_harvested", ok=False, summary="Could not reject item.", error=str(getattr(exc, "detail", exc)))
    return ToolResultData(
        tool_name="reject_harvested",
        ok=True,
        summary=f"Rejected harvested item {payload.item_id}.",
        data=data,
        nav_target="/harvest",
    )


class GetCurationCoverageInput(ToolInput):
    brands: list[str] | None = None
    therapeutic_areas: list[str] | None = None
    diseases: list[str] | None = None
    personas: list[str] | None = None
    limit: int = 100


async def get_curation_coverage(payload: GetCurationCoverageInput) -> ToolResultData:
    from app.curation import service as svc

    async with AsyncSessionLocal() as db:
        result = await svc.coverage_report(
            db,
            brands=payload.brands,
            therapeutic_areas=payload.therapeutic_areas,
            diseases=payload.diseases,
            personas=payload.personas,
            limit=max(1, min(payload.limit, 500)),
        )
    shown = len(result.get("gaps") or [])
    total = shown + int(result.get("gaps_truncated") or 0)
    return ToolResultData(
        tool_name="get_curation_coverage",
        ok=True,
        summary=(
            f"{total} uncovered brand-vs-competitor comparison(s) in this scope "
            f"({shown} shown, ranked by what is most worth asking)."
        ),
        data={"result": result},
    )


class GenerateCurationQuestionsInput(ToolInput):
    brands: list[str] | None = None
    therapeutic_areas: list[str] | None = None
    diseases: list[str] | None = None
    personas: list[str] | None = None
    limit: int = 20
    commit: bool = False


async def generate_curation_questions(payload: GenerateCurationQuestionsInput) -> ToolResultData:
    from app.curation import service as svc

    limit = max(1, min(payload.limit, svc.MAX_CELLS_PER_RUN))
    async with AsyncSessionLocal() as db:
        result = await svc.generate(
            db,
            brands=payload.brands,
            therapeutic_areas=payload.therapeutic_areas,
            diseases=payload.diseases,
            personas=payload.personas,
            limit=limit,
            commit=payload.commit,
        )
    mode = "COMMIT" if payload.commit else "DRY RUN"
    note = "" if payload.commit else " Dry run — nothing was staged and nothing was billed."
    return ToolResultData(
        tool_name="generate_curation_questions",
        ok=True,
        summary=(
            f"Curation over {limit} coverage gap(s) ({mode}): "
            f"{result.get('generated', result.get('staged_created', 0))} question(s)."
            f"{note}"
        ),
        data={"result": result},
        nav_target="/harvest",
    )


class RunQuestionsToPipelineInput(ToolInput):
    item_ids: list[int]
    reviewer_name: str
    monitoring_mode: str = "BRAND"


async def run_questions_to_pipeline(payload: RunQuestionsToPipelineInput) -> ToolResultData:
    from app.schemas import RunCreate
    from app.services import harvest_service, run_service
    from app.utils.audit import write_audit

    item_ids = [int(i) for i in (payload.item_ids or [])]
    if not item_ids:
        return ToolResultData(tool_name="run_questions_to_pipeline", ok=False, summary="No items selected.", error="item_ids must not be empty")

    async with AsyncSessionLocal() as db:
        result = await harvest_service.promote_and_approve_batch(
            db, item_ids, reviewer_name=payload.reviewer_name
        )
        question_ids = result["question_ids"]
        run_id = None
        run_data = None
        if question_ids:
            run_data = RunCreate(
                trigger="ADHOC",
                monitoring_mode=payload.monitoring_mode,
                question_ids=question_ids,
            )
            run = await run_service.create_run(db, run_data)
            run_id = run.run_id
            await write_audit(
                db, role="REVIEWER", event="HARVEST_RUN_TO_PIPELINE", run_id=run_id,
                context={
                    "item_ids": item_ids,
                    "question_ids": question_ids,
                    "reviewer_name": payload.reviewer_name,
                    "skipped_item_ids": [s["id"] for s in result["skipped"]],
                    "via": "copilot",
                },
            )

    if run_id is None:
        return ToolResultData(
            tool_name="run_questions_to_pipeline",
            ok=False,
            summary="Nothing qualified to run — every selected item was skipped.",
            error="; ".join(f"{s['id']}: {s['reason']}" for s in result["skipped"]) or "no eligible items",
        )

    asyncio.create_task(run_service.run_in_background(run_id, run_data))
    skipped = len(result["skipped"])
    tail = f" {skipped} item(s) were skipped (adverse-event, PII or incomplete)." if skipped else ""
    return ToolResultData(
        tool_name="run_questions_to_pipeline",
        ok=True,
        summary=(
            f"Approved {len(question_ids)} discovered question(s) and started run {run_id}.{tail}"
        ),
        data={
            "run_id": run_id,
            "ran_count": len(question_ids),
            "promoted": result["promoted"],
            "skipped": result["skipped"],
        },
        nav_target="/run-analysis",
        job={"kind": "run", "run_id": run_id},
    )


SPECS: list[ToolSpec] = [
    ToolSpec("run_harvest", "Start discovering new questions from public health communities (background job). Optionally scope by persona (Prospect/Provider/Patient) and therapeutic_area; optional max_queries and max_items. Unset = discover across all audiences and areas.", RunHarvestInput, run_harvest, mutating=True, nav_target="/harvest"),
    ToolSpec("create_question", "Create a new question in the bank (lands as PENDING for Medical-Affairs approval). Requires persona (Prospect/Provider/Patient), therapeutic_area, brand_focus, domain (Efficacy/Safety/Access/Comparative/General), and an approver_name.", CreateQuestionInput, create_question, mutating=True, governance=True, nav_target="/questions"),
    ToolSpec("update_question", "Edit a question's text/persona/therapeutic_area/brand_focus/domain/active (creates a new version). Does NOT change approval status.", UpdateQuestionInput, update_question, mutating=True, nav_target="/questions"),
    ToolSpec("set_question_approval", "Approve or reject a question. Requires approval_status (APPROVED/REJECTED) and an approver_name.", SetApprovalInput, set_question_approval, mutating=True, governance=True, nav_target="/questions"),
    ToolSpec("delete_question", "Soft-delete a question (requires a reason). Never physically removed.", DeleteQuestionInput, delete_question, mutating=True, nav_target="/questions"),
    ToolSpec("promote_harvested", "Promote a staged harvested item into the question bank as a PENDING question. Requires a reviewer_name. Adverse-event items are blocked unless override_ae=true AND pharmacovigilance sign-off is confirmed.", PromoteHarvestedInput, promote_harvested, mutating=True, governance=True, nav_target="/questions"),
    ToolSpec("reject_harvested", "Reject a staged harvested item with a reason.", RejectHarvestedInput, reject_harvested, mutating=True, nav_target="/harvest"),
    ToolSpec("get_curation_coverage", "Measure BRAND-VS-COMPETITOR comparison coverage: which head-to-head comparisons the question bank does not ask yet, ranked by what is most worth writing. Optional scope: brands, therapeutic_areas, diseases, personas, limit. This is the comparison matrix — for plain persona/area/domain counts of the bank use question_coverage instead.", GetCurationCoverageInput, get_curation_coverage),
    ToolSpec("generate_curation_questions", "Write the missing brand-vs-competitor comparison questions for the top-ranked coverage gaps. BILLED (one model call per batch of gaps). Dry run unless commit=true; the dry run reports the exact number of model calls a real run would make. Candidates land in the Discover review queue, never straight into the bank. Optional scope: brands, therapeutic_areas, diseases, personas, limit (max 50).", GenerateCurationQuestionsInput, generate_curation_questions, mutating=True, nav_target="/harvest"),
    ToolSpec("run_questions_to_pipeline", "One-click Discover action: promote the selected harvested items, APPROVE them and immediately launch a run scoped to exactly those questions. This BYPASSES the normal Medical-Affairs approval step and the approved questions stay in the bank for future runs, so it requires a reviewer_name. Adverse-event, PII and incomplete items are skipped and reported, never run. Needs item_ids (the staged Discover item ids, from list_harvested).", RunQuestionsToPipelineInput, run_questions_to_pipeline, mutating=True, governance=True, nav_target="/run-analysis"),
]
