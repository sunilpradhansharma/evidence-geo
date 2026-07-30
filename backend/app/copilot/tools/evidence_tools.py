"""Clinical Evidence tools — the ``/evidence/*`` section (corpus, networks, governance).

Reads reuse the same services the REST routers call, so behaviour matches the API
exactly. Writes are split by **whose decision it is**, mirroring the split the
evidence programme already makes between curator backlog and reviewer judgement:

* ``curate_evidence``  — data-accuracy calls (study/label verification, rejection,
  network membership). Audited as CURATOR.
* ``review_evidence``  — clinical/statistical judgement (protocol decisions,
  network ratification, competitor acceptance). Audited as REVIEWER.

Both are ``governance=True``, so the executor refuses to propose them until the
user has named who is deciding. There is no RBAC in this tree: the name is
*recorded*, not *authenticated*, and the tool summaries say so on the card.

Billed or external work (ingestion, claim evaluation, question generation, the
competitor sweep) is ``mutating`` even where it is read-shaped, so it always
reaches a Confirm card that states the cost before anything is spent.
"""
from __future__ import annotations

import asyncio

from app.copilot.tools.schemas import ToolInput, ToolResultData, ToolSpec
from app.models.database import AsyncSessionLocal

# Strong refs for fire-and-forget ingestion tasks (a bare create_task may be GC'd).
_BACKGROUND: set[asyncio.Task] = set()


def _ok(name: str, summary: str, data: dict | None = None) -> ToolResultData:
    return ToolResultData(tool_name=name, ok=True, summary=summary, data=data or {})


def _err(name: str, summary: str, error: str) -> ToolResultData:
    return ToolResultData(tool_name=name, ok=False, summary=summary, error=error)


def _bad_view(name: str, view: str, allowed: tuple[str, ...]) -> ToolResultData:
    return _err(
        name,
        f"Unknown view {view!r}.",
        f"view must be one of {' | '.join(allowed)}",
    )


def _needs(name: str, view: str, arg: str) -> ToolResultData:
    return _err(name, f"The {view!r} view needs {arg}.", f"{arg} is required for view={view!r}")


def _detail(exc: Exception) -> str:
    """FastAPI HTTPExceptions carry the useful text on ``.detail``."""
    return str(getattr(exc, "detail", exc))


# =========================================================================================
# get_evidence — the corpus itself
# =========================================================================================
_EVIDENCE_VIEWS = (
    "overview", "networks", "network", "studies", "study",
    "drug_facts", "drug_fact", "ingest_status", "ingest_options",
)


class GetEvidenceInput(ToolInput):
    view: str = "overview"
    network_id: str | None = None
    study_id: str | None = None
    brand: str | None = None
    indication: str | None = None
    treatment: str | None = None
    verification_status: str | None = None
    ratification_status: str | None = None
    limit: int = 100


async def get_evidence(payload: GetEvidenceInput) -> ToolResultData:
    from app.services import evidence_read_service as svc

    view = (payload.view or "overview").strip().lower()
    if view not in _EVIDENCE_VIEWS:
        return _bad_view("get_evidence", view, _EVIDENCE_VIEWS)

    if view == "ingest_options" or view == "ingest_status":
        from app.api import evidence_ingestion as ing

        try:
            ing._guard_enabled()
        except Exception as exc:  # noqa: BLE001 — 403 when the surface is switched off
            return _err("get_evidence", "Evidence ingestion is disabled.", _detail(exc))
        if view == "ingest_status":
            job = dict(ing._JOB)
            state = "running" if job.get("running") else ("idle" if not job.get("started_at") else "finished")
            return _ok("get_evidence", f"Evidence ingestion is {state}.", {"view": view, "result": job})
        return _ok("get_evidence", "Evidence ingestion options.", {"view": view, "result": await ing.options()})

    async with AsyncSessionLocal() as db:
        try:
            if view == "overview":
                result = await svc.overview(db)
                return _ok("get_evidence", "Clinical evidence overview.", {"view": view, "result": result})
            if view == "networks":
                result = await svc.list_networks(
                    db, indication=payload.indication,
                    ratification_status=payload.ratification_status, limit=payload.limit,
                )
                return _ok("get_evidence", f"{result.get('total', 0)} evidence network(s).", {"view": view, "result": result})
            if view == "network":
                if not payload.network_id:
                    return _needs("get_evidence", view, "network_id")
                result = await svc.get_network(db, payload.network_id)
                return _ok("get_evidence", f"Network {payload.network_id}.", {"view": view, "result": result})
            if view == "studies":
                result = await svc.list_studies(
                    db, indication=payload.indication,
                    verification_status=payload.verification_status,
                    treatment=payload.treatment, limit=payload.limit,
                )
                return _ok("get_evidence", f"{result.get('total', 0)} ingested stud(ies).", {"view": view, "result": result})
            if view == "study":
                if not payload.study_id:
                    return _needs("get_evidence", view, "study_id")
                result = await svc.get_study(db, payload.study_id)
                return _ok("get_evidence", f"Study {payload.study_id}.", {"view": view, "result": result})
            if view == "drug_facts":
                result = await svc.list_drug_facts(
                    db, brand=payload.brand,
                    verification_status=payload.verification_status, limit=payload.limit,
                )
                return _ok("get_evidence", f"{result.get('total', 0)} label-derived drug fact(s).", {"view": view, "result": result})
            # drug_fact
            if not payload.brand:
                return _needs("get_evidence", view, "brand")
            result = await svc.get_drug_fact(db, payload.brand)
            return _ok("get_evidence", f"Label facts for {payload.brand}.", {"view": view, "result": result})
        except svc.EvidenceNotFound as exc:
            return _err("get_evidence", "Not found in the evidence store.", _detail(exc))


