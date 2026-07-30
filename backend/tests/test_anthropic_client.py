"""Anthropic provider adapter tests (direct Messages API + web_search tool).

Uses a fake Anthropic client (no network) to verify response parsing: answer text, token
accounting, finish-reason mapping, web-search citation extraction into
ProviderResult.sources / grounding_supports / search_queries, source de-duplication,
pause_turn continuation, and the shared error taxonomy. This is the OPTIONAL citation-capable
path for the monitored `claude` target; the Bedrock path is unaffected.
"""
import asyncio
from types import SimpleNamespace

from app.providers.anthropic_client import AnthropicClient, _extract_output
from app.providers.base import AuthError, ModelParams, SafetyBlocked


# --- fakes -----------------------------------------------------------------------
def _usage(i=10, o=20):
    return SimpleNamespace(input_tokens=i, output_tokens=o)


def _resp(content, *, stop_reason="end_turn", model="claude-sonnet-4-5-20250929", usage=None):
    return SimpleNamespace(
        content=content, stop_reason=stop_reason, model=model, usage=usage or _usage()
    )


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeAnthropic:
    def __init__(self, *responses):
        self.messages = _FakeMessages(list(responses))


def _client_with(*responses, api_key="sk-ant-test"):
    c = AnthropicClient()
    c._settings = SimpleNamespace(
        anthropic_api_key=api_key, anthropic_base_url="", anthropic_web_search_max_uses=5
    )
    c._client = _FakeAnthropic(*responses)  # bypass real SDK/auth
    return c


# --- grounding wiring ------------------------------------------------------------
def test_grounding_enables_web_search_tool():
    client = _client_with(_resp([{"type": "text", "text": "hi", "citations": []}]))
    asyncio.run(client.chat("claude-x", "sys", "q", ModelParams(extra={"grounding": True})))
    kwargs = client._client.messages.calls[0]
    tools = kwargs.get("tools")
    assert tools and tools[0]["type"] == "web_search_20250305"
    assert tools[0]["name"] == "web_search"
    assert tools[0]["max_uses"] == 5
    assert kwargs["system"] == "sys"


def test_no_grounding_omits_tools():
    client = _client_with(_resp([{"type": "text", "text": "parametric answer"}]))
    result = asyncio.run(client.chat("claude-x", "", "q", ModelParams()))
    assert "tools" not in client._client.messages.calls[0]
    assert result.text == "parametric answer"
    assert result.sources == []
    assert result.search_queries == []


def test_force_search_forces_web_search_tool_choice():
    client = _client_with(_resp([{"type": "text", "text": "hi", "citations": []}]))
    asyncio.run(client.chat("claude-x", "", "q", ModelParams(extra={"grounding": True, "force_search": True})))
    assert client._client.messages.calls[0]["tool_choice"] == {"type": "tool", "name": "web_search"}


def test_no_force_search_leaves_tool_choice_auto():
    client = _client_with(_resp([{"type": "text", "text": "hi", "citations": []}]))
    asyncio.run(client.chat("claude-x", "", "q", ModelParams(extra={"grounding": True})))
    assert "tool_choice" not in client._client.messages.calls[0]


def test_force_search_dropped_on_pause_turn_continuation():
    first = _resp(
        [{"type": "server_tool_use", "name": "web_search", "input": {"query": "q1"}}],
        stop_reason="pause_turn",
    )
    second = _resp([{"type": "text", "text": "done", "citations": []}], stop_reason="end_turn")
    client = _client_with(first, second)
    asyncio.run(client.chat("claude-x", "", "q", ModelParams(extra={"grounding": True, "force_search": True})))
    calls = client._client.messages.calls
    assert calls[0]["tool_choice"] == {"type": "tool", "name": "web_search"}  # forced on turn 1
    assert "tool_choice" not in calls[1]  # dropped so the resumed turn can finalize


# --- citation / grounding parsing ------------------------------------------------
def test_parses_grounded_answer_sources_supports_queries():
    content = [
        {"type": "server_tool_use", "name": "web_search", "input": {"query": "venetoclax CLL first line"}},
        # A web_search_tool_result block is present but uncited — must NOT become a source.
        {"type": "web_search_tool_result", "content": [{"type": "web_search_result", "url": "https://ignored.example/x", "title": "Ignored"}]},
        {"type": "text", "text": "Venetoclax is recommended first-line. ", "citations": [
            {"type": "web_search_result_location", "url": "https://www.nejm.org/x",
             "title": "NEJM Study", "cited_text": "venetoclax ... first line"}]},
        {"type": "text", "text": "Consult guidelines.", "citations": []},
    ]
    client = _client_with(_resp(content, usage=_usage(100, 50)))
    result = asyncio.run(client.chat(
        "claude-sonnet-4-5-20250929", "sys", "q", ModelParams(extra={"grounding": True})
    ))
    assert result.text == "Venetoclax is recommended first-line. Consult guidelines."
    assert result.search_queries == ["venetoclax CLL first line"]
    assert len(result.sources) == 1  # only the CITED nejm.org source (ignored result dropped)
    s = result.sources[0]
    assert s.url == "https://www.nejm.org/x"
    assert s.title == "NEJM Study"
    assert s.domain == "nejm.org"
    assert s.snippet == "venetoclax ... first line"
    assert len(result.grounding_supports) == 1
    sup = result.grounding_supports[0]
    assert sup.source_indices == [0]
    assert sup.text == "Venetoclax is recommended first-line. "
    assert sup.start_index == 0
    assert sup.end_index == len("Venetoclax is recommended first-line. ")
    assert result.prompt_tokens == 100 and result.completion_tokens == 50
    assert result.finish_reason == "stop"
    assert result.model_version == "claude-sonnet-4-5-20250929"


