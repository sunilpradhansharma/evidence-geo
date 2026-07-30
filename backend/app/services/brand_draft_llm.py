"""Model-drafted starting points for the Add Brand modal.

Kept apart from ``brand_authoring_service`` so the write path has no model dependency at all.
Everything here produces a *suggestion* that an analyst edits or ticks; nothing here decides
what is stored. That separation is the point — it means the taxonomy can be written with the
model unavailable, and a model failure degrades the modal to a blank form rather than
blocking the save.

Two rules the prompts enforce, because they are the ones with consequences:

* **Closed sets stay closed.** ``administration_route`` and ``evidence_depth`` are picked from
  the curated vocabularies, never invented, so a drafted value cannot fail startup validation.
* **A drafted endpoint is a reference, not a definition.** ``canonical_outcomes`` must name IDs
  that already exist in ``canonical_outcomes.yaml``; anything else is dropped here rather than
  written and rejected later.
"""
from __future__ import annotations

from app.config import outcomes, taxonomy
from app.insights.llm import chat_json
from app.utils.logging import get_logger

logger = get_logger("services.brand_draft")

_IDENTITY_SYSTEM = (
    "You are a pharmaceutical competitive-intelligence analyst. Answer with facts about "
    "marketed drugs only. Return strict JSON and nothing else. If you are not confident of a "
    "field, return null for it rather than guessing — a wrong company or drug class is worse "
    "than a blank one, because it will be stored and trusted."
)

_COMPETITOR_SYSTEM = (
    "You are a pharmaceutical competitive-intelligence analyst. You suggest which marketed "
    "drugs genuinely compete with a given brand in a given indication. Return strict JSON and "
    "nothing else. Only name drugs that hold, or are widely used in, that indication — a drug "
    "of the same class that is not used in the indication is NOT a competitor. Give a one-line "
    "reason for each, which a human reviewer will read before accepting it."
)


_SPELLING_SYSTEM = (
    "You are a pharmaceutical name-checker. Given a string an analyst typed as a drug brand "
    "name, decide whether it is spelled correctly. Return strict JSON and nothing else. Be "
    "conservative: only propose a correction when the typed text is clearly a misspelling of "
    "a specific real marketed drug. Many real brand names look unusual, so an unfamiliar name "
    "is not evidence of a typo."
)


async def check_spelling(name: str) -> dict:
    """Is this typed name a misspelling of a real marketed drug?

    Runs ONLY after the deterministic pass has returned ``novel``, and answers a question that
    pass structurally cannot. ``difflib`` compares against drugs already curated, so a typo of
    a drug the taxonomy has never heard of has nothing to match — "Mavyre" scores no match at
    any cutoff precisely because Mavyret is absent. Catching that needs knowledge of drugs
    outside the taxonomy, which is the one thing a model has here and the index does not.

    Deliberately advisory. A correction is offered, never applied: the analyst may legitimately
    be adding a drug the model has not heard of, and a name silently rewritten under them would
    be worse than a name left misspelled.
    """
    typed = (name or "").strip()
    if not typed:
        return {"checked": False}

    user = (
        f"Typed brand name: {typed}\n\n"
        "Return JSON with exactly these keys:\n"
        '  "verdict": one of "correct" (a real drug, spelled right), '
        '"misspelling" (clearly a misspelling of one specific real drug), '
        '"unknown" (you do not recognise it either way)\n'
        '  "corrected": the correctly spelled brand name when verdict is "misspelling", '
        "else null\n"
        '  "generic": generic name of the drug you matched, or null\n'
        '  "company": marketing company of the drug you matched, or null\n'
        '  "note": one short sentence explaining the verdict\n'
    )

    try:
        data = await chat_json(_SPELLING_SYSTEM, user, max_tokens=400)
    except Exception as e:  # noqa: BLE001 — advisory only; never block the flow on it
        logger.warning("spelling check failed for %r: %s", typed, e)
        return {"checked": False, "reason": str(e)}

    if not isinstance(data, dict):
        return {"checked": False}

    verdict = (_clean(data.get("verdict")) or "unknown").lower()
    corrected = _clean(data.get("corrected"))
    # A "correction" identical to what was typed is not a correction, and showing it as one
    # would train the analyst to dismiss this panel.
    if verdict != "misspelling" or not corrected or corrected.lower() == typed.lower():
        return {
            "checked": True,
            "verdict": "correct" if verdict == "correct" else "unknown",
            "generic": _clean(data.get("generic")),
            "company": _clean(data.get("company")),
            "note": _clean(data.get("note")) or "",
        }

    return {
        "checked": True,
        "verdict": "misspelling",
        "corrected": corrected,
        "generic": _clean(data.get("generic")),
        "company": _clean(data.get("company")),
        "note": _clean(data.get("note")) or "",
    }