# =========================================================================================
# get_evidence_comparison — what the network can actually answer
# =========================================================================================
_COMPARISON_VIEWS = ("resolve", "matrix", "evidence")


class GetEvidenceComparisonInput(ToolInput):
    view: str = "matrix"
    network_id: str
    treatment_a: str | None = None
    treatment_b: str | None = None
    execution_mode: str | None = None


async def get_evidence_comparison(payload: GetEvidenceComparisonInput) -> ToolResultData:
    from app.api import comparisons as comparisons_api
    from app.models.nma_result import EXECUTION_MODES, EXPLORATORY
    from app.services import comparison_service as svc

    view = (payload.view or "matrix").strip().lower()
    if view not in _COMPARISON_VIEWS:
        return _bad_view("get_evidence_comparison", view, _COMPARISON_VIEWS)
    mode = (payload.execution_mode or EXPLORATORY).strip().upper()
    if mode not in EXECUTION_MODES:
        return _err(
            "get_evidence_comparison",
            f"Unknown execution mode {mode!r}.",
            f"execution_mode must be one of {', '.join(EXECUTION_MODES)}",
        )
    if view in {"resolve", "evidence"} and not (payload.treatment_a and payload.treatment_b):
        return _needs("get_evidence_comparison", view, "treatment_a and treatment_b")

    async with AsyncSessionLocal() as db:
        try:
            if view == "matrix":
                result = await svc.resolve_all_pairs(
                    db, network_id=payload.network_id, execution_mode=mode
                )
                return _ok("get_evidence_comparison", "Every pair in the network, resolved.", {"view": view, "result": result})
            if view == "resolve":
                result = await svc.resolve_comparison(
                    db, network_id=payload.network_id,
                    treatment_a=payload.treatment_a, treatment_b=payload.treatment_b,
                    execution_mode=mode, persist=False,
                )
                status = (result or {}).get("status") if isinstance(result, dict) else None
                return _ok(
                    "get_evidence_comparison",
                    f"{payload.treatment_a} vs {payload.treatment_b}: {status or 'resolved'}.",
                    {"view": view, "result": result},
                )
            # evidence — reuse the router's own assembly so the shape matches the API.
            result = await comparisons_api.evidence(
                network_id=payload.network_id,
                treatment_a=payload.treatment_a,
                treatment_b=payload.treatment_b,
                db=db,
            )
            return _ok(
                "get_evidence_comparison",
                f"What the resolver sees for {payload.treatment_a} vs {payload.treatment_b}.",
                {"view": view, "result": result},
            )
        except svc.ComparisonError as exc:
            return _err("get_evidence_comparison", "That comparison scope does not exist.", _detail(exc))
        except Exception as exc:  # noqa: BLE001 — HTTPException from the router helper
            return _err("get_evidence_comparison", "Could not read that comparison.", _detail(exc))


# =========================================================================================
# get_evidence_governance — the three gates
# =========================================================================================
_GOVERNANCE_VIEWS = (
    "protocols", "protocol", "network_gate", "memberships", "curation_queue",
    "study_source_check", "drug_facts_queue", "drug_fact_source_check",
    "question_evidence", "approval_blockers", "vocabulary",
)


class GetEvidenceGovernanceInput(ToolInput):
    view: str = "protocols"
    protocol_id: str | None = None
    network_id: str | None = None
    study_id: str | None = None
    fact_id: str | None = None
    question_id: str | None = None
    brand: str | None = None
    indication: str | None = None
    verification_status: str | None = None
    limit: int = 200


