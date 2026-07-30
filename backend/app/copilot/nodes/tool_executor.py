"""Tool executor node — dispatches tool calls; intercepts mutating ones.

Read (and other non-mutating) tools execute immediately and their results are
fed back to the Orchestrator. Mutating tools are NOT executed here: instead we
mint an HMAC-signed ``pending_action`` and end the turn so the UI can render a
Confirm card. Governance tools that are missing a reviewer/approver name are
bounced back to the Orchestrator (as an ok=false tool result) so it asks the
user for the name before re-proposing the action.
"""
from __future__ import annotations

import json
import logging
import time
from functools import lru_cache
from typing import Any

from app.config import taxonomy
from app.copilot.confirm import mint_token
from app.copilot.state import AgentState
from app.copilot.tools.registry import TOOLS, get_tool
from app.copilot.tools.schemas import ToolResultData, ToolSpec

LOG = logging.getLogger("copilot.tool_executor")

# Arg keys that satisfy the "reviewer/approver name required" governance rule.
# Every governance surface names the deciding human in its own vocabulary, so this
# list has to cover all of them: a key missing here means the executor can never
# see a reviewer and bounces the tool back forever.
_REVIEWER_KEYS = (
    "approver_name", "reviewer_name", "scored_by",
    # Evidence curation + review (recorded, not authenticated — no RBAC in this tree).
    "verified_by", "rejected_by", "decided_by", "submitted_by", "reopened_by",
    "revoked_by", "applied_by", "reviewer",
    # Activation & Impact.
    "actor_name", "owner_name",
)


async def tool_executor_node(state: AgentState) -> dict[str, Any]:
    messages = list(state.get("messages") or [])
    tool_calls = _pending_tool_calls(messages)
    if not tool_calls:
        return {"tool_just_ran": False}

    trace_id = str(state.get("trace_id") or "")
    new_messages: list[dict[str, Any]] = []
    tools_used: list[str] = list(state.get("tools_used") or [])
    summaries: list[dict[str, Any]] = list(state.get("tool_calls") or [])
    last_result: dict[str, Any] | None = state.get("last_tool_result")
    ui_action: dict[str, Any] | None = state.get("ui_action") or None
    prompt_options: dict[str, Any] | None = state.get("prompt_options") or None
    pending_action: dict[str, Any] | None = None

    for call in tool_calls:
        name = str(call.get("name", ""))
        args = dict(call.get("input", {}) or {})
        call_id = str(call.get("id", ""))

        try:
            spec = get_tool(name)
        except KeyError as exc:
            new_messages.append(_tool_msg(call_id, name, ToolResultData(
                tool_name=name or "<unknown>", ok=False,
                summary=f"Unknown tool {name!r}.", error=str(exc),
            )))
            continue

        # Governance: require a reviewer/approver name BEFORE proposing.
        if spec.mutating and spec.governance and not _has_reviewer(args):
            new_messages.append(_tool_msg(call_id, name, ToolResultData(
                tool_name=name, ok=False,
                summary="This action needs a reviewer/approver name. Ask the user who is approving, then call again.",
                error="reviewer_required",
            )))
            continue

        # Mutating: do NOT execute — mint a pending action for confirmation.
        if spec.mutating and pending_action is None:
            pending_action = build_pending_action(spec, args, trace_id)
            new_messages.append(_tool_msg(call_id, name, ToolResultData(
                tool_name=name, ok=True,
                summary=f"Awaiting user confirmation: {pending_action['summary']}",
                data={"awaiting_confirmation": True},
            )))
            continue

        if spec.mutating:
            # A second mutating call in the same turn — skip; one confirm at a time.
            new_messages.append(_tool_msg(call_id, name, ToolResultData(
                tool_name=name, ok=False,
                summary="Only one action can be confirmed at a time; propose this next.",
                error="another_action_pending",
            )))
            continue

        # Non-mutating: execute now.
        t0 = time.perf_counter()
        result = await _dispatch(spec, args)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        tools_used.append(name)
        last_result = result.model_dump(mode="json")
        summaries.append({
            "tool_name": result.tool_name or name,
            "elapsed_ms": elapsed_ms,
            "ok": bool(result.ok),
            "summary": (result.summary or "")[:200],
        })
        if result.ok and result.nav_target:
            ui_action = {"target": "navigate", "to": result.nav_target}
        if result.ok and result.prompt_options:
            prompt_options = result.prompt_options
        new_messages.append(_tool_msg(call_id, name, result))

    patch: dict[str, Any] = {
        "messages": new_messages,
        "tools_used": tools_used,
        "tool_calls": summaries,
        "react_iter": int(state.get("react_iter") or 0) + 1,
        "tool_just_ran": True,
        "last_tool_result": last_result,
    }
    if ui_action is not None:
        patch["ui_action"] = ui_action
    if prompt_options is not None:
        patch["prompt_options"] = prompt_options
    if pending_action is not None:
        patch["pending_action"] = pending_action
        # Give the user a short prose confirmation prompt alongside the card.
        patch["messages"] = [
            *new_messages,
            {"role": "assistant", "content": f"I'm ready to do this: {pending_action['summary']} Please confirm.", "tool_calls": []},
        ]
    return patch


