"""Analysis protocol definitions and their derived content hash (X1).

A protocol is the statistical contract for one analysable question. Structural gates —
network connected, network ratified, endpoint matches — constrain the *evidence set* and
say nothing about the *statistics*. This module owns the statistics half.

Two objects, deliberately separate:

    AnalysisProtocolDefinition   analysis_protocols.yaml   methodology only
    AnalysisProtocolApproval     database rows             keyed by content_hash

**Why they must be separate.** If approval lived inside the YAML, recording an approval
would change the file's content, change its hash, and so invalidate the approval that had
just been granted. The definition therefore carries no approval state and no hash field,
and this module refuses to load a protocol that tries to author either — see
``FORBIDDEN_KEYS``. ``content_hash`` is *derived*, never accepted as input.

**Editing a protocol invalidates its prior approvals.** That is the intended behaviour:
an approval attests to specific methodology, so changing the methodology must retire it.
``approvals.derived_status`` compares the stored hash against the current one, which is
what makes the invalidation automatic rather than a process anyone has to remember.

Restart the backend after editing the YAML: the loader is lru_cache'd.
"""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache

from app.config import outcomes
from app.config.settings import load_yaml_config

# Keys that must never appear in a protocol definition. Approval state and the hash are
# both derived or stored elsewhere; authoring either here is the one mistake that would
# quietly break the whole governance model, so it fails loudly at startup instead.
FORBIDDEN_KEYS = (
    "content_hash", "hash", "approval", "approvals", "approval_status",
    "approved", "approved_by", "decision", "reviewer_id", "reviewed_at", "revoked_at",
)

EFFECT_MEASURES = (
    "risk_ratio", "odds_ratio", "risk_difference",
    "mean_difference", "standardised_mean_difference", "hazard_ratio",
)
DOSE_POLICIES = ("SEPARATE_BY_APPROVED_DOSE", "POOL_ALL_DOSES", "POOL_WITHIN_APPROVED_RANGE")

# Whether a biosimilar shares its originator's node. Regulatory biosimilarity is an
# assertion about the molecule, not about interchangeability in every network, so the
# default is separation and pooling has to be chosen and approved.
# `SEPARATE_NODES` is the honest default; `POOL_WITH_ORIGINATOR` asserts equivalence;
# `EXCLUDE_BIOSIMILARS` keeps an originator-only network clean.
BIOSIMILAR_POLICIES = ("SEPARATE_NODES", "POOL_WITH_ORIGINATOR", "EXCLUDE_BIOSIMILARS")

# Whether a combination arm is its own node. "ADA + MTX" is not adalimumab: pooling it with
# the monotherapy attributes the combination's efficacy to one of its components, and that
# error flatters whichever agent is named first.
# `OWN_NODE` treats the combination as distinct; `POOL_WITH_BACKBONE` is the pooling choice
# stated out loud; `EXCLUDE_COMBINATIONS` keeps a monotherapy network monotherapy-only.
COMBINATION_POLICIES = ("OWN_NODE", "POOL_WITH_BACKBONE", "EXCLUDE_COMBINATIONS")
MISSING_DATA_POLICIES = (
    "NON_RESPONDER_IMPUTATION", "COMPLETE_CASE",
    "MULTIPLE_IMPUTATION", "LAST_OBSERVATION_CARRIED_FORWARD",
)
ZERO_EVENT_POLICIES = (
    "TREATMENT_ARM_CONTINUITY_CORRECTION", "FIXED_0_5_CORRECTION", "EXCLUDE_ZERO_EVENT_STUDIES",
)
MODEL_SELECTION_RULES = (
    "NETMETA_IF_LOOPS_OR_MULTI_ARM_ELSE_BUCHER", "ALWAYS_NETMETA", "ALWAYS_BUCHER",
)
HETEROGENEITY_RULES = (
    "RANDOM_EFFECTS_IF_I2_ABOVE_50", "RANDOM_EFFECTS_ALWAYS", "FIXED_EFFECTS_ALWAYS",
)
INCONSISTENCY_RULES = (
    "NET_SPLIT_IF_INDEPENDENT_LOOPS", "NONE_STAR_NETWORK", "DESIGN_BY_TREATMENT_INTERACTION",
)