async def get_evidence_governance(payload: GetEvidenceGovernanceInput) -> ToolResultData:
    from app.evidence import approvals, lifecycles, protocols
    from app.services import drug_fact_curation_service as fact_curation
    from app.services import evidence_question_service as eqs
    from app.services import evidence_review_service as svc
    from app.services import study_curation_service as curation

    view = (payload.view or "protocols").strip().lower()
    if view not in _GOVERNANCE_VIEWS:
        return _bad_view("get_evidence_governance", view, _GOVERNANCE_VIEWS)

    if view == "vocabulary":
        return _ok("get_evidence_governance", "The governance vocabularies.", {
            "view": view,
            "result": {
                "approval_roles": list(approvals.APPROVAL_ROLES),
                "decisions": list(approvals.DECISIONS),
                "derived_statuses": list(approvals.DERIVED_STATUSES),
                "ratification_states": list(lifecycles.RATIFICATION_STATES),
                "verification_states": list(lifecycles.VERIFICATION_STATES),
                "membership_decisions": [
                    s for s in lifecycles.MEMBERSHIP_STATES if s != lifecycles.PROPOSED
                ],
            },
        })

    async with AsyncSessionLocal() as db:
        try:
            if view == "protocols":
                result = [await svc.protocol_status(db, pid) for pid in protocols.protocol_ids()]
                return _ok("get_evidence_governance", f"{len(result)} analysis protocol(s).", {"view": view, "result": result})
            if view == "protocol":
                if not payload.protocol_id:
                    return _needs("get_evidence_governance", view, "protocol_id")
                definition = protocols.protocol(payload.protocol_id)
                if definition is None:
                    return _err("get_evidence_governance", "No such protocol.", f"Unknown protocol {payload.protocol_id!r}")
                state = await svc.protocol_status(db, payload.protocol_id)
                return _ok("get_evidence_governance", f"Protocol {payload.protocol_id}.", {
                    "view": view, "result": {**state, "definition": definition},
                })
            if view == "network_gate":
                if not payload.network_id:
                    return _needs("get_evidence_governance", view, "network_id")
                result = await svc.governance_gate(db, network_id=payload.network_id)
                return _ok("get_evidence_governance", f"Governance gate for {payload.network_id}.", {"view": view, "result": result})
            if view == "memberships":
                if not payload.network_id:
                    return _needs("get_evidence_governance", view, "network_id")
                result = await svc.membership_preview(db, network_id=payload.network_id)
                return _ok("get_evidence_governance", f"Membership state for {payload.network_id}.", {"view": view, "result": result})
            if view == "curation_queue":
                result = await curation.curation_queue(
                    db, network_id=payload.network_id, indication=payload.indication,
                    verification_status=payload.verification_status, limit=payload.limit,
                )
                worth = result.get("worth_verifying") if isinstance(result, dict) else None
                summary = (
                    f"{worth} stud(ies) worth verifying — verifying these changes the answer."
                    if isinstance(worth, int) else "The study curation queue."
                )
                return _ok("get_evidence_governance", summary, {"view": view, "result": result})
            if view == "study_source_check":
                if not payload.study_id:
                    return _needs("get_evidence_governance", view, "study_id")
                result = await curation.rederivation_diff(db, payload.study_id)
                return _ok("get_evidence_governance", f"Re-derivation diff for {payload.study_id}.", {"view": view, "result": result})
            if view == "drug_facts_queue":
                result = await fact_curation.curation_queue(
                    db, brand=payload.brand, verification_status=payload.verification_status,
                    limit=payload.limit,
                )
                return _ok("get_evidence_governance", "Regulatory labels awaiting a curator.", {"view": view, "result": result})
            if view == "drug_fact_source_check":
                if not payload.fact_id:
                    return _needs("get_evidence_governance", view, "fact_id")
                result = await fact_curation.rederivation_diff(db, payload.fact_id)
                return _ok("get_evidence_governance", f"Re-derivation diff for label {payload.fact_id}.", {"view": view, "result": result})
            if view == "question_evidence":
                if not payload.question_id:
                    return _needs("get_evidence_governance", view, "question_id")
                links = await eqs.associations(db, payload.question_id)
                return _ok("get_evidence_governance", f"{len(links)} evidence association(s).", {
                    "view": view,
                    "result": {
                        "question_id": payload.question_id,
                        "association_count": len(links),
                        "verified_count": sum(1 for link in links if link["is_verified"]),
                        "associations": links,
                    },
                })
            # approval_blockers
            if not payload.question_id:
                return _needs("get_evidence_governance", view, "question_id")
            from sqlalchemy import select

            from app.models.question import Question

            question = (await db.execute(
                select(Question).where(
                    Question.question_id == payload.question_id,
                    Question.deleted_at.is_(None),
                    Question.superseded_by.is_(None),
                )
            )).scalars().first()
            if question is None:
                return _err("get_evidence_governance", "No such question.", f"question {payload.question_id!r} does not exist")
            blockers = await eqs.approval_blockers(db, question)
            return _ok(
                "get_evidence_governance",
                "This question may be approved." if not blockers else f"{len(blockers)} blocker(s) stop approval.",
                {
                    "view": view,
                    "result": {
                        "question_id": payload.question_id,
                        "generation_method": question.generation_method,
                        "approval_status": question.approval_status,
                        "may_approve": not blockers,
                        "blockers": blockers,
                    },
                },
            )
        except (svc.ReviewError, curation.CurationError) as exc:
            return _err("get_evidence_governance", "Not found.", _detail(exc))


# =========================================================================================
# get_evidence_alignment — AI vs Evidence (Phase 8)
# =========================================================================================
_ALIGNMENT_VIEWS = ("alignment", "response_claims", "vocabulary")


class GetEvidenceAlignmentInput(ToolInput):
    view: str = "alignment"
    run_id: str | None = None
    llm_name: str | None = None
    indication: str | None = None
    response_id: str | None = None


async def get_evidence_alignment(payload: GetEvidenceAlignmentInput) -> ToolResultData:
    from app.services import claim_evaluation_service as svc

    view = (payload.view or "alignment").strip().lower()
    if view not in _ALIGNMENT_VIEWS:
        return _bad_view("get_evidence_alignment", view, _ALIGNMENT_VIEWS)

    if view == "vocabulary":
        from app.api import claim_evaluation as ce_api

        return _ok("get_evidence_alignment", "Claim types and their authoritative evidence.", {
            "view": view, "result": await ce_api.vocabulary(),
        })

    async with AsyncSessionLocal() as db:
        if view == "alignment":
            result = await svc.alignment_report(
                db, run_id=payload.run_id, llm_name=payload.llm_name,
                indication=payload.indication,
            )
            return _ok(
                "get_evidence_alignment",
                "AI-vs-evidence alignment. Read coverage before the score — a high score "
                "on a handful of checkable claims is unmeasured, not aligned.",
                {"view": view, "result": result},
            )
        if not payload.response_id:
            return _needs("get_evidence_alignment", view, "response_id")
        from app.api import claim_evaluation as ce_api

        result = await ce_api.claims_for_response(payload.response_id, db=db)
        return _ok(
            "get_evidence_alignment",
            f"{result.get('claim_count', 0)} graded claim(s) for {payload.response_id}.",
            {"view": view, "result": result},
        )


# =========================================================================================
# get_competitor_discovery — Phase 5 Tier A
# =========================================================================================
_COMPETITOR_VIEWS = ("candidates", "reasons", "config_proposal", "class_map")


class GetCompetitorDiscoveryInput(ToolInput):
    view: str = "candidates"
    indication: str | None = None
    review_status: str | None = None
    limit: int = 200


