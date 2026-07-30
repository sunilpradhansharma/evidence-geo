"""Read tools — answer questions about the app's data (no writes).

Each tool opens its own ``AsyncSessionLocal`` and calls the existing services
so behaviour matches the REST API exactly. Results are JSON-safe dicts the
LLM grounds its answer in.
"""
from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

from app.copilot.tools.schemas import ToolInput, ToolResultData, ToolSpec
from app.models.database import AsyncSessionLocal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _normalize(result: Any) -> Any:
    """Endpoint functions sometimes return a JSONResponse; unwrap to a dict."""
    if isinstance(result, JSONResponse):
        import json

        return json.loads(bytes(result.body).decode("utf-8"))
    return result


def _ok(
    name: str,
    summary: str,
    data: dict | None = None,
    *,
    prompt_options: dict | None = None,
) -> ToolResultData:
    return ToolResultData(
        tool_name=name, ok=True, summary=summary, data=data or {}, prompt_options=prompt_options
    )


def _err(name: str, summary: str, error: str) -> ToolResultData:
    return ToolResultData(tool_name=name, ok=False, summary=summary, error=error)


# ---------------------------------------------------------------------------
# query_data — Cortex Analyst natural-language Q&A
# ---------------------------------------------------------------------------
class QueryDataInput(ToolInput):
    question: str


async def query_data(payload: QueryDataInput) -> ToolResultData:
    from app.snowflake import agent as sf_agent

    res = await sf_agent.chat(payload.question, [])
    answer = res.get("answer", "")
    if not res.get("enabled"):
        return _ok(
            "query_data",
            "The warehouse Q&A (Cortex) is not connected; use the structured "
            "analytics tools instead.",
            {"enabled": False, "answer": answer},
        )
    return _ok("query_data", answer or "No answer.", {"enabled": True, "answer": answer})


# ---------------------------------------------------------------------------
# get_analytics — dashboard KPIs / distributions
# ---------------------------------------------------------------------------
_ANALYTICS_KINDS = {
    "sentiment_distribution",
    "positioning",
    "volume",
    "alerts_summary",
    "consensus_summary",
    "intent_distribution",
    "llm_comparison",
    "worst_questions",
    "run_summary",
    "persona_summary",
    "landscape",
}

# Kinds that take no therapeutic-area/brand scope, so a supplied filter is reported as
# ignored rather than silently dropped.
_UNSCOPED_KINDS = {
    "volume", "alerts_summary", "consensus_summary", "intent_distribution",
    "llm_comparison", "run_summary",
}


class GetAnalyticsInput(ToolInput):
    kind: str
    run_id: str | None = None
    persona: str | None = None
    therapeutic_area: str | None = None
    indication: str | None = None
    disease: str | None = None
    brand: str | None = None
    limit: int = 5


async def get_analytics(payload: GetAnalyticsInput) -> ToolResultData:
    from app.api import analytics as api

    kind = (payload.kind or "").strip().lower()
    if kind not in _ANALYTICS_KINDS:
        return _err(
            "get_analytics",
            f"Unknown analytics kind {kind!r}.",
            f"kind must be one of: {sorted(_ANALYTICS_KINDS)}",
        )
    # Every endpoint below declares its filters BEFORE the session, so `db` MUST be passed
    # by keyword: positionally it binds to the first filter instead, and the call only
    # appears to work while Snowflake is serving the answer.
    scope = {
        "therapeutic_area": payload.therapeutic_area or None,
        "indication": payload.indication or None,
        "disease": payload.disease or None,
        "brand": payload.brand or None,
    }
    ignored = sorted(k for k, v in scope.items() if v) if kind in _UNSCOPED_KINDS else []
    async with AsyncSessionLocal() as db:
        try:
            if kind == "sentiment_distribution":
                data = _normalize(await api.sentiment_distribution(db=db, **scope))
            elif kind == "positioning":
                data = _normalize(await api.positioning(db=db, **scope))
            elif kind == "volume":
                data = _normalize(await api.volume(db=db))
            elif kind == "alerts_summary":
                data = _normalize(await api.alerts_summary(db=db))
            elif kind == "consensus_summary":
                data = _normalize(await api.consensus_summary(db=db))
            elif kind == "intent_distribution":
                data = _normalize(await api.intent_distribution(db=db))
            elif kind == "llm_comparison":
                data = _normalize(await api.llm_comparison(db=db))
            elif kind == "worst_questions":
                data = _normalize(await api.worst_questions(
                    limit=payload.limit, persona=payload.persona, db=db, **scope))
            elif kind == "run_summary":
                if not payload.run_id:
                    return _err("get_analytics", "run_summary needs a run_id.", "run_id is required")
                data = _normalize(await api.run_summary(run_id=payload.run_id, db=db))
            elif kind == "persona_summary":
                data = _normalize(await api.persona_summary(
                    persona=payload.persona, db=db, **scope))
            elif kind == "landscape":
                # Disease-state only: brand_focus is null on those answers, so the brand
                # scope has nothing to match and is not offered.
                data = _normalize(await api.landscape(
                    therapeutic_area=scope["therapeutic_area"],
                    indication=scope["indication"],
                    disease=scope["disease"],
                    db=db,
                ))
            else:  # pragma: no cover
                data = {}
        except Exception as exc:  # noqa: BLE001
            return _err("get_analytics", f"Could not load {kind}.", str(exc))
    summary = f"Loaded {kind}."
    if ignored:
        summary += f" ({', '.join(ignored)} does not apply to this KPI and was ignored.)"
    return _ok(
        "get_analytics", summary,
        {"kind": kind, "result": data, "scope_ignored": ignored},
    )


# ---------------------------------------------------------------------------
# get_insights — themes / trends / signals
# ---------------------------------------------------------------------------
class GetInsightsInput(ToolInput):
    view: str = "themes"
    persona: str | None = None
    theme_id: str | None = None
    top: int = 8