# Every value here is an ANALYTIC STRATEGY. There is deliberately no "adjust the placebo
# rate" option: a standardised placebo adjustment would be inventing data, and no reviewer
# should be offered it as a choice. A differential placebo response is a transitivity
# threat to disclose, not a quantity to correct.
PLACEBO_RESPONSE_POLICIES = (
    "CONTRAST_ONLY", "SENSITIVITY_REQUIRED", "METAREGRESSION",
    "SUBGROUP_BY_ROUTE", "NOT_ESTIMABLE",
)
ROUTE_MIXING_POLICIES = ("DISCLOSE_AND_PROCEED", "BLOCK", "SEPARATE_SUBNETWORKS")
RISK_OF_BIAS_HANDLING = ("SENSITIVITY_ANALYSIS_ONLY", "EXCLUDE_HIGH_RISK", "NONE")

_REQUIRED_FIELDS = (
    "version", "indication", "canonical_outcome_id", "treatment_phase", "estimand",
    "effect_measure", "approved_time_window", "dose_policy", "missing_data_policy",
    "zero_event_policy", "model_selection_rule", "heterogeneity_rule",
    "inconsistency_rule", "placebo_response_policy", "route_mixing_policy",
    "risk_of_bias_handling", "biosimilar_policy", "combination_policy",
    "min_studies", "min_nodes",
)

_ENUM_FIELDS: dict[str, tuple[str, ...]] = {
    "effect_measure": EFFECT_MEASURES,
    "dose_policy": DOSE_POLICIES,
    "missing_data_policy": MISSING_DATA_POLICIES,
    "zero_event_policy": ZERO_EVENT_POLICIES,
    "model_selection_rule": MODEL_SELECTION_RULES,
    "heterogeneity_rule": HETEROGENEITY_RULES,
    "inconsistency_rule": INCONSISTENCY_RULES,
    "placebo_response_policy": PLACEBO_RESPONSE_POLICIES,
    "route_mixing_policy": ROUTE_MIXING_POLICIES,
    "risk_of_bias_handling": RISK_OF_BIAS_HANDLING,
    "biosimilar_policy": BIOSIMILAR_POLICIES,
    "combination_policy": COMBINATION_POLICIES,
    "treatment_phase": outcomes.TREATMENT_PHASES,
}

_WHITESPACE = re.compile(r"\s+")


class ProtocolError(ValueError):
    """A protocol definition that cannot be trusted to govern an analysis."""


@lru_cache
def _config() -> dict:
    return load_yaml_config("analysis_protocols.yaml") or {}


@lru_cache
def protocols() -> dict:
    """``{protocol_id: definition}`` for every defined analysis protocol.

    Raises ``ProtocolError`` if any definition authors approval state or a hash, because
    a protocol file that can express its own approval has already lost the guarantee this
    module exists to provide.
    """
    defined = _config().get("protocols", {}) or {}
    for protocol_id, definition in defined.items():
        present = [k for k in FORBIDDEN_KEYS if k in (definition or {})]
        if present:
            raise ProtocolError(
                f"analysis_protocols.yaml protocol {protocol_id!r} authors {', '.join(present)}. "
                "Approval state and content_hash are never authored: approval lives in "
                "analysis_protocol_approvals keyed by the hash, and the hash is derived "
                "from this content. Authoring either would let recording an approval "
                "invalidate that same approval."
            )
    return defined


@lru_cache
def protocol_ids() -> tuple[str, ...]:
    return tuple(protocols())


def protocol(protocol_id: str | None) -> dict | None:
    """Definition for *protocol_id*, or ``None`` when undefined.

    Tolerant on read; ``validate`` is what turns an unknown reference into a hard error.
    """
    if not protocol_id:
        return None
    return protocols().get(protocol_id.strip())


def is_defined(protocol_id: str | None) -> bool:
    return protocol(protocol_id) is not None


