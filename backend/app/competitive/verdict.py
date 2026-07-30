"""Who won one head-to-head answer. Pure — no DB, no network, no model call.

**The verdict is DERIVED and the UI must say so.** The BRAND-mode scoring prompt records
``brand_mentions: [{brand, is_competitor, sentiment}]`` — a sentiment number per agent and
nothing else. It does not record the competitor's own competitive position (verified: 0 of
189 scored comparison answers carry one). So "who won" is inferred from the position the
scorer gave OUR brand, with the sentiment gap used only where position is genuinely
indecisive. Capturing a real competitor position would need a schema change plus a billed
re-score of every stored answer.

Position leads, sentiment follows. That order matters: a position is the scorer's explicit
judgement about where our brand sits, while a sentiment delta is a soft read of tone. Using
tone to overturn an explicit "first-line recommended" would let a rounding-level difference
in two floats contradict the one field that actually answers the question.

In one sentence, for the reader: *we won when the model put us first, we lost when it put
us second, advised against us or left us out, and when it listed us as one option among
several the tone gap decides.*
"""
from __future__ import annotations

WINNING = "WINNING"
EVEN = "EVEN"
LOSING = "LOSING"

VERDICTS = (WINNING, EVEN, LOSING)

# Worse first, so a tie between equally common verdicts resolves toward the risk signal
# rather than hiding it — the same tie-breaking principle as ``scorer._modal_position``.
VERDICT_SEVERITY = {LOSING: 0, EVEN: 1, WINNING: 2}

VERDICT_LABELS = {
    WINNING: "We win this comparison",
    EVEN: "Too close to call",
    LOSING: "We lose this comparison",
}

STRONG_POSITION = "FIRST_LINE_RECOMMENDED"
NEUTRAL_POSITION = "AMONG_OPTIONS"
# Absence is a loss, not a neutral: asked directly against a named rival, an answer that
# never names us hands the comparison over by default.
WEAK_POSITIONS = ("SECOND_LINE", "NOT_RECOMMENDED", "NOT_MENTIONED")

# Below this, a sentiment difference is measurement noise rather than a stance. Matches the
# order of magnitude of the existing ``alert_engine.COMPETITOR_ADVANTAGE_DELTA`` (0.4) but
# lower, because that rule fires a per-answer alert and this only tips an already-neutral
# position one way or the other.
SENTIMENT_EDGE = 0.15

RULE_NOTE = (
    "Derived: the scorer records our brand's position plus a sentiment number per agent, "
    "never the competitor's own position. Position decides the verdict; the tone gap only "
    "breaks a tie when we are listed as one option among several."
)


def for_answer(
    our_position: str | None,
    our_sentiment: float | None,
    their_sentiment: float | None,
) -> str:
    """The verdict for a single answer.

    ``EVEN`` is also the honest answer when the scorer recorded no position: an unscored
    stance is not evidence of a loss, and counting it as one would inflate every loss rate
    with rows that simply were not graded.
    """
    if our_position in WEAK_POSITIONS:
        return LOSING
    if our_position == STRONG_POSITION:
        return WINNING
    if our_position != NEUTRAL_POSITION:
        return EVEN
    gap = sentiment_gap(our_sentiment, their_sentiment)
    if gap is None:
        return EVEN
    if gap >= SENTIMENT_EDGE:
        return WINNING
    if gap <= -SENTIMENT_EDGE:
        return LOSING
    return EVEN


def sentiment_gap(ours: float | None, theirs: float | None) -> float | None:
    """``ours - theirs``, or ``None`` when either side was not scored.

    Returning ``None`` rather than treating a missing competitor sentiment as 0.0 keeps an
    unscored answer out of the average instead of dragging it toward neutral.
    """
    if ours is None or theirs is None:
        return None
    return round(ours - theirs, 4)


def overall(counts: dict[str, int]) -> str:
    """The pair-level verdict: the most common per-answer verdict, ties going to the worse.

    A majority read rather than a mean, because averaging a first-line answer against a
    not-mentioned answer produces a middle number that describes neither.
    """
    if not any(counts.get(v) for v in VERDICTS):
        return EVEN
    return min(
        VERDICTS,
        key=lambda v: (-counts.get(v, 0), VERDICT_SEVERITY[v]),
    )