async def get_competitor_discovery(payload: GetCompetitorDiscoveryInput) -> ToolResultData:
    from app.models.competitor_candidate import REVIEW_STATES
    from app.services import competitor_discovery_service as svc

    view = (payload.view or "candidates").strip().lower()
    if view not in _COMPETITOR_VIEWS:
        return _bad_view("get_competitor_discovery", view, _COMPETITOR_VIEWS)

    if view == "reasons":
        from app.api import competitor_discovery as cd_api

        return _ok("get_competitor_discovery", "The Tier A reason vocabulary.", {
            "view": view, "result": await cd_api.reasons(),
        })

    if view == "class_map" and not payload.indication:
        return _needs("get_competitor_discovery", view, "indication")
    if payload.review_status and payload.review_status not in REVIEW_STATES:
        return _err(
            "get_competitor_discovery",
            f"Unknown review status {payload.review_status!r}.",
            f"review_status must be one of {', '.join(REVIEW_STATES)}",
        )

    async with AsyncSessionLocal() as db:
        if view == "candidates":
            result = await svc.list_candidates(
                db, indication=payload.indication,
                review_status=payload.review_status, limit=payload.limit,
            )
            return _ok("get_competitor_discovery", "The competitor review queue, strongest signal first.", {"view": view, "result": result})
        if view == "config_proposal":
            result = await svc.config_proposal(db, indication=payload.indication)
            return _ok(
                "get_competitor_discovery",
                "The brands.yaml fragment for accepted candidates. Nothing writes this file — a human commits it.",
                {"view": view, "result": result},
            )
        result = await svc.class_map(db, indication=payload.indication)
        return _ok("get_competitor_discovery", f"Class map for {payload.indication}.", {"view": view, "result": result})


# =========================================================================================
# get_evidence_synthesis — Phase 9 + published syntheses
# =========================================================================================
_SYNTHESIS_VIEWS = ("synthesis", "published", "assess")


class GetEvidenceSynthesisInput(ToolInput):
    view: str = "synthesis"
    indication: str | None = None
    network_id: str | None = None
    change_window_days: int = 90
    treatment_a: str | None = None
    treatment_b: str | None = None
    limit: int = 100


async def get_evidence_synthesis(payload: GetEvidenceSynthesisInput) -> ToolResultData:
    from app.services import evidence_synthesis_service as synth
    from app.services import published_synthesis_service as pubs

    view = (payload.view or "synthesis").strip().lower()
    if view not in _SYNTHESIS_VIEWS:
        return _bad_view("get_evidence_synthesis", view, _SYNTHESIS_VIEWS)
    if view in {"synthesis", "assess"} and not payload.indication:
        return _needs("get_evidence_synthesis", view, "indication")
    if view == "assess" and not (payload.treatment_a and payload.treatment_b):
        return _needs("get_evidence_synthesis", view, "treatment_a and treatment_b")

    async with AsyncSessionLocal() as db:
        if view == "synthesis":
            result = await synth.synthesise(
                db, indication=payload.indication, network_id=payload.network_id,
                change_window_days=payload.change_window_days,
            )
            return _ok(
                "get_evidence_synthesis",
                f"Evidence synthesis for {payload.indication}. Read the limitations first.",
                {"view": view, "result": result},
            )
        if view == "published":
            from app.api import published_synthesis as ps_api

            result = await ps_api.list_syntheses(
                indication=payload.indication, limit=payload.limit, db=db
            )
            return _ok("get_evidence_synthesis", f"{len(result)} stored published synthes(es).", {"view": view, "result": result})
        result = await pubs.assess_for_question(
            db, indication=payload.indication,
            treatment_a=payload.treatment_a, treatment_b=payload.treatment_b,
        )
        return _ok(
            "get_evidence_synthesis",
            f"Published-synthesis suitability for {payload.treatment_a} vs {payload.treatment_b}.",
            {"view": view, "result": result},
        )


# =========================================================================================
# run_evidence_ingest — external fetches + the discovery sweep (mutating)
# =========================================================================================
_INGEST_ACTIONS = ("trials", "drug_facts", "reparse", "competitor_sweep")


class RunEvidenceIngestInput(ToolInput):
    action: str
    indication: str | None = None
    drugs: list[str] | None = None
    brands: list[str] | None = None
    study_ids: list[str] | None = None
    outcome: str | None = None
    protocol: str | None = None
    phase: str = "PRIMARY"
    stratum: str | None = None
    limit: int | None = None
    commit: bool = False


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