def test_dedups_source_across_text_blocks():
    url = "https://www.fda.gov/x"
    content = [
        {"type": "text", "text": "Claim one.", "citations": [
            {"type": "web_search_result_location", "url": url, "title": "FDA"}]},
        {"type": "text", "text": "Claim two.", "citations": [
            {"type": "web_search_result_location", "url": url, "title": "FDA"}]},
    ]
    client = _client_with(_resp(content))
    result = asyncio.run(client.chat("claude-x", "", "q", ModelParams(extra={"grounding": True})))
    assert len(result.sources) == 1  # deduped by URL
    assert len(result.grounding_supports) == 2  # one per cited text block
    assert all(sup.source_indices == [0] for sup in result.grounding_supports)
    assert result.grounding_supports[0].start_index == 0
    assert result.grounding_supports[1].start_index == len("Claim one.")


def test_extract_output_pure():
    content = [
        {"type": "server_tool_use", "name": "web_search", "input": {"query": "x"}},
        {"type": "text", "text": "A.", "citations": [
            {"type": "web_search_result_location", "url": "https://a.com/1", "title": "A"}]},
    ]
    text, sources, supports, queries = _extract_output(content)
    assert text == "A."
    assert [s.url for s in sources] == ["https://a.com/1"]
    assert queries == ["x"]
    assert supports[0].source_indices == [0]


# --- pause_turn continuation -----------------------------------------------------
def test_pause_turn_continues_and_accumulates():
    first = _resp([
        {"type": "server_tool_use", "name": "web_search", "input": {"query": "q1"}},
        {"type": "text", "text": "Partial. ", "citations": []},
    ], stop_reason="pause_turn", usage=_usage(10, 5))
    second = _resp([
        {"type": "text", "text": "Final answer.", "citations": [
            {"type": "web_search_result_location", "url": "https://nih.gov/a", "title": "NIH"}]},
    ], stop_reason="end_turn", usage=_usage(8, 12))
    client = _client_with(first, second)
    result = asyncio.run(client.chat("claude-x", "", "q", ModelParams(extra={"grounding": True})))
    assert result.text == "Partial. Final answer."
    assert result.search_queries == ["q1"]
    assert {s.url for s in result.sources} == {"https://nih.gov/a"}
    assert result.prompt_tokens == 18 and result.completion_tokens == 17
    assert len(client._client.messages.calls) == 2
    # The paused assistant turn was fed back to continue the search.
    assert client._client.messages.calls[1]["messages"][-1]["role"] == "assistant"
    assert result.finish_reason == "stop"


# --- finish-reason + error taxonomy ----------------------------------------------
def test_max_tokens_maps_to_length():
    client = _client_with(_resp([{"type": "text", "text": "truncated"}], stop_reason="max_tokens"))
    result = asyncio.run(client.chat("claude-x", "", "q", ModelParams()))
    assert result.finish_reason == "length"


def test_refusal_raises_safety_blocked():
    client = _client_with(_resp([], stop_reason="refusal"))
    try:
        asyncio.run(client.chat("claude-x", "", "q", ModelParams()))
        assert False, "expected SafetyBlocked"
    except SafetyBlocked:
        pass


def test_missing_key_raises_auth_error():
    c = AnthropicClient()
    c._client = None
    # Throwaway settings stub so we don't mutate the shared cached Settings.
    c._settings = SimpleNamespace(
        anthropic_api_key="", anthropic_base_url="", anthropic_web_search_max_uses=5
    )
    try:
        asyncio.run(c.chat("claude-x", "", "q", ModelParams()))
        assert False, "expected AuthError"
    except AuthError:
        pass


def test_get_client_base_url_has_scheme_when_blank(monkeypatch):
    # Reproduces the load_yaml_config pollution (ANTHROPIC_BASE_URL="" in os.environ): the SDK
    # must NOT build a schemeless URL — we pass an explicit production base_url.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "")
    c = AnthropicClient()
    c._client = None
    c._settings = SimpleNamespace(
        anthropic_api_key="sk-ant-x", anthropic_base_url="", anthropic_web_search_max_uses=5,
        target_call_timeout_seconds=120,
    )
    client = c._get_client()
    assert str(client.base_url).startswith("https://api.anthropic.com")


def test_get_client_honors_custom_base_url():
    c = AnthropicClient()
    c._client = None
    c._settings = SimpleNamespace(
        anthropic_api_key="sk-ant-x", anthropic_base_url="https://proxy.example/anthropic",
        anthropic_web_search_max_uses=5, target_call_timeout_seconds=120,
    )
    client = c._get_client()
    assert str(client.base_url).startswith("https://proxy.example")
