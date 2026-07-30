"""Structured comparison statuses — never fabricate, never silently approximate.

Every path through the resolver ends at one of these, including every failure. That is
the point: *"this comparison is not estimable, and here is exactly why"* is a legitimate
product output, not an error. A resolver that returned ``None`` on failure would force
callers to invent an explanation, and an invented explanation in an evidence system is
worse than no answer.

The vocabulary is grouped by what a consumer should do with it:

    SUCCESS       an answer exists; ``evidence_level`` says how strong
    GAP           no answer; the reason is specific and actionable
    GOVERNANCE    an answer may exist but is not releasable yet
    SERVICE       transient infrastructure failure, retryable
"""
from __future__ import annotations

# --- success ---------------------------------------------------------------------------
DIRECT_EVIDENCE_AVAILABLE = "DIRECT_EVIDENCE_AVAILABLE"          # L1
PUBLISHED_RESULT_AVAILABLE = "PUBLISHED_RESULT_AVAILABLE"        # L2
BUCHER_ITC_COMPLETED = "BUCHER_ITC_COMPLETED"                    # L3, star topology
INTERNAL_NMA_COMPLETED = "INTERNAL_NMA_COMPLETED"                # L3, netmeta
GOVERNED_SYNTHESIS_COMPLETED = "GOVERNED_SYNTHESIS_COMPLETED"    # L3, fully governed
EXPLORATORY_RESULT_COMPLETED = "EXPLORATORY_RESULT_COMPLETED"    # L3, unapproved protocol

SUCCESS_STATUSES = (
    DIRECT_EVIDENCE_AVAILABLE,
    PUBLISHED_RESULT_AVAILABLE,
    BUCHER_ITC_COMPLETED,
    INTERNAL_NMA_COMPLETED,
    GOVERNED_SYNTHESIS_COMPLETED,
    EXPLORATORY_RESULT_COMPLETED,
)

# --- structured evidence gaps (Level 4) --------------------------------------------------
DIRECT_EVIDENCE_UNSUITABLE = "DIRECT_EVIDENCE_UNSUITABLE"
# The Level-2 counterpart. Needed because "no published synthesis covers these treatments"
# and "one does, but it does not fit this question" are different findings, and only the
# second one tells a reviewer there is a paper worth reading.
PUBLISHED_SYNTHESIS_UNSUITABLE = "PUBLISHED_SYNTHESIS_UNSUITABLE"
NETWORK_DISCONNECTED = "NETWORK_DISCONNECTED"
ENDPOINT_MISMATCH = "ENDPOINT_MISMATCH"
TIMEPOINT_MISMATCH = "TIMEPOINT_MISMATCH"
POPULATION_NONCOMPARABLE = "POPULATION_NONCOMPARABLE"
INSUFFICIENT_ARM_DATA = "INSUFFICIENT_ARM_DATA"
# Distinct from INSUFFICIENT_ARM_DATA for the same reason PUBLISHED_SYNTHESIS_UNSUITABLE is
# distinct above: "the number is missing" and "we hold two numbers that contradict each other"
# send a reviewer to different places. The first is nothing to act on; the second names a study
# whose analysis populations someone must choose between.
AMBIGUOUS_ARM_DATA = "AMBIGUOUS_ARM_DATA"
ROUTE_MIXING_NOT_ESTIMABLE = "ROUTE_MIXING_NOT_ESTIMABLE"
TREATMENT_PHASE_MISMATCH = "TREATMENT_PHASE_MISMATCH"

GAP_STATUSES = (
    DIRECT_EVIDENCE_UNSUITABLE,
    PUBLISHED_SYNTHESIS_UNSUITABLE,
    NETWORK_DISCONNECTED,
    ENDPOINT_MISMATCH,
    TIMEPOINT_MISMATCH,
    POPULATION_NONCOMPARABLE,
    INSUFFICIENT_ARM_DATA,
    AMBIGUOUS_ARM_DATA,
    ROUTE_MIXING_NOT_ESTIMABLE,
    TREATMENT_PHASE_MISMATCH,
)

# --- governance ---------------------------------------------------------------------------
PROTOCOL_PENDING_APPROVAL = "PROTOCOL_PENDING_APPROVAL"
PROTOCOL_REJECTED = "PROTOCOL_REJECTED"
MEDICAL_REVIEW_REQUIRED = "MEDICAL_REVIEW_REQUIRED"
NETWORK_NOT_RATIFIED = "NETWORK_NOT_RATIFIED"

