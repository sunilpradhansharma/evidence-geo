"""The claim ledger's ranking.

The point of these tests is the ordering, not the grouping. On the real corpus 124 of 125
extracted claims were textually unique, so recurrence is almost always 1 and a volume-first
sort collapses into alphabetical order — which would bury the claim a brand team needs behind
whichever happens to start with "A". These pin the informative-class-first rule that prevents
that, and pin that nothing is ever merged by similarity.
"""
from app.competitive import claims, verdict


def _entry(llm, result, *texts):
    return (llm, result, list(texts))


def test_claim_naming_the_rival_in_a_lost_answer_ranks_first():
    """Even when a bland claim is more common, the actionable one leads."""
    out = claims.cluster(
        [
            _entry("claude", verdict.WINNING, "Both drugs are administered by injection"),
            _entry("gpt", verdict.WINNING, "Both drugs are administered by injection"),
            _entry("llama", verdict.WINNING, "Both drugs are administered by injection"),
            _entry("gpt", verdict.LOSING, "Stelara has a longer safety record than ours"),
        ],
        competitor="Stelara",
    )
    assert out["claims"][0]["text"].startswith("Stelara has a longer")
    assert out["claims"][0]["against_us"] is True
    assert out["claims_against_us"] == 1


def test_alphabetical_order_does_not_decide_the_ranking():
    """The regression this guards: all-unique claims previously sorted by first letter."""
    out = claims.cluster(
        [
            _entry("gpt", verdict.WINNING, "Aaa a bland uncontested statement"),
            _entry("gpt", verdict.LOSING, "Zzz Tremfya inhibits structural joint damage"),
        ],
        competitor="Tremfya",
    )
    assert out["claims"][0]["text"].startswith("Zzz Tremfya")


def test_losing_claims_outrank_winning_ones_when_neither_names_the_rival():
    out = claims.cluster(
        [
            _entry("gpt", verdict.WINNING, "Dosing is once every twelve weeks"),
            _entry("gpt", verdict.LOSING, "Response rates were lower in the head trial"),
        ],
        competitor="Stelara",
    )
    assert out["claims"][0]["losing_answers"] == 1


def test_cross_model_repetition_is_counted_and_flagged():
    """A claim several platforms repeat is a market narrative, not one model's phrasing."""
    text = "Stelara has a longer real-world safety record"
    out = claims.cluster(
        [
            _entry("claude", verdict.LOSING, text),
            _entry("gpt", verdict.LOSING, text),
        ],
        competitor="Stelara",
    )
    top = out["claims"][0]
    assert top["model_count"] == 2
    assert top["cross_model"] is True
    assert top["answers"] == 2
    assert out["distinct_claims"] == 1


def test_one_answer_repeating_itself_cannot_inflate_recurrence():
    text = "Stelara has a longer real-world safety record"
    out = claims.cluster([_entry("gpt", verdict.LOSING, text, text, text)], competitor="Stelara")
    assert out["claims"][0]["answers"] == 1


def test_differently_worded_claims_are_never_merged():
    """Merging by similarity would fuse two different clinical statements. It must not."""
    out = claims.cluster(
        [
            _entry("gpt", verdict.LOSING, "Stelara works faster than ours"),
            _entry("gpt", verdict.LOSING, "Stelara works better than ours"),
        ],
        competitor="Stelara",
    )
    assert out["distinct_claims"] == 2


def test_casing_and_spacing_differences_are_the_same_claim():
    out = claims.cluster(
        [
            _entry("gpt", verdict.LOSING, "Stelara has a longer safety record"),
            _entry("claude", verdict.LOSING, "  stelara   has a LONGER safety record "),
        ],
        competitor="Stelara",
    )
    assert out["distinct_claims"] == 1


def test_fragments_are_dropped_but_real_claims_are_kept():
    out = claims.cluster(
        [_entry("gpt", verdict.LOSING, "efficacy", "cost", "Stelara has a longer safety record")],
        competitor="Stelara",
    )
    assert out["distinct_claims"] == 1


def test_empty_and_malformed_input_does_not_raise():
    assert claims.cluster([])["distinct_claims"] == 0
    out = claims.cluster([("gpt", verdict.LOSING, None), ("gpt", verdict.EVEN, [None, ""])])
    assert out["distinct_claims"] == 0
    assert out["answers_with_claims"] == 0


def test_answers_with_claims_reports_the_denominator_honestly():
    """An empty ledger must be distinguishable from a scorer that extracted nothing."""
    out = claims.cluster(
        [
            _entry("gpt", verdict.LOSING, "Stelara has a longer safety record"),
            _entry("claude", verdict.WINNING),
        ],
        competitor="Stelara",
    )
    assert out["answers_with_claims"] == 1


def test_ranking_is_stable_without_a_known_competitor():
    """No competitor supplied means the flag simply never fires; it must not crash."""
    out = claims.cluster([_entry("gpt", verdict.LOSING, "Some claim about the market")])
    assert out["claims"][0]["names_competitor"] is False
    assert out["claims_against_us"] == 0
