"""The source adapter contract (Phase 3A).

Mirrors ``app.geo.sources.openfda`` exactly, because that module is already a proven
never-raises external-API boundary in this codebase and there is no reason to invent a
second convention:

* **httpx**, settings-driven base URL, offline-safe.
* **Never raises.** A timeout, a 404, a 500 or a malformed body all produce a
  ``FetchResult`` carrying ``ok=False`` and a reason. An ingestion run that hits a flaky
  registry degrades to fewer rows; it does not abort and lose the rows it already had.
* **Retrieval and interpretation are separate.** ``fetch`` returns the payload and its
  licence classification. Turning that into ``ClinicalStudy`` rows is the adapter's
  ``parse``, which is pure and therefore testable against committed fixtures with no
  network.

That split is what lets Phase 3B's tests run offline: ``parse`` is exercised against
fixture JSON, and only ``fetch`` ever touches the wire.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

import httpx

from app.evidence import licensing

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20.0


@dataclass
class FetchResult:
    """The outcome of one retrieval. Never an exception.

    ``ok=False`` with a populated ``reason`` is a normal, expected value — callers branch
    on it rather than wrapping every call in try/except.
    """

    ok: bool
    source_type: str
    source_identifier: str
    payload: Any = None
    raw_text: str | None = None
    url: str | None = None
    reason: str | None = None
    status_code: int | None = None
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def license_class(self) -> str:
        """Licence tier for this source — decides what may be retained downstream."""
        return licensing.license_for_source(self.source_type)

    @property
    def may_retain_document(self) -> bool:
        return licensing.may_retain_full_document(self.license_class)

    @classmethod
    def failure(
        cls,
        source_type: str,
        source_identifier: str,
        reason: str,
        *,
        status_code: int | None = None,
        url: str | None = None,
    ) -> FetchResult:
        return cls(
            ok=False,
            source_type=source_type,
            source_identifier=source_identifier,
            reason=reason,
            status_code=status_code,
            url=url,
        )


@runtime_checkable
class SourceAdapter(Protocol):
    """What every evidence source must provide.

    ``parse`` is deliberately synchronous and pure: given a payload it returns canonical
    rows and makes no I/O. That is the whole reason adapter tests need no network.
    """

    source_type: str

    async def fetch(self, identifier: str) -> FetchResult:
        """Retrieve one record. Never raises."""
        ...

    def parse(self, result: FetchResult) -> Any:
        """Map a successful payload onto canonical entities. Pure; no I/O."""
        ...


async def get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    source_type: str,
    source_identifier: str,
    timeout: float = DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
) -> FetchResult:
    """Shared JSON GET that converts every failure mode into a ``FetchResult``.

    Adapters call this rather than using httpx directly, so the never-raises guarantee
    is implemented once instead of being re-derived (and eventually forgotten) in each
    new source.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, params=params, headers=headers)
    except httpx.HTTPError as e:
        logger.debug("%s: transport failure for %s: %s", source_type, source_identifier, e)
        return FetchResult.failure(source_type, source_identifier, f"transport error: {e}", url=url)

    if response.status_code == 404:
        # A 404 means "no such record", which is information rather than breakage.
        return FetchResult.failure(
            source_type, source_identifier, "not found", status_code=404, url=url
        )
    if response.status_code >= 400:
        return FetchResult.failure(
            source_type,
            source_identifier,
            f"HTTP {response.status_code}",
            status_code=response.status_code,
            url=url,
        )

    try:
        payload = response.json()
    except ValueError as e:
        return FetchResult.failure(
            source_type,
            source_identifier,
            f"malformed JSON: {e}",
            status_code=response.status_code,
            url=url,
        )

    return FetchResult(
        ok=True,
        source_type=source_type,
        source_identifier=source_identifier,
        payload=payload,
        raw_text=response.text,
        url=str(response.request.url) if response.request else url,
        status_code=response.status_code,
    )
