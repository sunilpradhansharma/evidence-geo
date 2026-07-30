"""Full network meta-analysis via the R ``netmeta`` sidecar (Phase 6, Level 3).

**Why a sidecar rather than Python.** A graph-theoretic NMA is not hard to code and is very
hard to code *correctly* — multi-arm correlation structure, net-splitting, SUCRA by
simulation. ``netmeta`` is validated, cited in the HTA submissions our results will be
compared against, and reports a package version a statistical reviewer can check. A
hand-rolled implementation would ask reviewers to trust our arithmetic over a reference
package, which is not a trade worth making for a number that ends up in a promotional
review. So this module is a **wire contract**, not an algorithm.

**The multi-arm structure must survive the wire.** A three-arm trial sent as three
independent pairwise rows double-counts its control group and understates every standard
error involving it. ``build_request`` therefore transmits *arms grouped by study* and never
flattens, which is the same reason ``OutcomeResult`` is stored arm-level.

**Never raises.** Mirrors ``sources/base.get_json``: a timeout, a 500 or a malformed body
all return ``NetmetaResponse(ok=False)`` carrying ``NMA_SERVICE_UNAVAILABLE``. A sidecar
outage is a *transient service* status and must be distinguishable from a *structured
evidence gap* — the first says retry, the second says this comparison is not estimable, and
conflating them would let an infrastructure blip masquerade as a finding about the evidence.

The sidecar is not required to exist for this module's tests: ``build_request`` and
``parse_response`` are pure, and only ``run`` touches the wire.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.evidence import statuses
from app.evidence.engines.pairwise import FIXED, RANDOM, Z_95, is_ratio_measure

logger = logging.getLogger(__name__)

ENGINE = "NETMETA"
ENGINE_VERSION = "1.0.0"
# The wire contract's own version. Bumped when the request or response shape changes, so a
# sidecar built against an older contract fails loudly instead of misreading fields.
CONTRACT_VERSION = "1"

DEFAULT_TIMEOUT = 120.0

FLAG_INCONSISTENCY_NOT_ASSESSABLE = "INCONSISTENCY_NOT_ASSESSABLE_NO_INDEPENDENT_LOOPS"
FLAG_MULTI_ARM_PRESENT = "MULTI_ARM_STUDIES_PRESENT"
FLAG_SIDECAR_DEGRADED = "SIDECAR_RETURNED_PARTIAL_RESULT"


@dataclass(frozen=True)
class ArmPayload:
    """One arm, as transmitted. Binary and continuous fields are mutually exclusive."""

    treatment: str
    sample_size: int | None = None
    events: int | None = None
    mean: float | None = None
    standard_deviation: float | None = None
    administration_route: str | None = None

    def as_dict(self) -> dict:
        return {
            k: v
            for k, v in {
                "treatment": self.treatment,
                "sample_size": self.sample_size,
                "events": self.events,
                "mean": self.mean,
                "standard_deviation": self.standard_deviation,
                "administration_route": self.administration_route,
            }.items()
            if v is not None
        }


@dataclass(frozen=True)
class StudyPayload:
    """One study and all of its arms, kept together so correlation survives the wire."""

    study_id: str
    arms: tuple[ArmPayload, ...]
    risk_of_bias: str | None = None

    @property
    def is_multi_arm(self) -> bool:
        return len(self.arms) > 2

    def as_dict(self) -> dict:
        payload: dict[str, Any] = {
            "study_id": self.study_id,
            "arms": [a.as_dict() for a in self.arms],
        }
        if self.risk_of_bias:
            payload["risk_of_bias"] = self.risk_of_bias
        return payload


@dataclass(frozen=True)
class NetmetaRequest:
    """Everything the sidecar needs, and nothing it should decide for itself.

    Every statistical choice is transmitted explicitly from the protocol. The sidecar has
    no defaults of its own by design: a default applied on the far side of a wire is a
    methodology decision nobody approved and nobody can see.
    """

    outcome_type: str  # binary | continuous
    effect_measure: str
    model: str  # fixed | random
    reference_treatment: str
    studies: tuple[StudyPayload, ...]
    zero_event_policy: str = "TREATMENT_ARM_CONTINUITY_CORRECTION"
    inconsistency_rule: str | None = None
    protocol_id: str | None = None
    protocol_hash: str | None = None

    @property
    def multi_arm_studies(self) -> tuple[str, ...]:
        return tuple(s.study_id for s in self.studies if s.is_multi_arm)

    def as_dict(self) -> dict:
        return {
            "contract_version": CONTRACT_VERSION,
            "outcome_type": self.outcome_type,
            "effect_measure": self.effect_measure,
            "model": self.model,
            "reference_treatment": self.reference_treatment,
            "zero_event_policy": self.zero_event_policy,
            "inconsistency_rule": self.inconsistency_rule,
            "protocol_id": self.protocol_id,
            "protocol_hash": self.protocol_hash,
            "studies": [s.as_dict() for s in self.studies],
        }


@dataclass(frozen=True)
class NetworkContrast:
    """One cell of the sidecar's league table, as returned."""

    treatment: str
    comparator: str
    estimate: float
    ci_lower: float | None = None
    ci_upper: float | None = None
    standard_error: float | None = None
    # Present only where the network has an independent loop to split on.
    direct_estimate: float | None = None
    indirect_estimate: float | None = None
    net_split_p_value: float | None = None

    def as_dict(self) -> dict:
        return {
            "treatment": self.treatment,
            "comparator": self.comparator,
            "estimate": self.estimate,
            "ci_lower": self.ci_lower,
            "ci_upper": self.ci_upper,
            "interval_type": "CI",
            "standard_error": self.standard_error,
            "direct_estimate": self.direct_estimate,
            "indirect_estimate": self.indirect_estimate,
            "net_split_p_value": self.net_split_p_value,
        }


