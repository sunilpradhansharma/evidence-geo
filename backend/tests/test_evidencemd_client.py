"""EvidenceMD provider adapter tests (OpenAI-compatible Chat Completions).

Uses a fake OpenAI-compatible client (no network) to verify response parsing: answer
text, token accounting, finish-reason mapping, and best-effort citation extraction into
ProviderResult.sources. EvidenceMD is a SEPARATE product from the manual OpenEvidence
capture tool — these tests do not touch that path.
"""
import asyncio

from app.providers.base import AuthError, ModelParams
from app.providers.evidencemd_client import (
    EvidenceMDClient,
    _extract_citations,
    _extract_markdown_citations,
)


class _FakeMessage:
    def __init__(self, content, citations=None, annotations=None):
        self.content = content
        self.citations = citations or []
        self.annotations = annotations or []

    def model_dump(self):
        return {"content": self.content, "citations": self.citations,
                "annotations": self.annotations}


class _FakeChoice:
    def __init__(self, message, finish_reason="stop"):
        self.message = message
        self.finish_reason = finish_reason


class _FakeUsage:
    def __init__(self, p, c):
        self.prompt_tokens = p
        self.completion_tokens = c


class _FakeResponse:
    def __init__(self, message, finish_reason="stop", model="evidencemd-x"):
        self.choices = [_FakeChoice(message, finish_reason)]
        self.usage = _FakeUsage(11, 22)
        self.model = model


class _FakeCompletions:
    def __init__(self, response):
        self._response = response
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._response


class _FakeChat:
    def __init__(self, response):
        self.completions = _FakeCompletions(response)


class _FakeClient:
    def __init__(self, response):
        self.chat = _FakeChat(response)


def _client_with(response) -> EvidenceMDClient:
    c = EvidenceMDClient()
    c._client = _FakeClient(response)  # bypass real SDK/auth
    return c


def test_parses_answer_and_usage():
    msg = _FakeMessage("Evidence-based answer.")
    client = _client_with(_FakeResponse(msg))
    result = asyncio.run(client.chat("evidencemd", "sys", "question?", ModelParams()))
    assert result.text == "Evidence-based answer."
    assert result.prompt_tokens == 11 and result.completion_tokens == 22
    assert result.finish_reason == "stop"
    assert result.model_version == "evidencemd-x"


def test_extracts_citations_from_both_shapes():
    msg = _FakeMessage(
        "Answer with refs.",
        citations=[{"url": "https://pubmed.ncbi.nlm.nih.gov/1", "title": "Study A"},
                   "https://nejm.org/2"],
        annotations=[{"type": "url_citation",
                      "url_citation": {"url": "https://jamanetwork.com/3", "title": "Study C"}}],
    )
    sources = _extract_citations(msg)
    urls = {s.url for s in sources}
    assert urls == {
        "https://pubmed.ncbi.nlm.nih.gov/1",
        "https://nejm.org/2",
        "https://jamanetwork.com/3",
    }
    # Domain is derived for each.
    by_url = {s.url: s for s in sources}
    assert by_url["https://nejm.org/2"].domain == "nejm.org"


def test_extracts_inline_markdown_citations():
    """EvidenceMD's real shape: references are inline `[n](url)` links in the content."""
    text = (
        "Methotrexate is first-line [17](https://pmc.ncbi.nlm.nih.gov/articles/PMC8133095/) "
        "for RA [20](https://www.frontiersin.org/journals/pharmacology/full)."
    )
    client = _client_with(_FakeResponse(_FakeMessage(text)))
    result = asyncio.run(client.chat("evidencemd", "", "q", ModelParams()))
    assert result.text == text
    urls = {s.url for s in result.sources}
    assert urls == {
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC8133095/",
        "https://www.frontiersin.org/journals/pharmacology/full",
    }


def test_markdown_citations_dedupe_against_structured():
    existing = [__import__("app.providers.base", fromlist=["Source"]).Source(
        url="https://a.com/1", origin="GROUNDED")]
    out = _extract_markdown_citations("see [1](https://a.com/1) and [2](https://b.com/2)", existing)
    assert {s.url for s in out} == {"https://b.com/2"}


def test_length_finish_reason_maps():
    client = _client_with(_FakeResponse(_FakeMessage("truncated"), finish_reason="length"))
    result = asyncio.run(client.chat("evidencemd", "", "q", ModelParams()))
    assert result.finish_reason == "length"


def test_missing_key_raises_auth_error():
    from types import SimpleNamespace

    client = EvidenceMDClient()
    client._client = None
    # Use a throwaway settings stub so we don't mutate the shared cached Settings.
    client._settings = SimpleNamespace(evidencemd_api_key="", evidencemd_base_url="")
    try:
        asyncio.run(client.chat("evidencemd", "", "q", ModelParams()))
        assert False, "expected AuthError"
    except AuthError:
        pass
