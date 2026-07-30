"""Activation & Impact tools — owned interventions created from GEO recommendations.

An intervention is the measured half of a GEO recommendation: someone owns it,
publishes it, and the system then re-asks the same questions to see whether the
answer moved. Two tools, mirroring that split — one read, one write.

``manage_intervention`` is ``governance=True`` because every path records a named
actor and ``publish`` additionally launches real, billed baseline runs. The name
is recorded, not authenticated (there is no RBAC in this tree).
"""
from __future__ import annotations

from app.copilot.tools.schemas import ToolInput, ToolResultData, ToolSpec
from app.models.database import AsyncSessionLocal

_NAV = "/dashboard/activation-impact"
_VIEWS = ("list", "detail", "result", "timeline")
_ACTIONS = ("create_from_recommendation", "update", "transition", "publish", "measure_now")


def _err(name: str, summary: str, error: str) -> ToolResultData:
    return ToolResultData(tool_name=name, ok=False, summary=summary, error=error)


def _detail(exc: Exception) -> str:
    return str(getattr(exc, "detail", exc))


class GetInterventionsInput(ToolInput):
    view: str = "list"
    intervention_id: str | None = None
    status: str | None = None


async def get_interventions(payload: GetInterventionsInput) -> ToolResultData:
    from app.models.intervention import STATUSES
    from app.services import intervention_service as svc

    view = (payload.view or "list").strip().lower()
    if view not in _VIEWS:
        return _err("get_interventions", f"Unknown view {view!r}.", f"view must be one of {' | '.join(_VIEWS)}")
    status = (payload.status or "").strip().upper() or None
    if status and status not in STATUSES:
        return _err(
            "get_interventions",
            f"Unknown status {status!r}.",
            f"status must be one of {', '.join(STATUSES)}",
        )
    if view != "list" and not payload.intervention_id:
        return _err(
            "get_interventions",
            f"The {view!r} view needs an intervention_id.",
            f"intervention_id is required for view={view!r}",
        )

    async with AsyncSessionLocal() as db:
        try:
            if view == "list":
                result = await svc.list_interventions(db, status=status)
                scope = f" ({status})" if status else ""
                return ToolResultData(
                    tool_name="get_interventions", ok=True,
                    summary=f"{result.get('count', 0)} intervention(s){scope}.",
                    data={"view": view, "result": result},
                )
            if view == "detail":
                result = await svc.get_intervention(db, payload.intervention_id)
            elif view == "result":
                result = await svc.get_result(db, payload.intervention_id)
            else:
                result = await svc.get_timeline(db, payload.intervention_id)
        except Exception as exc:  # noqa: BLE001 — 404 from the service
            return _err("get_interventions", "No such intervention.", _detail(exc))

    return ToolResultData(
        tool_name="get_interventions", ok=True,
        summary=f"Intervention {payload.intervention_id}: {view}.",
        data={"view": view, "result": result},
    )


class ManageInterventionInput(ToolInput):
    action: str
    intervention_id: str | None = None
    recommendation_id: str | None = None
    title: str | None = None
    description: str | None = None
    owner_name: str | None = None
    reviewer_name: str | None = None
    priority: str | None = None
    to_status: str | None = None
    actor_name: str | None = None
    notes: str | None = None
    publication_url: str | None = None
    measurement_wait_days: int | None = None
    repetitions_per_question: int | None = None