def route_after_tool_executor(state: AgentState, *, react_iter_cap: int) -> str:
    if state.get("pending_action"):
        return "validator"
    if int(state.get("react_iter") or 0) < react_iter_cap:
        return "orchestrator"
    return "validator"


# ---------------------------------------------------------------------------
def _pending_tool_calls(messages: list[Any]) -> list[dict[str, Any]]:
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "assistant":
            return list(m.get("tool_calls") or [])
    return []


def _has_reviewer(args: dict[str, Any]) -> bool:
    return any(str(args.get(k) or "").strip() for k in _REVIEWER_KEYS)


def _tool_msg(call_id: str, name: str, result: ToolResultData) -> dict[str, Any]:
    return {
        "role": "tool",
        "content": json.dumps(result.model_dump(mode="json")),
        "tool_call_id": call_id,
        "tool_name": name,
    }


async def _dispatch(spec: ToolSpec, args: dict[str, Any]) -> ToolResultData:
    try:
        validated = spec.input_schema.model_validate(args)
    except Exception as exc:  # noqa: BLE001 — surface validation error to the LLM
        return ToolResultData(tool_name=spec.name, ok=False, summary=f"Invalid input for {spec.name}: {exc}", error=str(exc))
    try:
        return await spec.callable(validated)
    except Exception as exc:  # noqa: BLE001 — never raise out of a tool
        LOG.exception("Tool %s raised: %s", spec.name, exc)
        return ToolResultData(tool_name=spec.name, ok=False, summary=f"{spec.name} failed.", error=str(exc))


