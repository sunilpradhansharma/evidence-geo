"""Router node — classify the user's turn into ACTION / DATA / HELP / OFF_TOPIC.

Keyword fast-path only (no LLM call) so routing is instant and free:
  * greetings / thanks / chit-chat            -> OFF_TOPIC (analyst, templated)
  * "how do I ...", "what can you do", etc.   -> HELP    (analyst, help corpus)
  * contains an action verb                    -> ACTION  (orchestrator, tools)
  * everything else                            -> DATA    (orchestrator, tools)

ACTION vs DATA both go to the Orchestrator (which forces a tool on the first
pass); the distinction is only used for display + audit.
"""
from __future__ import annotations

import re
from typing import Any

from app.copilot.state import AgentState, IntentEnum

_GREETING = re.compile(
    r"^\s*(hi|hey|hello|yo|sup|good (morning|afternoon|evening)|thanks|thank you|ty|thx|ok|okay|cool|nice|great)\b",
    re.IGNORECASE,
)
_HELP = re.compile(
    r"\b(how (do|can|would) (i|we|you)|how to|how does (this|the app|it)|"
    r"what can (you|this app|the app|it) do|what do you do|help me (use|with)|"
    r"guide me|explain how|where (do|can) i|where is|how is this|"
    r"what is this (app|tool|page)|getting started|get started)\b",
    re.IGNORECASE,
)
_ACTION = re.compile(
    r"\b(start|run|launch|kick off|cancel|stop|create|add|edit|update|approve|"
    r"reject|delete|remove|promote|discover|harvest|ingest|schedule|enable|disable|"
    r"rebuild|sync|export|override|rescore|re-score|sweep|capture|finalize|"
    r"navigate|open|go to|take me)\b",
    re.IGNORECASE,
)


def _last_user_text(messages: list[Any]) -> str:
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            return str(m.get("content", ""))
    return ""


def router_node(state: AgentState) -> dict[str, Any]:
    text = _last_user_text(state.get("messages") or []).strip()
    if not text:
        intent = IntentEnum.OFF_TOPIC
    elif _HELP.search(text):
        intent = IntentEnum.HELP
    elif _GREETING.match(text) and len(text) <= 40:
        intent = IntentEnum.OFF_TOPIC
    elif _ACTION.search(text):
        intent = IntentEnum.ACTION
    else:
        intent = IntentEnum.DATA
    return {"intent": intent, "fast_path_hit": True}


def route_after_router(state: AgentState) -> str:
    intent = state.get("intent")
    if intent in (IntentEnum.HELP, IntentEnum.OFF_TOPIC):
        return "analyst"
    return "orchestrator"