async def get_insights(payload: GetInsightsInput) -> ToolResultData:
    from app.insights import pipeline as ins_pipeline, trends as ins_trends

    view = (payload.view or "themes").strip().lower()
    async with AsyncSessionLocal() as db:
        try:
            if view == "themes":
                data = await ins_trends.theme_overview(db, persona=payload.persona)
            elif view == "trends":
                data = await ins_trends.theme_timeseries(db, top=payload.top, persona=payload.persona)
            elif view == "signals":
                data = await ins_trends.signals(db, persona=payload.persona)
            elif view == "theme_detail":
                if not payload.theme_id:
                    return _err("get_insights", "theme_detail needs a theme_id.", "theme_id required")
                data = await ins_trends.theme_detail(db, payload.theme_id)
            elif view == "status":
                data = await ins_pipeline.status(db)
            else:
                return _err("get_insights", f"Unknown view {view!r}.", "view must be themes|trends|signals|theme_detail|status")
        except Exception as exc:  # noqa: BLE001
            return _err("get_insights", f"Could not load insights {view}.", str(exc))
    return _ok("get_insights", f"Loaded insights: {view}.", {"view": view, "result": data})


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
class ListRunsInput(ToolInput):
    limit: int = 10


async def list_runs(payload: ListRunsInput) -> ToolResultData:
    from app.schemas import RunOut
    from app.services import run_service

    async with AsyncSessionLocal() as db:
        runs = await run_service.list_runs(db, limit=max(1, min(payload.limit, 100)))
        data = [RunOut.model_validate(r).model_dump(mode="json") for r in runs]
    return _ok("list_runs", f"Found {len(data)} run(s).", {"runs": data})


class GetRunInput(ToolInput):
    run_id: str


async def get_run(payload: GetRunInput) -> ToolResultData:
    from app.schemas import RunOut
    from app.services import run_service

    async with AsyncSessionLocal() as db:
        run = await run_service.get_run(db, payload.run_id)
        if run is None:
            return _err("get_run", "Run not found.", f"No run {payload.run_id}")
        out = RunOut.model_validate(run).model_dump(mode="json")
        progress = await run_service.run_progress(db, payload.run_id)
    return _ok("get_run", f"Run {payload.run_id} is {out['status']}.", {"run": out, "progress": progress})


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------
class ListQuestionsInput(ToolInput):
    persona: str | None = None
    therapeutic_area: str | None = None
    brand_focus: str | None = None
    domain: str | None = None
    approval_status: str | None = None
    active: bool | None = None
    limit: int = 25


async def list_questions(payload: ListQuestionsInput) -> ToolResultData:
    from app.schemas import QuestionOut
    from app.services import question_service

    async with AsyncSessionLocal() as db:
        qs = await question_service.list_questions(
            db,
            persona=payload.persona,
            therapeutic_area=payload.therapeutic_area,
            brand_focus=payload.brand_focus,
            domain=payload.domain,
            approval_status=payload.approval_status,
            active=payload.active,
            limit=max(1, min(payload.limit, 200)),
        )
        data = [QuestionOut.model_validate(q).model_dump(mode="json") for q in qs]
    return _ok("list_questions", f"Found {len(data)} question(s).", {"questions": data})


class GetQuestionInput(ToolInput):
    row_id: int


async def get_question(payload: GetQuestionInput) -> ToolResultData:
    from app.schemas import QuestionOut
    from app.services import question_service

    async with AsyncSessionLocal() as db:
        q = await question_service.get_question(db, payload.row_id)
        if q is None:
            return _err("get_question", "Question not found.", f"No question id {payload.row_id}")
        data = QuestionOut.model_validate(q).model_dump(mode="json")
    return _ok("get_question", f"Question {data['question_id']}.", {"question": data})


class CoverageInput(ToolInput):
    pass


async def question_coverage(payload: CoverageInput) -> ToolResultData:
    from app.services import question_service

    async with AsyncSessionLocal() as db:
        data = await question_service.coverage_report(db)
    return _ok(
        "question_coverage",
        f"{data.get('total_active_approved', 0)} active approved questions.",
        data,
    )


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------
class ListResponsesInput(ToolInput):
    llm_name: str | None = None
    persona: str | None = None
    therapeutic_area: str | None = None
    brand_focus: str | None = None
    competitor: str | None = None
    domain: str | None = None
    status: str | None = None
    run_id: str | None = None
    consensus_level: str | None = None
    sentiment_min: float | None = None
    sentiment_max: float | None = None
    alert_only: bool = False
    limit: int = 20


async def list_responses(payload: ListResponsesInput) -> ToolResultData:
    from app.services import response_service

    async with AsyncSessionLocal() as db:
        data = await response_service.query_responses(
            db,
            llm_name=payload.llm_name,
            persona=payload.persona,
            therapeutic_area=payload.therapeutic_area,
            brand_focus=payload.brand_focus,
            competitor=payload.competitor,
            domain=payload.domain,
            status=payload.status,
            run_id=payload.run_id,
            consensus_level=payload.consensus_level,
            sentiment_min=payload.sentiment_min,
            sentiment_max=payload.sentiment_max,
            alert_only=payload.alert_only,
            limit=max(1, min(payload.limit, 100)),
        )
    summary = f"{data.get('count', 0)} of {data.get('total', 0)} responses."
    if payload.competitor:
        summary = (
            f"{data.get('count', 0)} of {data.get('total', 0)} answers NAME "
            f"{payload.competitor}. Each carries competitor_sentiment (theirs) beside "
            f"sentiment_score (our brand's)."
        )
    return _ok("list_responses", summary, data)


class CompareResponsesInput(ToolInput):
    question_id: str
    run_id: str | None = None


async def compare_responses(payload: CompareResponsesInput) -> ToolResultData:
    from app.services import response_service

    async with AsyncSessionLocal() as db:
        data = await response_service.compare_question(db, payload.question_id, payload.run_id)
    n = len(data.get("answers", []))
    return _ok("compare_responses", f"Compared {n} model answer(s).", data)


class GetResponseInput(ToolInput):
    response_id: str


async def get_response(payload: GetResponseInput) -> ToolResultData:
    from app.services import response_service

    async with AsyncSessionLocal() as db:
        data = await response_service.get_response_detail(db, payload.response_id)
        if data is None:
            return _err("get_response", "Response not found.", f"No response {payload.response_id}")
    return _ok("get_response", f"Response {payload.response_id}.", {"response": data})


# ---------------------------------------------------------------------------
# Harvest (read)
# ---------------------------------------------------------------------------
class HarvestStatusInput(ToolInput):
    pass