async def draft_identity(name: str) -> dict:
    """Draft the drug record for a brand name: generic, company, class, route, aliases.

    Every field is editable in the UI and shown as a draft. ``evidence_depth`` is deliberately
    not asked for — a new brand always starts at ``standard``.
    """
    routes = ", ".join(taxonomy.ADMINISTRATION_ROUTES)
    user = (
        f"Brand name: {name}\n\n"
        "Return JSON with exactly these keys:\n"
        '  "generic": international nonproprietary name, or null\n'
        '  "company": the marketing-authorisation holder, or null\n'
        '  "drug_class": short mechanism label, e.g. "IL-23 inhibitor", or null\n'
        f'  "administration_route": exactly one of [{routes}], or null\n'
        '  "aliases": other names this drug is commonly called, including the generic '
        "and any development code. Do NOT include the brand name itself. [] if none.\n"
        '  "known": true if you are confident this is a real marketed drug, false otherwise\n'
    )

    try:
        data = await chat_json(_IDENTITY_SYSTEM, user, max_tokens=600)
    except Exception as e:  # noqa: BLE001 — the modal still works as a blank form
        logger.warning("brand identity draft failed for %r: %s", name, e)
        return {"available": False, "reason": str(e)}

    if not isinstance(data, dict):
        return {"available": False, "reason": "model did not return an object"}

    route = (data.get("administration_route") or "").strip().upper() or None
    return {
        "available": True,
        "known": bool(data.get("known")),
        "generic": _clean(data.get("generic")),
        "company": _clean(data.get("company")),
        "drug_class": _clean(data.get("drug_class")),
        # Silently dropped rather than passed through: an off-vocabulary route would fail
        # startup validation, and the analyst can pick the right one from the select.
        "administration_route": route if route in taxonomy.ADMINISTRATION_ROUTES else None,
        "aliases": _alias_list(data.get("aliases"), exclude=name),
    }


async def suggest_competitors(brand: str, disease: str) -> list[dict]:
    """Suggest competitors for one brand in one indication, each with a reason.

    Returned unticked. Anything already curated for the indication is filtered out here so the
    analyst is not asked to re-approve a decision the taxonomy already carries.
    """
    already = set(taxonomy.competitors_for_disease(disease))
    user = (
        f"Brand: {brand}\nIndication: {disease}\n\n"
        f"Already recorded as competitors (do not repeat): "
        f"{', '.join(sorted(already)) or 'none'}\n\n"
        'Return JSON: {"competitors": [{"name": brand name of the competing drug, '
        '"generic": generic name or null, "company": marketing company or null, '
        '"reason": one sentence on why it competes with this brand in this indication}]}\n'
        "Return at most 8, most directly competitive first. Return an empty list if none."
    )

    try:
        data = await chat_json(_COMPETITOR_SYSTEM, user, max_tokens=1200)
    except Exception as e:  # noqa: BLE001
        logger.warning("competitor suggestions failed for %s/%s: %s", brand, disease, e)
        return []

    entries = (data or {}).get("competitors") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return []

    out: list[dict] = []
    seen = {a.lower() for a in already}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = _clean(entry.get("name"))
        if not name or name.lower() in seen or name.lower() == brand.strip().lower():
            continue
        seen.add(name.lower())
        record = taxonomy.drug_index().get(name.lower()) or {}
        out.append({
            "name": name,
            "generic": _clean(entry.get("generic")),
            "company": _clean(entry.get("company")) or record.get("company"),
            "reason": _clean(entry.get("reason")) or "",
            # Shown in the UI: an already-curated drug is a safer tick than a brand-new name,
            # because its class and route have been reviewed.
            "already_curated": bool(record),
        })
    return out


async def draft_outcomes(disease: str) -> dict:
    """Suggest canonical outcome IDs for a brand-new indication.

    Only IDs that already exist in ``canonical_outcomes.yaml`` are returned — the model is
    choosing from a fixed vocabulary, not defining an endpoint. An indication saved with these
    is stored ``DRAFT`` regardless, and stays fenced out of the evidence programme until a
    human verifies it.
    """
    defined = list(outcomes.outcome_ids())
    if not defined:
        return {"available": False, "reason": "no canonical outcomes are defined"}

    user = (
        f"Indication: {disease}\n\n"
        "Choose the primary efficacy endpoints normally used in registrational trials for "
        "this indication, from this fixed list of IDs:\n"
        f"{', '.join(defined)}\n\n"
        'Return JSON: {"canonical_outcomes": [ids], "reason": one sentence}\n'
        "Choose only from the list. Return an empty list if none of them fit."
    )

    try:
        data = await chat_json(_IDENTITY_SYSTEM, user, max_tokens=600)
    except Exception as e:  # noqa: BLE001
        logger.warning("outcome draft failed for %r: %s", disease, e)
        return {"available": False, "reason": str(e)}

    chosen = (data or {}).get("canonical_outcomes") if isinstance(data, dict) else None
    valid = [o for o in (chosen or []) if isinstance(o, str) and outcomes.is_defined(o)]
    return {
        "available": True,
        "canonical_outcomes": valid,
        "reason": _clean((data or {}).get("reason")) or "",
        # Stated back so the modal can be explicit that this is why the indication saves as
        # DRAFT, rather than the fence being an unexplained surprise later.
        "verification_status": "DRAFT",
    }


def _clean(value) -> str | None:
    """A trimmed non-empty string, or None. Models return "null" and "" interchangeably."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if text and text.lower() not in ("null", "none", "n/a", "unknown") else None


def _alias_list(values, *, exclude: str) -> list[str]:
    """Deduped aliases, minus the brand name itself and anything blank."""
    if not isinstance(values, list):
        return []
    out: list[str] = []
    skip = {exclude.strip().lower()}
    for value in values:
        text = _clean(value)
        if text and text.lower() not in skip:
            out.append(text)
            skip.add(text.lower())
    return out
