"""Source domain model (FR-706a) — a domain-level classification/enrichment cache.

One row per authority domain (e.g. ``ncbi.nlm.nih.gov``), classified on TWO independent
axes — ``control_type`` (who owns it) and ``authority_type`` (what kind of source it is) —
plus a derived ``display_category`` for the requirement's flat enum. Registration metadata
(registrant org / registrar / redaction) comes from RDAP first, then optionally WhoisXML, and
is stored SEPARATELY from the curated classification and never used as proof of authority.
For uncurated domains an evidence-based LLM classification may set ``authority_type`` with a
``classification_confidence``; low-confidence results are flagged ``requires_review`` for
Medical Affairs and keep ``classification_evidence`` (the signals the model saw). Caching here
means we classify + (optionally) enrich once per domain, not once per citation, and can
reclassify in bulk when ``rules_version`` changes.
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base

# --- control_type: ownership / control -------------------------------------------
CONTROL_ABBVIE = "ABBVIE"
CONTROL_COMPETITOR = "COMPETITOR"
CONTROL_INDEPENDENT = "INDEPENDENT"
CONTROL_UNKNOWN = "UNKNOWN"

# --- authority_type: content / authority class -----------------------------------
AUTH_REGULATORY = "REGULATORY"
AUTH_GUIDELINE = "GUIDELINE"                # clinical-practice guidelines / HTA bodies (NCCN, NICE, ESMO)
AUTH_PEER_REVIEWED = "PEER_REVIEWED"
AUTH_MEDICAL_REFERENCE = "MEDICAL_REFERENCE"
AUTH_HEALTH_MEDIA = "HEALTH_MEDIA"
AUTH_SOCIAL_UGC = "SOCIAL_UGC"
AUTH_OTHER = "OTHER"

# --- display_category: flattened for the requirement's enum (FR-706a.2) ----------
DISPLAY_ABBVIE_CONTROLLED = "ABBVIE_CONTROLLED"
DISPLAY_COMPETITOR_CONTROLLED = "COMPETITOR_CONTROLLED"
# else display_category == authority_type

# --- verification: can we vouch for the source? (distinct from whois_visibility) --
VERIFIED = "VERIFIED"
UNVERIFIED = "UNVERIFIED"
VERIFICATION_UNKNOWN = "UNKNOWN"

# --- whois_visibility: registration-record visibility (NOT a trust signal) --------
WHOIS_PUBLIC = "PUBLIC"
WHOIS_REDACTED = "REDACTED"
WHOIS_NO_DATA = "NO_DATA"

# --- classification_status / _source ---------------------------------------------
STATUS_CURATED = "CURATED"                 # matched a curated list / ownership map
STATUS_EXTERNALLY_ENRICHED = "EXTERNALLY_ENRICHED"  # only an external signal classified it
STATUS_AUTO_CLASSIFIED = "AUTO_CLASSIFIED"
STATUS_UNCLASSIFIED = "UNCLASSIFIED"       # nothing matched -> OTHER/UNKNOWN

SRC_CONFIG = "CONFIG"
SRC_CATEGORIZATION = "CATEGORIZATION"
SRC_LLM = "LLM"                            # evidence-based LLM classification of an uncurated domain
SRC_WHOIS = "WHOIS"
SRC_MANUAL = "MANUAL"
SRC_STUB = "STUB"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SourceDomain(Base):
    """Cached, reusable classification for a single authority domain."""

    __tablename__ = "source_domains"

    domain_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # UUID
    authority_domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    registrable_domain: Mapped[str] = mapped_column(String(255), index=True)

    # Publisher name comes from curated config / categorization — NEVER from a WHOIS
    # registrar (which is often a privacy service, not the publisher).
    publisher_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    registrant_organization: Mapped[str | None] = mapped_column(String(255), nullable=True)
    registrar_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    control_type: Mapped[str] = mapped_column(String(16), index=True, default=CONTROL_UNKNOWN)
    authority_type: Mapped[str] = mapped_column(String(24), index=True, default=AUTH_OTHER)
    display_category: Mapped[str] = mapped_column(String(24), index=True, default=AUTH_OTHER)

    verification: Mapped[str] = mapped_column(String(16), default=VERIFICATION_UNKNOWN)
    whois_visibility: Mapped[str | None] = mapped_column(String(16), nullable=True)

    classification_status: Mapped[str] = mapped_column(String(24), default=STATUS_UNCLASSIFIED)
    classification_source: Mapped[str] = mapped_column(String(16), default=SRC_CONFIG)
    classification_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    classification_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Structured evidence the LLM classifier saw (JSON list), and a review flag routing
    # low-confidence / conflicting auto-classifications to Medical Affairs.
    classification_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    rules_version: Mapped[int] = mapped_column(Integer, default=0)

    enriched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    enrichment_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