async def harvest_status(payload: HarvestStatusInput) -> ToolResultData:
    from app.api import harvest as harvest_api

    async with AsyncSessionLocal() as db:
        data = await harvest_api.status(db)
    running = bool(((data or {}).get("harvest") or {}).get("running"))
    return _ok("harvest_status", "Discovery is running." if running else "Discovery is idle.", data)


class ListHarvestedInput(ToolInput):
    status: str | None = None
    persona: str | None = None
    therapeutic_area: str | None = None
    ae_only: bool = False
    limit: int = 25


async def list_harvested(payload: ListHarvestedInput) -> ToolResultData:
    from app.services import harvest_service

    async with AsyncSessionLocal() as db:
        items = await harvest_service.list_items(
            db,
            status=payload.status,
            persona=payload.persona,
            therapeutic_area=payload.therapeutic_area,
            ae_only=payload.ae_only,
            limit=max(1, min(payload.limit, 200)),
        )
    return _ok("list_harvested", f"Found {len(items)} staged item(s).", {"items": items})


# ---------------------------------------------------------------------------
# OpenEvidence (read)
# ---------------------------------------------------------------------------
class OeStatusInput(ToolInput):
    run_id: str | None = None


async def oe_status(payload: OeStatusInput) -> ToolResultData:
    from app.services import openevidence_service as oe

    async with AsyncSessionLocal() as db:
        runs = await oe.list_runs_with_provider(db)
        worklist = await oe.worklist(db, payload.run_id) if payload.run_id else None
    data: dict[str, Any] = {"runs": runs}
    if worklist is not None:
        data["worklist"] = worklist
    return _ok("oe_status", f"{len(runs)} run(s) with Provider questions.", data)


# ---------------------------------------------------------------------------
# Social Listening (read)
# ---------------------------------------------------------------------------
class SocialStatusInput(ToolInput):
    pass


async def social_status(payload: SocialStatusInput) -> ToolResultData:
    from app.api import social as social_api

    async with AsyncSessionLocal() as db:
        data = await social_api.status(db)
    running = bool(((data or {}).get("social") or {}).get("running"))
    configured = bool((data or {}).get("configured"))
    total = int((data or {}).get("total", 0) or 0)
    if running:
        summary = "A social-listening ingest is running."
    elif not configured:
        summary = "Social listening is not configured (no Apify token)."
    else:
        summary = f"Social listening idle; {total} captured post(s)."
    return _ok("social_status", summary, data)


class SocialInsightsInput(ToolInput):
    therapeutic_area: str | None = None


async def social_insights(payload: SocialInsightsInput) -> ToolResultData:
    from app.services import social_service as svc

    ta = (payload.therapeutic_area or "").strip()
    async with AsyncSessionLocal() as db:
        # No area named: Social Listening is per-area, so ask which one instead of
        # defaulting. Use the single captured area if there's exactly one.
        if not ta:
            areas = await svc.available_areas(db)
            if not areas:
                return _ok(
                    "social_insights",
                    "No social data has been captured yet, so there are no areas to summarize.",
                    {"needs_area": False, "available_areas": []},
                )
            if len(areas) == 1:
                ta = areas[0]["therapeutic_area"]
            else:
                names = ", ".join(a["therapeutic_area"] for a in areas)
                return _ok(
                    "social_insights",
                    f"Captured social data spans {len(areas)} therapeutic areas.",
                    {
                        "needs_area": True,
                        "available_areas": areas,
                        # LLM-only steer: the UI tool chip renders `summary` only
                        # (never `data`), so keep the "ask which area" instruction
                        # here where the orchestrator sees it but the user doesn't.
                        "assistant_guidance": (
                            "Social insights are per therapeutic area. Ask the user "
                            f"which area they want — captured areas are: {names}."
                        ),
                    },
                    # Structured picker the chat renders as a dropdown so the user
                    # clicks an area instead of typing it. Selecting one sends the
                    # `send_template` (with {value} substituted) as the next message.
                    prompt_options={
                        "prompt": "Which therapeutic area would you like insights for?",
                        "param": "therapeutic_area",
                        "options": [
                            {
                                "value": a["therapeutic_area"],
                                "label": a["therapeutic_area"],
                                "hint": f"{a['posts']} post{'' if a['posts'] == 1 else 's'}",
                            }
                            for a in areas
                        ],
                        "send_template": "Summarize the social listening insights for {value}.",
                    },
                )
        try:
            data = await svc.insights(db, therapeutic_area=ta)
        except Exception as exc:  # noqa: BLE001
            return _err("social_insights", "Could not load social insights.", str(exc))
    return _ok("social_insights", f"Social insights for {ta}: {data.get('total_posts', 0)} post(s).", data)


class ListSocialPostsInput(ToolInput):
    therapeutic_area: str | None = None
    channel: str | None = None
    brand_focus: str | None = None
    ae_only: bool = False
    limit: int = 25


async def list_social_posts(payload: ListSocialPostsInput) -> ToolResultData:
    from app.services import social_service as svc

    async with AsyncSessionLocal() as db:
        posts = await svc.list_posts(
            db,
            therapeutic_area=payload.therapeutic_area,
            channel=payload.channel,
            brand_focus=payload.brand_focus,
            ae_only=payload.ae_only,
            limit=max(1, min(payload.limit, 200)),
        )
    return _ok("list_social_posts", f"Found {len(posts)} social post(s).", {"posts": posts})


class ListSocialCommentsInput(ToolInput):
    therapeutic_area: str | None = None
    channel: str | None = None
    ae_only: bool = False
    post_id: int | None = None
    limit: int = 25


async def list_social_comments(payload: ListSocialCommentsInput) -> ToolResultData:
    from app.services import social_service as svc

    async with AsyncSessionLocal() as db:
        comments = await svc.query_comments(
            db,
            therapeutic_area=payload.therapeutic_area,
            channel=payload.channel,
            ae_only=payload.ae_only,
            post_id=payload.post_id,
            limit=max(1, min(payload.limit, 200)),
        )
    return _ok("list_social_comments", f"Found {len(comments)} social comment(s).", {"comments": comments})