async def run_evidence_ingest(payload: RunEvidenceIngestInput) -> ToolResultData:
    from fastapi import BackgroundTasks

    from app.api import evidence_ingestion as ing

    action = (payload.action or "").strip().lower()
    if action not in _INGEST_ACTIONS:
        return _err(
            "run_evidence_ingest",
            f"Unknown action {action!r}.",
            f"action must be one of {' | '.join(_INGEST_ACTIONS)}",
        )
    mode = "COMMIT" if payload.commit else "PREVIEW"

    if action == "competitor_sweep":
        from app.services import competitor_discovery_service as cds

        async with AsyncSessionLocal() as db:
            try:
                result = await cds.discover(
                    db, indication=payload.indication, commit=payload.commit
                )
            except cds.DiscoveryError as exc:
                return _err("run_evidence_ingest", "Could not run the discovery sweep.", _detail(exc))
        scope = payload.indication or "every indication"
        return ToolResultData(
            tool_name="run_evidence_ingest",
            ok=True,
            summary=f"Competitor discovery sweep over {scope} ({mode}).",
            data={"action": action, "mode": mode, "result": result},
            nav_target="/evidence/competitors",
        )

    # The three ingestion jobs reuse the router so validation, the 403 enable-guard and
    # the single-slot 409 all behave identically to the page. BackgroundTasks is just a
    # queue: the route fills it, we run it ourselves.
    bg = BackgroundTasks()
    try:
        if action == "trials":
            if not payload.indication:
                return _err("run_evidence_ingest", "Trial ingestion needs an indication.", "indication is required for action='trials'")
            started = await ing.ingest_trials(
                ing.TrialsIn(
                    indication=payload.indication, drugs=payload.drugs,
                    outcome=payload.outcome, protocol=payload.protocol,
                    phase=payload.phase, stratum=payload.stratum,
                    limit=payload.limit, commit=payload.commit,
                ),
                bg,
            )
        elif action == "drug_facts":
            started = await ing.ingest_drug_facts(
                ing.DrugFactsIn(brands=payload.brands, commit=payload.commit), bg
            )
        else:
            started = await ing.reparse(
                ing.ReparseIn(
                    indication=payload.indication, study_ids=payload.study_ids,
                    commit=payload.commit,
                ),
                bg,
            )
    except Exception as exc:  # noqa: BLE001 — 403 disabled / 409 busy / 422 bad scope
        return _err("run_evidence_ingest", "Could not start evidence ingestion.", _detail(exc))

    _spawn(bg())
    scope = payload.indication or ", ".join(payload.brands or []) or "the configured full-depth drugs"
    note = "" if payload.commit else " Preview only — nothing will be written."
    return ToolResultData(
        tool_name="run_evidence_ingest",
        ok=True,
        summary=f"Started {action.replace('_', ' ')} ingestion for {scope} ({mode}).{note}",
        data={"action": action, "mode": mode, "started": started},
        nav_target="/evidence/ingest",
        job={"kind": "evidence_ingest"},
    )


# =========================================================================================
# evaluate_claims — Phase 8, one model call per answer (mutating, billed)
# =========================================================================================
class EvaluateClaimsInput(ToolInput):
    scope: str = "run"
    run_id: str | None = None
    response_id: str | None = None
    limit: int = 200


async def evaluate_claims(payload: EvaluateClaimsInput) -> ToolResultData:
    from sqlalchemy import select

    from app.models.response import Response
    from app.services import claim_evaluation_service as svc

    scope = (payload.scope or "run").strip().lower()
    if scope not in {"run", "response"}:
        return _err("evaluate_claims", f"Unknown scope {scope!r}.", "scope must be run | response")
    limit = max(1, min(int(payload.limit or 200), 1000))

    async with AsyncSessionLocal() as db:
        if scope == "response":
            if not payload.response_id:
                return _err("evaluate_claims", "Evaluating one answer needs a response_id.", "response_id is required for scope='response'")
            response = (await db.execute(
                select(Response).where(Response.response_id == payload.response_id)
            )).scalars().first()
            if response is None:
                return _err("evaluate_claims", "No such response.", f"response {payload.response_id!r} does not exist")
            result = await svc.evaluate_response(db, response, commit=True)
            return ToolResultData(
                tool_name="evaluate_claims",
                ok=True,
                summary=f"Graded {result.get('claim_count', 0)} claim(s) in response {payload.response_id}.",
                data={"scope": scope, "result": result},
                nav_target="/evidence/alignment",
            )
        if not payload.run_id:
            return _err("evaluate_claims", "Evaluating a run needs a run_id.", "run_id is required for scope='run'")
        result = await svc.evaluate_run(db, payload.run_id, limit=limit)

    return ToolResultData(
        tool_name="evaluate_claims",
        ok=True,
        summary=(
            f"Checked {result.get('evaluated', 0)} answer(s) in run {payload.run_id} against "
            f"verified evidence; {result.get('finding_count', 0)} finding(s)."
        ),
        data={"scope": scope, "result": result},
        nav_target="/evidence/alignment",
    )


# =========================================================================================
# generate_evidence_questions — Phase 7 (mutating, billed; dry run by default)
# =========================================================================================
class GenerateEvidenceQuestionsInput(ToolInput):
    network_id: str
    commit: bool = False


async def generate_evidence_questions(payload: GenerateEvidenceQuestionsInput) -> ToolResultData:
    from app.services import comparison_service, evidence_question_service as svc

    async with AsyncSessionLocal() as db:
        try:
            result = await svc.generate_for_network(
                db, network_id=payload.network_id, commit=payload.commit
            )
        except comparison_service.ComparisonError as exc:
            return _err("generate_evidence_questions", "No such network.", _detail(exc))

    mode = "COMMIT" if payload.commit else "PREVIEW"
    note = "" if payload.commit else " Preview only — nothing was staged."
    return ToolResultData(
        tool_name="generate_evidence_questions",
        ok=True,
        summary=(
            f"Generated {result.get('generated', 0)} evidence-backed question(s) for "
            f"{payload.network_id} ({mode}).{note}"
        ),
        data={"result": result},
        nav_target="/evidence/governance",
    )


# =========================================================================================
# curate_evidence — CURATOR decisions (governance)
# =========================================================================================
_CURATE_ACTIONS = (
    "study_check", "study_reject", "drug_fact_check", "drug_fact_reject",
    "membership_decision",
)


class CurateEvidenceInput(ToolInput):
    action: str
    study_id: str | None = None
    fact_id: str | None = None
    network_id: str | None = None
    decision: str | None = None
    verified_by: str | None = None
    rejected_by: str | None = None
    decided_by: str | None = None
    reason: str | None = None
    note: str | None = None


