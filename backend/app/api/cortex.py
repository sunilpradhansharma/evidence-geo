"""Snowflake + Cortex API.

- GET  /snowflake/status   connectivity + per-table mirror watermarks.
- POST /snowflake/sync     trigger an incremental mirror pass (background).
- GET  /cortex/insights    Cortex-generated insights (sentiment rollups + exec summary).
- POST /cortex/ask         natural-language Q&A over the mirrored data.
- POST /cortex/chat        conversational "Cortex Agent" chat (plain-English answers).
"""
from fastapi import APIRouter, BackgroundTasks, Query
from pydantic import BaseModel

from app.snowflake import agent, analyst, client, cortex, mirror

router = APIRouter(tags=["snowflake"])


class AskBody(BaseModel):
    question: str


class ChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatBody(BaseModel):
    message: str
    history: list[ChatMessage] = []


@router.get("/snowflake/status")
async def snowflake_status():
    info = await client.ping()
    info["sync_state"] = await mirror.sync_state()
    return info


@router.post("/snowflake/sync", status_code=202)
async def snowflake_sync(background_tasks: BackgroundTasks):
    if not client.is_enabled():
        return {"status": "disabled"}
    background_tasks.add_task(mirror.run_mirror_safe)
    return {"status": "started"}


@router.get("/cortex/insights")
async def cortex_insights(force: bool = Query(False)):
    return await cortex.insights(force=force)


@router.post("/cortex/ask")
async def cortex_ask(body: AskBody):
    return await analyst.ask(body.question)


@router.get("/cortex/agent/status")
async def cortex_agent_status():
    """Whether the conversational Cortex Agent chat widget is available."""
    return {"enabled": agent.is_enabled()}


@router.post("/cortex/chat")
async def cortex_chat(body: ChatBody):
    """Conversational, plain-English Q&A for the global Cortex Agent chat widget."""
    history = [m.model_dump() for m in body.history]
    return await agent.chat(body.message, history)