# ---------------------------------------------------------------------------
# Source Authority (read) — which web domains the models cite, classified
# ---------------------------------------------------------------------------
_SOURCE_AUTHORITY_VIEWS = {
    "distribution", "top_domains", "coverage", "share_of_voice", "pages",
    "trends", "domain", "sentiment_correlation", "provenance", "preferred",
    "preferred_observations", "influence_graph", "influence_node_evidence",
}


class GetSourceAuthorityInput(ToolInput):
    view: str = "distribution"
    llm_name: str | None = None
    therapeutic_area: str | None = None
    indication: str | None = None
    brand: str | None = None
    persona: str | None = None
    control: str | None = None          # pages: ABBVIE | COMPETITOR | INDEPENDENT | UNKNOWN
    authority_domain: str | None = None  # required for the "domain" view
    response_id: str | None = None       # required for the "provenance" view
    theme: str | None = None             # influence_graph: focus on one narrative
    focus_domain: str | None = None      # influence_graph: focus on one domain's subgraph
    node_type: str | None = None         # influence_node_evidence: theme | position
    key: str | None = None               # influence_node_evidence: the node's label/value
    top_n: int = 60
    limit: int = 25


async def get_source_authority(payload: GetSourceAuthorityInput) -> ToolResultData:
    from app.source_authority import service as svc

    view = (payload.view or "distribution").strip().lower()
    if view not in _SOURCE_AUTHORITY_VIEWS:
        return _err("get_source_authority", f"Unknown view {view!r}.",
                    f"view must be one of: {sorted(_SOURCE_AUTHORITY_VIEWS)}")
    f = dict(llm_name=payload.llm_name, therapeutic_area=payload.therapeutic_area,
             indication=payload.indication, brand=payload.brand, persona=payload.persona)
    async with AsyncSessionLocal() as db:
        try:
            if view == "distribution":
                data = await svc.distribution(db, **f)
            elif view == "top_domains":
                data = await svc.top_domains(db, **f, limit=max(1, min(payload.limit, 100)))
            elif view == "coverage":
                data = await svc.coverage(db, **f)
            elif view == "share_of_voice":
                data = await svc.share_of_voice(db, **f)
            elif view == "pages":
                data = await svc.top_pages(db, **f, control=payload.control, limit=max(1, min(payload.limit, 200)))
            elif view == "trends":
                data = await svc.citation_trends(db, **f)
            elif view == "domain":
                if not payload.authority_domain:
                    return _err("get_source_authority", "domain needs an authority_domain.", "authority_domain is required")
                data = await svc.domain_detail(db, authority_domain=payload.authority_domain, **f, limit=max(1, min(payload.limit, 200)))
            elif view == "sentiment_correlation":
                data = await svc.sentiment_by_source(db, **f)
            elif view == "provenance":
                if not payload.response_id:
                    return _err("get_source_authority", "provenance needs a response_id.", "response_id is required")
                data = await svc.response_provenance(db, payload.response_id)
            elif view == "preferred":
                data = {"items": await svc.list_preferred(db, therapeutic_area=payload.therapeutic_area)}
            elif view == "preferred_observations":
                data = await svc.preferred_observations(db, therapeutic_area=payload.therapeutic_area, llm_name=payload.llm_name)
            elif view == "influence_graph":
                data = await svc.influence_graph(
                    db, **f, theme=payload.theme, focus_domain=payload.focus_domain,
                    top_n=max(10, min(payload.top_n, 200)),
                )
            elif view == "influence_node_evidence":
                node_type = (payload.node_type or "").strip().lower()
                if node_type not in {"theme", "position"}:
                    return _err("get_source_authority", "influence_node_evidence needs a node_type.", "node_type must be theme or position")
                if not payload.key:
                    return _err("get_source_authority", "influence_node_evidence needs a key.", "key is required (the theme label, or the competitive_position value)")
                data = await svc.node_evidence(
                    db, node_type=node_type, key=payload.key, **f,
                    limit=max(1, min(payload.limit, 200)),
                )
            else:  # pragma: no cover
                data = {}
        except Exception as exc:  # noqa: BLE001
            return _err("get_source_authority", f"Could not load Source Authority {view}.", str(exc))
    return _ok("get_source_authority", f"Loaded Source Authority: {view}.", {"view": view, "result": data})


# ---------------------------------------------------------------------------
# GEO Interventions / Recommendations (read)
# ---------------------------------------------------------------------------
_RECOMMENDATION_VIEWS = {
    "list", "citation_opportunities", "share_of_citation", "preferred_source_gaps",
    "query_fanouts", "citation_trend", "reviews", "content_types",
}


class GetRecommendationsInput(ToolInput):
    view: str = "list"
    persona: str | None = None
    therapeutic_area: str | None = None
    model: str | None = None  # llm_name filter
    batch_id: str | None = None
    limit: int = 20


async def get_recommendations(payload: GetRecommendationsInput) -> ToolResultData:
    from app.services import recommendation_service as svc

    view = (payload.view or "list").strip().lower()
    if view not in _RECOMMENDATION_VIEWS:
        return _err("get_recommendations", f"Unknown view {view!r}.",
                    f"view must be one of: {sorted(_RECOMMENDATION_VIEWS)}")
    llm = payload.model
    ta = payload.therapeutic_area
    persona = payload.persona
    limit = max(1, min(payload.limit, 100))
    async with AsyncSessionLocal() as db:
        try:
            if view == "list":
                data = await svc.list_recommendations(db, persona=persona, therapeutic_area=ta, llm_name=llm, batch_id=payload.batch_id)
            elif view == "citation_opportunities":
                data = await svc.citation_opportunities(db, persona=persona, therapeutic_area=ta, llm_name=llm, limit=limit)
            elif view == "share_of_citation":
                data = await svc.share_of_citation(db, persona=persona, therapeutic_area=ta, llm_name=llm)
            elif view == "preferred_source_gaps":
                data = await svc.preferred_source_gaps(db, therapeutic_area=ta, llm_name=llm)
            elif view == "query_fanouts":
                data = await svc.query_fanouts(db, persona=persona, therapeutic_area=ta, llm_name=llm, limit=limit)
            elif view == "citation_trend":
                data = await svc.citation_trend(db, persona=persona, therapeutic_area=ta, llm_name=llm)
            elif view == "reviews":
                data = await svc.list_reviews(db, batch_id=payload.batch_id)
            elif view == "content_types":
                from app.remediation import semrush
                from app.remediation.prompts import APPROVED_CONTENT_TYPES
                data = {"content_types": list(APPROVED_CONTENT_TYPES), "semrush_configured": semrush.is_configured()}
            else:  # pragma: no cover
                data = {}
        except Exception as exc:  # noqa: BLE001
            return _err("get_recommendations", f"Could not load GEO interventions {view}.", str(exc))
    return _ok("get_recommendations", f"Loaded GEO interventions: {view}.", {"view": view, "result": data})