GOVERNANCE_STATUSES = (
    PROTOCOL_PENDING_APPROVAL,
    PROTOCOL_REJECTED,
    MEDICAL_REVIEW_REQUIRED,
    NETWORK_NOT_RATIFIED,
)

# --- service ------------------------------------------------------------------------------
NMA_SERVICE_UNAVAILABLE = "NMA_SERVICE_UNAVAILABLE"

SERVICE_STATUSES = (NMA_SERVICE_UNAVAILABLE,)

ALL_STATUSES = SUCCESS_STATUSES + GAP_STATUSES + GOVERNANCE_STATUSES + SERVICE_STATUSES

# Human-readable reasons. Held here rather than at each raise site so the same failure
# always reads the same way, in the API, the UI and the audit trail alike.
DESCRIPTIONS: dict[str, str] = {
    DIRECT_EVIDENCE_AVAILABLE: "A head-to-head trial randomised both treatments and passed suitability.",
    PUBLISHED_RESULT_AVAILABLE: "A published synthesis covers both treatments and passed suitability.",
    BUCHER_ITC_COMPLETED: "Computed by adjusted indirect comparison through a common comparator.",
    INTERNAL_NMA_COMPLETED: "Computed by network meta-analysis across the ratified network.",
    GOVERNED_SYNTHESIS_COMPLETED: "Computed under an approved protocol against a ratified network.",
    EXPLORATORY_RESULT_COMPLETED: (
        "Computed in exploratory mode. Not releasable: cannot become ratified evidence, "
        "generate approved questions, affect scoring, or create recommendations."
    ),
    DIRECT_EVIDENCE_UNSUITABLE: (
        "Direct evidence exists but does not match the requested population, dose, endpoint "
        "or timepoint. It remains in the provenance rather than being silently discarded."
    ),
    PUBLISHED_SYNTHESIS_UNSUITABLE: (
        "A published synthesis covers these treatments but does not fit this question — "
        "its citation is retained so a reviewer can read it and judge for themselves."
    ),
    NETWORK_DISCONNECTED: "No path of shared comparators connects the two treatments.",
    ENDPOINT_MISMATCH: "The available results measure a different endpoint.",
    TIMEPOINT_MISMATCH: "Results fall outside the protocol's approved time window.",
    POPULATION_NONCOMPARABLE: "Populations differ enough that transitivity cannot be assumed.",
    INSUFFICIENT_ARM_DATA: "Arm-level data required for computation is missing.",
    AMBIGUOUS_ARM_DATA: (
        "Two or more in-scope results describe the same arm and disagree, so no single value "
        "is true of it. The arm was withheld rather than resolved by row order."
    ),
    ROUTE_MIXING_NOT_ESTIMABLE: (
        "Oral and injectable placebo responses differ too much for transitivity to hold "
        "in this network."
    ),
    TREATMENT_PHASE_MISMATCH: (
        "Induction and maintenance results cannot be pooled — maintenance populations are "
        "re-randomised induction responders."
    ),
    PROTOCOL_PENDING_APPROVAL: "The governing analysis protocol has not been approved.",
    PROTOCOL_REJECTED: "The governing analysis protocol was rejected in review.",
    MEDICAL_REVIEW_REQUIRED: "A medical reviewer must sign off before this result is usable.",
    NETWORK_NOT_RATIFIED: "The network has not completed both medical and statistical review.",
    NMA_SERVICE_UNAVAILABLE: "The NMA sidecar did not respond. This is transient; retry.",
}


def describe(status: str | None) -> str:
    """Human-readable explanation for a status, or a safe fallback."""
    return DESCRIPTIONS.get(status or "", "Unknown comparison status.")


def is_success(status: str | None) -> bool:
    return status in SUCCESS_STATUSES


def is_gap(status: str | None) -> bool:
    """True for a structured evidence gap — a finding, not a failure."""
    return status in GAP_STATUSES


def is_releasable(status: str | None) -> bool:
    """True only for results that may flow downstream to consumers.

    ``EXPLORATORY_RESULT_COMPLETED`` is deliberately excluded: an exploratory result
    cannot become ratified evidence, generate approved questions, affect AI scoring or
    create a recommendation. Routing every consumer through this one predicate is what
    keeps that guarantee from depending on each caller remembering it.
    """
    return status in SUCCESS_STATUSES and status != EXPLORATORY_RESULT_COMPLETED