async def manage_intervention(payload: ManageInterventionInput) -> ToolResultData:
    from app.schemas import (
        InterventionCreate,
        InterventionPublish,
        InterventionTransition,
        InterventionUpdate,
    )
    from app.services import intervention_service as svc

    action = (payload.action or "").strip().lower()
    if action not in _ACTIONS:
        return _err("manage_intervention", f"Unknown action {action!r}.", f"action must be one of {' | '.join(_ACTIONS)}")
    if action == "create_from_recommendation":
        if not payload.recommendation_id:
            return _err("manage_intervention", "Creating an intervention needs a recommendation_id.", "recommendation_id is required")
    elif not payload.intervention_id:
        return _err("manage_intervention", f"The {action!r} action needs an intervention_id.", "intervention_id is required")

    async with AsyncSessionLocal() as db:
        try:
            if action == "create_from_recommendation":
                result = await svc.create_from_recommendation(
                    db, payload.recommendation_id,
                    InterventionCreate(
                        title=payload.title, description=payload.description,
                        owner_name=payload.owner_name, reviewer_name=payload.reviewer_name,
                        priority=payload.priority,
                        measurement_wait_days=payload.measurement_wait_days,
                        repetitions_per_question=payload.repetitions_per_question,
                    ),
                )
                summary = (
                    f"Created intervention {result.get('id')} from recommendation "
                    f"{payload.recommendation_id}, owned by {payload.owner_name or 'nobody yet'}."
                )
            elif action == "update":
                result = await svc.update_intervention(
                    db, payload.intervention_id,
                    InterventionUpdate(
                        title=payload.title, description=payload.description,
                        owner_name=payload.owner_name, reviewer_name=payload.reviewer_name,
                        priority=payload.priority,
                        measurement_wait_days=payload.measurement_wait_days,
                        repetitions_per_question=payload.repetitions_per_question,
                    ),
                )
                summary = f"Updated intervention {payload.intervention_id}."
            elif action == "transition":
                if not payload.to_status:
                    return _err("manage_intervention", "A transition needs a to_status.", "to_status is required (PROPOSED | IN_PROGRESS | DEFERRED | CANCELLED)")
                result = await svc.transition(
                    db, payload.intervention_id,
                    InterventionTransition(
                        to_status=(payload.to_status or "").strip().upper(),
                        actor_name=payload.actor_name, notes=payload.notes,
                    ),
                )
                summary = f"Intervention {payload.intervention_id} moved to {payload.to_status.upper()}."
            elif action == "publish":
                if not payload.publication_url:
                    return _err("manage_intervention", "Publishing needs the publication_url.", "publication_url is required")
                result = await svc.publish(
                    db, payload.intervention_id,
                    InterventionPublish(
                        publication_url=payload.publication_url,
                        actor_name=payload.actor_name,
                    ),
                )
                summary = (
                    f"Published intervention {payload.intervention_id} and launched its "
                    "official baseline runs (these call the AI models and are billed)."
                )
            else:
                result = await svc.measure_now(db, payload.intervention_id)
                summary = f"Advanced measurement for intervention {payload.intervention_id} by one step."
        except Exception as exc:  # noqa: BLE001 — HTTPException from the service
            return _err("manage_intervention", "Could not complete that action.", _detail(exc))

    return ToolResultData(
        tool_name="manage_intervention", ok=True, summary=summary,
        data={"action": action, "result": result}, nav_target=_NAV,
    )


SPECS: list[ToolSpec] = [
    ToolSpec(
        "get_interventions",
        "Read Activation & Impact: the owned, measured interventions created from GEO "
        "recommendations (did publishing the content actually move what the AI says?). "
        "view = list (optional status PROPOSED|IN_PROGRESS|PUBLISHED|MEASURING|COMPLETED|"
        "DEFERRED|CANCELLED) | detail | result (the before/after measurement) | timeline "
        "(the immutable event log). All but list need an intervention_id.",
        GetInterventionsInput, get_interventions,
    ),
    ToolSpec(
        "manage_intervention",
        "Act on an Activation & Impact intervention. action = create_from_recommendation "
        "(needs recommendation_id; optional title/owner_name/priority) | update | transition "
        "(needs to_status PROPOSED|IN_PROGRESS|DEFERRED|CANCELLED) | publish (needs "
        "publication_url — this LAUNCHES billed baseline runs against the AI models) | "
        "measure_now (force the measurement sweep forward one step). Every path records a "
        "named actor; the name is recorded, NOT authenticated.",
        ManageInterventionInput, manage_intervention,
        mutating=True, governance=True, nav_target=_NAV,
    ),
]
