"""Copilot agent API — chat, SSE stream, confirm, health.

The LangGraph is compiled lazily on first use and cached for the process. The
chat/stream endpoints run the full graph; confirm executes a previously
previewed mutating action after re-verifying its HMAC token.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.copilot.audit import write_audit, write_confirm_audit
from app.copilot.confirm import verify_token
from app.copilot.response import state_to_response
from app.copilot.state import AgentResponse, PendingAction

LOG = logging.getLogger("copilot.api")

router = APIRouter(prefix="/copilot", tags=["copilot"])

_MAX_HISTORY = 20

_compiled_graph: Any = None


def _graph() -> Any:
    global _compiled_graph
    if _compiled_graph is not None:
        return _compiled_graph
    from app.copilot.graph import build_graph
    from app.copilot.providers.factory import get_provider

    _compiled_graph = build_graph(get_provider())
    LOG.info("Copilot graph compiled.")
    return _compiled_graph


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
class ChatHistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=8000)
    history: list[ChatHistoryMessage] = Field(default_factory=list, max_length=_MAX_HISTORY)
    ui_context: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    trace_id: str
    issued_at: float
    actor: str | None = None


class ConfirmResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    ui_action: dict[str, Any] | None = None
    job: dict[str, Any] | None = None
    """A background job the UI should poll (via /copilot/job) to acknowledge completion."""


class JobStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    status: Literal["running", "done", "unknown"]
    ok: bool
    summary: str


class PreviewRequest(BaseModel):
    """Re-price/re-mint a pending action after the user edits the card options."""
    model_config = ConfigDict(extra="forbid")
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    trace_id: str
    base_token: str
    base_args: dict[str, Any] = Field(default_factory=dict)
    base_issued_at: float


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok", "unavailable"]
    provider: str | None = None
    model_id: str | None = None
    error: str | None = None


def _build_messages(req: ChatRequest) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    for m in req.history:
        content = (m.content or "").strip()
        if content:
            msgs.append({"role": m.role, "content": content})
    msgs.append({"role": "user", "content": req.message})
    return msgs


def _initial_state(req: ChatRequest, trace_id: str) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "messages": _build_messages(req),
        "ui_context": req.ui_context or {},
        "tools_used": [],
        "tool_calls": [],
        "react_iter": 0,
        "guardrail_flags": [],
    }


def _model_id() -> str | None:
    try:
        from app.copilot.providers.factory import get_provider

        return getattr(get_provider(), "model_id", None)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    try:
        from app.copilot.providers.factory import get_provider

        _graph()
        provider = get_provider()
        return HealthResponse(
            status="ok",
            provider=type(provider).__name__,
            model_id=getattr(provider, "model_id", None),
        )
    except Exception as exc:  # noqa: BLE001
        return HealthResponse(status="unavailable", error=f"{type(exc).__name__}: {exc}")


@router.post("/chat", response_model=AgentResponse)
async def chat(req: ChatRequest) -> AgentResponse:
    try:
        graph = _graph()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, detail={"code": "copilot_unavailable", "error": f"{type(exc).__name__}: {exc}"}) from exc

    trace_id = req.trace_id or str(uuid.uuid4())
    t0 = time.perf_counter()
    try:
        final_state = await graph.ainvoke(_initial_state(req, trace_id))
    except Exception as exc:  # noqa: BLE001
        LOG.exception("Copilot chat failed trace_id=%s: %s", trace_id, exc)
        raise HTTPException(500, detail={"code": "copilot_failed", "trace_id": trace_id}) from exc

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    if not final_state.get("trace_id"):
        final_state["trace_id"] = trace_id
    write_audit(final_state, user_message=req.message, elapsed_ms=elapsed_ms, model_id=_model_id())
    return state_to_response(final_state)


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.post("/stream")
async def stream(req: ChatRequest) -> StreamingResponse:
    try:
        graph = _graph()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, detail={"code": "copilot_unavailable", "error": f"{type(exc).__name__}: {exc}"}) from exc

    trace_id = req.trace_id or str(uuid.uuid4())
    initial = _initial_state(req, trace_id)

    async def gen() -> AsyncIterator[str]:
        yield _sse("start", {"trace_id": trace_id})
        last_state: dict[str, Any] | None = None
        last_tools = 0
        emitted_intent = False
        emitted_pending = False
        last_ui_action: dict[str, Any] | None = None
        t0 = time.perf_counter()
        try:
            async for state in graph.astream(initial, stream_mode="values"):
                intent = state.get("intent")
                if intent is not None and not emitted_intent:
                    emitted_intent = True
                    yield _sse("status", {"node": "router", "intent": getattr(intent, "value", str(intent))})
                tcs = list(state.get("tool_calls") or [])
                if len(tcs) > last_tools:
                    for tc in tcs[last_tools:]:
                        yield _sse("tool", dict(tc))
                    last_tools = len(tcs)
                ui_action = state.get("ui_action") or None
                if ui_action and ui_action != last_ui_action:
                    yield _sse("ui_action", dict(ui_action))
                    last_ui_action = ui_action
                pending = state.get("pending_action") or None
                if pending and not emitted_pending:
                    emitted_pending = True
                    yield _sse("pending", dict(pending))
                last_state = state

            final_state = last_state or initial
            if not final_state.get("trace_id"):
                final_state["trace_id"] = trace_id
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            write_audit(final_state, user_message=req.message, elapsed_ms=elapsed_ms, model_id=_model_id())
            yield _sse("done", state_to_response(final_state).model_dump(mode="json"))
        except Exception as exc:  # noqa: BLE001
            LOG.exception("Copilot stream failed trace_id=%s: %s", trace_id, exc)
            yield _sse("error", {"code": "copilot_failed", "trace_id": trace_id, "error": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.post("/confirm", response_model=ConfirmResponse)
async def confirm(req: ConfirmRequest) -> ConfirmResponse:
    """Execute a previously-previewed mutating action after re-verifying its token."""
    from app.copilot.tools.registry import get_tool

    if not verify_token(req.token, req.tool_name, req.args, req.trace_id, req.issued_at):
        raise HTTPException(400, detail={"code": "invalid_or_expired_token"})

    try:
        spec = get_tool(req.tool_name)
    except KeyError as exc:
        raise HTTPException(400, detail={"code": "unknown_tool", "error": str(exc)}) from exc

    if not spec.mutating:
        raise HTTPException(400, detail={"code": "not_a_write_action"})

    # Governance defense-in-depth: a confirmed governance action must carry a reviewer.
    if spec.governance and not any(str(req.args.get(k) or "").strip() for k in ("approver_name", "reviewer_name", "scored_by")):
        raise HTTPException(400, detail={"code": "reviewer_required"})

    try:
        validated = spec.input_schema.model_validate(req.args)
        result = await spec.callable(validated)
    except Exception as exc:  # noqa: BLE001
        LOG.exception("Copilot confirm dispatch failed tool=%s: %s", req.tool_name, exc)
        write_confirm_audit(trace_id=req.trace_id, tool_name=req.tool_name, ok=False, actor=req.actor, summary=str(exc))
        return ConfirmResponse(ok=False, summary=f"{req.tool_name} failed.", error=str(exc))

    ui_action = {"target": "navigate", "to": result.nav_target} if result.ok and result.nav_target else None
    write_confirm_audit(trace_id=req.trace_id, tool_name=req.tool_name, ok=bool(result.ok), actor=req.actor, summary=result.summary)
    return ConfirmResponse(ok=bool(result.ok), summary=result.summary, data=result.data, error=result.error, ui_action=ui_action, job=result.job if result.ok else None)


@router.post("/preview", response_model=PendingAction)
async def preview(req: PreviewRequest) -> PendingAction:
    """Re-mint a pending action after the user edits its options on the confirm
    card. The original proposal token authorizes the request (it proves the agent
    proposed this tool in this conversation); we then validate the edited args
    and sign a fresh token bound to them. Governance is still enforced at
    /confirm."""
    from app.copilot.nodes.tool_executor import build_pending_action
    from app.copilot.tools.registry import get_tool

    if not verify_token(req.base_token, req.tool_name, req.base_args, req.trace_id, req.base_issued_at):
        raise HTTPException(400, detail={"code": "invalid_or_expired_token"})
    try:
        spec = get_tool(req.tool_name)
    except KeyError as exc:
        raise HTTPException(400, detail={"code": "unknown_tool", "error": str(exc)}) from exc
    if not spec.mutating:
        raise HTTPException(400, detail={"code": "not_a_write_action"})
    try:
        spec.input_schema.model_validate(req.args)
    except Exception as exc:  # noqa: BLE001 — surface a friendly validation error
        raise HTTPException(400, detail={"code": "invalid_args", "error": str(exc)}) from exc
    pa = build_pending_action(spec, req.args, req.trace_id)
    return PendingAction(**pa)


@router.get("/job", response_model=JobStatusResponse)
async def job_status(kind: str, run_id: str | None = None) -> JobStatusResponse:
    """Poll a background job's completion so the chat can acknowledge it.

    The frontend calls this after a confirmed action that started a long-running
    task (discovery, a monitoring run, an insight rebuild) and posts ``summary``
    to the chat once ``status == 'done'``.
    """
    k = (kind or "").strip().lower()

    if k == "harvest":
        from app.api import harvest as harvest_api

        h = harvest_api._HARVEST
        if h.get("running"):
            return JobStatusResponse(kind=k, status="running", ok=True, summary="Discovering new questions…")
        if not h.get("finished_at"):
            return JobStatusResponse(kind=k, status="unknown", ok=True, summary="")
        if h.get("error"):
            return JobStatusResponse(kind=k, status="done", ok=False, summary=f"Discovery failed: {h['error']}")
        res = h.get("last_result") or {}
        staged = int(res.get("staged", 0) or 0)
        held = int(res.get("quarantined_ae", 0) or 0)
        off = int(res.get("filtered_off_topic", 0) or 0)
        scope = ", ".join(s for s in (res.get("persona"), res.get("therapeutic_area")) if s)
        summ = f"Discovery finished: staged {staged} new question{'' if staged == 1 else 's'}"
        if scope:
            summ += f" for {scope}"
        extras = []
        if held:
            extras.append(f"{held} held for safety review")
        if off:
            extras.append(f"{off} off-topic filtered")
        if extras:
            summ += " (" + "; ".join(extras) + ")"
        summ += ". Review them on the Discover page."
        return JobStatusResponse(kind=k, status="done", ok=True, summary=summ)

    if k == "insights":
        from app.api import insights as insights_api

        r = insights_api._REBUILD
        if r.get("running"):
            return JobStatusResponse(kind=k, status="running", ok=True, summary="Rebuilding insight themes…")
        if not r.get("finished_at"):
            return JobStatusResponse(kind=k, status="unknown", ok=True, summary="")
        if r.get("error"):
            return JobStatusResponse(kind=k, status="done", ok=False, summary=f"Insight rebuild failed: {r['error']}")
        res = r.get("last_result") or {}
        themes = res.get("themes") or res.get("n_themes") or res.get("theme_count")
        summ = "Insight rebuild finished"
        if isinstance(themes, int):
            summ += f": {themes} themes"
        summ += ". See the Insights page."
        return JobStatusResponse(kind=k, status="done", ok=True, summary=summ)

    if k == "social":
        from app.api import social as social_api

        st = social_api._SOCIAL
        if st.get("running"):
            return JobStatusResponse(kind=k, status="running", ok=True, summary="Ingesting social posts…")
        if not st.get("finished_at"):
            return JobStatusResponse(kind=k, status="unknown", ok=True, summary="")
        if st.get("error"):
            return JobStatusResponse(kind=k, status="done", ok=False, summary=f"Social ingest failed: {st['error']}")
        res = st.get("last_result") or {}
        status_str = (res.get("status") or "").lower()
        if status_str in {"disabled", "not_configured"}:
            reason = res.get("reason") or "social listening is not configured."
            return JobStatusResponse(kind=k, status="done", ok=False, summary=f"Social ingest did not run: {reason}")
        ingested = int(res.get("ingested", 0) or 0)
        ae = int(res.get("ae", 0) or 0)
        dupes = int(res.get("duplicates", 0) or 0)
        summ = f"Social ingest finished: {ingested} post{'' if ingested == 1 else 's'} captured"
        extras = []
        if ae:
            extras.append(f"{ae} adverse-event signal{'' if ae == 1 else 's'}")
        if dupes:
            extras.append(f"{dupes} duplicate{'' if dupes == 1 else 's'} skipped")
        if extras:
            summ += " (" + "; ".join(extras) + ")"
        summ += ". See the Social Listening page."
        return JobStatusResponse(kind=k, status="done", ok=True, summary=summ)

    if k == "evidence_ingest":
        from app.api import evidence_ingestion as ing_api

        j = ing_api._JOB
        kind_label = str(j.get("kind") or "evidence").replace("-", " ")
        if j.get("running"):
            return JobStatusResponse(kind=k, status="running", ok=True, summary=f"Ingesting {kind_label} evidence…")
        if not j.get("finished_at"):
            return JobStatusResponse(kind=k, status="unknown", ok=True, summary="")
        if j.get("error"):
            return JobStatusResponse(kind=k, status="done", ok=False, summary=f"Evidence ingestion failed: {j['error']}")
        report = j.get("report") or {}
        committed = bool(report.get("committed"))
        summ = f"Evidence {kind_label} finished ({'committed' if committed else 'preview only — nothing was written'})"
        body = report.get("studies") or report.get("drug_facts") or report.get("reparse") or {}
        if isinstance(body, dict):
            counts = [
                f"{v} {label}" for label, v in (
                    ("ingested", body.get("ingested")),
                    ("updated", body.get("updated")),
                    ("studies", body.get("studies")),
                ) if isinstance(v, int) and v
            ]
            if counts:
                summ += ": " + ", ".join(counts)
        summ += ". Nothing is verified by ingestion — see the curation queue."
        return JobStatusResponse(kind=k, status="done", ok=True, summary=summ)

    if k == "run":
        if not run_id:
            return JobStatusResponse(kind=k, status="unknown", ok=False, summary="")
        from app.models.database import AsyncSessionLocal
        from app.services import run_service

        async with AsyncSessionLocal() as db:
            run = await run_service.get_run(db, run_id)
            if run is None:
                return JobStatusResponse(kind=k, status="unknown", ok=False, summary="")
            status = (run.status or "").upper()
            if status == "AWAITING_OPENEVIDENCE":
                return JobStatusResponse(kind=k, status="done", ok=True,
                    summary=f"Run {run_id} finished its automated targets and is awaiting Clinician (OpenEvidence) input.")
            if status not in {"COMPLETED", "FAILED", "CANCELLED", "CANCELED"}:
                return JobStatusResponse(kind=k, status="running", ok=True, summary="Run in progress…")
            done = (run.responses_success + run.responses_failed
                    + run.responses_truncated + run.responses_blocked)
            if status == "COMPLETED":
                return JobStatusResponse(kind=k, status="done", ok=True,
                    summary=f"Run {run_id} completed: {done} response{'' if done == 1 else 's'} captured. See AI Response Review.")
            if status in {"CANCELLED", "CANCELED"}:
                return JobStatusResponse(kind=k, status="done", ok=False, summary=f"Run {run_id} was cancelled.")
            note = f" {run.notes}" if run.notes else ""
            return JobStatusResponse(kind=k, status="done", ok=False, summary=f"Run {run_id} failed.{note}")

    return JobStatusResponse(kind=k, status="unknown", ok=False, summary="")


__all__ = ["router"]