# ---------------------------------------------------------------------------
# Prompt Volume Intelligence (read)
# ---------------------------------------------------------------------------
_PROMPT_VOLUME_VIEWS = {
    "intelligence", "gaps", "gap_alerts", "gap_alerts_summary", "trend",
    "batches", "prioritized",
}


class GetPromptVolumeInput(ToolInput):
    view: str = "intelligence"
    batch_id: str | None = None
    status: str = "OPEN"  # gap_alerts: OPEN | RESOLVED | DISMISSED | ALL


async def get_prompt_volume(payload: GetPromptVolumeInput) -> ToolResultData:
    from app.services import prompt_volume_service as svc

    view = (payload.view or "intelligence").strip().lower()
    if view not in _PROMPT_VOLUME_VIEWS:
        return _err("get_prompt_volume", f"Unknown view {view!r}.",
                    f"view must be one of: {sorted(_PROMPT_VOLUME_VIEWS)}")
    async with AsyncSessionLocal() as db:
        try:
            if view == "intelligence":
                data = await svc.intelligence(db, batch_id=payload.batch_id)
            elif view == "gaps":
                data = await svc.gap_topics(db, batch_id=payload.batch_id)
            elif view == "gap_alerts":
                data = await svc.list_gap_alerts(db, status=payload.status or "OPEN")
            elif view == "gap_alerts_summary":
                data = await svc.gap_alert_summary(db)
            elif view == "trend":
                data = await svc.demand_trend(db)
            elif view == "batches":
                data = await svc.list_batches(db)
            elif view == "prioritized":
                data = await svc.prioritized_questions(db, batch_id=payload.batch_id)
            else:  # pragma: no cover
                data = {}
        except Exception as exc:  # noqa: BLE001
            return _err("get_prompt_volume", f"Could not load Prompt Volume {view}.", str(exc))
    return _ok("get_prompt_volume", f"Loaded Prompt Volume: {view}.", {"view": view, "result": data})


# ---------------------------------------------------------------------------
# Workshop AI Answer Insights + Stakeholder Digests (read)
# ---------------------------------------------------------------------------
class GetWorkshopInsightsInput(ToolInput):
    scope: str = "workshop"  # workshop | all


async def get_workshop_insights(payload: GetWorkshopInsightsInput) -> ToolResultData:
    from app.services import digest_service as svc

    scope = (payload.scope or "workshop").strip().lower()
    if scope not in ("workshop", "all"):
        return _err("get_workshop_insights", f"Unknown scope {scope!r}.", "scope must be workshop|all")
    async with AsyncSessionLocal() as db:
        try:
            data = await svc.workshop_insights(db, scope=scope)
        except Exception as exc:  # noqa: BLE001
            return _err("get_workshop_insights", "Could not load workshop insights.", str(exc))
    if not data:
        return _ok("get_workshop_insights",
                   f"No AI answer insights are available for the {scope} scope in this environment.",
                   {"scope": scope, "available": False, "insights": None})
    return _ok("get_workshop_insights", f"Loaded AI answer insights ({scope} scope).",
               {"scope": scope, "available": True, "insights": data})


class GetDigestsInput(ToolInput):
    view: str = "profiles"  # profiles | runs | run
    profile_id: int | None = None
    run_id: int | None = None


async def get_digests(payload: GetDigestsInput) -> ToolResultData:
    from app.schemas import DigestProfileOut, DigestRunOut
    from app.services import digest_service as svc

    view = (payload.view or "profiles").strip().lower()
    if view not in ("profiles", "runs", "run"):
        return _err("get_digests", f"Unknown view {view!r}.", "view must be profiles|runs|run")
    async with AsyncSessionLocal() as db:
        try:
            if view == "profiles":
                rows = await svc.list_profiles(db)
                data = {"profiles": [DigestProfileOut.model_validate(p).model_dump(mode="json") for p in rows]}
                summary = f"Found {len(rows)} digest profile(s)."
            elif view == "runs":
                rows = await svc.list_runs(db, profile_id=payload.profile_id)
                data = {"runs": [DigestRunOut.model_validate(r).model_dump(mode="json") for r in rows]}
                summary = f"Found {len(rows)} digest run(s)."
            else:  # run
                if payload.run_id is None:
                    return _err("get_digests", "run needs a run_id.", "run_id is required")
                run = await svc.get_run(db, payload.run_id)
                if run is None:
                    return _err("get_digests", "Digest run not found.", f"No digest run {payload.run_id}")
                data = {"run": DigestRunOut.model_validate(run).model_dump(mode="json")}
                summary = f"Digest run {payload.run_id}."
        except Exception as exc:  # noqa: BLE001
            return _err("get_digests", f"Could not load digests {view}.", str(exc))
    return _ok("get_digests", summary, {"view": view, **data})


# ---------------------------------------------------------------------------
# Model Releases / AI Update Impact + Question Variations (read)
# ---------------------------------------------------------------------------
_MODEL_RELEASE_VIEWS = {
    "list", "drifts", "drift_detail", "drift_timeline", "version_impact",
    "high_impact", "correlation_ratio", "versions", "sync_status",
}


class GetModelReleasesInput(ToolInput):
    view: str = "list"
    target_platform: str | None = None
    diff_id: int | None = None  # required for drift_detail
    limit: int = 50


