"""Which head-to-head comparison an answer belongs to. Pure — no DB, no network.

One question can inform SEVERAL comparisons. *"How does Rinvoq compare to Tremfya and
Cosentyx?"* genuinely answers both pairings, and ``coverage.covers`` already marks both
cells covered because it tests each cell independently. So this returns a TUPLE of pairs
rather than picking a winner: collapsing to one would make the scoreboard disagree with
the coverage matrix that commissioned the question.

**The candidate competitor set is the same competitive field the scorer graded against.**
``scorer._competitive_field`` prefers the indication's competitors and falls back to the
therapeutic-area block; this mirrors that order exactly. If the two disagreed, the
scoreboard would attribute a sentiment number to a comparison the scorer never scored.

**Nothing is dropped silently.** Every answer that yields no pair carries a machine-
readable reason, because "109 of 189 comparative answers name no competitor" is a finding
the reader needs, and a scoreboard that quietly discarded 58% of its input would be
reporting a filtered universe as if it were the whole one.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from app.config import taxonomy
from app.prompt_volume import mapping

# Why an answer yields no head-to-head. Surfaced verbatim so the UI can explain the
# difference between "not applicable" and "we could not tell".
NO_FOCUS_BRAND = "no_focus_brand"
NO_COMPETITIVE_FIELD = "no_competitive_field"
BRAND_NOT_NAMED = "brand_not_named"
NO_COMPETITOR_NAMED = "no_competitor_named"

REASON_LABELS = {
    NO_FOCUS_BRAND: "No focus brand on the answer — a brand-less landscape question is "
                    "read by the landscape view, not as a head-to-head.",
    NO_COMPETITIVE_FIELD: "The taxonomy declares no competitors for this indication or "
                          "therapeutic area, so there is no comparison to score.",
    BRAND_NOT_NAMED: "The question never names our brand, so it is not asking a "
                     "head-to-head about it.",
    NO_COMPETITOR_NAMED: "The question names no tracked competitor — it asks about our "
                         "brand in general, not against a named rival.",
}

ORIGIN_STORED = "stored"
ORIGIN_DERIVED = "derived"
# Both drugs are named in the question, but the taxonomy declares no competitive field for
# the row's scope — 29 stored answers carry ``therapeutic_area="Immunology"``, which is not
# a declared key, and among them is a real Skyrizi-vs-Tremfya comparison. Discarding a
# question that explicitly names a tracked rival because of a legacy tag would lose the
# finding to a bookkeeping error, so it is kept and labelled as the weaker claim it is.
ORIGIN_TEXT_ONLY = "text_only"

ORIGIN_LABELS = {
    ORIGIN_STORED: "The question is tagged with this comparison.",
    ORIGIN_DERIVED: "Both drugs are named in the question, and the taxonomy lists them as "
                    "competitors in this indication.",
    ORIGIN_TEXT_ONLY: "Both drugs are named in the question, but this row carries no "
                      "recognised indication or therapeutic area — treat the pairing as "
                      "indicative.",
}


@dataclass(frozen=True)
class Pair:
    """One head-to-head: our brand against one named comparator, in one indication."""

    brand: str
    competitor: str
    disease: str | None = None

    @property
    def key(self) -> str:
        """Stable id. The indication is part of it: the same two drugs in a different
        indication is a different competitive field and a different answer."""
        return f"{self.brand}|{self.competitor}|{self.disease or ''}"

    @property
    def therapeutic_area(self) -> str | None:
        return taxonomy.therapeutic_area_key_for_disease(self.disease)

    @property
    def area(self) -> str | None:
        return taxonomy.area_for_disease(self.disease)

    @property
    def label(self) -> str:
        base = f"{self.brand} vs {self.competitor}"
        return f"{base} · {self.disease}" if self.disease else base

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "brand": self.brand,
            "competitor": self.competitor,
            "disease": self.disease,
            "therapeutic_area": self.therapeutic_area,
            "area": self.area,
        }


@dataclass(frozen=True)
class Resolution:
    """The comparisons an answer informs, plus why it informs none."""

    pairs: tuple[Pair, ...] = ()
    origin: str = ORIGIN_DERIVED
    reason: str | None = None

    @property
    def resolved(self) -> bool:
        return bool(self.pairs)


def parse_competitor_focus(value: str | list | None) -> list[str]:
    """Competitor tags off a Question/Response row, whatever shape they are stored in.

    The column holds a JSON list, but a legacy row can hold a bare string. Mirrors
    ``variation_service._competitor_focus`` rather than inventing a third reading.
    """
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return [str(value).strip()] if str(value).strip() else []
    if isinstance(parsed, list):
        return [str(v).strip() for v in parsed if str(v).strip()]
    text = str(parsed).strip()
    return [text] if text else []


def competitive_field(disease: str | None, therapeutic_area: str | None) -> tuple[str, ...]:
    """The comparators in play, in the SAME order of preference the scorer uses.

    Indication first, therapeutic-area block second. The area block flattens every
    indication into one list, which is why it is a fallback and not the primary.

    A stored ``therapeutic_area`` can be either a TA key (``Gastroenterology``) or a broad
    area display name (``Immunology``), so it is expanded through ``keys_for_area`` the way
    ``analytics._apply_ta_filters`` and ``coverage._diseases_in_scope`` already do. Reading
    the block without expanding returns nothing for every area-named row, which reported
    real head-to-heads as having no competitive field at all.
    """
    if disease:
        field = taxonomy.competitors_for_disease(disease)
        if field:
            return field

    key = (therapeutic_area or "").strip()
    if not key:
        return ()
    out: list[str] = []
    for ta_key in taxonomy.keys_for_area(key) or (key,):
        for name in taxonomy.competitors_for_key(ta_key):
            if name not in out:
                out.append(name)
    return tuple(out)


def all_competitors() -> tuple[str, ...]:
    """Every competitor the taxonomy tracks anywhere, for the last-resort tier only.

    Read off the shared ``alias_index`` so the focus-brand / competitor split stays a single
    opinion held in brands.yaml. Safe to widen this far ONLY because the caller still
    requires the drug to be named outright in the question: a false pairing would need the
    question to literally name a drug from an unrelated area, in which case it genuinely is
    being compared and the reader should see it.
    """
    seen: dict[str, None] = {}
    for entry in taxonomy.alias_index():
        if entry.get("is_competitor") and entry.get("canonical"):
            seen.setdefault(entry["canonical"], None)
    return tuple(seen)


def resolve(
    question_text: str,
    *,
    brand_focus: str | None,
    therapeutic_area: str | None = None,
    disease: str | None = None,
    competitor_focus: str | list | None = None,
) -> Resolution:
    """The head-to-head comparisons this question asks.

    Stored tags win over text derivation: a curation-generated question carries the exact
    cell it was written for, and re-deriving it would let a wording change silently move
    the answer to a different comparison than the one commissioned.

    The derived path applies the same three conditions as ``coverage.covers`` — our brand
    named, the comparator named, and the indication resolved from the text — so a question
    the matrix counts as covering a cell is the same question this attributes to that pair.
    """
    brand = (brand_focus or "").strip()
    if not brand:
        return Resolution(reason=NO_FOCUS_BRAND)

    resolved_disease = taxonomy.canonical_disease(disease)
    tagged = parse_competitor_focus(competitor_focus)
    if tagged:
        pairs = tuple(
            Pair(brand=brand, competitor=name, disease=resolved_disease) for name in tagged
        )
        return Resolution(pairs=pairs, origin=ORIGIN_STORED)

    # Derivation reads the indication out of the text when the row does not carry one,
    # which is the whole reason a legacy answer can still be placed on the board.
    text = question_text or ""
    if resolved_disease is None:
        resolved_disease = mapping.resolve_disease(text)

    # Our own brand has to be named before anything else is considered. Without it the
    # question is about the category, not about us against a rival.
    if not mapping.mentions(text, brand):
        return Resolution(reason=BRAND_NOT_NAMED)

    field = competitive_field(resolved_disease, therapeutic_area)
    origin = ORIGIN_DERIVED
    if not field:
        # The scope declares no field. Rather than discard, look for any tracked rival the
        # question names outright, and mark the pairing as the weaker claim it is.
        field = all_competitors()
        origin = ORIGIN_TEXT_ONLY
        if not field:
            return Resolution(reason=NO_COMPETITIVE_FIELD)

    named = [c for c in field if mapping.mentions(text, c)]
    if not named:
        return Resolution(reason=NO_COMPETITOR_NAMED)

    pairs = tuple(
        Pair(brand=brand, competitor=name, disease=resolved_disease) for name in named
    )
    return Resolution(pairs=pairs, origin=origin)
