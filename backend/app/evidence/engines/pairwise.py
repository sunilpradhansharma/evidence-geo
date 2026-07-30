"""Pairwise meta-analysis — the arithmetic under Levels 1 and 3 (Phase 6).

Two consumers. Level 1 pools direct head-to-head evidence; Bucher consumes the same pooled
contrasts as its two legs. One implementation, so a direct estimate and the direct leg of
an indirect estimate can never disagree because they were computed differently.

Everything here is pure and exact. No sidecar, no network, no randomness.

**Ratios are pooled on the log scale.** Averaging risk ratios directly is wrong: RR 0.5 and
RR 2.0 are equal and opposite effects, and their arithmetic mean is 1.25, not 1.0. So the
analysis scale is log for RR/OR/HR and the identity for RD/MD/SMD, and results are converted
back only for reporting.

Four places where the honest answer is "not estimable" rather than a number:

* **A single study has no estimable heterogeneity.** ``i_squared`` is ``None``, not 0.
  Reporting 0 would assert that homogeneity was assessed and confirmed.
* **A double-zero study carries no information about a ratio.** With no events in either
  arm there is no ratio to estimate, so it is excluded — and the exclusion is recorded,
  because silently dropping studies is how a network shrinks without anyone noticing.
* **A continuity correction is a change to the data.** It biases toward the null and
  slightly overstates precision, so every corrected study is flagged. Which correction to
  apply is the protocol's ``zero_event_policy``, never a default chosen here.
* **A zero-variance contrast cannot be weighted.** Inverse-variance weighting would divide
  by zero, so it is refused rather than approximated.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.evidence import protocols

# Measures analysed on the log scale. A ratio's sampling distribution is skewed on the
# natural scale and symmetric on the log scale, which is also what makes Bucher's
# subtraction valid.
_RATIO_MEASURES = frozenset({"risk_ratio", "odds_ratio", "hazard_ratio"})

# Which arm-data shape each measure requires. These string values intentionally equal
# `app.models.clinical_study.BINARY` / `CONTINUOUS`; a test pins that they stay in step.
# The constants are duplicated rather than imported so this module keeps no dependency on
# the ORM and stays pure.
BINARY_OUTCOME = "binary"
CONTINUOUS_OUTCOME = "continuous"

_BINARY_MEASURES = frozenset({"risk_ratio", "odds_ratio", "risk_difference"})
_CONTINUOUS_MEASURES = frozenset({"mean_difference", "standardised_mean_difference"})

FIXED = "fixed"
RANDOM = "random"

# Flags travel with the pooled result to the reviewer. Never silently resolved.
FLAG_CONTINUITY_CORRECTED = "CONTINUITY_CORRECTION_APPLIED"
FLAG_DOUBLE_ZERO_EXCLUDED = "DOUBLE_ZERO_STUDY_EXCLUDED"
FLAG_ZERO_EVENT_EXCLUDED = "ZERO_EVENT_STUDY_EXCLUDED"
FLAG_SINGLE_STUDY = "SINGLE_STUDY_NO_HETEROGENEITY_ESTIMABLE"
FLAG_HETEROGENEITY_HIGH = "HETEROGENEITY_ABOVE_THRESHOLD"
FLAG_ZERO_VARIANCE = "ZERO_VARIANCE_CONTRAST_EXCLUDED"

# 1.96 exactly would imply a normal quantile we never computed. This is that quantile.
Z_95 = 1.959963984540054


def is_ratio_measure(measure: str | None) -> bool:
    return (measure or "") in _RATIO_MEASURES


def outcome_type_for(measure: str | None) -> str:
    """The arm-data shape *measure* requires: ``binary`` or ``continuous``.

    Derived from the **protocol's** declared effect measure, never sniffed from the stored
    rows. Sniffing would let a single mistyped row change the analysis shape for a whole
    network, and would tell the sidecar something different from what an approver signed.

    An unrecognised measure raises rather than defaulting to binary. A wrong default here
    is silent: it would hand continuous arms to the binary engine and produce a risk ratio
    out of means.
    """
    normalised = (measure or "").strip()
    if normalised in _BINARY_MEASURES:
        return BINARY_OUTCOME
    if normalised in _CONTINUOUS_MEASURES:
        return CONTINUOUS_OUTCOME
    raise ValueError(
        f"{measure!r} is not a recognised effect measure; expected one of "
        + ", ".join(sorted(_BINARY_MEASURES | _CONTINUOUS_MEASURES))
    )


def to_analysis_scale(value: float, measure: str | None) -> float:
    """Map a reported effect onto the scale the pooling arithmetic assumes."""
    if is_ratio_measure(measure):
        if value <= 0:
            raise ValueError(f"a {measure} of {value} cannot be log-transformed")
        return math.log(value)
    return value


def from_analysis_scale(value: float, measure: str | None) -> float:
    """Map a pooled effect back to the scale a reader expects."""
    return math.exp(value) if is_ratio_measure(measure) else value


@dataclass(frozen=True)
class BinaryArm:
    """Events out of a sample, for one arm of one study."""

    treatment: str
    events: int
    sample_size: int

    @property
    def non_events(self) -> int:
        return max(0, self.sample_size - self.events)


@dataclass(frozen=True)
class ContinuousArm:
    treatment: str
    mean: float
    standard_deviation: float
    sample_size: int


@dataclass(frozen=True)
class StudyContrast:
    """One study's estimate of ``treatment`` versus ``comparator``, on the analysis scale.

    ``effect`` is a log ratio for ratio measures and a difference otherwise. Direction is
    part of the identity: swapping the pair negates the effect, which ``reversed()`` does
    explicitly rather than leaving to a caller.
    """

    study_id: str
    treatment: str
    comparator: str
    effect: float
    variance: float
    measure: str
    flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def standard_error(self) -> float:
        return math.sqrt(self.variance)

    @property
    def weight(self) -> float:
        """Fixed-effect inverse-variance weight."""
        return 1.0 / self.variance if self.variance > 0 else 0.0

    def reversed(self) -> StudyContrast:
        """The same evidence stated the other way round."""
        return StudyContrast(
            study_id=self.study_id,
            treatment=self.comparator,
            comparator=self.treatment,
            effect=-self.effect,
            variance=self.variance,
            measure=self.measure,
            flags=self.flags,
        )

    def oriented(self, treatment: str, comparator: str) -> StudyContrast | None:
        """This contrast expressed as *treatment* vs *comparator*, or ``None`` if unrelated."""
        if self.treatment == treatment and self.comparator == comparator:
            return self
        if self.treatment == comparator and self.comparator == treatment:
            return self.reversed()
        return None


@dataclass(frozen=True)
class PooledEffect:
    """A pooled contrast with its heterogeneity, on the analysis scale.

    ``estimate``/``ci_lower``/``ci_upper`` are on the analysis scale; the ``*_reported``
    properties convert for display. Both are exposed because Bucher must consume the
    analysis scale and a reader must see the reported one, and converting in the wrong
    direction is a silent, plausible-looking error.
    """

    treatment: str
    comparator: str
    measure: str
    model: str
    estimate: float
    standard_error: float
    study_count: int
    contributing_studies: tuple[str, ...] = field(default_factory=tuple)
    excluded_studies: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    tau_squared: float = 0.0
    q_statistic: float | None = None
    degrees_freedom: int | None = None
    i_squared: float | None = None
    flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ci_lower(self) -> float:
        return self.estimate - Z_95 * self.standard_error

    @property
    def ci_upper(self) -> float:
        return self.estimate + Z_95 * self.standard_error

    @property
    def estimate_reported(self) -> float:
        return from_analysis_scale(self.estimate, self.measure)

    @property
    def ci_lower_reported(self) -> float:
        return from_analysis_scale(self.ci_lower, self.measure)

    @property
    def ci_upper_reported(self) -> float:
        return from_analysis_scale(self.ci_upper, self.measure)

    @property
    def variance(self) -> float:
        return self.standard_error ** 2

    def as_dict(self) -> dict:
        return {
            "treatment": self.treatment,
            "comparator": self.comparator,
            "effect_measure": self.measure,
            "model": self.model,
            "estimate": self.estimate_reported,
            "ci_lower": self.ci_lower_reported,
            "ci_upper": self.ci_upper_reported,
            "interval_type": "CI",
            "estimate_log_scale": self.estimate if is_ratio_measure(self.measure) else None,
            "standard_error": self.standard_error,
            "study_count": self.study_count,
            "contributing_studies": list(self.contributing_studies),
            "excluded_studies": [
                {"study_id": s, "reason": r} for s, r in self.excluded_studies
            ],
            "tau_squared": self.tau_squared,
            "q_statistic": self.q_statistic,
            "degrees_freedom": self.degrees_freedom,
            "i_squared": self.i_squared,
            "flags": list(self.flags),
        }


# =====================================================================================
# Zero-event handling
# =====================================================================================
def _corrections(
    arm: BinaryArm, other: BinaryArm, policy: str
) -> tuple[float, float]:
    """``(correction_for_arm, correction_for_other)`` under *policy*.

    ``TREATMENT_ARM_CONTINUITY_CORRECTION`` is Sweeting's correction, where each arm's
    correction is proportional to the **reciprocal of the opposite arm's size**. Normalised
    so the two corrections sum to 1, that works out as proportional to the arm's *own*
    size: ``c_i = n_i / (n_1 + n_2)``.

    Why that form is the right one. The correction shifts each arm's observed risk by
    roughly ``c_i / n_i``, so making ``c_i`` proportional to ``n_i`` shifts **both** arms
    by the same ``1 / (n_1 + n_2)`` and therefore does not distort the ratio. A flat 0.5
    shifts the smaller arm more than the larger one, which is precisely the bias this
    exists to avoid — and inverting the proportionality would be worse than the flat 0.5
    rather than better. For balanced arms all three coincide at 0.5.
    """
    if policy == "FIXED_0_5_CORRECTION":
        return 0.5, 0.5
    total = arm.sample_size + other.sample_size
    if total <= 0:
        return 0.5, 0.5
    return arm.sample_size / total, other.sample_size / total


def _needs_correction(a: BinaryArm, b: BinaryArm, measure: str) -> bool:
    """True when a cell that must be non-zero is zero for this measure."""
    if measure == "risk_difference":
        return False
    if measure == "odds_ratio":
        return 0 in (a.events, b.events, a.non_events, b.non_events)
    return 0 in (a.events, b.events)


def binary_contrast(
    study_id: str,
    treatment_arm: BinaryArm,
    comparator_arm: BinaryArm,
    *,
    measure: str = "risk_ratio",
    zero_event_policy: str = "TREATMENT_ARM_CONTINUITY_CORRECTION",
) -> tuple[StudyContrast | None, str | None]:
    """``(contrast, exclusion_reason)`` for one binary comparison.

    Returns ``(None, reason)`` rather than raising, because a study that cannot contribute
    is a fact to report, not an error to handle. Every caller records the reason.
    """
    if treatment_arm.sample_size <= 0 or comparator_arm.sample_size <= 0:
        return None, FLAG_ZERO_VARIANCE

    ratio = measure in _RATIO_MEASURES
    # No events anywhere means there is no ratio to estimate. Excluded rather than
    # corrected: a correction here would manufacture an effect estimate of exactly 1 with
    # a finite confidence interval out of a study that observed nothing.
    if ratio and treatment_arm.events == 0 and comparator_arm.events == 0:
        return None, FLAG_DOUBLE_ZERO_EXCLUDED

    flags: list[str] = []
    a, n1 = float(treatment_arm.events), float(treatment_arm.sample_size)
    c, n2 = float(comparator_arm.events), float(comparator_arm.sample_size)

    if _needs_correction(treatment_arm, comparator_arm, measure):
        if zero_event_policy == "EXCLUDE_ZERO_EVENT_STUDIES":
            return None, FLAG_ZERO_EVENT_EXCLUDED
        k1, k2 = _corrections(treatment_arm, comparator_arm, zero_event_policy)
        a, n1 = a + k1, n1 + 2 * k1
        c, n2 = c + k2, n2 + 2 * k2
        flags.append(FLAG_CONTINUITY_CORRECTED)

    if measure == "risk_ratio":
        effect = math.log((a / n1) / (c / n2))
        variance = 1.0 / a - 1.0 / n1 + 1.0 / c - 1.0 / n2
    elif measure == "odds_ratio":
        b_cell, d_cell = n1 - a, n2 - c
        effect = math.log((a / b_cell) / (c / d_cell))
        variance = 1.0 / a + 1.0 / b_cell + 1.0 / c + 1.0 / d_cell
    elif measure == "risk_difference":
        p1, p2 = a / n1, c / n2
        effect = p1 - p2
        variance = p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2
    else:
        raise ValueError(
            f"{measure!r} is not a binary effect measure; use continuous_contrast for "
            "mean differences"
        )

    if variance <= 0:
        return None, FLAG_ZERO_VARIANCE

    return StudyContrast(
        study_id=study_id,
        treatment=treatment_arm.treatment,
        comparator=comparator_arm.treatment,
        effect=effect,
        variance=variance,
        measure=measure,
        flags=tuple(flags),
    ), None


def continuous_contrast(
    study_id: str,
    treatment_arm: ContinuousArm,
    comparator_arm: ContinuousArm,
    *,
    measure: str = "mean_difference",
) -> tuple[StudyContrast | None, str | None]:
    """``(contrast, exclusion_reason)`` for one continuous comparison.

    ``standardised_mean_difference`` is Hedges' g — the small-sample correction is applied
    rather than reporting Cohen's d, because trial arms are routinely small enough for the
    uncorrected statistic to be biased upward.
    """
    n1, n2 = treatment_arm.sample_size, comparator_arm.sample_size
    if n1 <= 0 or n2 <= 0:
        return None, FLAG_ZERO_VARIANCE
    s1, s2 = treatment_arm.standard_deviation, comparator_arm.standard_deviation
    if s1 is None or s2 is None or s1 < 0 or s2 < 0:
        return None, FLAG_ZERO_VARIANCE

    difference = treatment_arm.mean - comparator_arm.mean

    if measure == "mean_difference":
        effect = difference
        variance = (s1 ** 2) / n1 + (s2 ** 2) / n2
    elif measure == "standardised_mean_difference":
        df = n1 + n2 - 2
        if df <= 0:
            return None, FLAG_ZERO_VARIANCE
        pooled_sd = math.sqrt((((n1 - 1) * s1 ** 2) + ((n2 - 1) * s2 ** 2)) / df)
        if pooled_sd <= 0:
            return None, FLAG_ZERO_VARIANCE
        correction = 1.0 - (3.0 / (4.0 * df - 1.0))
        effect = correction * difference / pooled_sd
        variance = (n1 + n2) / (n1 * n2) + (effect ** 2) / (2.0 * (n1 + n2))
    else:
        raise ValueError(
            f"{measure!r} is not a continuous effect measure; use binary_contrast for "
            "risk ratios and odds ratios"
        )

    if variance <= 0:
        return None, FLAG_ZERO_VARIANCE

    return StudyContrast(
        study_id=study_id,
        treatment=treatment_arm.treatment,
        comparator=comparator_arm.treatment,
        effect=effect,
        variance=variance,
        measure=measure,
    ), None


# =====================================================================================
# Pooling
# =====================================================================================
def _dersimonian_laird(contrasts: list[StudyContrast], q: float, df: int) -> float:
    """Method-of-moments ``tau^2``. Truncated at zero, which is the standard convention.

    A negative moment estimate means the observed variation is *less* than sampling error
    alone predicts, which is not a possible between-study variance.
    """
    if df <= 0:
        return 0.0
    weights = [c.weight for c in contrasts]
    total = sum(weights)
    if total <= 0:
        return 0.0
    sum_squares = sum(w ** 2 for w in weights)
    denominator = total - sum_squares / total
    if denominator <= 0:
        return 0.0
    return max(0.0, (q - df) / denominator)


def heterogeneity_model(
    rule: str | None, i_squared: float | None, threshold: float = 50.0
) -> str:
    """The model the protocol's ``heterogeneity_rule`` selects.

    ``RANDOM_EFFECTS_IF_I2_ABOVE_50`` with an **unknown** I² selects random effects. A
    single study cannot demonstrate homogeneity, and defaulting to fixed effects there
    would assert an assumption the data cannot support.
    """
    if rule == "FIXED_EFFECTS_ALWAYS":
        return FIXED
    if rule == "RANDOM_EFFECTS_ALWAYS":
        return RANDOM
    if i_squared is None:
        return RANDOM
    return RANDOM if i_squared > threshold else FIXED


def pool(
    contrasts: list[StudyContrast],
    *,
    treatment: str,
    comparator: str,
    model: str = FIXED,
    heterogeneity_rule: str | None = None,
    excluded: list[tuple[str, str]] | None = None,
) -> PooledEffect | None:
    """Inverse-variance pool of every contrast reporting this pair, in either direction.

    Returns ``None`` when nothing contributes — the caller turns that into
    ``INSUFFICIENT_ARM_DATA`` with the exclusion reasons attached.

    When *heterogeneity_rule* is given it overrides *model*: the protocol decides the model
    from the observed I², and letting a caller pass a model alongside a rule would allow
    the protocol to be quietly bypassed.
    """
    oriented = [
        c for c in (x.oriented(treatment, comparator) for x in contrasts) if c is not None
    ]
    usable = [c for c in oriented if c.variance > 0]
    dropped = list(excluded or [])
    dropped += [(c.study_id, FLAG_ZERO_VARIANCE) for c in oriented if c.variance <= 0]

    if not usable:
        return None

    measure = usable[0].measure
    if any(c.measure != measure for c in usable):
        raise ValueError(
            "cannot pool contrasts on different effect measures: "
            + ", ".join(sorted({c.measure for c in usable}))
        )

    flags: list[str] = sorted({f for c in usable for f in c.flags})

    weights = [c.weight for c in usable]
    total_weight = sum(weights)
    fixed_estimate = sum(w * c.effect for w, c in zip(weights, usable)) / total_weight

    df = len(usable) - 1
    if df > 0:
        q = sum(w * (c.effect - fixed_estimate) ** 2 for w, c in zip(weights, usable))
        i_squared = max(0.0, (q - df) / q * 100.0) if q > 0 else 0.0
        tau_squared = _dersimonian_laird(usable, q, df)
    else:
        # One study: heterogeneity is not estimable. None, not zero — see the docstring.
        q, i_squared, tau_squared = None, None, 0.0
        flags.append(FLAG_SINGLE_STUDY)

    chosen = heterogeneity_model(heterogeneity_rule, i_squared) if heterogeneity_rule else model

    if chosen == RANDOM and tau_squared > 0:
        adjusted = [1.0 / (c.variance + tau_squared) for c in usable]
        total_adjusted = sum(adjusted)
        estimate = sum(w * c.effect for w, c in zip(adjusted, usable)) / total_adjusted
        standard_error = math.sqrt(1.0 / total_adjusted)
    else:
        # Random effects with tau^2 == 0 is arithmetically identical to fixed effects.
        estimate = fixed_estimate
        standard_error = math.sqrt(1.0 / total_weight)

    if i_squared is not None and i_squared > 50.0:
        flags.append(FLAG_HETEROGENEITY_HIGH)

    return PooledEffect(
        treatment=treatment,
        comparator=comparator,
        measure=measure,
        model=chosen,
        estimate=estimate,
        standard_error=standard_error,
        study_count=len(usable),
        contributing_studies=tuple(sorted(c.study_id for c in usable)),
        excluded_studies=tuple(sorted(set(dropped))),
        tau_squared=tau_squared,
        q_statistic=q,
        degrees_freedom=df if df > 0 else None,
        i_squared=i_squared,
        flags=tuple(dict.fromkeys(flags)),
    )


def zero_event_policy_of(protocol_id: str | None) -> str:
    """The protocol's zero-event policy, or the conservative default.

    Defaulting to the treatment-arm correction rather than the fixed 0.5 matters for
    unbalanced designs, which are common where an active comparator arm is smaller than
    placebo.
    """
    definition = protocols.protocol(protocol_id) or {}
    policy = definition.get("zero_event_policy")
    return policy if policy in protocols.ZERO_EVENT_POLICIES else "TREATMENT_ARM_CONTINUITY_CORRECTION"
