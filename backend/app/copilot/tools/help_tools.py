"""Help + navigation tools (read / UI only)."""
from __future__ import annotations

from app.copilot.help import search_help
from app.copilot.tools.schemas import ToolInput, ToolResultData, ToolSpec

# Pages the agent may navigate to when the user asks to open/go somewhere.
_VALID_ROUTES = {
    "/dashboard",
    "/dashboard/insights",
    "/dashboard/head-to-head",
    "/dashboard/recommendations",
    "/dashboard/activation-impact",
    "/dashboard/source-authority",
    "/dashboard/influence-graph",
    "/dashboard/ai-update-impact",
    "/dashboard/cortex",
    "/harvest",
    "/social-listening",
    "/questions",
    "/prompt-volume",
    "/run-analysis",
    "/run-analysis/variations",
    "/results",
    "/evidence",
    "/evidence/networks",
    "/evidence/comparisons",
    "/evidence/studies",
    "/evidence/drug-facts",
    "/evidence/governance",
    "/evidence/competitors",
    "/evidence/alignment",
    "/evidence/synthesis",
    "/evidence/ingest",
    "/digests",
    "/how-to-use",
}


class GetHelpInput(ToolInput):
    topic: str | None = None


async def get_help(payload: GetHelpInput) -> ToolResultData:
    sections = search_help(payload.topic)
    return ToolResultData(
        tool_name="get_help",
        ok=True,
        summary=f"Found {len(sections)} relevant help section(s).",
        data={"sections": sections},
    )


class NavigateInput(ToolInput):
    to: str


async def navigate(payload: NavigateInput) -> ToolResultData:
    to = (payload.to or "").strip()
    if to not in _VALID_ROUTES:
        return ToolResultData(
            tool_name="navigate",
            ok=False,
            summary=f"Unknown page {to!r}.",
            error=f"Valid pages: {sorted(_VALID_ROUTES)}",
        )
    return ToolResultData(
        tool_name="navigate",
        ok=True,
        summary=f"Opening {to}.",
        data={"to": to},
        nav_target=to,
    )


SPECS: list[ToolSpec] = [
    ToolSpec("get_help", "Explain how to use the app or a specific page/feature. Pass a topic (e.g. 'how do I start a run', 'discover questions', 'approvals').", GetHelpInput, get_help),
    ToolSpec("navigate", "Navigate the user to an app page when they ask to open/go there. to = one of /dashboard, /dashboard/insights, /dashboard/recommendations (GEO Interventions), /dashboard/activation-impact (Activation & Impact), /dashboard/source-authority, /dashboard/influence-graph (Influence Graph), /dashboard/ai-update-impact (AI Update Impact / model releases), /dashboard/cortex, /harvest, /social-listening, /questions, /prompt-volume, /run-analysis, /run-analysis/variations (Variation Testing), /results, /evidence (Clinical Evidence overview), /evidence/networks, /evidence/comparisons, /evidence/studies, /evidence/drug-facts, /evidence/governance, /evidence/competitors (Competitor Discovery), /evidence/alignment (AI vs Evidence), /evidence/synthesis, /evidence/ingest, /digests (Stakeholder Digests + Workshop AI Answer Insights), /how-to-use.", NavigateInput, navigate),
]
