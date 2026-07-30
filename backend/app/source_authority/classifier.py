"""Domain classifier (FR-706a.2) — two independent axes + a derived display category.

Resolution order (curated taxonomy is the source of truth; enrichment only assists):
  1. control_type  — AbbVie/competitor from explicit config domains, then brand-name tokens
  2. authority_type — longest-suffix match against the curated authority lists
  3. LLM ENRICHMENT — only if authority is still unresolved: apply an evidence-based LLM
     classification when its confidence clears the apply threshold (flagged requires_review
     until it clears the higher auto threshold)
  4. control fallback — INDEPENDENT when it's a known third-party authority, else UNKNOWN
  5. display_category — ABBVIE_CONTROLLED / COMPETITOR_CONTROLLED else the authority_type

Registration (RDAP/WHOIS) registrant/registrar/visibility are stored as metadata only.
Redaction is recorded in ``whois_visibility`` and NEVER lowers ``verification`` (a redacted
record is not "unverified").
"""
from __future__ import annotations

import json

from app.config.settings import get_settings
from app.models.source_domain import (
    AUTH_GUIDELINE,
    AUTH_HEALTH_MEDIA,
    AUTH_MEDICAL_REFERENCE,
    AUTH_OTHER,
    AUTH_PEER_REVIEWED,
    AUTH_REGULATORY,
    AUTH_SOCIAL_UGC,
    CONTROL_ABBVIE,
    CONTROL_COMPETITOR,
    CONTROL_INDEPENDENT,
    CONTROL_UNKNOWN,
    DISPLAY_ABBVIE_CONTROLLED,
    DISPLAY_COMPETITOR_CONTROLLED,
    SRC_CONFIG,
    SRC_LLM,
    STATUS_AUTO_CLASSIFIED,
    STATUS_CURATED,
    STATUS_EXTERNALLY_ENRICHED,
    STATUS_UNCLASSIFIED,
    UNVERIFIED,
    VERIFICATION_UNKNOWN,
    VERIFIED,
)
from app.source_authority import taxonomy

_INDEPENDENT_AUTHORITIES = {
    AUTH_REGULATORY, AUTH_GUIDELINE, AUTH_PEER_REVIEWED, AUTH_MEDICAL_REFERENCE,
    AUTH_HEALTH_MEDIA, AUTH_SOCIAL_UGC,
}


def classify(
    normalized_host: str,
    registrable_domain: str,
    authority_domain: str,
    *,
    whois: dict | None = None,
    llm: dict | None = None,
) -> dict:
    """Classify a domain into the SourceDomain fields (see module docstring for order)."""
    reasons: list[str] = []

    # 1-2. Curated control + authority.
    control = taxonomy.control_for(normalized_host, registrable_domain)
    authority = taxonomy.authority_type_for(normalized_host)
    curated = bool(control or authority)
    if control:
        reasons.append(f"ownership matched curated taxonomy ({control})")
    if authority:
        reasons.append(f"authority matched curated list ({authority})")

    # 3. LLM enrichment — only to resolve an otherwise-unknown authority, and only when the
    #    model's confidence clears the apply threshold. The apply..auto band is flagged for
    #    Medical Affairs review; a genuinely uncertain call leaves the domain unclassified.
    source = SRC_CONFIG
    confidence: float | None = 1.0 if curated else None
    status = STATUS_CURATED if curated else STATUS_UNCLASSIFIED
    llm_publisher: str | None = None
    requires_review = False
    evidence: list[str] = []
    if authority is None and llm:
        settings = get_settings()
        apply_min = settings.source_authority_llm_apply_min_confidence
        auto_min = settings.source_authority_llm_auto_confidence
        llm_auth = llm.get("authority_type")
        llm_conf = llm.get("confidence")
        llm_conf = float(llm_conf) if isinstance(llm_conf, (int, float)) else None
        evidence = [str(e) for e in (llm.get("evidence") or []) if str(e).strip()]
        if llm_auth and llm_auth != AUTH_OTHER and llm_conf is not None and llm_conf >= apply_min:
            authority = llm_auth
            source = SRC_LLM
            status = STATUS_AUTO_CLASSIFIED
            confidence = llm_conf
            llm_publisher = llm.get("publisher") or None
            requires_review = bool(llm.get("requires_review")) or llm_conf < auto_min
            reasons.append(f"authority inferred by LLM ({authority}, confidence {llm_conf:.2f})")
        elif llm_auth and llm_auth != AUTH_OTHER:
            # The model suggested an authority but was not confident enough to apply it.
            requires_review = True
            reasons.append("LLM suggested an authority below the apply threshold — needs review")

    # 4. control fallback.
    if not control:
        control = CONTROL_INDEPENDENT if authority in _INDEPENDENT_AUTHORITIES else CONTROL_UNKNOWN

    if authority is None:
        authority = AUTH_OTHER
    if status == STATUS_UNCLASSIFIED:
        reasons.append("no curated or external match")

    # 5. display_category (flattened enum for the requirement).
    if control == CONTROL_ABBVIE:
        display = DISPLAY_ABBVIE_CONTROLLED
    elif control == CONTROL_COMPETITOR:
        display = DISPLAY_COMPETITOR_CONTROLLED
    else:
        display = authority

    # verification (independent of WHOIS visibility).
    if curated:
        verification = VERIFIED
    elif status in (STATUS_EXTERNALLY_ENRICHED, STATUS_AUTO_CLASSIFIED):
        verification = VERIFICATION_UNKNOWN
    else:
        verification = UNVERIFIED

    # WHOIS metadata (never used to set authority/verification; publisher_name is NOT taken
    # from a WHOIS registrar, which is often a privacy proxy).
    whois = whois or {}
    registrant_org = whois.get("registrant_organization")
    registrar_name = whois.get("registrar_name")
    whois_visibility = whois.get("whois_visibility")
    if whois_visibility:
        reasons.append(f"whois visibility {whois_visibility}")

    return {
        "control_type": control,
        "authority_type": authority,
        "display_category": display,
        "publisher_name": llm_publisher,
        "registrant_organization": registrant_org,
        "registrar_name": registrar_name,
        "whois_visibility": whois_visibility,
        "verification": verification,
        "classification_status": status,
        "classification_source": source,
        "classification_confidence": confidence,
        "classification_reason": "; ".join(reasons) or None,
        "classification_evidence": json.dumps(evidence) if evidence else None,
        "requires_review": requires_review,
        "rules_version": taxonomy.rules_version(),
    }