# Human-readable previews for the confirmation card (no execution).
def _preview_summary(spec: ToolSpec, args: dict[str, Any]) -> str:
    name = spec.name
    if name == "start_run":
        return "Start a dry run (preview only)." if args.get("dry_run") else "Start a monitoring run."
    if name == "cancel_run":
        return f"Cancel run {args.get('run_id', '')}."
    if name == "run_harvest":
        return "Discover new questions from public communities."
    if name == "set_schedule":
        return "Update the daily run schedule."
    if name == "rebuild_insights":
        return "Rebuild the theme taxonomy and re-tag all responses."
    if name == "snowflake_sync":
        return "Sync operational data into Snowflake."
    if name == "score_sweep":
        return "Score any unscored responses."
    if name == "rescore":
        return f"Re-score responses (prompt {args.get('prompt_version', 'v2')})."
    if name == "create_question":
        return f"Create a PENDING question: \"{str(args.get('question_text', ''))[:80]}\"."
    if name == "update_question":
        return f"Edit question id {args.get('row_id', '')}."
    if name == "set_question_approval":
        return f"Set question {args.get('row_id', '')} to {args.get('approval_status', '')}."
    if name == "delete_question":
        return f"Soft-delete question {args.get('row_id', '')}."
    if name == "promote_harvested":
        return f"Promote harvested item {args.get('item_id', '')} into the question bank (PENDING)."
    if name == "reject_harvested":
        return f"Reject harvested item {args.get('item_id', '')}."
    if name == "override_score":
        return f"Override the score on response {args.get('response_id', '')}."
    if name == "export_data":
        return f"Export {args.get('target', 'responses')} data."
    if name == "oe_capture":
        return f"Capture an OpenEvidence answer for question {args.get('question_id', '')}."
    if name == "oe_finalize_run":
        return f"Finalize run {args.get('run_id', '')} without OpenEvidence."
    if name == "oe_auto":
        return f"Run OpenEvidence auto {args.get('action', '')}."
    if name == "run_evidence_ingest":
        mode = "Commit" if args.get("commit") else "Preview"
        scope = args.get("indication") or ", ".join(args.get("brands") or []) or "the configured drugs"
        return f"{mode}: {str(args.get('action', '')).replace('_', ' ')} for {scope}."
    if name == "evaluate_claims":
        if args.get("scope") == "response":
            return f"Check the claims in response {args.get('response_id', '')} against verified evidence (1 model call)."
        limit = args.get("limit") or 200
        return (
            f"Check run {args.get('run_id', '')} against verified evidence — up to "
            f"{limit} answers, one model call each."
        )
    if name == "generate_evidence_questions":
        mode = "Generate and stage" if args.get("commit") else "Preview (nothing stored)"
        return f"{mode}: evidence-backed questions for network {args.get('network_id', '')}."
    if name == "curate_evidence":
        subject = args.get("study_id") or args.get("fact_id") or args.get("network_id") or ""
        return f"Record a curator decision ({str(args.get('action', '')).replace('_', ' ')}) on {subject}."
    if name == "review_evidence":
        subject = args.get("network_id") or args.get("protocol_id") or args.get("candidate_id") or ""
        return f"Record a reviewer decision ({str(args.get('action', '')).replace('_', ' ')}) on {subject}."
    if name == "manage_intervention":
        if args.get("action") == "publish":
            return "Publish this intervention and launch its baseline measurement runs (billed)."
        return f"Intervention {str(args.get('action', '')).replace('_', ' ')}."
    if name == "generate_curation_questions":
        mode = "Generate and stage" if args.get("commit") else "Dry run (nothing stored, nothing billed)"
        return f"{mode}: questions for the top {args.get('limit', 20)} comparison coverage gaps."
    if name == "run_questions_to_pipeline":
        n = len(args.get("item_ids") or [])
        return (
            f"Approve {n} discovered question(s) and run them now. This bypasses the "
            "Medical-Affairs approval step."
        )
    # Generic fallback.
    shown = ", ".join(f"{k}={v}" for k, v in args.items() if v not in (None, [], ""))
    return f"{name.replace('_', ' ')}{(' (' + shown + ')') if shown else ''}."