async def get_model_releases(payload: GetModelReleasesInput) -> ToolResultData:
    from app.services import model_release_service as svc

    view = (payload.view or "list").strip().lower()
    if view not in _MODEL_RELEASE_VIEWS:
        return _err("get_model_releases", f"Unknown view {view!r}.",
                    f"view must be one of: {sorted(_MODEL_RELEASE_VIEWS)}")
    tp = payload.target_platform
    limit = max(1, min(payload.limit, 500))
    async with AsyncSessionLocal() as db:
        try:
            if view == "list":
                from app.schemas import ModelReleaseOut
                rows = await svc.list_releases(db, target_platform=tp)
                data = {"releases": [ModelReleaseOut.model_validate(r).model_dump(mode="json") for r in rows]}
            elif view == "drifts":
                data = {"drifts": await svc.list_drifts(db, target_platform=tp, limit=limit)}
            elif view == "drift_detail":
                if payload.diff_id is None:
                    return _err("get_model_releases", "drift_detail needs a diff_id.", "diff_id is required")
                detail = await svc.get_drift_detail(db, payload.diff_id)
                if detail is None:
                    return _err("get_model_releases", "Drift not found.", f"No drift {payload.diff_id}")
                data = {"drift": detail}
            elif view == "drift_timeline":
                data = await svc.drift_timeline(db, target_platform=tp)
            elif view == "version_impact":
                data = {"items": await svc.version_impact(db, target_platform=tp)}
            elif view == "high_impact":
                data = {"items": await svc.high_impact_updates(db, target_platform=tp)}
            elif view == "correlation_ratio":
                data = await svc.correlation_ratio(db)
            elif view == "versions":
                from app.model_updates.versions import list_current_versions
                data = {"versions": await list_current_versions(db)}
            elif view == "sync_status":
                from app.model_updates import sync_status
                data = sync_status()
            else:  # pragma: no cover
                data = {}
        except Exception as exc:  # noqa: BLE001
            return _err("get_model_releases", f"Could not load AI update impact {view}.", str(exc))
    return _ok("get_model_releases", f"Loaded AI update impact: {view}.", {"view": view, "result": data})


# ---------------------------------------------------------------------------
# Competitors — the two questions a rival's name can ask
# ---------------------------------------------------------------------------
class GetCompetitorMentionsInput(ToolInput):
    agent: str | None = None
    therapeutic_area: str | None = None
    indication: str | None = None
    disease: str | None = None
    brand: str | None = None
    persona: str | None = None
    llm_name: str | None = None
    run_id: str | None = None
    monitoring_mode: str | None = None
    side: str | None = None
    limit: int = 25


async def get_competitor_mentions(payload: GetCompetitorMentionsInput) -> ToolResultData:
    """How an agent is talked about across EVERY scored answer, not just comparisons."""
    from app.competitive import mentions as svc

    scope = {
        "therapeutic_area": payload.therapeutic_area or None,
        "indication": payload.indication or None,
        "disease": payload.disease or None,
        "brand": payload.brand or None,
        "persona": payload.persona or None,
        "llm_name": payload.llm_name or None,
        "run_id": payload.run_id or None,
        "monitoring_mode": payload.monitoring_mode or None,
    }
    agent = (payload.agent or "").strip()
    async with AsyncSessionLocal() as db:
        try:
            if agent:
                data = await svc.agent_detail(db, agent, **scope)
            else:
                data = await svc.rollup(
                    db, side=payload.side or None,
                    limit=max(1, min(payload.limit, 200)), **scope,
                )
        except Exception as exc:  # noqa: BLE001
            return _err("get_competitor_mentions", "Could not read competitor mentions.", str(exc))

    # The denominator these shares are taken over comes from the application database, while
    # several KPIs are warehouse-served. Carried into the summary line, not just the payload,
    # because the summary is what the model actually narrates back.
    store_caveat = ""
    if data.get("corpus", {}).get("warehouse_enabled"):
        store_caveat = (
            " Counted in the application database only — do not compare this denominator "
            "with warehouse-served KPIs like sentiment_distribution."
        )

    if agent:
        if not data.get("found"):
            # An honest empty: nobody named them, which is a finding about the answers.
            return _ok(
                "get_competitor_mentions",
                data.get("note", "Not mentioned.") + store_caveat,
                data,
            )
        s = data["summary"]
        avg = s.get("avg_sentiment")
        avg_text = "no sentiment recorded" if avg is None else f"average sentiment {avg}"
        summary = (
            f"{data['agent']} ({s['side'].lower()}) is named in {s['answers_naming_it']} of "
            f"{data['answers_scored']} scored answers "
            f"({s['share_of_scored_answers_pct']}%) — {avg_text} over {s['sentiment_n']} "
            f"scored mention(s). Silence is not counted as neutral." + store_caveat
        )
        return _ok("get_competitor_mentions", summary, data)

    named = data.get("agents_total", 0)
    return _ok(
        "get_competitor_mentions",
        f"{named} agent(s) named across {data.get('answers_scored', 0)} scored answers."
        + store_caveat,
        data,
    )


class GetHeadToHeadInput(ToolInput):
    view: str = "board"  # board | detail
    pair_key: str | None = None
    # Every dimension on the board is multi-select, so each accepts one value or a list:
    # "how do we do against Tremfya and Stelara" is one board, not two.
    therapeutic_area: str | list[str] | None = None
    disease: str | list[str] | None = None
    brand: str | list[str] | None = None
    competitor: str | list[str] | None = None
    persona: str | list[str] | None = None
    llm_name: str | list[str] | None = None
    verdict: str | list[str] | None = None
    limit: int = 20


