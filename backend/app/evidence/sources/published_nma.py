"""Published synthesis adapter — league table, SUCRA and GRADE extraction (Phase 4).

Ingest published syntheses **before** deciding whether computation is needed. A published
NMA that already answers the question is Level 2 in the hierarchy and outranks anything we
would compute ourselves, so this adapter runs ahead of the engines rather than beside them.

**Why there is no ``fetch``.** Published NMAs live in journal PDFs. No API returns a
structured league table, so retrieval is either PubMed metadata (``sources/pubmed.py``,
already built) or a governed manual upload. What arrives here is a *normalised extraction
record* — produced by a curator or the 3A pipeline — and this module's job is to reconcile
the many shapes those records take and to refuse the ones that cannot be trusted. ``parse``
is pure, so every test runs offline.

**Heterogeneous by necessity.** The same league table is published as a triangular matrix,
as a list of contrasts, or as effects against a single common reference; the ranking metric
is called SUCRA or P-score and is scaled 0-1 or 0-100; the interval is a CI or a CrI. All of
that is normalised here so downstream code sees one shape.

Four things this module will not do, because each would misrepresent the source:

* **Never conflate a CrI with a CI.** A Bayesian credible interval and a frequentist
  confidence interval support different statements. ``interval_type`` is preserved, and
  when the record does not say, it is flagged rather than assumed.
* **Never infer the effect measure from magnitude.** An estimate near 1.0 could be a risk
  ratio or an odds ratio, and guessing wrong inverts clinical conclusions.
* **Never invent a missing interval.** An estimate with no interval stays intervalless and
  carries a flag; a fabricated interval would imply precision nobody reported.
* **Never silently reverse a contrast.** "RR 1.4 Rinvoq vs Humira" and the same number for
  Humira vs Rinvoq are reciprocals, so direction is preserved exactly as extracted and
  flagged when the record leaves it ambiguous.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from app.config import outcomes
from app.evidence import protocols
from app.evidence.endpoints import match_endpoint, parse_timepoint_weeks
from app.evidence.treatments import canonical_treatment
from app.models.nma_result import PUBLISHED, NMAResult

SOURCE_TYPES = ("COCHRANE", "HTA", "JOURNAL", "SOCIETY_ABSTRACT", "MANUAL_UPLOAD")

# Ranking metrics. SUCRA and P-score are numerically near-identical but not the same
# quantity, so the name is recorded rather than normalised to one label.
SUCRA = "SUCRA"
P_SCORE = "P_SCORE"
RANKING_METRICS = (SUCRA, P_SCORE)

# Interval types. Kept distinct on purpose — see the module docstring.
CONFIDENCE_INTERVAL = "CI"
CREDIBLE_INTERVAL = "CrI"

# Flags travel with the row to curation. Never silently resolved.
FLAG_NO_EFFECT_MEASURE = "EFFECT_MEASURE_NOT_STATED"
FLAG_NO_INTERVAL = "INTERVAL_NOT_REPORTED"
FLAG_INTERVAL_TYPE_UNSTATED = "INTERVAL_TYPE_NOT_STATED"
FLAG_ESTIMATE_NOT_PARSED = "ESTIMATE_NOT_PARSED"
FLAG_DIRECTION_UNSTATED = "CONTRAST_DIRECTION_UNSTATED"
FLAG_STUDIES_NOT_RECOVERABLE = "INCLUDED_STUDIES_NOT_RECOVERABLE"
FLAG_ENDPOINT_NOT_CANONICAL = "ENDPOINT_NOT_CANONICAL"
FLAG_NO_TIMEPOINT = "TIMEPOINT_NOT_PARSED"
FLAG_NO_MODEL_TYPE = "MODEL_TYPE_NOT_STATED"
FLAG_RANKING_RESCALED = "RANKING_SCORES_RESCALED_FROM_PERCENT"
FLAG_NO_HETEROGENEITY = "HETEROGENEITY_NOT_REPORTED"
FLAG_NO_INCONSISTENCY_ASSESSMENT = "INCONSISTENCY_NOT_ASSESSED"

# Effect-measure synonyms as journals write them. Values are the canonical vocabulary in
# ``protocols.EFFECT_MEASURES`` so a published result and a computed one are comparable.
_EFFECT_MEASURES = {
    "rr": "risk_ratio", "riskratio": "risk_ratio", "risk ratio": "risk_ratio",
    "relative risk": "risk_ratio",
    "or": "odds_ratio", "oddsratio": "odds_ratio", "odds ratio": "odds_ratio",
    "rd": "risk_difference", "risk difference": "risk_difference",
    "md": "mean_difference", "mean difference": "mean_difference",
    "smd": "standardised_mean_difference",
    "standardised mean difference": "standardised_mean_difference",
    "standardized mean difference": "standardised_mean_difference",
    "hr": "hazard_ratio", "hazard ratio": "hazard_ratio",
}

_MODEL_TYPES = {
    "random": "random", "random effects": "random", "random-effects": "random",
    "re": "random", "fixed": "fixed", "fixed effects": "fixed",
    "fixed-effect": "fixed", "fixed-effects": "fixed", "fe": "fixed", "common": "fixed",
}

# Key aliases per field. Journals and extractors disagree on naming; the reconciliation
# lives in one table so a new source shape is a data change, not a code change.
_ESTIMATE_KEYS = ("estimate", "effect", "value", "point_estimate", "rr", "or", "hr", "md", "smd")
_LOWER_KEYS = ("lower", "ci_lower", "lcl", "l95", "lower_ci", "lower_cri", "ci_low")
_UPPER_KEYS = ("upper", "ci_upper", "ucl", "u95", "upper_ci", "upper_cri", "ci_high")
_INTERVAL_KEYS = ("ci", "crl", "cri", "credible_interval", "confidence_interval", "interval")
_CREDIBLE_HINT = re.compile(r"cr[il]|credible|bayes", re.IGNORECASE)
_CONFIDENCE_HINT = re.compile(r"\bci\b|confidence", re.IGNORECASE)


def _first(mapping: dict, keys: tuple[str, ...]):
    """First present, non-null value among *keys*, matched case-insensitively."""
    lowered = {str(k).strip().lower(): v for k, v in mapping.items()}
    for key in keys:
        if lowered.get(key) is not None:
            return lowered[key]
    return None


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def normalise_effect_measure(raw: object) -> str | None:
    """Canonical effect measure, or ``None`` when the record does not state one.

    Deliberately returns ``None`` rather than a default: an unstated effect measure is a
    gap to flag, and guessing one from the estimate's magnitude inverts conclusions.
    """
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in protocols.EFFECT_MEASURES:
        return text
    return _EFFECT_MEASURES.get(text)


def normalise_model_type(raw: object) -> str | None:
    if raw is None:
        return None
    return _MODEL_TYPES.get(str(raw).strip().lower())


@dataclass(frozen=True)
class Contrast:
    """One cell of a league table, with its direction preserved.

    ``estimate`` is the effect of ``treatment`` relative to ``comparator``. Reversing them
    means taking a reciprocal, so the pair is never reordered for convenience.
    """

    treatment: str
    comparator: str
    effect_measure: str | None = None
    estimate: float | None = None
    interval_lower: float | None = None
    interval_upper: float | None = None
    interval_type: str | None = None
    # Exactly what the source printed, before dose and route were stripped to form the
    # node name. Retained because dose materiality cannot be judged from a node: whether
    # two doses are one node is a `dose_policy` decision, so the label is the only record
    # of which dose the published estimate actually describes.
    treatment_label: str | None = None
    comparator_label: str | None = None
    flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_interval(self) -> bool:
        return self.interval_lower is not None and self.interval_upper is not None

    def as_dict(self) -> dict:
        return {
            "treatment": self.treatment,
            "comparator": self.comparator,
            "effect_measure": self.effect_measure,
            "estimate": self.estimate,
            "interval_lower": self.interval_lower,
            "interval_upper": self.interval_upper,
            "interval_type": self.interval_type,
            "treatment_label": self.treatment_label,
            "comparator_label": self.comparator_label,
            "flags": list(self.flags),
        }


@dataclass(frozen=True)
class ParsedSynthesis:
    """A published synthesis normalised onto the canonical vocabulary.

    ``problems`` are reasons this record cannot be trusted as evidence at all; ``flags``
    are gaps a curator must see but which do not by themselves disqualify it. Keeping them
    separate is what lets the upload path reject the first and queue the second.
    """

    source_type: str
    source_identifier: str | None = None
    citation: str | None = None
    publication_date: date | None = None
    funding_source: str | None = None

    indication: str | None = None
    endpoint: str | None = None
    canonical_outcome_id: str | None = None
    timepoint_week: float | None = None
    population_stratum: str | None = None
    treatment_phase: str = "PRIMARY"

    treatments: tuple[str, ...] = field(default_factory=tuple)
    contrasts: tuple[Contrast, ...] = field(default_factory=tuple)
    effect_measure: str | None = None
    model_type: str | None = None

    ranking_metric: str | None = None
    ranking_scores: dict[str, float] = field(default_factory=dict)

    tau_squared: float | None = None
    q_statistic: float | None = None
    degrees_freedom: int | None = None
    heterogeneity_note: str | None = None
    inconsistency: dict | None = None
    grade_certainty: str | None = None

    included_studies: tuple[str, ...] = field(default_factory=tuple)
    included_studies_recoverable: bool = False

    flags: tuple[str, ...] = field(default_factory=tuple)
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def usable(self) -> bool:
        """True when the record is coherent enough to be stored as evidence.

        Flags do not block; problems do. A synthesis missing its included-study list is
        still stored — the list's absence is what the Level-2 gate refuses on later, and
        discarding the record here would lose the citation a reviewer needs.
        """
        return not self.problems


# =====================================================================================
# League-table shape reconciliation
# =====================================================================================
# "1.40 (1.10 to 1.80)", "0.85 [0.70, 1.03]", "-0.50 (-1.20 to 0.20)".
#
# A bare hyphen is deliberately NOT accepted as the bound separator: in "-0.50 (-1.20-0.20)"
# it cannot be distinguished from the sign of a negative bound, and mean differences are
# routinely negative. Sources using a bare hyphen must be normalised upstream rather than
# guessed at here.
_CELL_RE = re.compile(
    r"^\s*(?P<estimate>-?\d+(?:\.\d+)?)\s*"
    r"[\(\[]\s*(?P<lower>-?\d+(?:\.\d+)?)\s*(?:,|;|to|\u2013|\u2014)\s*"
    r"(?P<upper>-?\d+(?:\.\d+)?)\s*[\)\]]",
    re.IGNORECASE,
)


def _first_with_key(mapping: dict, keys: tuple[str, ...]) -> tuple[object, str | None]:
    """First present value among *keys*, plus the key that matched.

    The matched key is what lets an interval's type be read from its own name — a value
    under ``ci_lower`` and one under ``lower_cri`` mean different things.
    """
    lowered = {str(k).strip().lower(): v for k, v in mapping.items()}
    for key in keys:
        if lowered.get(key) is not None:
            return lowered[key], key
    return None, None


def _interval_type_from(*hints: object) -> str | None:
    """CI or CrI, read from explicit fields or key names. ``None`` when unstated."""
    for hint in hints:
        text = str(hint or "")
        if _CREDIBLE_HINT.search(text):
            return CREDIBLE_INTERVAL
        if _CONFIDENCE_HINT.search(text):
            return CONFIDENCE_INTERVAL
    return None


def _parse_cell(cell: object) -> tuple[float | None, float | None, float | None, str | None]:
    """``(estimate, lower, upper, interval_type_hint)`` from one league-table cell.

    Accepts a dict, a printed string like ``1.40 (1.10 to 1.80)``, or a bare number.
    """
    if isinstance(cell, dict):
        estimate_raw, _ = _first_with_key(cell, _ESTIMATE_KEYS)
        lower_raw, lower_key = _first_with_key(cell, _LOWER_KEYS)
        upper_raw, _ = _first_with_key(cell, _UPPER_KEYS)
        interval_raw, interval_key = _first_with_key(cell, _INTERVAL_KEYS)

        lower, upper = _to_float(lower_raw), _to_float(upper_raw)
        if (lower is None or upper is None) and isinstance(interval_raw, (list, tuple)):
            if len(interval_raw) == 2:
                lower, upper = _to_float(interval_raw[0]), _to_float(interval_raw[1])
        if (lower is None or upper is None) and isinstance(interval_raw, str):
            match = _CELL_RE.search(f"0 ({interval_raw})") or None
            if match:
                lower, upper = _to_float(match["lower"]), _to_float(match["upper"])

        hint = _interval_type_from(cell.get("interval_type"), interval_key, lower_key)
        return _to_float(estimate_raw), lower, upper, hint

    if isinstance(cell, str):
        match = _CELL_RE.search(cell)
        if match:
            return (
                _to_float(match["estimate"]),
                _to_float(match["lower"]),
                _to_float(match["upper"]),
                _interval_type_from(cell),
            )
        return _to_float(cell), None, None, None

    return _to_float(cell), None, None, None


def _build_contrast(
    treatment: object, comparator: object, cell: object, *,
    default_measure: str | None, default_interval_type: str | None,
) -> Contrast:
    """One normalised contrast, with every gap flagged rather than filled."""
    node, _ = canonical_treatment(str(treatment or ""))
    against, _ = canonical_treatment(str(comparator or ""))
    estimate, lower, upper, type_hint = _parse_cell(cell)

    measure = default_measure
    if isinstance(cell, dict):
        # Precedence: an explicit field, then the estimate's own key name, then the
        # record-level default. Reading "rr" off the key is not inference from magnitude —
        # the source has named the measure, just structurally rather than in a field.
        _, estimate_key = _first_with_key(cell, _ESTIMATE_KEYS)
        measure = (
            normalise_effect_measure(_first(cell, ("effect_measure", "measure", "scale")))
            or _EFFECT_MEASURES.get(estimate_key or "")
            or default_measure
        )

    flags: list[str] = []
    if estimate is None:
        flags.append(FLAG_ESTIMATE_NOT_PARSED)
    if lower is None or upper is None:
        flags.append(FLAG_NO_INTERVAL)
    if measure is None:
        flags.append(FLAG_NO_EFFECT_MEASURE)
    if not node or not against:
        flags.append(FLAG_DIRECTION_UNSTATED)

    interval_type = type_hint or default_interval_type
    if (lower is not None and upper is not None) and interval_type is None:
        flags.append(FLAG_INTERVAL_TYPE_UNSTATED)

    return Contrast(
        treatment=node, comparator=against, effect_measure=measure, estimate=estimate,
        interval_lower=lower, interval_upper=upper, interval_type=interval_type,
        treatment_label=str(treatment or "").strip() or None,
        comparator_label=str(comparator or "").strip() or None,
        flags=tuple(flags),
    )


def contrasts_from_matrix(
    matrix: dict, *, default_measure: str | None = None,
    default_interval_type: str | None = None,
) -> tuple[Contrast, ...]:
    """League table published as ``{treatment: {comparator: cell}}``."""
    out: list[Contrast] = []
    for treatment, row in (matrix or {}).items():
        for comparator, cell in (row or {}).items():
            out.append(_build_contrast(
                treatment, comparator, cell,
                default_measure=default_measure,
                default_interval_type=default_interval_type,
            ))
    return tuple(out)


def contrasts_from_rows(
    rows: list, *, default_measure: str | None = None,
    default_interval_type: str | None = None, common_reference: object = None,
) -> tuple[Contrast, ...]:
    """League table published as a list of contrast records.

    *common_reference* covers the frequent case of a table reporting every treatment
    against one reference (usually placebo) without naming it on each row.
    """
    out: list[Contrast] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        treatment = _first(row, ("treatment", "intervention", "arm", "node", "drug"))
        comparator = _first(
            row, ("comparator", "reference", "control", "versus", "vs", "against")
        ) or common_reference
        out.append(_build_contrast(
            treatment, comparator, row,
            default_measure=default_measure,
            default_interval_type=default_interval_type,
        ))
    return tuple(out)


def normalise_ranking(raw: object) -> tuple[str | None, dict[str, float], tuple[str, ...]]:
    """``(metric, {treatment: score 0-1}, flags)`` from a SUCRA or P-score block.

    Scores above 1 are rescaled from percent, which is determinate rather than a guess —
    neither SUCRA nor a P-score can exceed 1 as a proportion. The conversion is flagged so
    a reviewer comparing against the printed table knows why the numbers differ.
    """
    if not isinstance(raw, dict):
        return None, {}, ()

    metric: str | None = None
    block: object = None
    for key, value in raw.items():
        # A present-but-null key is not a reported metric. Extractors routinely emit
        # `"sucra": null` for a paper that published a P-score instead, and treating that
        # as the metric would discard the score the paper actually reported.
        if value is None:
            continue
        name = str(key).strip().lower()
        if name in ("sucra", "sucra_percent", "sucra%"):
            metric, block = SUCRA, value
            break
        if name in ("p_score", "p-score", "pscore", "p_scores"):
            metric, block = P_SCORE, value
            break
    if block is None:
        return None, {}, ()

    scores: dict[str, float] = {}
    if isinstance(block, dict):
        for treatment, score in block.items():
            node, _ = canonical_treatment(str(treatment))
            value = _to_float(score)
            if node and value is not None:
                scores[node] = value
    elif isinstance(block, list):
        for entry in block:
            if not isinstance(entry, dict):
                continue
            node, _ = canonical_treatment(str(_first(entry, ("treatment", "node", "drug")) or ""))
            value = _to_float(_first(entry, ("score", "sucra", "p_score", "value")))
            if node and value is not None:
                scores[node] = value

    flags: list[str] = []
    if scores and any(v > 1.0 for v in scores.values()):
        scores = {k: v / 100.0 for k, v in scores.items()}
        flags.append(FLAG_RANKING_RESCALED)
    return metric, scores, tuple(flags)


# =====================================================================================
# The adapter entry point
# =====================================================================================
def parse(record: dict) -> ParsedSynthesis:
    """Normalise one extracted published synthesis. Pure; no I/O.

    Reconciles whichever league-table shape the record uses, resolves treatments and the
    endpoint through the shared normalisers, and records every gap as a flag. Nothing is
    inferred that a reader of the article could not verify.
    """
    record = record or {}
    problems: list[str] = []
    flags: list[str] = []

    source_type = str(record.get("source_type") or "MANUAL_UPLOAD").strip().upper()
    if source_type not in SOURCE_TYPES:
        problems.append(
            f"source_type {source_type!r} is not one of {', '.join(SOURCE_TYPES)}"
        )

    indication = (record.get("indication") or "").strip() or None
    if not indication:
        problems.append("indication is required — a synthesis with no indication cannot be scoped")

    endpoint = (record.get("endpoint") or "").strip() or None
    treatment_phase = (record.get("treatment_phase") or "PRIMARY").strip().upper()
    if treatment_phase not in outcomes.TREATMENT_PHASES:
        problems.append(
            f"treatment_phase {treatment_phase!r} is not one of "
            f"{', '.join(outcomes.TREATMENT_PHASES)} — induction and maintenance results "
            "are never poolable, so an unrecognised phase cannot be defaulted"
        )

    week = _to_float(record.get("timepoint_week"))
    if week is None:
        week = parse_timepoint_weeks(record.get("timepoint") or endpoint)
    if week is None:
        flags.append(FLAG_NO_TIMEPOINT)

    outcome_id = (record.get("canonical_outcome_id") or "").strip() or None
    if outcome_id is None and endpoint:
        match = match_endpoint(
            endpoint, indication=indication, week=week, treatment_phase=treatment_phase
        )
        outcome_id = match.outcome_id
    if outcome_id is None:
        flags.append(FLAG_ENDPOINT_NOT_CANONICAL)

    default_measure = normalise_effect_measure(record.get("effect_measure"))
    if default_measure is None:
        flags.append(FLAG_NO_EFFECT_MEASURE)
    model_type = normalise_model_type(record.get("model_type") or record.get("model"))
    if model_type is None:
        flags.append(FLAG_NO_MODEL_TYPE)

    default_interval_type = _interval_type_from(
        record.get("interval_type"), record.get("model_type"), record.get("model")
    )

    league = record.get("league_table")
    common_reference = record.get("common_reference") or record.get("reference_treatment")
    if isinstance(league, dict):
        contrasts = contrasts_from_matrix(
            league, default_measure=default_measure,
            default_interval_type=default_interval_type,
        )
    elif isinstance(league, list):
        contrasts = contrasts_from_rows(
            league, default_measure=default_measure,
            default_interval_type=default_interval_type, common_reference=common_reference,
        )
    elif isinstance(record.get("estimates"), list):
        contrasts = contrasts_from_rows(
            record["estimates"], default_measure=default_measure,
            default_interval_type=default_interval_type, common_reference=common_reference,
        )
    else:
        contrasts = ()
        problems.append(
            "no league_table or estimates found — a synthesis with no effect estimates "
            "carries no evidence"
        )

    declared = record.get("treatments") or []
    nodes = {canonical_treatment(str(t))[0] for t in declared if str(t).strip()}
    for contrast in contrasts:
        nodes.update({contrast.treatment, contrast.comparator})
    nodes.discard("")

    metric, scores, ranking_flags = normalise_ranking(record)
    flags.extend(ranking_flags)

    studies = tuple(
        str(s).strip() for s in (record.get("included_studies") or []) if str(s).strip()
    )
    # A hard requirement, not a nice-to-have: without it the NMA cannot be validated,
    # reused, or overlap-checked against an internal network. Recorded as a flag here and
    # refused by the Level-2 gate, so the citation is not lost in the meantime.
    recoverable = bool(record.get("included_studies_recoverable", bool(studies)))
    if not studies or not recoverable:
        recoverable = False
        flags.append(FLAG_STUDIES_NOT_RECOVERABLE)

    tau = _to_float(record.get("tau_squared"))
    q = _to_float(record.get("q_statistic"))
    if tau is None and q is None and not record.get("heterogeneity_note"):
        flags.append(FLAG_NO_HETEROGENEITY)
    inconsistency = record.get("inconsistency")
    if not inconsistency:
        flags.append(FLAG_NO_INCONSISTENCY_ASSESSMENT)

    df_raw = _to_float(record.get("degrees_freedom"))

    return ParsedSynthesis(
        source_type=source_type,
        source_identifier=(record.get("source_identifier") or record.get("doi")
                           or record.get("pmid") or None),
        citation=record.get("citation"),
        publication_date=_parse_date(record.get("publication_date")),
        funding_source=record.get("funding_source"),
        indication=indication,
        endpoint=endpoint,
        canonical_outcome_id=outcome_id,
        timepoint_week=week,
        population_stratum=(record.get("population_stratum") or None),
        treatment_phase=treatment_phase,
        treatments=tuple(sorted(nodes)),
        contrasts=contrasts,
        effect_measure=default_measure,
        model_type=model_type,
        ranking_metric=metric,
        ranking_scores=scores,
        tau_squared=tau,
        q_statistic=q,
        degrees_freedom=int(df_raw) if df_raw is not None else None,
        heterogeneity_note=record.get("heterogeneity_note"),
        inconsistency=inconsistency if isinstance(inconsistency, dict) else None,
        grade_certainty=(record.get("grade_certainty") or None),
        included_studies=studies,
        included_studies_recoverable=recoverable,
        flags=tuple(dict.fromkeys(flags)),
        problems=tuple(problems),
    )


def _parse_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def to_nma_result(
    parsed: ParsedSynthesis, *, result_id: str, status: str,
    source_payload_id: str | None = None, source_is_citable: bool = True,
) -> NMAResult:
    """Map a parsed synthesis onto an ``NMAResult`` row with ``source=PUBLISHED``.

    ``source_is_citable`` defaults True — a published article is citable by definition —
    while ``claim_is_approved_for_external_use`` stays False, because our *extraction* of
    it is unreviewed even though the source is public. The two are independent, and
    collapsing them would let an unverified extraction inherit the article's authority.
    """
    return NMAResult(
        result_id=result_id,
        source=PUBLISHED,
        indication=parsed.indication or "",
        canonical_outcome_id=parsed.canonical_outcome_id,
        endpoint=parsed.endpoint,
        timepoint_week=parsed.timepoint_week,
        population_stratum=parsed.population_stratum,
        treatment_phase=parsed.treatment_phase,
        source_payload_id=source_payload_id,
        citation=parsed.citation,
        publication_date=parsed.publication_date,
        funding_source=parsed.funding_source,
        grade_certainty=parsed.grade_certainty,
        included_studies=json.dumps(list(parsed.included_studies)),
        included_studies_recoverable=parsed.included_studies_recoverable,
        status=status,
        model_type=parsed.model_type,
        effect_measure=parsed.effect_measure,
        estimates=json.dumps([c.as_dict() for c in parsed.contrasts]),
        rankings=json.dumps({"metric": parsed.ranking_metric}) if parsed.ranking_metric else None,
        sucra=json.dumps(parsed.ranking_scores) if parsed.ranking_scores else None,
        tau_squared=parsed.tau_squared,
        q_statistic=parsed.q_statistic,
        degrees_freedom=parsed.degrees_freedom,
        heterogeneity_note=parsed.heterogeneity_note,
        inconsistency=json.dumps(parsed.inconsistency) if parsed.inconsistency else None,
        source_is_citable=source_is_citable,
        claim_is_approved_for_external_use=False,
    )
