"""Map a source's own endpoint wording onto a canonical outcome ID (Phase 3A/3B).

Shared by every adapter — ClinicalTrials.gov, PubMed, published NMAs, manual upload —
because two adapters that resolve "ACR50 at week 16" to different IDs would silently
build two incompatible networks out of the same evidence.

Matching is deliberately **conservative**. Three properties matter more than recall:

* **Ambiguity is never resolved by guessing.** A title naming both ACR20 and ACR50
  returns no match plus the candidate list, so a curator decides. Picking the first hit
  would assign real trial numbers to the wrong endpoint.
* **The timepoint window is a filter, not a tiebreak.** It is what separates UC induction
  remission (weeks 8-12) from UC maintenance remission (weeks 44-60) — two endpoints with
  identical wording and non-comparable populations.
* **Vocabulary lives in canonical_outcomes.yaml**, not here. Adding a synonym is a
  reviewed config change. This module only knows how to compare strings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from app.config import outcomes as canonical

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# "Week 12", "Weeks 12 to 16", "At 16 Weeks", "Up to 52 weeks", "Baseline to Week 24".
#
# Patterns are deliberately anchored on the word "week"/"month" rather than scanning for
# any number in the string. A greedy scan would read the 3104400 out of "NCT03104400" as
# a timepoint, and a wrong week silently pushes a result into the wrong allowed window.
#
# A RANGE is one assessment: "Weeks 12 to 24" means measured through week 24, so it
# collapses to its upper bound. Taking the lower bound would land it in the wrong window.
# `and` is deliberately NOT a range separator — "Weeks 12 and 24" names two assessments,
# and reading it as a window silently picks one of them.
_RANGE_PATTERN = re.compile(
    r"\b(?P<unit>weeks?|months?)\s*\d+(?:\.\d+)?\s*"
    r"(?:to|through|thru|[-\u2013\u2014])\s*(?P<upper>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Every remaining timepoint mention, in order. The third alternative catches a bare
# number continuing a list ("Weeks 2, 4, 8, 12"), which the registry uses constantly for
# repeated-measures outcomes and which a single-number pattern reads as week 2 alone.
_SCAN_PATTERN = re.compile(
    r"\b(?P<unit_first>weeks?|months?)\s*(?P<n_after>\d+(?:\.\d+)?)"
    r"|\b(?P<n_before>\d+(?:\.\d+)?)\s*(?P<unit_last>weeks?|months?)\b"
    r"|(?P<sep>,|\band\b|&)\s*(?P<n_more>\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)

# Close enough to land inside an allowed window, and never used as a reported value.
_WEEKS_PER_MONTH = 4.345


def normalise(text: str | None) -> str:
    """Lowercase, strip every non-alphanumeric character.

    Collapsing punctuation is what lets ``IGA 0/1``, ``vIGA-AD 0/1`` and ``IGA-0/1`` all
    reduce to a form containing ``iga01``.
    """
    return _NON_ALNUM.sub("", (text or "").lower())


@lru_cache
def _tokens_for(outcome_id: str) -> tuple[str, ...]:
    """Normalised match tokens for an outcome, defaulting to its endpoint label."""
    definition = canonical.outcome(outcome_id) or {}
    raw = definition.get("match_tokens") or [definition.get("endpoint") or ""]
    return tuple(sorted({normalise(t) for t in raw if normalise(t)}, key=len, reverse=True))


def _as_weeks(value: float, unit: str | None) -> float:
    return value * _WEEKS_PER_MONTH if (unit or "").lower().startswith("month") else value


def timepoint_weeks_in(text: str | None) -> tuple[float, ...]:
    """Every distinct timepoint *text* names, in the order it names them.

    Ranges are consumed first and masked out, so "Weeks 12 to 24" yields ``(24.0,)``
    rather than both bounds.
    """
    if not text:
        return ()

    found: list[float] = []
    masked = list(text)
    for match in _RANGE_PATTERN.finditer(text):
        found.append(_as_weeks(float(match.group("upper")), match.group("unit")))
        for index in range(*match.span()):
            masked[index] = " "

    unit: str | None = None
    last_end = -2
    for match in _SCAN_PATTERN.finditer("".join(masked)):
        if match.group("n_after") is not None:
            unit, raw = match.group("unit_first"), match.group("n_after")
        elif match.group("n_before") is not None:
            unit, raw = match.group("unit_last"), match.group("n_before")
        else:
            # A bare number continuing a list. Counted only when it directly adjoins the
            # previous timepoint, so an unrelated number later in the sentence is not
            # swept in as a visit.
            if unit is None or match.start() - last_end > 1:
                continue
            raw = match.group("n_more")
        found.append(_as_weeks(float(raw), unit))
        last_end = match.end()

    return tuple(dict.fromkeys(found))


def parse_timepoint_weeks(text: str | None) -> float | None:
    """The single week a registry ``timeFrame`` identifies, or ``None``.

    ``None`` covers two cases that must not be conflated with a number: the text names no
    timepoint at all, and the text names **several** ("Weeks 2, 4, 8, 12, 16, 20 and 24").
    A repeated-measures time frame does not identify one timepoint, and picking one from
    the list — first, largest, or otherwise — assigns real trial values to a week the
    source never claimed. Callers with a per-visit label available should resolve the week
    from that instead; callers without one get an honest unknown.

    Months are converted at 4.345 weeks/month, close enough to land inside an allowed
    window and never used as a reported value.
    """
    weeks = timepoint_weeks_in(text)
    return weeks[0] if len(weeks) == 1 else None


# Why a mapping attempt ended where it did. The matcher names its own outcome because
# callers were previously inferring it from `len(candidates) > 1`, which conflated "this
# title names two endpoints" with "this title names none" — see NO_CANONICAL_WORDING.
MATCHED_WORDING_AND_TIMEPOINT = "MATCHED_WORDING_AND_TIMEPOINT"
MATCHED_WORDING_ONLY = "MATCHED_WORDING_ONLY"
EMPTY_TITLE = "EMPTY_TITLE"
NO_INDICATION = "NO_INDICATION"
INDICATION_NOT_MODELLED = "INDICATION_NOT_MODELLED"
NO_CANONICAL_WORDING = "NO_CANONICAL_WORDING"
WORDING_AMBIGUOUS_NO_TIMEPOINT = "WORDING_AMBIGUOUS_NO_TIMEPOINT"
TIMEPOINT_OUTSIDE_ALL_WINDOWS = "TIMEPOINT_OUTSIDE_ALL_WINDOWS"
AMBIGUOUS_WORDING_AND_TIMEPOINT = "AMBIGUOUS_WORDING_AND_TIMEPOINT"


@dataclass(frozen=True)
class EndpointMatch:
    """The outcome of one mapping attempt. ``outcome_id`` is None when unresolved."""

    outcome_id: str | None
    confidence: float
    reason: str
    candidates: tuple[str, ...] = field(default_factory=tuple)
    reason_code: str = ""
    # Every canonical endpoint defined for the indication. Reported separately from
    # `candidates` because a title that names no endpoint at all has **no** candidates,
    # and calling the whole vocabulary a candidate set is what made 91% of harvested rows
    # look ambiguous when they were simply not canonical endpoints.
    scoped: tuple[str, ...] = field(default_factory=tuple)

    @property
    def matched(self) -> bool:
        return self.outcome_id is not None

    @property
    def is_ambiguous(self) -> bool:
        """True when the title names more than one canonical endpoint.

        Distinct from unmatched: an ambiguous title needs a curator to *choose*, while a
        non-canonical one needs vocabulary or nothing at all.
        """
        return self.reason_code in (
            AMBIGUOUS_WORDING_AND_TIMEPOINT,
            WORDING_AMBIGUOUS_NO_TIMEPOINT,
        )

    @property
    def needs_curation(self) -> bool:
        """True when a human must decide — ambiguous, or matched only on wording."""
        return not self.matched or self.confidence < 1.0


def match_endpoint(
    title: str | None,
    *,
    indication: str | None,
    week: float | None = None,
    treatment_phase: str | None = None,
) -> EndpointMatch:
    """Resolve *title* to a canonical outcome ID within *indication*.

    ``week`` is optional but strongly recommended: without it, endpoints that differ only
    by timepoint cannot be separated and the result is reported as ambiguous rather than
    guessed.
    """
    normalised_title = normalise(title)
    if not normalised_title:
        return EndpointMatch(None, 0.0, "empty endpoint title", reason_code=EMPTY_TITLE)
    if not indication:
        return EndpointMatch(
            None, 0.0, "no indication supplied; cannot scope the lookup",
            reason_code=NO_INDICATION,
        )

    scoped = [
        oid
        for oid, definition in canonical.outcomes().items()
        if (definition or {}).get("indication") == indication
        and (treatment_phase is None or (definition or {}).get("treatment_phase") == treatment_phase)
    ]
    if not scoped:
        return EndpointMatch(
            None, 0.0, f"no canonical outcomes defined for {indication!r}",
            reason_code=INDICATION_NOT_MODELLED,
        )

    textual = [oid for oid in scoped if any(t in normalised_title for t in _tokens_for(oid))]
    if not textual:
        # No candidates, deliberately: nothing in this title resembles a canonical
        # endpoint. The indication's vocabulary travels in `scoped` for curation.
        return EndpointMatch(
            None, 0.0, f"no canonical endpoint wording found in {title!r}",
            reason_code=NO_CANONICAL_WORDING, scoped=tuple(scoped),
        )

    if week is None:
        if len(textual) == 1:
            # Wording is unambiguous but the timepoint is unknown, so this is a proposal
            # for a curator rather than a resolved mapping.
            return EndpointMatch(
                textual[0], 0.6, "matched on wording only; timepoint unknown", tuple(textual),
                reason_code=MATCHED_WORDING_ONLY, scoped=tuple(scoped),
            )
        return EndpointMatch(
            None, 0.0, "several endpoints match the wording and no timepoint was given",
            tuple(textual),
            reason_code=WORDING_AMBIGUOUS_NO_TIMEPOINT, scoped=tuple(scoped),
        )

    in_window = [oid for oid in textual if canonical.in_allowed_window(oid, week)]
    if not in_window:
        return EndpointMatch(
            None, 0.0,
            f"wording matched but week {week:g} falls outside every allowed window",
            tuple(textual),
            reason_code=TIMEPOINT_OUTSIDE_ALL_WINDOWS, scoped=tuple(scoped),
        )
    if len(in_window) > 1:
        # Deliberately unresolved. Two endpoints matching the same title and window means
        # the title names more than one thing (e.g. "ACR20 and ACR50 response").
        return EndpointMatch(
            None, 0.0,
            "ambiguous: more than one canonical endpoint matches this title and timepoint",
            tuple(in_window),
            reason_code=AMBIGUOUS_WORDING_AND_TIMEPOINT, scoped=tuple(scoped),
        )

    return EndpointMatch(
        in_window[0], 1.0, "matched on wording and timepoint", tuple(in_window),
        reason_code=MATCHED_WORDING_AND_TIMEPOINT, scoped=tuple(scoped),
    )