async def curate_evidence(payload: CurateEvidenceInput) -> ToolResultData:
    from app.evidence import lifecycles
    from app.services import drug_fact_curation_service as fact_curation
    from app.services import evidence_ingestion_service as ingestion
    from app.services import evidence_review_service as review
    from app.services import study_curation_service as curation

    action = (payload.action or "").strip().lower()
    if action not in _CURATE_ACTIONS:
        return _err(
            "curate_evidence",
            f"Unknown action {action!r}.",
            f"action must be one of {' | '.join(_CURATE_ACTIONS)}",
        )

    async with AsyncSessionLocal() as db:
        try:
            if action == "study_check":
                if not payload.study_id:
                    return _err("curate_evidence", "Verifying a study needs a study_id.", "study_id is required")
                result = await curation.record_curator_check(
                    db, study_id=payload.study_id,
                    verified_by=payload.verified_by or "", note=payload.note,
                )
                return ToolResultData(
                    tool_name="curate_evidence", ok=True,
                    summary=f"{payload.study_id} verified by {payload.verified_by} (recorded, not authenticated).",
                    data={"action": action, "result": result}, nav_target="/evidence/studies",
                )
            if action == "study_reject":
                if not (payload.study_id and payload.reason):
                    return _err("curate_evidence", "Rejecting a study needs a study_id and a reason.", "study_id and reason are required")
                study = await ingestion.reject_study(
                    db, payload.study_id,
                    rejected_by=payload.rejected_by or "", reason=payload.reason,
                )
                return ToolResultData(
                    tool_name="curate_evidence", ok=True,
                    summary=f"{payload.study_id} rejected — it will not be used by any network.",
                    data={"action": action, "result": {
                        "study_id": study.study_id,
                        "verification_status": study.verification_status,
                        "rejection_reason": study.rejection_reason,
                    }},
                    nav_target="/evidence/studies",
                )
            if action == "drug_fact_check":
                if not payload.fact_id:
                    return _err("curate_evidence", "Verifying a label needs a fact_id.", "fact_id is required")
                result = await fact_curation.record_curator_check(
                    db, fact_id=payload.fact_id,
                    verified_by=payload.verified_by or "", note=payload.note,
                )
                return ToolResultData(
                    tool_name="curate_evidence", ok=True,
                    summary=f"Label {payload.fact_id} verified by {payload.verified_by}.",
                    data={"action": action, "result": result}, nav_target="/evidence/drug-facts",
                )
            if action == "drug_fact_reject":
                if not (payload.fact_id and payload.reason):
                    return _err("curate_evidence", "Rejecting a label needs a fact_id and a reason.", "fact_id and reason are required")
                fact = await ingestion.reject_drug_fact(
                    db, payload.fact_id,
                    rejected_by=payload.rejected_by or "", reason=payload.reason,
                )
                return ToolResultData(
                    tool_name="curate_evidence", ok=True,
                    summary=f"Label {payload.fact_id} ({fact.brand}) rejected.",
                    data={"action": action, "result": {
                        "fact_id": fact.fact_id, "brand": fact.brand,
                        "verification_status": fact.verification_status,
                    }},
                    nav_target="/evidence/drug-facts",
                )
            # membership_decision
            if not (payload.network_id and payload.study_id and payload.decision):
                return _err(
                    "curate_evidence",
                    "A membership decision needs a network_id, a study_id and a decision.",
                    "network_id, study_id and decision are required",
                )
            result = await review.decide_membership(
                db, network_id=payload.network_id, study_id=payload.study_id,
                decision=(payload.decision or "").strip().upper(),
                decided_by=payload.decided_by or "",
                reason=payload.reason, note=payload.note,
            )
            return ToolResultData(
                tool_name="curate_evidence", ok=True,
                summary=(
                    f"{payload.study_id} set to {payload.decision} for {payload.network_id}. "
                    "The first inclusion binds the membership filter for the whole network."
                ),
                data={"action": action, "result": result}, nav_target="/evidence/networks",
            )
        except (curation.CurationError, ingestion.IngestionError,
                review.ReviewError, lifecycles.LifecycleError) as exc:
            return _err("curate_evidence", "That curation step was refused.", _detail(exc))


# =========================================================================================
# review_evidence — REVIEWER decisions (governance)
# =========================================================================================
_REVIEW_ACTIONS = (
    "network_submit", "network_medical", "network_statistical", "network_reopen",
    "protocol_decision", "protocol_revoke", "competitor_candidate",
    "competitor_config_applied",
)


class ReviewEvidenceInput(ToolInput):
    action: str
    network_id: str | None = None
    protocol_id: str | None = None
    candidate_id: str | None = None
    candidate_ids: list[str] | None = None
    approval_role: str | None = None
    decision: str | None = None
    approve: bool | None = None
    reviewer: str | None = None
    submitted_by: str | None = None
    reopened_by: str | None = None
    revoked_by: str | None = None
    applied_by: str | None = None
    reason: str | None = None
    note: str | None = None