def _canonical(value):
    """Reduce a parsed value to its meaning, so the hash tracks content not layout.

    Whitespace inside a string is collapsed because YAML folded scalars (``>-``) have
    already discarded the author's line breaks by the time we see them — re-wrapping a
    long ``estimand`` cannot be distinguished from the original after parsing, so it must
    not change the hash. Dict key order is normalised for the same reason. Anything that
    alters the *words* still changes the hash.
    """
    if isinstance(value, str):
        return _WHITESPACE.sub(" ", value).strip()
    if isinstance(value, dict):
        return {k: _canonical(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def content_hash(protocol_id: str | None) -> str | None:
    """``sha256:…`` over the canonical content of *protocol_id*, or ``None`` if undefined.

    DERIVED, never authored. Recording an approval does not touch the definition and so
    cannot change this value; editing any methodology field does change it, which is what
    retires the prior approvals.
    """
    definition = protocol(protocol_id)
    if definition is None:
        return None
    payload = {"protocol_id": protocol_id.strip(), "definition": _canonical(definition)}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def approved_time_window(protocol_id: str | None) -> tuple[float, float] | None:
    """The statistician-approved ``(min_week, max_week)`` for this protocol."""
    definition = protocol(protocol_id)
    if not definition:
        return None
    window = definition.get("approved_time_window") or {}
    lo, hi = window.get("min_week"), window.get("max_week")
    if lo is None or hi is None:
        return None
    return float(lo), float(hi)


def in_approved_window(protocol_id: str | None, week: float | None) -> bool:
    """True when *week* falls inside the protocol's approved window.

    An unknown protocol or unknown week returns ``False``. A timepoint that cannot be
    checked is never treated as matching — silently accepting it is precisely the
    ``TIMEPOINT_MISMATCH`` failure the hierarchy exists to surface.
    """
    window = approved_time_window(protocol_id)
    if window is None or week is None:
        return False
    return window[0] <= float(week) <= window[1]


def validate() -> list[str]:
    """Structural problems in analysis_protocols.yaml as human-readable strings.

    Called from ``config.taxonomy.validate_config`` so there is one startup entry point
    for all configuration validation.
    """
    errors: list[str] = []
    try:
        defined = protocols()
    except ProtocolError as e:
        return [str(e)]

    for protocol_id, definition in defined.items():
        definition = definition or {}
        where = f"analysis_protocols.yaml protocol {protocol_id!r}"

        missing = [f for f in _REQUIRED_FIELDS if definition.get(f) in (None, "")]
        if missing:
            errors.append(f"{where}: missing required field(s) {', '.join(missing)}")
            continue

        for field, allowed in _ENUM_FIELDS.items():
            value = definition.get(field)
            if value not in allowed:
                errors.append(
                    f"{where}: {field} {value!r} is not one of {', '.join(allowed)}"
                )

        outcome_id = definition["canonical_outcome_id"]
        outcome = outcomes.outcome(outcome_id)
        if outcome is None:
            errors.append(
                f"{where}: canonical_outcome_id {outcome_id!r} is not defined in "
                "canonical_outcomes.yaml"
            )
        else:
            errors.extend(_window_errors(where, definition, outcome, outcome_id))
            if definition["treatment_phase"] != outcome.get("treatment_phase"):
                errors.append(
                    f"{where}: treatment_phase {definition['treatment_phase']!r} "
                    f"disagrees with outcome {outcome_id!r} "
                    f"({outcome.get('treatment_phase')!r}). Induction and maintenance are "
                    "different outcomes, so this is never a harmless mismatch."
                )

        for field in ("min_studies", "min_nodes"):
            value = definition.get(field)
            if not isinstance(value, int) or value < 2:
                errors.append(f"{where}: {field} must be an integer >= 2, got {value!r}")

        stratum = definition.get("population_stratum")
        if stratum and outcomes.stratum(stratum) is None:
            errors.append(
                f"{where}: population_stratum {stratum!r} is not defined in "
                "canonical_outcomes.yaml"
            )

    return errors


def _window_errors(where: str, definition: dict, outcome: dict, outcome_id: str) -> list[str]:
    """The approved window must sit INSIDE the outcome's allowed window, never outside.

    The outcome file says what could legitimately count as this timepoint; the protocol
    says what this analysis will accept. Narrower is a statistical judgement. Wider would
    silently overrule the endpoint definition, admitting results the outcome itself
    rejects, so it is an error rather than a warning.
    """
    approved = definition.get("approved_time_window") or {}
    allowed = outcome.get("allowed_window") or {}
    a_lo, a_hi = approved.get("min_week"), approved.get("max_week")
    o_lo, o_hi = allowed.get("min_week"), allowed.get("max_week")

    if a_lo is None or a_hi is None:
        return [f"{where}: approved_time_window needs both min_week and max_week"]
    if a_lo > a_hi:
        return [f"{where}: approved_time_window min_week {a_lo} exceeds max_week {a_hi}"]
    if o_lo is None or o_hi is None:
        return []
    if a_lo < o_lo or a_hi > o_hi:
        return [
            f"{where}: approved_time_window [{a_lo}, {a_hi}] is wider than outcome "
            f"{outcome_id!r} allowed_window [{o_lo}, {o_hi}]. A protocol may narrow the "
            "window under statistical judgement but never widen it."
        ]
    return []