async def get_head_to_head(payload: GetHeadToHeadInput) -> ToolResultData:
    """Who wins the us-vs-them comparisons: ranked board, or one comparison in full."""
    from app.competitive import head_to_head as svc

    view = (payload.view or "board").strip().lower()
    if view not in ("board", "detail"):
        return _err("get_head_to_head", f"Unknown view {view!r}.", "view must be board|detail")
    async with AsyncSessionLocal() as db:
        try:
            if view == "detail":
                if not payload.pair_key:
                    return _err(
                        "get_head_to_head",
                        "detail needs a pair_key from the board.",
                        "pair_key is required",
                    )
                data = await svc.pair_detail(
                    db, payload.pair_key,
                    personas=payload.persona, llm_names=payload.llm_name,
                )
                if data is None:
                    return _err(
                        "get_head_to_head", "That comparison has no answers on the board.",
                        f"No answers for {payload.pair_key}",
                    )
            else:
                data = await svc.scoreboard(
                    db,
                    therapeutic_areas=payload.therapeutic_area or None,
                    diseases=payload.disease or None,
                    brands=payload.brand or None,
                    competitors=payload.competitor or None,
                    personas=payload.persona or None,
                    llm_names=payload.llm_name or None,
                    verdicts=payload.verdict or None,
                    limit=max(1, min(payload.limit, 200)),
                )
        except Exception as exc:  # noqa: BLE001
            return _err("get_head_to_head", "Could not load the head-to-head board.", str(exc))

    if view == "detail":
        s = data["summary"]
        summary = (
            f"{s['brand']} vs {s['competitor']}: {s['verdict']} across {s['answers']} "
            f"answer(s), losing {s['losing_answers']}."
        )
        return _ok("get_head_to_head", summary, data)

    total = data.get("pairs_total", 0)
    if not total:
        note = (
            "No head-to-head comparisons resolved in this scope. The board only reads "
            "answers to Comparative questions; use get_competitor_mentions to see how a "
            "rival is talked about in every other answer."
        )
        return _ok("get_head_to_head", note, data)
    top = data["pairs"][0]
    summary = (
        f"{total} comparison(s) on the board from {data.get('answers_on_the_board', 0)} of "
        f"{data.get('answers_examined', 0)} answers examined. Worst: {top['brand']} vs "
        f"{top['competitor']} — {top['verdict']}, losing {top['losing_answers']} of "
        f"{top['answers']}."
    )
    return _ok("get_head_to_head", summary, data)


class GetVariationsInput(ToolInput):
    view: str = "groups"  # groups | group | group_results
    group_id: str | None = None
    run_id: str | None = None


async def get_variations(payload: GetVariationsInput) -> ToolResultData:
    from app.services import variation_service as svc

    view = (payload.view or "groups").strip().lower()
    if view not in ("groups", "group", "group_results"):
        return _err("get_variations", f"Unknown view {view!r}.", "view must be groups|group|group_results")
    async with AsyncSessionLocal() as db:
        try:
            if view == "groups":
                data = await svc.list_groups(db)
            elif view == "group":
                if not payload.group_id:
                    return _err("get_variations", "group needs a group_id.", "group_id is required")
                data = await svc.list_group(db, payload.group_id)
            else:  # group_results
                if not payload.group_id:
                    return _err("get_variations", "group_results needs a group_id.", "group_id is required")
                data = await svc.group_results(db, payload.group_id, run_id=payload.run_id)
        except Exception as exc:  # noqa: BLE001
            return _err("get_variations", f"Could not load variations {view}.", str(exc))
    return _ok("get_variations", f"Loaded question variations: {view}.", {"view": view, "result": data})


# ---------------------------------------------------------------------------
# Schedule (read)
# ---------------------------------------------------------------------------
class ScheduleStatusInput(ToolInput):
    pass


async def schedule_status(payload: ScheduleStatusInput) -> ToolResultData:
    from app.schemas import ScheduleOut
    from app.services import schedule_service

    async with AsyncSessionLocal() as db:
        row = await schedule_service.get_or_create(db)
        data = ScheduleOut.model_validate(row).model_dump(mode="json")
    state = "enabled" if data.get("enabled") else "disabled"
    return _ok("schedule_status", f"Daily run is {state} (cron {data.get('cron')}).", data)


