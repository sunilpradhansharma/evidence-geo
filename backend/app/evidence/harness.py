"""The agent evaluation harness — justify-or-drop, measured rather than assumed.

The plan's risk register says: *"if the pipeline cannot demonstrate measurably better
extraction accuracy than the ``chat_json`` baseline, the correct response is to ship the
baseline and keep the validation stage only — not to keep tuning agents on the critical
path."* That is only a real commitment if something measures it. This is that thing.

**Three decisions worth not re-litigating.**

1. **A miss and a wrong answer are counted separately.** An extractor that declines to
   answer is safer than one that guesses, and collapsing both into "not correct" would rank
   a confident fabricator level with an honest abstainer. ``accuracy`` counts only correct
   answers; ``error_rate`` counts wrong ones; ``miss_rate`` counts abstentions.

2. **Accuracy is reported per licence tier.** A restricted source retains only a fragment,
   so a single headline number would average a fully-checkable public-domain document
   together with one nobody can fully re-derive, and overstate what was verified.

3. **A tie ships the baseline.** Equal accuracy means the pipeline bought nothing for three
   model calls, three prompts to maintain and a longer trace to audit. The tie-break is not
   neutrality, it is the plan's position: agent count is not a quality metric.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.evidence import licensing
from app.evidence.agents import ExtractionTask
from app.evidence.extraction import PipelineResult

# A relative tolerance for numeric comparison. Extraction reads a printed number; it does
# not recompute one, so anything beyond floating-point noise is a genuine misread.
NUMERIC_TOLERANCE = 1e-9

CORRECT = "CORRECT"
WRONG = "WRONG"
MISSED = "MISSED"

Runner = Callable[[ExtractionTask], Awaitable[PipelineResult]]


@dataclass(frozen=True)
class LabelledCase:
    """One document with hand-checked ground truth for the fields under test.

    ``expected`` is what a person read out of the source. For a registry record the source
    document *is* the JSON, so a label is a transcription rather than a judgement — which
    is what makes a small corpus defensible without a clinician.
    """

    case_id: str
    document: str
    expected: dict[str, Any]
    license_class: str = licensing.PUBLIC_DOMAIN
    approved_time_window: tuple[float, float] | None = None

    def as_task(self) -> ExtractionTask:
        return ExtractionTask(
            source_id=self.case_id,
            document=self.document,
            fields=tuple(self.expected),
            license_class=self.license_class,
            approved_time_window=self.approved_time_window,
        )


def _normalise(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    return value


def grade_field(expected: Any, observed: Any) -> str:
    """``CORRECT`` / ``WRONG`` / ``MISSED`` for one field.

    A numeric answer given as a string still counts — the model returning ``"120"`` for
    ``120`` is a serialisation artefact, not a reading error, and penalising it would make
    the harness measure JSON formatting instead of extraction.
    """
    if observed is None:
        return MISSED
    left, right = _normalise(expected), _normalise(observed)
    if isinstance(left, float) and not isinstance(right, float):
        try:
            right = float(str(observed).strip())
        except (TypeError, ValueError):
            return WRONG
    if isinstance(left, float) and isinstance(right, float):
        return CORRECT if abs(left - right) <= NUMERIC_TOLERANCE * max(1.0, abs(left)) else WRONG
    return CORRECT if left == right else WRONG


@dataclass
class CaseScore:
    """How one runner did on one case."""

    case_id: str
    license_class: str
    grades: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def _count(self, grade: str) -> int:
        return sum(1 for g in self.grades.values() if g == grade)

    @property
    def correct(self) -> int:
        return self._count(CORRECT)

    @property
    def wrong(self) -> int:
        return self._count(WRONG)

    @property
    def missed(self) -> int:
        return self._count(MISSED)

    @property
    def total(self) -> int:
        return len(self.grades)


def score_case(case: LabelledCase, result: PipelineResult) -> CaseScore:
    observed = {v.field_name: v.value for v in result.values}
    return CaseScore(
        case_id=case.case_id,
        license_class=case.license_class,
        grades={
            name: grade_field(expected, observed.get(name))
            for name, expected in case.expected.items()
        },
        errors=list(result.errors),
    )


@dataclass
class HarnessReport:
    """One runner's performance over the whole corpus."""

    runner: str
    scores: list[CaseScore] = field(default_factory=list)
    # Blocked promotions are a quality signal in their own right: the validation stage
    # earning its place looks like *catching* something, not like agreeing more often.
    blocked_by_validation: int = 0
    proposals_auto_rejected: int = 0

    def _sum(self, attribute: str, scores: Sequence[CaseScore] | None = None) -> int:
        return sum(getattr(s, attribute) for s in (scores if scores is not None else self.scores))

    def _rates(self, scores: Sequence[CaseScore]) -> dict[str, Any]:
        total = self._sum("total", scores)
        if not total:
            return {"fields": 0, "accuracy": None, "error_rate": None, "miss_rate": None}
        return {
            "fields": total,
            "correct": self._sum("correct", scores),
            "wrong": self._sum("wrong", scores),
            "missed": self._sum("missed", scores),
            "accuracy": round(self._sum("correct", scores) / total, 4),
            # Reported apart from accuracy on purpose: a wrong value is a different and
            # worse failure than an absent one, and one number cannot say which happened.
            "error_rate": round(self._sum("wrong", scores) / total, 4),
            "miss_rate": round(self._sum("missed", scores) / total, 4),
        }

    @property
    def accuracy(self) -> float | None:
        return self._rates(self.scores)["accuracy"]

    @property
    def error_rate(self) -> float | None:
        return self._rates(self.scores)["error_rate"]

    def as_dict(self) -> dict[str, Any]:
        by_tier = {}
        for tier in licensing.LICENSE_CLASSES:
            scoped = [s for s in self.scores if s.license_class == tier]
            if scoped:
                by_tier[tier] = self._rates(scoped)
        return {
            "runner": self.runner,
            "cases": len(self.scores),
            "overall": self._rates(self.scores),
            # Per tier, never as one figure: a fragment-only source cannot be re-derived
            # in full, so averaging it with a public-domain one overstates coverage.
            "by_license_class": by_tier,
            "blocked_by_validation": self.blocked_by_validation,
            "proposals_auto_rejected": self.proposals_auto_rejected,
            "model_errors": sum(len(s.errors) for s in self.scores),
        }


