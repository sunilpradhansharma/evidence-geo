"""Response citation model (FR-706a) — one row per (response, authority domain).

Frequency-preserving: rather than collapsing a response's cited domains to a boolean, each
row keeps ``citation_count`` (how many cited URLs mapped to this domain), ``citation_urls``
(the actual URLs) and ``first_citation_position`` (the earliest index in the response's
ordered source list). Those drive top-cited ranking, competitor-top-source alerts and the
citation-share distribution. Denormalised filter dimensions (llm/persona/TA/…) mirror the
Response so the dashboard can slice without a join. Links to ``source_domains`` for the
shared, cached classification.
"""
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ResponseCitation(Base):
    """A response's citations of a single authority domain (append-only, upserted)."""

    __tablename__ = "response_citations"
    __table_args__ = (
        # One row per response × authority domain — the count lives in citation_count,
        # so re-running classification for a response is idempotent (upsert on this key).
        UniqueConstraint("response_id", "authority_domain", name="uq_response_authority_domain"),
    )

    citation_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # UUID
    # FR-706a.3: formal foreign key back to the primary Response table (enforced on Postgres;
    # documentation-only on SQLite unless PRAGMA foreign_keys=ON). Responses are append-only
    # (DM-002) so CASCADE only matters if a response is ever purged.
    response_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("responses.response_id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    domain_id: Mapped[str] = mapped_column(String(64), index=True)  # -> source_domains.domain_id
    authority_domain: Mapped[str] = mapped_column(String(255), index=True)

    # Denormalised filter dimensions (mirror Response) for fast dashboard slicing.
    llm_name: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    persona: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    therapeutic_area: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    indication: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    brand_focus: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # Frequency (FR-706a.1/.5) — preserved, never deduped away.
    citation_count: Mapped[int] = mapped_column(Integer, default=1)
    citation_urls: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list[str]
    first_citation_position: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