@dataclass(frozen=True)
class NetmetaResponse:
    """The sidecar's answer, or the reason there is not one.

    ``ok=False`` is a normal value carrying ``status = NMA_SERVICE_UNAVAILABLE``. Callers
    branch on it; nobody wraps this in try/except.
    """

    ok: bool
    status: str | None = None
    reason: str | None = None
    effect_measure: str | None = None
    model: str | None = None
    reference_treatment: str | None = None
    package_version: str | None = None
    contrasts: tuple[NetworkContrast, ...] = field(default_factory=tuple)
    sucra: dict[str, float] = field(default_factory=dict)
    tau_squared: float | None = None
    q_statistic: float | None = None
    degrees_freedom: int | None = None
    i_squared: float | None = None
    inconsistency: dict | None = None
    flags: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def unavailable(cls, reason: str) -> NetmetaResponse:
        """A transient infrastructure failure, explicitly not an evidence gap."""
        return cls(ok=False, status=statuses.NMA_SERVICE_UNAVAILABLE, reason=reason)

    def contrast_for(self, treatment: str, comparator: str) -> NetworkContrast | None:
        """The league-table cell for this pair, in either direction.

        A reversed cell is returned reversed — on a ratio scale that means the reciprocal
        and a swapped interval, which is done here rather than left to a caller who might
        forget the interval.
        """
        for contrast in self.contrasts:
            if contrast.treatment == treatment and contrast.comparator == comparator:
                return contrast
            if contrast.treatment == comparator and contrast.comparator == treatment:
                return _invert(contrast, self.effect_measure)
        return None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "status": self.status,
            "reason": self.reason,
            "effect_measure": self.effect_measure,
            "model": self.model,
            "reference_treatment": self.reference_treatment,
            "engine": ENGINE,
            "engine_version": ENGINE_VERSION,
            "package_version": self.package_version,
            "contrasts": [c.as_dict() for c in self.contrasts],
            "sucra": dict(self.sucra),
            "tau_squared": self.tau_squared,
            "q_statistic": self.q_statistic,
            "degrees_freedom": self.degrees_freedom,
            "i_squared": self.i_squared,
            "inconsistency": self.inconsistency,
            "flags": list(self.flags),
        }


