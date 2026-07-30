"""Retained source material for reprocessing and audit (Phase 2).

Every extracted fact points back at a ``SourcePayload``, so an evidence row can always
answer "where did this come from, and what exactly did the source say?".

What may be stored depends on the **licence class**, not on how the document arrived —
see ``app.evidence.licensing``. Use :meth:`SourcePayload.record` rather than the
constructor: it routes through ``licensing.enforce`` so a restricted source physically
cannot end up holding a full document.

Honest limitation: "re-run extraction without refetching" holds fully only for
public-domain sources. For restricted sources you can reprocess the retained fragment,
but a full re-extraction may require re-upload.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.evidence import licensing
from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def checksum_of(content: str | None) -> str | None:
    """SHA-256 of retrieved content, so a later refetch can be compared against it.

    Recorded even when the content itself may not be retained: a checksum is a fact
    *about* a document rather than a copy of it, so it survives every licence tier and
    is what lets a reviewer confirm the fragment came from the document they are
    holding.
    """
    if content is None:
        return None
    return "sha256:" + hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


class SourcePayload(Base):
    """One retrieval from one source, retained to the extent its licence permits."""

    __tablename__ = "source_payloads"

    payload_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # Provenance --------------------------------------------------------------------
    source_type: Mapped[str] = mapped_column(String(32), index=True)
    # CLINICALTRIALS_GOV | OPENFDA | PUBMED | PMC_OA | PMC | COCHRANE | HTA |
    # SOCIETY_ABSTRACT | MANUAL_UPLOAD | JOURNAL
    source_identifier: Mapped[str] = mapped_column(String(128), index=True)  # NCT / PMID / DOI / set_id
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    citation: Mapped[str | None] = mapped_column(Text, nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    # Licence + retention (derived, never authored independently) ---------------------
    license_class: Mapped[str] = mapped_column(String(24), index=True)
    retention_policy: Mapped[str] = mapped_column(String(24))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    # Content. `raw_payload` is NULL for every FRAGMENT_ONLY source — enforced, not
    # merely conventional.
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    retained_fragment: Mapped[str | None] = mapped_column(Text, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # Where in the document the fragment came from — the minimum a reviewer needs to
    # locate the claim in a source the platform is not allowed to store.
    page_provenance: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Audit of what enforcement removed, so a reviewer seeing an absent payload can tell
    # "licence forbade retention" from "retrieval failed".
    dropped_fields: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list[str]

    uploaded_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    superseded_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @classmethod
    def record(
        cls,
        *,
        payload_id: str,
        source_type: str,
        source_identifier: str,
        raw_payload: str | None = None,
        retained_fragment: str | None = None,
        license_class: str | None = None,
        citation: str | None = None,
        url: str | None = None,
        page_provenance: str | None = None,
        uploaded_by: str | None = None,
        now: datetime | None = None,
    ) -> SourcePayload:
        """Build a payload with the retention matrix already applied.

        The only supported way to create one. ``licensing.enforce`` is subtractive, so
        passing a full document for a restricted source silently yields a row with
        ``raw_payload=None`` and ``dropped_fields=["raw_payload"]`` — the checksum is
        still computed from what was passed in, because knowing *which* document the
        fragment came from is exactly what makes it auditable.
        """
        import json

        decision = licensing.enforce(
            source_type=source_type,
            raw_payload=raw_payload,
            retained_fragment=retained_fragment,
            license_class=license_class,
            now=now,
        )
        return cls(
            payload_id=payload_id,
            source_type=source_type,
            source_identifier=source_identifier,
            url=url,
            citation=citation,
            retrieved_at=now or utcnow(),
            license_class=decision.license_class,
            retention_policy=decision.retention_policy,
            expires_at=decision.expires_at,
            raw_payload=decision.raw_payload,
            retained_fragment=decision.retained_fragment,
            checksum=checksum_of(raw_payload if raw_payload is not None else retained_fragment),
            page_provenance=page_provenance,
            dropped_fields=json.dumps(list(decision.dropped_fields)) if decision.dropped_fields else None,
            uploaded_by=uploaded_by,
        )