async def review_evidence(payload: ReviewEvidenceInput) -> ToolResultData:
    from app.api import evidence_review as review_api
    from app.evidence import lifecycles
    from app.services import competitor_discovery_service as cds
    from app.services import evidence_review_service as svc

    action = (payload.action or "").strip().lower()
    if action not in _REVIEW_ACTIONS:
        return _err(
            "review_evidence",
            f"Unknown action {action!r}.",
            f"action must be one of {' | '.join(_REVIEW_ACTIONS)}",
        )
    needs_network = action.startswith("network_")
    if needs_network and not payload.network_id:
        return _err("review_evidence", "That review step needs a network_id.", "network_id is required")
    if action.startswith("protocol_") and not payload.protocol_id:
        return _err("review_evidence", "That protocol step needs a protocol_id.", "protocol_id is required")

    async with AsyncSessionLocal() as db:
        try:
            if action == "network_submit":
                network = await svc.submit_for_medical_review(
                    db, network_id=payload.network_id, submitted_by=payload.submitted_by or ""
                )
                summary = f"{payload.network_id} submitted for medical review."
            elif action in {"network_medical", "network_statistical"}:
                if payload.approve is None:
                    return _err("review_evidence", "A review must approve or reject.", "approve (true/false) is required")
                if not payload.approve and not (payload.note or "").strip():
                    return _err("review_evidence", "Rejecting needs a note.", "note is required when approve=false")
                recorder = (
                    svc.record_medical_review if action == "network_medical"
                    else svc.record_statistical_review
                )
                network = await recorder(
                    db, network_id=payload.network_id, reviewer=payload.reviewer or "",
                    approve=bool(payload.approve), note=payload.note,
                )
                stage = "Medical" if action == "network_medical" else "Statistical"
                verb = "approved" if payload.approve else "rejected"
                summary = (
                    f"{stage} review {verb} for {payload.network_id} "
                    f"(now {network.ratification_status})."
                )
            elif action == "network_reopen":
                if not (payload.reason or "").strip():
                    return _err("review_evidence", "Reopening needs a reason.", "reason is required")
                network = await svc.reopen_network(
                    db, network_id=payload.network_id,
                    reopened_by=payload.reopened_by or "", reason=payload.reason,
                )
                summary = f"{payload.network_id} reopened to DRAFT. This is not supersede — no approved snapshot is kept."
            elif action == "protocol_decision":
                if not (payload.approval_role and payload.decision):
                    return _err("review_evidence", "A protocol decision needs an approval_role and a decision.", "approval_role and decision are required")
                row = await svc.record_protocol_decision(
                    db, protocol_id=payload.protocol_id,
                    approval_role=(payload.approval_role or "").strip().upper(),
                    decision=(payload.decision or "").strip().upper(),
                    reviewer_id=payload.reviewer or "", review_note=payload.note,
                )
                state = await svc.protocol_status(db, payload.protocol_id)
                return ToolResultData(
                    tool_name="review_evidence", ok=True,
                    summary=(
                        f"{payload.approval_role} {payload.decision} recorded for protocol "
                        f"{payload.protocol_id} (now {state.get('status')})."
                    ),
                    data={"action": action, "result": {
                        "approval_id": row.approval_id, "content_hash": row.content_hash, **state,
                    }},
                    nav_target="/evidence/governance",
                )
            elif action == "protocol_revoke":
                if not (payload.approval_role and (payload.reason or "").strip()):
                    return _err("review_evidence", "Revoking needs an approval_role and a reason.", "approval_role and reason are required")
                await svc.revoke_protocol_approval(
                    db, protocol_id=payload.protocol_id,
                    approval_role=(payload.approval_role or "").strip().upper(),
                    revoked_by=payload.revoked_by or "", revocation_reason=payload.reason,
                )
                state = await svc.protocol_status(db, payload.protocol_id)
                return ToolResultData(
                    tool_name="review_evidence", ok=True,
                    summary=f"{payload.approval_role} approval withdrawn on {payload.protocol_id} (now {state.get('status')}).",
                    data={"action": action, "result": state}, nav_target="/evidence/governance",
                )
            elif action == "competitor_candidate":
                if not (payload.candidate_id and payload.decision):
                    return _err("review_evidence", "Reviewing a candidate needs a candidate_id and a decision.", "candidate_id and decision are required")
                result = await cds.review_candidate(
                    db, payload.candidate_id,
                    decision=(payload.decision or "").strip().upper(),
                    reviewer=payload.reviewer or "", note=payload.note,
                )
                return ToolResultData(
                    tool_name="review_evidence", ok=True,
                    summary=(
                        f"Candidate {payload.candidate_id} {payload.decision}. Accepting does "
                        "not add the drug to any competitor list — that is a separate config commit."
                    ),
                    data={"action": action, "result": result}, nav_target="/evidence/competitors",
                )
            else:  # competitor_config_applied
                ids = [c for c in (payload.candidate_ids or []) if c]
                if not ids:
                    return _err("review_evidence", "Recording a config commit needs candidate_ids.", "candidate_ids is required")
                result = await cds.mark_config_applied(db, ids, applied_by=payload.applied_by or "")
                return ToolResultData(
                    tool_name="review_evidence", ok=True,
                    summary=f"Recorded that brands.yaml was committed for {len(ids)} candidate(s).",
                    data={"action": action, "result": result}, nav_target="/evidence/competitors",
                )
        except (svc.ReviewError, cds.DiscoveryError, lifecycles.LifecycleError) as exc:
            return _err("review_evidence", "That review step was refused.", _detail(exc))

    return ToolResultData(
        tool_name="review_evidence",
        ok=True,
        summary=summary,
        data={"action": action, "result": review_api._network_out(network)},
        nav_target="/evidence/governance",
    )