SPECS: list[ToolSpec] = [
    ToolSpec("query_data", "Answer a free-form natural-language question about the monitoring data (brand sentiment, positioning, consensus, alerts, run cost/volume) using the warehouse. Use for open-ended 'which/why/how many' data questions.", QueryDataInput, query_data),
    ToolSpec("get_analytics", "Get a specific dashboard analytics bundle. kind = sentiment_distribution | positioning | volume | alerts_summary | consensus_summary | intent_distribution | llm_comparison | worst_questions | run_summary (needs run_id) | persona_summary (optional persona) | landscape (disease-state multi-competitor matrix: share of voice, mean sentiment and position mix for EVERY agent named across brand-less answers). Optional scope for the scoped kinds: therapeutic_area, indication, disease, brand. NOTE: sentiment_distribution and positioning describe OUR focus brand only — for a competitor's own sentiment use get_competitor_mentions.", GetAnalyticsInput, get_analytics),
    ToolSpec("get_insights", "Get advanced insights. view = themes | trends | signals | theme_detail (needs theme_id) | status.", GetInsightsInput, get_insights),
    ToolSpec("list_runs", "List recent monitoring runs (most recent first) with status and counts.", ListRunsInput, list_runs),
    ToolSpec("get_run", "Get one run's full status plus live per-model progress.", GetRunInput, get_run),
    ToolSpec("list_questions", "List questions in the bank, filterable by persona, therapeutic_area, brand_focus, domain, approval_status, active.", ListQuestionsInput, list_questions),
    ToolSpec("get_question", "Get a single question by its numeric row id.", GetQuestionInput, get_question),
    ToolSpec("question_coverage", "Report question-bank coverage gaps by persona, therapeutic area, and domain.", CoverageInput, question_coverage),
    ToolSpec("list_responses", "Search scored AI responses with filters (llm_name, persona, therapeutic_area, brand_focus, competitor, domain, status, run_id, consensus_level, sentiment range, alert_only). brand_focus is the monitored AbbVie brand an answer is ABOUT; competitor finds answers that NAME any other agent (alias-aware, so 'guselkumab' finds Tremfya) and tags each row with that agent's own competitor_sentiment. Never put a rival's name in brand_focus — it can only ever return zero rows.", ListResponsesInput, list_responses),
    ToolSpec("get_competitor_mentions", "How an agent is actually talked about across EVERY scored answer — the right tool for 'what is the sentiment for <competitor>'. Pass agent (brand, generic or alias) for one drug's rollup: share of answers naming it, mean sentiment with its mention count, sentiment mix, position mix, breakdowns by model/persona/therapeutic area/our brand, and sample answers. Omit agent to rank every agent the models named; optional side = OURS | COMPETITOR | UNTRACKED (UNTRACKED surfaces rivals missing from brands.yaml). Optional scope: therapeutic_area, indication, disease, brand, persona, llm_name, run_id, monitoring_mode. An agent is scored only in answers that NAMED it — silence is never averaged in as neutral.", GetCompetitorMentionsInput, get_competitor_mentions),
    ToolSpec("get_head_to_head", "The us-vs-them scoreboard: for each brand-vs-competitor comparison, who the AI models favour. view = board (ranked worst-exposure first) | detail (one comparison in full — claims, cited sources, absence gaps, sample answers; needs pair_key from the board). Every board filter — therapeutic_area, disease, brand, competitor, persona, llm_name, verdict (WINNING/EVEN/LOSING) — takes one value OR a list, so 'against Tremfya and Stelara' is a single call. Reads ONLY answers to Comparative questions, so a rival can be absent here yet heavily discussed elsewhere — use get_competitor_mentions for that.", GetHeadToHeadInput, get_head_to_head),
    ToolSpec("compare_responses", "Compare every model's latest answer to one question side by side.", CompareResponsesInput, compare_responses),
    ToolSpec("get_response", "Get one response's full detail (text, score, alerts, consensus, diff).", GetResponseInput, get_response),
    ToolSpec("harvest_status", "Get the current question-discovery (harvest) status and counts.", HarvestStatusInput, harvest_status),
    ToolSpec("list_harvested", "List staged harvested (discovered) questions awaiting review, filterable by status/persona/therapeutic_area/ae_only.", ListHarvestedInput, list_harvested),
    ToolSpec("oe_status", "Show runs with pending Provider (OpenEvidence) questions; pass run_id for that run's worklist.", OeStatusInput, oe_status),
    ToolSpec("social_status", "Get the Social Listening status (whether Apify is configured, whether an ingest is running, and how many social posts are captured).", SocialStatusInput, social_status),
    ToolSpec("social_insights", "Get the Social Listening dashboard for ONE therapeutic area: share of voice by brand/channel, POST sentiment, COMMENT sentiment (a separate dimension: comment_sentiment_overall + comment_sentiment_by_channel), volume over time, top topics, adverse-event signals (with a posts-vs-comments breakdown), per-channel engagement leaders, and platform_comparison (per-platform AbbVie vs each competitor brand + abbvie_present). Insights are per-area; if the user does NOT name an area, call this with no therapeutic_area — the tool returns the captured areas so you can ask which one they mean (it auto-uses the area when only one exists). This is a captured social sample, not market-level share of voice.", SocialInsightsInput, social_insights),
    ToolSpec("list_social_posts", "List captured social posts. Defaults to ALL therapeutic areas; pass therapeutic_area to scope to one, plus optional channel (reddit/tiktok/instagram/facebook/x/myrateam/bezzy), brand_focus, and ae_only. Each post includes its average comment sentiment (comment_sentiment), comments_captured, and translation fields (language, is_translated) when the original was non-English.", ListSocialPostsInput, list_social_posts),
    ToolSpec("list_social_comments", "List captured social COMMENTS/replies (the crowd's reaction, scored separately from posts). Defaults to ALL therapeutic areas; pass therapeutic_area to scope to one, plus optional channel, ae_only (adverse-event comments only), and post_id. Non-English comments include language + an English translation.", ListSocialCommentsInput, list_social_comments),
    ToolSpec("schedule_status", "Get the current daily-run schedule (enabled, cron, timezone, next run).", ScheduleStatusInput, schedule_status),
    ToolSpec("get_source_authority", "Read the Source Authority dashboard (which web domains the AI models cite, classified as AbbVie/competitor/independent) and the Influence Graph. view = distribution | top_domains | coverage | share_of_voice | pages | trends | domain (needs authority_domain) | sentiment_correlation | provenance (needs response_id) | preferred | preferred_observations | influence_graph (the corpus-wide source -> claim -> theme -> brand-position web, with the sources driving each narrative; optional theme, focus_domain, top_n) | influence_node_evidence (the real answers behind one narrative or position node; needs node_type=theme|position and key). Optional filters: llm_name, therapeutic_area, indication, brand, persona, control (ABBVIE|COMPETITOR|INDEPENDENT|UNKNOWN, for pages), limit.", GetSourceAuthorityInput, get_source_authority),
    ToolSpec("get_recommendations", "Read GEO Intervention recommendations and citation-strategy analytics (ranked content actions for weak brand positions; strategic suggestions only, not MLR-approved). view = list | citation_opportunities | share_of_citation | preferred_source_gaps | query_fanouts | citation_trend | reviews | content_types. Optional filters: persona, therapeutic_area, model (llm_name), batch_id, limit. To CREATE a new batch use generate_recommendations (billed, confirmed).", GetRecommendationsInput, get_recommendations),
    ToolSpec("get_prompt_volume", "Read AI Prompt Volume Intelligence (uploaded third-party search-demand data as a proxy for AI-inquiry demand). view = intelligence (volume by TA/competitor) | gaps (high-volume topics missing from the question bank) | gap_alerts (status = OPEN|RESOLVED|DISMISSED|ALL) | gap_alerts_summary | trend | batches (upload history) | prioritized (bank questions ranked by demand). Optional: batch_id, status.", GetPromptVolumeInput, get_prompt_volume),
    ToolSpec("get_workshop_insights", "Get the 'AI Answer Insights' snapshot the Stakeholder Digests render: how AI positions the brands by designation, per-platform summaries + provenance sources, needs-attention callouts, and citation share of voice. scope = workshop (curated Workshop Questions set) | all (every tracked question).", GetWorkshopInsightsInput, get_workshop_insights),
    ToolSpec("get_digests", "Read the Stakeholder Digest configuration + history (role-specific intelligence digests). view = profiles (role/cadence/recipients) | runs (past generated digests; optional profile_id) | run (one run's metadata; needs run_id). HTML/PDF bodies are not returned here.", GetDigestsInput, get_digests),
    ToolSpec("get_model_releases", "Read the AI Update Impact surface (detected model updates auto-correlated with response drift). view = list (releases) | drifts (material answer changes; optional target_platform, limit) | drift_detail (needs diff_id) | drift_timeline | version_impact | high_impact | correlation_ratio | versions (current live vendor version per model) | sync_status. Optional: target_platform.", GetModelReleasesInput, get_model_releases),
    ToolSpec("get_variations", "Read Question Variation testing (phrasing-robustness groups: a base question + approved paraphrases run together to compare answers). view = groups (all variation groups) | group (needs group_id) | group_results (variation x model matrix + divergence; needs group_id, optional run_id).", GetVariationsInput, get_variations),
]
