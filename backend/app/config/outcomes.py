"""Canonical outcome (endpoint) definitions, loaded from canonical_outcomes.yaml.

``canonical_outcomes.yaml`` is the single owner of endpoint semantics: what the
endpoint is, its nominal timepoint, the window a result may fall in and still count
as that timepoint, the effect measure it is reported on, and which treatment phase
it belongs to. brands.yaml and every analysis protocol REFERENCE these IDs and never
restate their meaning.

Two rules this module exists to enforce:

* An outcome ID referenced anywhere but absent here is a configuration error, not a
  silent no-op — see ``config.taxonomy.validate_config``.
* Induction and maintenance are different outcomes even when the endpoint text is
  identical, because maintenance populations are re-randomised induction responders.
  ``treatment_phase`` makes that difference machine-checkable.

Restart the backend after editing the YAML: the loader is lru_cache'd.
"""
from functools import lru_cache

from app.config.settings import load_yaml_config

TREATMENT_PHASES = ("PRIMARY", "INDUCTION", "MAINTENANCE")
OUTCOME_TYPES = ("binary", "continuous")

# Does a higher event rate mean a better outcome? Required, with no default: it is the only
# thing that turns a risk ratio into a statement about which treatment is preferable, and a
# default would silently invert every comparative verdict on the first adverse-event
# endpoint anyone adds. Read by Phase 8's claim grading, which refuses rather than assumes.
HIGHER_IS_BETTER = "HIGHER_IS_BETTER"
LOWER_IS_BETTER = "LOWER_IS_BETTER"
BENEFIT_DIRECTIONS = (HIGHER_IS_BETTER, LOWER_IS_BETTER)

_REQUIRED_FIELDS = ("endpoint", "outcome_type", "nominal_timepoint_week",
                    "allowed_window", "effect_measure", "treatment_phase",
                    "benefit_direction")


@lru_cache
def _config() -> dict:
    return load_yaml_config("canonical_outcomes.yaml") or {}


@lru_cache
def outcomes() -> dict:
    """``{outcome_id: definition}`` for every canonical endpoint."""
    return _config().get("outcomes", {}) or {}


@lru_cache
def outcome_ids() -> tuple[str, ...]:
    return tuple(outcomes())


def outcome(outcome_id: str | None) -> dict | None:
    """Definition for *outcome_id*, or ``None`` when it is not defined.

    Returning ``None`` rather than raising keeps read paths tolerant; the startup
    validator is what turns an unknown reference into a hard error.
    """
    if not outcome_id:
        return None
    return outcomes().get(outcome_id.strip())


def is_defined(outcome_id: str | None) -> bool:
    return outcome(outcome_id) is not None


@lru_cache
def strata() -> dict:
    """``{stratum_id: definition}`` for the canonical population strata."""
    return _config().get("population_strata", {}) or {}


def stratum(stratum_id: str | None) -> dict | None:
    if not stratum_id:
        return None
    return strata().get(stratum_id.strip())


def in_allowed_window(outcome_id: str | None, week: float | None) -> bool:
    """True when *week* falls inside the outcome's allowed timepoint window.

    Unknown outcome or unknown week returns ``False`` — a timepoint that cannot be
    checked is never treated as matching, since silently accepting it is exactly the
    ``TIMEPOINT_MISMATCH`` failure the evidence hierarchy exists to surface.
    """
    definition = outcome(outcome_id)
    if not definition or week is None:
        return False
    window = definition.get("allowed_window") or {}
    lo, hi = window.get("min_week"), window.get("max_week")
    if lo is None or hi is None:
        return False
    return float(lo) <= float(week) <= float(hi)


def benefit_direction(outcome_id: str | None) -> str | None:
    """``HIGHER_IS_BETTER`` / ``LOWER_IS_BETTER``, or ``None`` when the outcome is unknown.

    ``None`` is a refusal, not a default. A caller that cannot establish the direction must
    decline to say which treatment an estimate favours — guessing produces a confidently
    inverted finding, which is the worst output this system can emit.
    """
    definition = outcome(outcome_id)
    if not definition:
        return None
    value = definition.get("benefit_direction")
    return value if value in BENEFIT_DIRECTIONS else None


def validate() -> list[str]:
    """Structural problems in canonical_outcomes.yaml, as human-readable strings.

    Empty list means the file is well-formed. Called by ``taxonomy.validate_config``
    so there is one startup entry point for all config validation.
    """
    errors: list[str] = []
    defined = outcomes()
    if not defined:
        errors.append("canonical_outcomes.yaml defines no `outcomes:` — nothing can reference an endpoint")

    for oid, definition in defined.items():
        definition = definition or {}
        where = f"canonical_outcomes.yaml outcome {oid!r}"
        missing = [f for f in _REQUIRED_FIELDS if definition.get(f) in (None, "")]
        if missing:
            errors.append(f"{where}: missing required field(s) {', '.join(missing)}")
            continue

        if definition["outcome_type"] not in OUTCOME_TYPES:
            errors.append(
                f"{where}: outcome_type {definition['outcome_type']!r} "
                f"is not one of {', '.join(OUTCOME_TYPES)}"
            )
        if definition["treatment_phase"] not in TREATMENT_PHASES:
            errors.append(
                f"{where}: treatment_phase {definition['treatment_phase']!r} "
                f"is not one of {', '.join(TREATMENT_PHASES)}"
            )
        if definition["benefit_direction"] not in BENEFIT_DIRECTIONS:
            errors.append(
                f"{where}: benefit_direction {definition['benefit_direction']!r} "
                f"is not one of {', '.join(BENEFIT_DIRECTIONS)}"
            )

        window = definition.get("allowed_window") or {}
        lo, hi, nominal = window.get("min_week"), window.get("max_week"), definition["nominal_timepoint_week"]
        if lo is None or hi is None:
            errors.append(f"{where}: allowed_window needs both min_week and max_week")
        elif not (lo <= nominal <= hi):
            errors.append(
                f"{where}: nominal_timepoint_week {nominal} falls outside "
                f"allowed_window [{lo}, {hi}]"
            )

    for sid, definition in strata().items():
        if not (definition or {}).get("label"):
            errors.append(f"canonical_outcomes.yaml stratum {sid!r}: missing label")

    return errors