SPECS: list[ToolSpec] = [
    ToolSpec(
        "get_evidence",
        "Read the Clinical Evidence corpus (curated trials, evidence networks and label-derived "
        "drug facts). view = overview | networks | network (needs network_id) | studies | study "
        "(needs study_id) | drug_facts | drug_fact (needs brand) | ingest_status | ingest_options. "
        "Optional filters: indication, treatment, brand, verification_status "
        "(EXTRACTED/MAPPED/VERIFIED/REJECTED), ratification_status, limit.",
        GetEvidenceInput, get_evidence,
    ),
    ToolSpec(
        "get_evidence_comparison",
        "Ask an evidence network what it can actually answer for a head-to-head comparison. "
        "view = matrix (every pair) | resolve (one pair; needs treatment_a + treatment_b) | "
        "evidence (which studies the resolver saw and why others were excluded). Needs network_id; "
        "execution_mode EXPLORATORY (default) or GOVERNED. An unanswerable comparison is a normal "
        "result with a named gap, not an error, and only a GOVERNED result is releasable.",
        GetEvidenceComparisonInput, get_evidence_comparison,
    ),
    ToolSpec(
        "get_evidence_governance",
        "Read the evidence governance gates: protocol approvals, network ratification and "
        "membership, and the curator queues. view = protocols | protocol (needs protocol_id) | "
        "network_gate (needs network_id) | memberships (needs network_id) | curation_queue "
        "(studies awaiting a curator; optional network_id/indication — read 'worth_verifying') | "
        "study_source_check (needs study_id) | drug_facts_queue | drug_fact_source_check (needs "
        "fact_id) | question_evidence (needs question_id) | approval_blockers (needs question_id) "
        "| vocabulary.",
        GetEvidenceGovernanceInput, get_evidence_governance,
    ),
    ToolSpec(
        "get_evidence_alignment",
        "Read AI vs Evidence alignment (Phase 8): how well monitored models' claims match our "
        "verified evidence. view = alignment (overall, by model, by claim type; optional run_id, "
        "llm_name, indication) | response_claims (needs response_id) | vocabulary. Always report "
        "coverage alongside the score — a high score over few checkable claims is unmeasured.",
        GetEvidenceAlignmentInput, get_evidence_alignment,
    ),
    ToolSpec(
        "get_competitor_discovery",
        "Read discovered competitor molecules found in the trial evidence. view = candidates "
        "(review queue; optional indication, review_status) | reasons (the signal vocabulary and "
        "weights) | config_proposal (the brands.yaml fragment awaiting a human commit) | class_map "
        "(needs indication).",
        GetCompetitorDiscoveryInput, get_competitor_discovery,
    ),
    ToolSpec(
        "get_evidence_synthesis",
        "Read the evidence synthesis for one indication (what the evidence shows, what changed, "
        "competitor threats, AI alignment and strategic implications). view = synthesis (needs "
        "indication) | published (stored third-party published network meta-analyses) | assess "
        "(is a published synthesis usable for one comparison; needs indication + treatment_a + "
        "treatment_b). Lead with the limitations.",
        GetEvidenceSynthesisInput, get_evidence_synthesis,
    ),
    ToolSpec(
        "run_evidence_ingest",
        "Fetch or re-derive clinical evidence. action = trials (ClinicalTrials.gov for one "
        "indication; needs indication, optional drugs/outcome/protocol/phase/stratum/limit) | "
        "drug_facts (openFDA labels; optional brands) | reparse (re-extract stored studies from "
        "retained payloads, no network call) | competitor_sweep (find competitor molecules in the "
        "evidence). PREVIEW unless commit=true. Ingestion CANNOT verify anything — rows land "
        "EXTRACTED/MAPPED and a curator verifies them one at a time.",
        RunEvidenceIngestInput, run_evidence_ingest,
        mutating=True, nav_target="/evidence/ingest",
    ),
    ToolSpec(
        "evaluate_claims",
        "Check what the AI models said against our verified evidence (Phase 8). scope = run "
        "(needs run_id; optional limit, max 1000) | response (needs response_id). BILLED: one "
        "model call per answer. Claims can only be graded against VERIFIED studies/labels and "
        "RATIFIED networks, so on an uncurated corpus this returns near-zero coverage — check "
        "get_evidence view=overview first.",
        EvaluateClaimsInput, evaluate_claims,
        mutating=True, nav_target="/evidence/alignment",
    ),
    ToolSpec(
        "generate_evidence_questions",
        "Generate monitoring questions backed by one network's evidence, plus every refusal and "
        "the gaps attributable to our own verification backlog (Phase 7). Needs network_id. Dry "
        "run unless commit=true. Staged into the review queue — it never creates or approves a "
        "bank question.",
        GenerateEvidenceQuestionsInput, generate_evidence_questions,
        mutating=True, nav_target="/evidence/governance",
    ),
    ToolSpec(
        "curate_evidence",
        "Record a CURATOR decision on the evidence corpus (data accuracy, not clinical judgement). "
        "action = study_check (verify; needs study_id + verified_by) | study_reject (needs "
        "study_id + rejected_by + reason) | drug_fact_check (needs fact_id + verified_by) | "
        "drug_fact_reject (needs fact_id + rejected_by + reason) | membership_decision (needs "
        "network_id + study_id + decision INCLUDED/EXCLUDED/REQUIRES_REVIEW + decided_by; a reason "
        "is required to exclude). Verification is refused while a study does not reproduce from "
        "its retained source. The name is recorded, NOT authenticated.",
        CurateEvidenceInput, curate_evidence,
        mutating=True, governance=True, nav_target="/evidence/studies",
    ),
    ToolSpec(
        "review_evidence",
        "Record a REVIEWER decision (clinical/statistical judgement). action = network_submit "
        "(needs submitted_by) | network_medical | network_statistical (both need reviewer + "
        "approve; a note is required to reject — approving the statistical stage is what RATIFIES "
        "the network) | network_reopen (needs reopened_by + reason) | protocol_decision (needs "
        "protocol_id + approval_role MEDICAL/STATISTICAL + decision APPROVED/REJECTED + reviewer) "
        "| protocol_revoke (needs approval_role + revoked_by + reason) | competitor_candidate "
        "(needs candidate_id + decision + reviewer) | competitor_config_applied (needs "
        "candidate_ids + applied_by). The name is recorded, NOT authenticated.",
        ReviewEvidenceInput, review_evidence,
        mutating=True, governance=True, nav_target="/evidence/governance",
    ),
]