# ---------------------------------------------------------------------------
# Structured field previews for the confirmation card. Each pending action
# lists its effective options so the user sees exactly what will run (e.g.
# unset filters as "All", unset limits as "Auto (default)").
_FIELD_LABELS: dict[str, str] = {
    "persona": "Persona",
    "therapeutic_area": "Therapeutic area / indication",
    "brand_focus": "Brand",
    "domain": "Domain",
    "question_ids": "Specific questions",
    "dry_run": "Dry run (preview only)",
    "max_queries": "Max search queries",
    "max_items": "Max items",
    "run_id": "Run",
    "row_id": "Question",
    "item_id": "Item",
    "response_id": "Response",
    "question_id": "Question",
    "approval_status": "Set status to",
    "approver_name": "Approver",
    "reviewer_name": "Reviewer",
    "scored_by": "Reviewed by",
    "reason": "Reason",
    "enabled": "Enabled",
    "cron": "Schedule (cron)",
    "timezone": "Timezone",
    "question_text": "Question text",
    "active": "Active",
    "prompt_version": "Prompt version",
    "target": "Export target",
    "override_ae": "Adverse-event override",
    "sentiment_score": "New sentiment score",
    "competitive_position": "New positioning",
    "action": "Action",
    "model_version": "Model version",
    # Evidence + activation
    "network_id": "Network",
    "study_id": "Study",
    "fact_id": "Drug label",
    "protocol_id": "Analysis protocol",
    "candidate_id": "Competitor candidate",
    "candidate_ids": "Competitor candidates",
    "intervention_id": "Intervention",
    "recommendation_id": "Source recommendation",
    "indication": "Indication",
    "treatment_a": "Treatment",
    "treatment_b": "Comparator",
    "execution_mode": "Execution mode",
    "commit": "Write the results (off = preview only)",
    "approve": "Approve",
    "approval_role": "Approval role",
    "decision": "Decision",
    "note": "Note",
    "scope": "Scope",
    "drugs": "Drugs",
    "brands": "Brands",
    "study_ids": "Studies",
    "outcome": "Canonical outcome",
    "protocol": "Analysis protocol",
    "phase": "Treatment phase",
    "stratum": "Population stratum",
    "verified_by": "Verified by",
    "rejected_by": "Rejected by",
    "decided_by": "Decided by",
    "submitted_by": "Submitted by",
    "reopened_by": "Reopened by",
    "revoked_by": "Revoked by",
    "applied_by": "Applied by",
    "reviewer": "Reviewer",
    "actor_name": "Actor",
    "owner_name": "Owner",
    "to_status": "Move to",
    "publication_url": "Publication URL",
    "item_ids": "Discovered items",
    "monitoring_mode": "Monitoring mode",
    "brands_list": "Brands",
    "therapeutic_areas": "Therapeutic areas",
    "diseases": "Indications",
    "personas": "Personas",
}

# Tools whose unset parameters mean "apply to everything" (shown as All/Auto)
# rather than "leave unchanged" (which we hide).
_FILTER_TOOLS = {"start_run", "run_harvest"}
_NUMERIC_DEFAULT_KEYS = {"max_queries", "max_items", "limit", "top", "sample_cap", "target_themes"}

# Per-field edit metadata for the confirm card. Keys not listed fall back to a
# type inferred from the value (bool->boolean, number->number, else text).
_FIELD_META: dict[str, dict[str, Any]] = {
    "persona": {"type": "select", "options": ["Prospect", "Provider", "Patient"]},
    "domain": {"type": "select", "options": ["Efficacy", "Safety", "Access", "Comparative", "General"]},
    "approval_status": {"type": "select", "options": ["APPROVED", "REJECTED"]},
    "therapeutic_area": {"type": "text"},
    "brand_focus": {"type": "text"},
    "question_text": {"type": "text"},
    "reason": {"type": "text"},
    "approver_name": {"type": "text"},
    "reviewer_name": {"type": "text"},
    "scored_by": {"type": "text"},
    "cron": {"type": "text"},
    "timezone": {"type": "text"},
    "prompt_version": {"type": "text"},
    "target": {"type": "text"},
    "action": {"type": "text"},
    "model_version": {"type": "text"},
    "competitive_position": {"type": "text"},
    "dry_run": {"type": "boolean"},
    "enabled": {"type": "boolean"},
    "active": {"type": "boolean"},
    "override_ae": {"type": "boolean"},
    "max_queries": {"type": "number"},
    "max_items": {"type": "number"},
    "sentiment_score": {"type": "number"},
    # Evidence + activation
    "commit": {"type": "boolean"},
    "approve": {"type": "boolean"},
    "approval_role": {"type": "select", "options": ["MEDICAL", "STATISTICAL"]},
    "decision": {"type": "text"},
    "execution_mode": {"type": "select", "options": ["EXPLORATORY", "GOVERNED"]},
    "to_status": {"type": "select", "options": ["PROPOSED", "IN_PROGRESS", "DEFERRED", "CANCELLED"]},
    "monitoring_mode": {"type": "select", "options": ["BRAND", "DISEASE_STATE"]},
    "scope": {"type": "text"},
    "indication": {"type": "text"},
    "note": {"type": "text"},
    "verified_by": {"type": "text"},
    "rejected_by": {"type": "text"},
    "decided_by": {"type": "text"},
    "submitted_by": {"type": "text"},
    "reopened_by": {"type": "text"},
    "revoked_by": {"type": "text"},
    "applied_by": {"type": "text"},
    "reviewer": {"type": "text"},
    "actor_name": {"type": "text"},
    "owner_name": {"type": "text"},
    "publication_url": {"type": "text"},
}
# Identifier / complex fields that must not be edited on the card.
_NON_EDITABLE = {
    "run_id", "row_id", "item_id", "response_id", "question_id", "question_ids",
    "network_id", "study_id", "study_ids", "fact_id", "protocol_id",
    "candidate_id", "candidate_ids", "intervention_id", "recommendation_id",
    "item_ids",
}