def _invert(contrast: NetworkContrast, measure: str | None) -> NetworkContrast:
    """The same cell stated the other way round.

    Ratios invert by reciprocal and the interval bounds swap; differences negate. Getting
    the bound swap wrong produces an interval that silently excludes its own estimate.
    """
    if is_ratio_measure(measure):
        return NetworkContrast(
            treatment=contrast.comparator,
            comparator=contrast.treatment,
            estimate=1.0 / contrast.estimate if contrast.estimate else contrast.estimate,
            ci_lower=1.0 / contrast.ci_upper if contrast.ci_upper else None,
            ci_upper=1.0 / contrast.ci_lower if contrast.ci_lower else None,
            standard_error=contrast.standard_error,
        )
    return NetworkContrast(
        treatment=contrast.comparator,
        comparator=contrast.treatment,
        estimate=-contrast.estimate,
        ci_lower=-contrast.ci_upper if contrast.ci_upper is not None else None,
        ci_upper=-contrast.ci_lower if contrast.ci_lower is not None else None,
        standard_error=contrast.standard_error,
    )


# =====================================================================================
# Building the request
# =====================================================================================
def build_request(
    studies: dict[str, list[ArmPayload]],
    *,
    outcome_type: str,
    effect_measure: str,
    model: str = FIXED,
    reference_treatment: str = "Placebo",
    zero_event_policy: str = "TREATMENT_ARM_CONTINUITY_CORRECTION",
    inconsistency_rule: str | None = None,
    protocol_id: str | None = None,
    protocol_hash: str | None = None,
    risk_of_bias: dict[str, str] | None = None,
) -> NetmetaRequest:
    """Assemble a sidecar request from ``{study_id: [arms]}``. Pure; no I/O.

    Studies with fewer than two arms are dropped — a single-arm record contributes no
    comparison — and multi-arm studies are transmitted whole.
    """
    payloads = tuple(
        StudyPayload(
            study_id=study_id,
            arms=tuple(arms),
            risk_of_bias=(risk_of_bias or {}).get(study_id),
        )
        for study_id, arms in sorted(studies.items())
        if len({a.treatment for a in arms}) >= 2
    )
    if model not in (FIXED, RANDOM):
        raise ValueError(f"model must be {FIXED!r} or {RANDOM!r}, got {model!r}")

    return NetmetaRequest(
        outcome_type=outcome_type,
        effect_measure=effect_measure,
        model=model,
        reference_treatment=reference_treatment,
        studies=payloads,
        zero_event_policy=zero_event_policy,
        inconsistency_rule=inconsistency_rule,
        protocol_id=protocol_id,
        protocol_hash=protocol_hash,
    )


