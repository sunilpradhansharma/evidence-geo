"""Licence-aware retention rules for retrieved source material (Phase 2).

Retaining an API response and retaining a publication are not the same thing legally,
so retention follows the **licence class of the source**, never the mechanism by which
the document arrived. A reviewer uploading a paywalled PDF does not create a right to
store that PDF — it creates a right to store the extracted values and the provenance
needed to find it again.

    PUBLIC_DOMAIN   ClinicalTrials.gov, openFDA        full raw payload, indefinitely
    OPEN_ACCESS     PMC OA and similar                 full text while the licence permits
    RESTRICTED      Cochrane, HTA, society abstracts   fragment + provenance ONLY

``enforce`` is the single chokepoint: it decides what a ``SourcePayload`` is allowed to
carry and strips anything the licence does not permit, rather than trusting callers to
remember. ``SourcePayload.record`` routes through it, so the rule cannot be bypassed by
constructing the model directly.

> **Legal-review dependency.** This matrix is a conservative engineering default, not a
> legal determination. It needs sign-off before restricted-source ingestion goes live.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Licence classes, ordered most to least permissive.
PUBLIC_DOMAIN = "PUBLIC_DOMAIN"
OPEN_ACCESS = "OPEN_ACCESS"
RESTRICTED = "RESTRICTED"
LICENSE_CLASSES = (PUBLIC_DOMAIN, OPEN_ACCESS, RESTRICTED)

# Retention policies. Derived from the licence class — never authored independently,
# because two fields that can disagree about the same question are a bug waiting to
# happen.
FULL_INDEFINITE = "FULL_INDEFINITE"
FULL_WHILE_LICENSED = "FULL_WHILE_LICENSED"
FRAGMENT_ONLY = "FRAGMENT_ONLY"

_POLICY_BY_CLASS = {
    PUBLIC_DOMAIN: FULL_INDEFINITE,
    OPEN_ACCESS: FULL_WHILE_LICENSED,
    RESTRICTED: FRAGMENT_ONLY,
}

# Open-access licences are revocable in practice (publishers reclassify, mirrors go
# away), so full text carries an expiry that forces a re-check rather than assuming the
# grant is permanent.
_OPEN_ACCESS_RETENTION = timedelta(days=365)

# Licence class per known source. Anything absent is treated as RESTRICTED: an unknown
# source is the one case where guessing wrong is unrecoverable, so the default is the
# most conservative option rather than the most convenient.
_SOURCE_LICENSE = {
    "CLINICALTRIALS_GOV": PUBLIC_DOMAIN,
    "OPENFDA": PUBLIC_DOMAIN,
    "PUBMED": PUBLIC_DOMAIN,       # abstracts + metadata from E-utilities
    "PMC_OA": OPEN_ACCESS,         # only the Open Access subset
    "PMC": RESTRICTED,             # general PMC is NOT the OA subset
    "COCHRANE": RESTRICTED,
    "HTA": RESTRICTED,             # NICE, CDA-AMC, G-BA — often commercial-in-confidence
    "SOCIETY_ABSTRACT": RESTRICTED,  # ACR / EULAR / ASCO
    "MANUAL_UPLOAD": RESTRICTED,
    "JOURNAL": RESTRICTED,
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def license_for_source(source_type: str | None) -> str:
    """Licence class for a source type. Unknown sources are RESTRICTED, deliberately."""
    return _SOURCE_LICENSE.get((source_type or "").strip().upper(), RESTRICTED)


def policy_for_license(license_class: str | None) -> str:
    """Retention policy implied by a licence class. Unknown classes are FRAGMENT_ONLY."""
    return _POLICY_BY_CLASS.get((license_class or "").strip().upper(), FRAGMENT_ONLY)


def may_retain_full_document(license_class: str | None) -> bool:
    """True only where the licence permits storing the source document itself."""
    return policy_for_license(license_class) in (FULL_INDEFINITE, FULL_WHILE_LICENSED)


def expiry_for(license_class: str | None, *, now: datetime | None = None) -> datetime | None:
    """When retained content must be re-checked, or ``None`` for indefinite retention.

    Restricted sources get no expiry because there is no full document to expire — only
    the extracted fragment and provenance, which are what makes the evidence auditable
    at all and must outlive the licence.
    """
    if policy_for_license(license_class) == FULL_WHILE_LICENSED:
        return (now or utcnow()) + _OPEN_ACCESS_RETENTION
    return None


@dataclass(frozen=True)
class RetentionDecision:
    """What a payload is permitted to store, and what was dropped to get there."""

    license_class: str
    retention_policy: str
    raw_payload: str | None
    retained_fragment: str | None
    expires_at: datetime | None
    dropped_fields: tuple[str, ...]

    @property
    def full_document_retained(self) -> bool:
        return self.raw_payload is not None


def enforce(
    *,
    source_type: str | None,
    raw_payload: str | None = None,
    retained_fragment: str | None = None,
    license_class: str | None = None,
    now: datetime | None = None,
) -> RetentionDecision:
    """Apply the retention matrix, stripping anything the licence does not permit.

    Enforcement is **subtractive by construction**: a caller can only ever end up with
    less than it passed in. That is what makes the guarantee testable — there is no code
    path where a RESTRICTED source ends up holding a document, regardless of what the
    adapter tried to hand over.

    *license_class* may be supplied to override the source-type default (a specific PMC
    article known to be in the OA subset, say), but an unrecognised value still resolves
    to FRAGMENT_ONLY rather than silently becoming permissive.
    """
    resolved = (license_class or license_for_source(source_type) or "").strip().upper()
    if resolved not in LICENSE_CLASSES:
        resolved = RESTRICTED
    policy = policy_for_license(resolved)

    dropped: list[str] = []
    if policy == FRAGMENT_ONLY and raw_payload is not None:
        # The load-bearing line of this module.
        dropped.append("raw_payload")
        raw_payload = None

    return RetentionDecision(
        license_class=resolved,
        retention_policy=policy,
        raw_payload=raw_payload,
        retained_fragment=retained_fragment,
        expires_at=expiry_for(resolved, now=now),
        dropped_fields=tuple(dropped),
    )


def validation_reach(license_class: str | None) -> str:
    """How far the Phase 3A validation agent can re-derive values for this licence.

    Validation coverage is reported per licence tier rather than as one figure, because
    a restricted source can only be re-checked against the retained fragment. Claiming
    uniform coverage would overstate what was actually verified.
    """
    if policy_for_license(license_class) == FRAGMENT_ONLY:
        return "FRAGMENT"
    return "FULL_SOURCE"