async def evaluate(
    runner: Runner, corpus: Sequence[LabelledCase], *, name: str
) -> HarnessReport:
    """Run one extractor over the corpus and score it. One model call set per case."""
    report = HarnessReport(runner=name)
    for case in corpus:
        result = await runner(case.as_task())
        report.scores.append(score_case(case, result))
        if result.disagreements:
            report.blocked_by_validation += 1
        report.proposals_auto_rejected += sum(
            1 for p in result.proposals if not p.is_actionable_by_human
        )
    return report


SHIP_PIPELINE = "SHIP_PIPELINE"
SHIP_BASELINE = "SHIP_BASELINE_PLUS_VALIDATION"


def verdict(baseline: HarnessReport, pipeline: HarnessReport) -> dict[str, Any]:
    """Justify-or-drop, decided by the numbers rather than by preference.

    The pipeline ships only if it is **strictly more accurate**. A tie ships the baseline
    plus the validation stage: equal accuracy for three model calls instead of one is a
    cost with no return, and the documented descope exists precisely so that outcome is
    cheap to act on rather than embarrassing to admit.
    """
    a, b = baseline.accuracy, pipeline.accuracy
    if a is None or b is None:
        return {
            "verdict": SHIP_BASELINE,
            "reason": (
                "the corpus produced no gradeable fields, so no claim about relative "
                "accuracy is supported — shipping the simpler runner is the default when "
                "the measurement is absent, not the more complex one"
            ),
            "baseline_accuracy": a,
            "pipeline_accuracy": b,
        }

    improvement = round(b - a, 4)
    if b > a:
        return {
            "verdict": SHIP_PIPELINE,
            "reason": (
                f"the pipeline is more accurate on the same corpus "
                f"({b:.1%} vs {a:.1%}, +{improvement:.1%})"
            ),
            "baseline_accuracy": a,
            "pipeline_accuracy": b,
            "improvement": improvement,
        }
    return {
        "verdict": SHIP_BASELINE,
        "reason": (
            f"the pipeline is not more accurate on the same corpus "
            f"({b:.1%} vs {a:.1%}). Agent count is not a quality metric, so the "
            "documented descope applies: ship the single call plus the validation stage"
        ),
        "baseline_accuracy": a,
        "pipeline_accuracy": b,
        "improvement": improvement,
    }


def load_corpus(path: str | Path) -> list[LabelledCase]:
    """Read a labelled corpus from JSON.

    The corpus is committed rather than generated. A corpus produced by the same models
    under test would agree with itself by construction and measure nothing.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = data.get("cases", data) if isinstance(data, dict) else data
    out: list[LabelledCase] = []
    for row in cases:
        window = row.get("approved_time_window")
        out.append(LabelledCase(
            case_id=row["case_id"],
            document=row["document"],
            expected=row["expected"],
            license_class=row.get("license_class", licensing.PUBLIC_DOMAIN),
            approved_time_window=tuple(window) if window else None,
        ))
    return out