# =====================================================================================
# Reading the response
# =====================================================================================
def _as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_response(payload: object, *, independent_loop_count: int = 0) -> NetmetaResponse:
    """Map the sidecar's JSON onto a ``NetmetaResponse``. Pure; no I/O.

    A response missing its league table is treated as unavailable rather than as an empty
    result: an NMA that returned no contrasts did not succeed, and reporting it as a
    successful empty answer would read as "no difference found".
    """
    if not isinstance(payload, dict):
        return NetmetaResponse.unavailable("sidecar returned a non-object body")

    rows = payload.get("contrasts")
    if not isinstance(rows, list) or not rows:
        return NetmetaResponse.unavailable(
            "sidecar returned no league table; an NMA with no contrasts did not succeed"
        )

    measure = payload.get("effect_measure")
    contrasts = tuple(
        NetworkContrast(
            treatment=str(row.get("treatment", "")),
            comparator=str(row.get("comparator", "")),
            estimate=_as_float(row.get("estimate")) or 0.0,
            ci_lower=_as_float(row.get("ci_lower")),
            ci_upper=_as_float(row.get("ci_upper")),
            standard_error=_as_float(row.get("standard_error")),
            direct_estimate=_as_float(row.get("direct_estimate")),
            indirect_estimate=_as_float(row.get("indirect_estimate")),
            net_split_p_value=_as_float(row.get("net_split_p_value")),
        )
        for row in rows
        if isinstance(row, dict) and row.get("treatment") and row.get("comparator")
    )
    if not contrasts:
        return NetmetaResponse.unavailable("sidecar league table had no usable rows")

    flags: list[str] = []
    inconsistency = payload.get("inconsistency")
    # A network with no independent loop cannot be tested for inconsistency at all. If the
    # sidecar reports one anyway it is describing within-study loops, which are correlated
    # and prove nothing — so the claim is dropped and the limitation recorded instead.
    if independent_loop_count <= 0:
        if inconsistency:
            logger.debug("discarding inconsistency result: no independent loops in network")
        inconsistency = None
        flags.append(FLAG_INCONSISTENCY_NOT_ASSESSABLE)

    sucra_raw = payload.get("sucra")
    sucra: dict[str, float] = {}
    if isinstance(sucra_raw, dict):
        for treatment, score in sucra_raw.items():
            value = _as_float(score)
            if value is not None:
                sucra[str(treatment)] = value / 100.0 if value > 1.0 else value

    if not payload.get("package_version"):
        # Without it a reviewer cannot reproduce the run, so the result is degraded even
        # though the numbers arrived.
        flags.append(FLAG_SIDECAR_DEGRADED)

    return NetmetaResponse(
        ok=True,
        status=statuses.INTERNAL_NMA_COMPLETED,
        effect_measure=measure,
        model=payload.get("model"),
        reference_treatment=payload.get("reference_treatment"),
        package_version=payload.get("package_version"),
        contrasts=contrasts,
        sucra=sucra,
        tau_squared=_as_float(payload.get("tau_squared")),
        q_statistic=_as_float(payload.get("q_statistic")),
        degrees_freedom=int(payload["degrees_freedom"])
        if _as_float(payload.get("degrees_freedom")) is not None else None,
        i_squared=_as_float(payload.get("i_squared")),
        inconsistency=inconsistency if isinstance(inconsistency, dict) else None,
        flags=tuple(dict.fromkeys(flags)),
    )


# =====================================================================================
# The wire
# =====================================================================================
async def run(
    request: NetmetaRequest,
    *,
    base_url: str,
    independent_loop_count: int = 0,
    timeout: float = DEFAULT_TIMEOUT,
) -> NetmetaResponse:
    """POST to the sidecar and read its answer. **Never raises.**

    Every failure mode — unreachable, timeout, 4xx, 5xx, malformed body — becomes
    ``NMA_SERVICE_UNAVAILABLE``, which is a *retry* signal and never an evidence gap.
    """
    url = base_url.rstrip("/") + "/nma"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=request.as_dict())
    except httpx.HTTPError as e:
        logger.warning("netmeta sidecar transport failure: %s", e)
        return NetmetaResponse.unavailable(f"transport error: {e}")

    if response.status_code >= 400:
        return NetmetaResponse.unavailable(f"sidecar returned HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as e:
        return NetmetaResponse.unavailable(f"malformed JSON from sidecar: {e}")

    return parse_response(payload, independent_loop_count=independent_loop_count)


def select_engine(model_selection_rule: str | None, topology) -> str:
    """``BUCHER`` or ``NETMETA`` under the protocol's rule.

    The rule comes from the approved protocol and the facts come from
    ``evidence.topology`` — so the engine is never chosen by whichever is convenient or
    whichever gives the preferred answer.

    ``NETMETA_IF_LOOPS_OR_MULTI_ARM_ELSE_BUCHER`` sends anything with a closed loop or a
    multi-arm trial to ``netmeta``, because Bucher can represent neither: it cannot
    reconcile a loop's competing paths and it cannot handle within-study correlation.
    """
    if model_selection_rule == "ALWAYS_NETMETA":
        return ENGINE
    if model_selection_rule == "ALWAYS_BUCHER":
        return "BUCHER"
    if topology.has_closed_loops or topology.has_multi_arm_studies:
        return ENGINE
    return "BUCHER"


def z_interval(estimate: float, standard_error: float) -> tuple[float, float]:
    """95% interval on the analysis scale, using the same quantile as ``pairwise``."""
    return estimate - Z_95 * standard_error, estimate + Z_95 * standard_error