# One-click "quick fill" presets for guided setup of filter-style write actions.
# Each preset is a PARTIAL set of args the UI merges onto the current args, then
# re-validates + re-signs via /copilot/preview (same token/governance/validation
# guarantees as a manual edit). Keep to <=4 per tool so the pills stay scannable.
_TOOL_PRESETS: dict[str, list[dict[str, Any]]] = {
    "start_run": [
        {"label": "Full run", "description": "All personas, areas & domains", "args": {"persona": None, "therapeutic_area": None, "domain": None, "dry_run": False}},
        {"label": "Dry run", "description": "Validate targets without saving responses", "args": {"dry_run": True}},
        {"label": "Providers only", "description": "Limit to HCP-facing questions", "args": {"persona": "Provider"}},
        {"label": "Safety focus", "description": "Only Safety-domain questions", "args": {"domain": "Safety"}},
    ],
    "run_harvest": [
        {"label": "Providers", "description": "Discover HCP (provider) questions", "args": {"persona": "Provider"}},
        {"label": "Patients", "description": "Discover patient questions", "args": {"persona": "Patient"}},
        {"label": "Quick scan", "description": "~10 search queries", "args": {"max_queries": 10}},
        {"label": "Deep scan", "description": "~40 search queries", "args": {"max_queries": 40}},
    ],
    "run_evidence_ingest": [
        {"label": "Preview", "description": "Report what would be stored, write nothing", "args": {"commit": False}},
        {"label": "Commit", "description": "Persist the fetched evidence", "args": {"commit": True}},
    ],
    "generate_curation_questions": [
        {"label": "Dry run", "description": "Count the model calls, write nothing", "args": {"commit": False}},
        {"label": "Commit", "description": "Generate and stage for review", "args": {"commit": True}},
        {"label": "Small batch", "description": "Top 10 gaps only", "args": {"limit": 10}},
    ],
    "generate_evidence_questions": [
        {"label": "Preview", "description": "Show what would be staged", "args": {"commit": False}},
        {"label": "Commit", "description": "Stage into the review queue", "args": {"commit": True}},
    ],
}


def _presets_for(tool_name: str) -> list[dict[str, Any]]:
    """Quick-fill presets for a tool's confirm card (empty if none defined)."""
    return [dict(p) for p in _TOOL_PRESETS.get(tool_name, [])]


def _is_empty(val: Any) -> bool:
    return val is None or val == "" or val == [] or val == {}


def _fmt_field_value(key: str, val: Any) -> str:
    if isinstance(val, bool):
        return "Yes" if val else "No"
    if _is_empty(val):
        return "Auto (default)" if key in _NUMERIC_DEFAULT_KEYS else "All"
    if isinstance(val, (list, tuple)):
        items = [str(x) for x in val]
        head = ", ".join(items[:6])
        return head + (f" (+{len(items) - 6} more)" if len(items) > 6 else "")
    return str(val)


