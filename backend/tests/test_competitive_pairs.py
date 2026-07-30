"""Pair resolution and the verdict rule — the two decisions the head-to-head board rests on.

Both are pure, so they are pinned without a database. What these protect is less the
arithmetic than the *honesty* of the output: that a weakly-resolved pair is labelled as such
instead of being presented with the same confidence as a tagged one, that an answer which
never named us counts as a loss rather than quietly vanishing, and that every answer kept off
the board carries a stated reason.
"""
import pytest

from app.competitive import pairs, verdict


# --- verdict rule ------------------------------------------------------------------
def test_absence_is_a_loss_not_a_neutral():
    """The most common real failure: asked against a named rival, we are never mentioned."""
    assert verdict.for_answer("NOT_MENTIONED", None, None) == verdict.LOSING


@pytest.mark.parametrize("position", ["SECOND_LINE", "NOT_RECOMMENDED", "NOT_MENTIONED"])
def test_weak_positions_lose_however_warm_the_tone(position):
    """Position outranks sentiment: a warmly-worded second-line placement is still a loss."""
    assert verdict.for_answer(position, 0.9, -0.9) == verdict.LOSING


def test_first_line_wins_even_when_tone_favours_the_rival():
    assert verdict.for_answer(verdict.STRONG_POSITION, -0.5, 0.9) == verdict.WINNING


def test_tone_decides_only_when_the_position_is_genuinely_ambiguous():
    edge = verdict.SENTIMENT_EDGE
    assert verdict.for_answer(verdict.NEUTRAL_POSITION, 0.1, 0.1 + edge) == verdict.LOSING
    assert verdict.for_answer(verdict.NEUTRAL_POSITION, 0.1 + edge, 0.1) == verdict.WINNING


def test_a_tone_difference_below_the_edge_is_called_even():
    """Under the threshold the difference is measurement noise, so neither side is credited."""
    half = verdict.SENTIMENT_EDGE / 2
    assert verdict.for_answer(verdict.NEUTRAL_POSITION, 0.5 + half, 0.5) == verdict.EVEN


def test_an_unscored_answer_is_even_not_a_loss():
    """Counting ungraded rows as losses would inflate every loss rate on the board."""
    assert verdict.for_answer(None, None, None) == verdict.EVEN
    assert verdict.for_answer(verdict.NEUTRAL_POSITION, 0.5, None) == verdict.EVEN


def test_sentiment_gap_is_none_when_either_side_is_unscored():
    assert verdict.sentiment_gap(0.5, None) is None
    assert verdict.sentiment_gap(None, 0.5) is None
    assert verdict.sentiment_gap(0.5, 0.2) == 0.3


def test_pair_verdict_is_the_majority_with_ties_going_to_the_worse():
    assert verdict.overall({verdict.LOSING: 3, verdict.WINNING: 1}) == verdict.LOSING
    assert verdict.overall({verdict.WINNING: 3, verdict.LOSING: 1}) == verdict.WINNING
    # A tie resolves toward the risk signal rather than hiding it.
    assert verdict.overall({verdict.WINNING: 2, verdict.LOSING: 2}) == verdict.LOSING
    assert verdict.overall({}) == verdict.EVEN


# --- pair resolution ---------------------------------------------------------------
PSO = "Plaque Psoriasis"


def test_stored_competitor_tags_win_over_text_derivation():
    """A curation-written question carries the exact cell it was commissioned for."""
    res = pairs.resolve(
        "Anything at all", brand_focus="Skyrizi", disease=PSO, competitor_focus=["Stelara"],
    )
    assert res.resolved
    assert res.origin == pairs.ORIGIN_STORED
    assert [p.competitor for p in res.pairs] == ["Stelara"]
    assert res.pairs[0].disease == PSO


def test_competitor_tags_are_read_from_a_json_string_or_a_bare_string():
    """Legacy rows store the column both ways; both must resolve identically."""
    as_json = pairs.resolve(
        "x", brand_focus="Skyrizi", disease=PSO, competitor_focus='["Stelara"]',
    )
    as_bare = pairs.resolve(
        "x", brand_focus="Skyrizi", disease=PSO, competitor_focus="Stelara",
    )
    assert [p.key for p in as_json.pairs] == [p.key for p in as_bare.pairs]


def test_indication_is_derived_from_the_text_when_the_row_carries_none():
    res = pairs.resolve(
        "For plaque psoriasis, how does Skyrizi compare with Stelara?",
        brand_focus="Skyrizi",
    )
    assert res.resolved
    assert res.origin == pairs.ORIGIN_DERIVED
    assert res.pairs[0].disease == PSO


