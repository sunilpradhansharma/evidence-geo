"""The Level-2 published-suitability gate (Phase 4).

A published synthesis outranks anything we would compute, so it is checked first — but
only if it actually answers the question asked. The rule this module exists to enforce:

    **An NMA containing both Rinvoq and Tremfya can still be unsuitable for a specific
    question.** Containing both treatments is necessary and nowhere near sufficient.

Every check runs and **every** failure is reported, rather than short-circuiting on the
first. A reviewer deciding whether to chase a paper down needs to know it missed on both
timepoint and population, not just the first thing tested.

Refusals are returned as structured statuses from ``evidence.statuses``, so a refusal is a
publishable finding rather than an error. The specific mismatch statuses are used where one
applies — ``ENDPOINT_MISMATCH``, ``TIMEPOINT_MISMATCH``, ``POPULATION_NONCOMPARABLE``,
``TREATMENT_PHASE_MISMATCH`` — because they tell a consumer *which* dimension failed.
``PUBLISHED_SYNTHESIS_UNSUITABLE`` covers the rest.

**On recency.** Age is a proxy, not the real question. What actually matters is whether the
synthesis includes the current evidence base, which is a study-overlap check against an
internal network — and that only becomes possible in Phase 6, where a network exists to
compare against. ``max_age_years`` is therefore an explicit caller-supplied gate with a
conservative default, and the honest limitation is recorded rather than hidden behind a
number that looks more principled than it is.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.config import outcomes
from app.evidence import protocols, statuses
from app.evidence.sources.published_nma import ParsedSynthesis
from app.evidence.treatments import canonical_treatment

# Conservative engineering default, not a clinical determination. A synthesis older than
# this is very likely to predate trials already in our own network.
DEFAULT_MAX_AGE_YEARS = 5


@dataclass(frozen=True)
class ComparisonRequest:
    """The question being asked, against which a synthesis is judged."""

    indication: str
    treatment_a: str
    treatment_b: str
    canonical_outcome_id: str | None = None
    population_stratum: str | None = None
    treatment_phase: str = "PRIMARY"
    protocol_id: str | None = None
    # Optional dose the question is about, e.g. "15 mg". Checked against the source's
    # printed labels, since node names have dose stripped by design.
    requested_dose: str | None = None
    as_of: date | None = None

    @property
    def nodes(self) -> tuple[str, str]:
        """Both treatments resolved through the shared normaliser."""
        return (
            canonical_treatment(self.treatment_a)[0],
            canonical_treatment(self.treatment_b)[0],
        )


@dataclass(frozen=True)
class SuitabilityDecision:
    """Whether a published synthesis may answer a question, and why not if it may not."""

    suitable: bool
    status: str
    reasons: tuple[str, ...] = field(default_factory=tuple)
    failed_dimensions: tuple[str, ...] = field(default_factory=tuple)
    matched_contrast: object = None

    @property
    def reason_text(self) -> str:
        return " ".join(self.reasons) if self.reasons else statuses.describe(self.status)


def _both_treatments_present(synthesis: ParsedSynthesis, request: ComparisonRequest):
    a, b = request.nodes
    have = set(synthesis.treatments)
    missing = [t for t in (a, b) if t not in have]
    return missing, a, b


def _find_contrast(synthesis: ParsedSynthesis, a: str, b: str):
    """The contrast covering this pair, in either direction.

    Direction is *reported*, never silently reversed — a caller wanting b-vs-a from an
    a-vs-b estimate must take the reciprocal deliberately, on a scale where that is valid.
    """
    for contrast in synthesis.contrasts:
        if contrast.treatment == a and contrast.comparator == b:
            return contrast, False
        if contrast.treatment == b and contrast.comparator == a:
            return contrast, True
    return None, False


def assess(
    synthesis: ParsedSynthesis,
    request: ComparisonRequest,
    *,
    max_age_years: int | None = DEFAULT_MAX_AGE_YEARS,
    today: date | None = None,
) -> SuitabilityDecision:
    """Judge one published synthesis against one question.

    Returns ``PUBLISHED_RESULT_AVAILABLE`` only when every dimension matches and a usable
    estimate for the pair exists. Otherwise reports every dimension that failed.
    """
    reasons: list[str] = []
    failed: list[str] = []
    # The first status assigned wins for reporting purposes, so the most specific
    # dimension-level mismatch is what a consumer sees rather than the generic refusal.
    status: str | None = None

    def fail(dimension: str, reason: str, dimension_status: str) -> None:
        nonlocal status
        failed.append(dimension)
        reasons.append(reason)
        if status is None:
            status = dimension_status

    if synthesis.problems:
        return SuitabilityDecision(
            suitable=False,
            status=statuses.PUBLISHED_SYNTHESIS_UNSUITABLE,
            reasons=tuple(
                [f"The extraction itself is not trustworthy: {p}" for p in synthesis.problems]
            ),
            failed_dimensions=("extraction",),
        )

    # --- indication -------------------------------------------------------------------
    if (synthesis.indication or "").strip().lower() != (request.indication or "").strip().lower():
        fail(
            "indication",
            f"Synthesis covers {synthesis.indication!r}, not {request.indication!r}.",
            statuses.PUBLISHED_SYNTHESIS_UNSUITABLE,
        )

    # --- both treatments present ------------------------------------------------------
    missing, node_a, node_b = _both_treatments_present(synthesis, request)
    if missing:
        fail(
            "treatments",
            f"Synthesis network does not include {', '.join(missing)}.",
            statuses.PUBLISHED_SYNTHESIS_UNSUITABLE,
        )

    # --- treatment phase --------------------------------------------------------------
    # Checked BEFORE the endpoint on purpose. Endpoint resolution is scoped by phase, so a
    # synthesis with the wrong phase also fails to resolve its endpoint — reporting the
    # endpoint first would name the symptom and hide the cause. A phase mismatch is never a
    # rounding error either: maintenance populations are re-randomised induction responders.
    if (synthesis.treatment_phase or "PRIMARY") != (request.treatment_phase or "PRIMARY"):
        fail(
            "treatment_phase",
            f"Synthesis reports the {synthesis.treatment_phase} phase, "
            f"not {request.treatment_phase}. These populations are not comparable.",
            statuses.TREATMENT_PHASE_MISMATCH,
        )

    # --- endpoint ---------------------------------------------------------------------
    if request.canonical_outcome_id:
        if synthesis.canonical_outcome_id is None:
            fail(
                "endpoint",
                "Synthesis endpoint was never resolved to a canonical outcome, so it "
                "cannot be confirmed to measure the requested one.",
                statuses.ENDPOINT_MISMATCH,
            )
        elif synthesis.canonical_outcome_id != request.canonical_outcome_id:
            fail(
                "endpoint",
                f"Synthesis measures {synthesis.canonical_outcome_id}, "
                f"not {request.canonical_outcome_id}.",
                statuses.ENDPOINT_MISMATCH,
            )

    # --- timepoint --------------------------------------------------------------------
    if synthesis.timepoint_week is None:
        fail(
            "timepoint",
            "Synthesis timepoint is unknown, so it cannot be placed inside an approved "
            "window.",
            statuses.TIMEPOINT_MISMATCH,
        )
    elif request.protocol_id and protocols.is_defined(request.protocol_id):
        if not protocols.in_approved_window(request.protocol_id, synthesis.timepoint_week):
            window = protocols.approved_time_window(request.protocol_id)
            fail(
                "timepoint",
                f"Week {synthesis.timepoint_week:g} falls outside the protocol's approved "
                f"window {window}.",
                statuses.TIMEPOINT_MISMATCH,
            )
    elif request.canonical_outcome_id and not outcomes.in_allowed_window(
        request.canonical_outcome_id, synthesis.timepoint_week
    ):
        fail(
            "timepoint",
            f"Week {synthesis.timepoint_week:g} falls outside the allowed window for "
            f"{request.canonical_outcome_id}.",
            statuses.TIMEPOINT_MISMATCH,
        )

    # --- population -------------------------------------------------------------------
    if (synthesis.population_stratum or None) != (request.population_stratum or None):
        fail(
            "population_stratum",
            f"Synthesis population is {synthesis.population_stratum or 'unstated'}, "
            f"requested {request.population_stratum or 'unstated'}.",
            statuses.POPULATION_NONCOMPARABLE,
        )

    # --- recoverable included studies (hard requirement) -------------------------------
    if not synthesis.included_studies_recoverable:
        fail(
            "included_studies",
            "The included-study list is not recoverable, so this synthesis cannot be "
            "validated, reused, or overlap-checked against an internal network.",
            statuses.PUBLISHED_SYNTHESIS_UNSUITABLE,
        )

    # --- recency ----------------------------------------------------------------------
    if max_age_years is not None:
        if synthesis.publication_date is None:
            fail(
                "recency",
                "Publication date is unknown, so recency cannot be established.",
                statuses.PUBLISHED_SYNTHESIS_UNSUITABLE,
            )
        else:
            reference = request.as_of or today or date.today()
            age_days = (reference - synthesis.publication_date).days
            if age_days > max_age_years * 365.25:
                fail(
                    "recency",
                    f"Published {age_days // 365} years before the reference date, beyond "
                    f"the {max_age_years}-year limit; newer trials are likely missing.",
                    statuses.PUBLISHED_SYNTHESIS_UNSUITABLE,
                )

    # --- a usable estimate for this exact pair ----------------------------------------
    contrast, reversed_direction = _find_contrast(synthesis, node_a, node_b)
    if contrast is None:
        if not missing:
            # Both nodes are in the network but no cell reports them against each other.
            # That is a real gap: the paper's own league table did not publish this pair.
            fail(
                "estimate",
                f"Both treatments appear in the network but no published estimate reports "
                f"{node_a} against {node_b}.",
                statuses.PUBLISHED_SYNTHESIS_UNSUITABLE,
            )
    elif contrast.estimate is None:
        fail(
            "estimate",
            "The contrast exists but its estimate could not be parsed.",
            statuses.PUBLISHED_SYNTHESIS_UNSUITABLE,
        )
    elif request.requested_dose:
        labels = " ".join(
            filter(None, (contrast.treatment_label, contrast.comparator_label))
        ).lower()
        if request.requested_dose.strip().lower() not in labels:
            fail(
                "dose",
                f"Requested dose {request.requested_dose!r} does not appear in the "
                f"source's own labels ({labels or 'none recorded'}), so the published "
                "estimate may describe a different dose.",
                statuses.PUBLISHED_SYNTHESIS_UNSUITABLE,
            )

    if failed:
        return SuitabilityDecision(
            suitable=False,
            status=status or statuses.PUBLISHED_SYNTHESIS_UNSUITABLE,
            reasons=tuple(reasons),
            failed_dimensions=tuple(dict.fromkeys(failed)),
            matched_contrast=contrast,
        )

    return SuitabilityDecision(
        suitable=True,
        status=statuses.PUBLISHED_RESULT_AVAILABLE,
        reasons=(
            "Indication, endpoint, timepoint, population, phase, dose and recency all "
            "match, and a published estimate reports this pair"
            + (" (direction reversed relative to the request)." if reversed_direction else "."),
        ),
        matched_contrast=contrast,
    )


def best_of(
    syntheses: list[ParsedSynthesis],
    request: ComparisonRequest,
    *,
    max_age_years: int | None = DEFAULT_MAX_AGE_YEARS,
    today: date | None = None,
) -> tuple[ParsedSynthesis | None, SuitabilityDecision]:
    """The most recent suitable synthesis, or the closest miss with its reasons.

    Returning the closest miss rather than a bare ``None`` is what lets the resolver
    report *"a paper exists but is unsuitable, here is why"* when it falls through to
    Level 3 — the citation stays visible instead of being dropped.
    """
    assessments = [
        (s, assess(s, request, max_age_years=max_age_years, today=today)) for s in syntheses
    ]
    suitable = [(s, d) for s, d in assessments if d.suitable]
    if suitable:
        suitable.sort(key=lambda pair: pair[0].publication_date or date.min, reverse=True)
        return suitable[0]

    if not assessments:
        return None, SuitabilityDecision(
            suitable=False,
            status=statuses.PUBLISHED_SYNTHESIS_UNSUITABLE,
            reasons=("No published synthesis was found for this question.",),
        )

    # Fewest failed dimensions first — the paper closest to answering the question.
    assessments.sort(key=lambda pair: len(pair[1].failed_dimensions))
    return assessments[0]