@lru_cache(maxsize=1)
def _valid_therapeutic_areas() -> tuple[str, ...]:
    """Therapeutic-area names from the taxonomy (for the TA dropdown). Cached.

    Cleared by ``taxonomy.reload()`` — see ``taxonomy._DEPENDENT_CACHES`` — so a newly added
    area appears in the dropdown without a restart.
    """
    try:
        cfg = taxonomy.config()
        return tuple(sorted((cfg.get("therapeutic_areas") or {}).keys()))
    except Exception:  # noqa: BLE001 — config missing -> fall back to free text
        return ()


def _field_meta(key: str, val: Any) -> dict[str, Any]:
    if key == "therapeutic_area":
        tas = _valid_therapeutic_areas()
        return {"type": "select", "options": list(tas)} if tas else {"type": "text"}
    if key in _FIELD_META:
        return _FIELD_META[key]
    if isinstance(val, bool):
        return {"type": "boolean"}
    if key in _NUMERIC_DEFAULT_KEYS or (isinstance(val, (int, float)) and not isinstance(val, bool)):
        return {"type": "number"}
    return {"type": "text"}


def _raw_value(ftype: str, val: Any) -> Any:
    """The value used to seed the editable control (None => shown as All/Auto)."""
    if ftype == "boolean":
        return bool(val)
    if _is_empty(val):
        return None
    return val


def _preview_fields(spec: ToolSpec, args: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the {key,label,value,editable,type,options,allow_empty,raw} list shown
    on the confirm card.

    For filter tools (start_run, run_harvest) every parameter is shown, with
    unset ones rendered as "All"/"Auto" and editable (allow_empty). For other
    tools we only show the parameters that are actually set (i.e. what's
    changing). Identifier fields are shown read-only.
    """
    try:
        dumped = spec.input_schema.model_validate(args).model_dump()
    except Exception:  # noqa: BLE001 — fall back to raw args if validation fails
        dumped = dict(args)
    is_filter = spec.name in _FILTER_TOOLS
    fields: list[dict[str, Any]] = []
    for key, val in dumped.items():
        if _is_empty(val) and not is_filter:
            continue
        editable = key not in _NON_EDITABLE
        meta = _field_meta(key, val) if editable else {"type": "text"}
        ftype = meta["type"]
        options = list(meta.get("options", []))
        # Always keep an agent-proposed value selectable (e.g. a persona in a
        # different casing, or an indication that isn't a canonical TA).
        if ftype == "select" and val not in (None, "", []) and str(val) not in options:
            options = [str(val), *options]
        fields.append({
            "key": key,
            "label": _FIELD_LABELS.get(key, key.replace("_", " ").capitalize()),
            "value": _fmt_field_value(key, val),
            "editable": editable,
            "type": ftype,
            "options": options,
            "allow_empty": bool(is_filter),
            "raw": _raw_value(ftype, val),
        })
    return fields


def build_pending_action(
    spec: ToolSpec, args: dict[str, Any], trace_id: str, *, issued_at: float | None = None
) -> dict[str, Any]:
    """Assemble a confirmable pending action: HMAC token + one-line summary +
    editable field options. Shared by the executor (initial proposal) and the
    /copilot/preview endpoint (re-mint after the user edits the options)."""
    issued_at = issued_at if issued_at is not None else time.time()
    return {
        "token": mint_token(spec.name, args, trace_id, issued_at),
        "tool_name": spec.name,
        "args": args,
        "summary": _preview_summary(spec, args),
        "issued_at": issued_at,
        "trace_id": trace_id,
        "governance": bool(spec.governance),
        "nav_target": spec.nav_target,
        "fields": _preview_fields(spec, args),
        "presets": _presets_for(spec.name),
    }