def test_a_legacy_area_tag_still_pairs_but_is_marked_indicative():
    """29 stored answers carry therapeutic_area='Immunology', which is not a declared key.
    Discarding a question that names a tracked rival over a bookkeeping tag loses a real
    finding, so it is kept and labelled as the weaker claim it is."""
    res = pairs.resolve(
        "Is Skyrizi better than Tremfya?", brand_focus="Skyrizi",
        therapeutic_area="Immunology",
    )
    assert res.resolved
    assert res.origin == pairs.ORIGIN_TEXT_ONLY
    assert res.pairs[0].disease is None
    assert "indicative" in pairs.ORIGIN_LABELS[res.origin].lower()


def test_no_focus_brand_is_reported_rather_than_guessed():
    res = pairs.resolve("Skyrizi vs Stelara", brand_focus=None)
    assert not res.resolved
    assert res.reason == pairs.NO_FOCUS_BRAND


def test_a_question_that_never_names_our_brand_is_not_a_head_to_head():
    res = pairs.resolve(
        "For plaque psoriasis, how does Stelara compare with Cosentyx?",
        brand_focus="Skyrizi",
    )
    assert not res.resolved
    assert res.reason == pairs.BRAND_NOT_NAMED


def test_a_question_naming_no_tracked_rival_is_reported_as_such():
    res = pairs.resolve(
        "How effective is Skyrizi for plaque psoriasis?", brand_focus="Skyrizi",
    )
    assert not res.resolved
    assert res.reason == pairs.NO_COMPETITOR_NAMED


def test_every_exclusion_reason_has_a_reader_facing_explanation():
    """The board subtracts these from a total, so each needs a sentence, not a code."""
    for reason in (
        pairs.NO_FOCUS_BRAND, pairs.NO_COMPETITIVE_FIELD,
        pairs.BRAND_NOT_NAMED, pairs.NO_COMPETITOR_NAMED,
    ):
        assert pairs.REASON_LABELS[reason]
        assert len(pairs.REASON_LABELS[reason]) > 20


def test_one_question_naming_two_rivals_yields_two_pairs():
    res = pairs.resolve(
        "For plaque psoriasis: Skyrizi vs Stelara vs Cosentyx", brand_focus="Skyrizi",
    )
    assert sorted(p.competitor for p in res.pairs) == ["Cosentyx", "Stelara"]


def test_the_key_carries_the_indication_so_two_settings_stay_separate():
    """The same two drugs in a different indication is a different competitive field."""
    a = pairs.Pair(brand="Skyrizi", competitor="Stelara", disease=PSO)
    b = pairs.Pair(brand="Skyrizi", competitor="Stelara", disease="Crohn's Disease")
    assert a.key != b.key


def test_the_key_is_stable_whether_the_indication_was_stored_or_derived():
    """Grouping across runs depends on this; a fork here would split one pair into two."""
    stored = pairs.resolve(
        "Skyrizi vs Stelara", brand_focus="Skyrizi", disease=PSO,
        competitor_focus=["Stelara"],
    )
    derived = pairs.resolve(
        "for PLAQUE PSORIASIS, skyrizi vs stelara", brand_focus="Skyrizi",
    )
    assert stored.pairs[0].key == derived.pairs[0].key


def test_a_pair_without_an_indication_is_labelled_not_hidden():
    assert pairs.Pair(brand="Skyrizi", competitor="Stelara").label == "Skyrizi vs Stelara"
    assert PSO in pairs.Pair(brand="Skyrizi", competitor="Stelara", disease=PSO).label


def test_parse_competitor_focus_tolerates_every_stored_shape():
    assert pairs.parse_competitor_focus(None) == []
    assert pairs.parse_competitor_focus("") == []
    assert pairs.parse_competitor_focus("[]") == []
    assert pairs.parse_competitor_focus(["Stelara", " "]) == ["Stelara"]
    assert pairs.parse_competitor_focus('["Stelara","Cosentyx"]') == ["Stelara", "Cosentyx"]
    assert pairs.parse_competitor_focus("Stelara") == ["Stelara"]


def test_competitive_field_prefers_the_indication_over_the_area_block():
    """Indication first, area second — the same order of preference the scorer uses."""
    by_disease = pairs.competitive_field(PSO, None)
    by_area = pairs.competitive_field(None, "Immunology_Derm")
    assert by_disease
    assert set(by_disease) <= set(by_area) or by_disease != by_area


def test_competitive_field_is_empty_when_nothing_is_declared():
    assert pairs.competitive_field(None, None) == ()
