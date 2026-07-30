"""Head-to-head aggregation: the accounting a reader has to be able to trust.

Two properties matter more than any individual number. First, answers are never silently
dropped between the total examined and the total on the board — every exclusion is counted
and explained. Second, the ranking puts real exposure first rather than flattering
percentages, because the list is truncated and whatever sorts last is not seen at all.

Answers are built through the real ``pairs.resolve`` rather than hand-made ``Resolution``
objects, so these exercise the same resolution path ``load_answers`` uses.
"""
from app.competitive import head_to_head as h2h
from app.competitive import pairs as pairs_mod
from app.competitive import verdict as verdict_mod
from app.config.taxonomy import area_for

PSO = "Plaque Psoriasis"


def answer(
    *, rid="r1", brand="Skyrizi", text=f"Skyrizi vs Stelara for plaque psoriasis",
    llm="gpt", position="AMONG_OPTIONS", ours=0.5, theirs=0.5, disease=PSO,
    run="run1", qid="q1", persona="Provider", ts=1, claims=None, competitor_focus=None,
    therapeutic_area=None, competitor="Stelara",
):
    mentions = []
    if theirs is not None:
        mentions.append({"brand": competitor, "sentiment": theirs, "is_competitor": True})
    return h2h.Answer(
        response_id=rid, run_id=run, question_id=qid, question_text=text, llm_name=llm,
        persona=persona, brand_focus=brand, therapeutic_area=therapeutic_area,
        disease=disease, timestamp=ts, position=position, our_sentiment=ours,
        brand_mentions=mentions, key_claims=claims or [],
        resolution=pairs_mod.resolve(
            text, brand_focus=brand, therapeutic_area=therapeutic_area,
            disease=disease, competitor_focus=competitor_focus,
        ),
    )


def only_bucket(rows):
    buckets, _ = h2h.group_by_pair(rows)
    return next(iter(buckets.values()))


# --- accounting --------------------------------------------------------------------
def test_no_answer_is_lost_between_the_total_and_the_board():
    """Every excluded answer carries a reason the UI can print verbatim."""
    rows = [
        answer(rid="a1"),
        answer(rid="a2", text="Skyrizi is effective for plaque psoriasis"),
        answer(rid="a3", brand=None, text="Skyrizi vs Stelara"),
    ]
    buckets, excluded = h2h.group_by_pair(rows)
    on_board = len({a.response_id for b in buckets.values() for a in b.answers})
    assert on_board + sum(excluded.values()) == len(rows)
    assert set(excluded) <= set(pairs_mod.REASON_LABELS)


def test_one_answer_naming_two_rivals_belongs_to_both_comparisons():
    """Per-pair counts therefore sum higher than the distinct answer total, by design."""
    rows = [answer(text="For plaque psoriasis: Skyrizi vs Stelara vs Cosentyx")]
    buckets, excluded = h2h.group_by_pair(rows)
    assert len(buckets) == 2
    assert not excluded
    assert sum(len(b.answers) for b in buckets.values()) == 2


# --- verdicts ----------------------------------------------------------------------
def test_verdict_counts_account_for_every_answer_in_the_pair():
    rows = [
        answer(rid="a1", position=verdict_mod.STRONG_POSITION),
        answer(rid="a2", position="NOT_MENTIONED"),
        answer(rid="a3", position="AMONG_OPTIONS", ours=0.5, theirs=0.5),
    ]
    summary = h2h.summarize_pair(only_bucket(rows))
    assert sum(summary["verdict_counts"].values()) == summary["answers"] == 3


def test_a_pair_losing_most_answers_is_reported_as_losing():
    rows = [
        answer(rid="a1", position="NOT_MENTIONED"),
        answer(rid="a2", position="SECOND_LINE"),
        answer(rid="a3", position=verdict_mod.STRONG_POSITION),
    ]
    summary = h2h.summarize_pair(only_bucket(rows))
    assert summary["verdict"] == verdict_mod.LOSING
    assert summary["losing_answers"] == 2
    assert summary["loss_rate"] == round(2 / 3, 3)


def test_position_mix_covers_every_answer_including_unscored_ones():
    rows = [answer(rid="a1", position=None), answer(rid="a2", position="NOT_MENTIONED")]
    summary = h2h.summarize_pair(only_bucket(rows))
    assert sum(summary["position_mix"].values()) == 2
    assert "UNSCORED" in summary["position_mix"]


# --- ranking -----------------------------------------------------------------------
def test_ranking_puts_absolute_exposure_above_a_flattering_rate():
    """15 losses of 25 outranks 2 of 2, which a rate-first sort would invert."""
    big = [answer(rid=f"b{i}", position="NOT_MENTIONED") for i in range(15)]
    big += [answer(rid=f"g{i}", position=verdict_mod.STRONG_POSITION) for i in range(10)]
    small = [
        answer(rid=f"s{i}", brand="Rinvoq", competitor="Dupixent",
               text="Rinvoq vs Dupixent for atopic dermatitis",
               disease="Atopic Dermatitis", position="NOT_MENTIONED")
        for i in range(2)
    ]
    buckets, _ = h2h.group_by_pair(big + small)
    ordered = sorted((h2h.summarize_pair(b) for b in buckets.values()), key=h2h.rank_key)
    assert ordered[0]["losing_answers"] == 15
    assert ordered[0]["loss_rate"] < ordered[1]["loss_rate"]


def test_ranking_is_deterministic_for_identically_exposed_pairs():
    a = {"losing_answers": 3, "loss_rate": 0.5, "answers": 6, "key": "B|x|y"}
    b = {"losing_answers": 3, "loss_rate": 0.5, "answers": 6, "key": "A|x|y"}
    assert sorted([a, b], key=h2h.rank_key)[0]["key"] == "A|x|y"


# --- honesty about weak data -------------------------------------------------------
def test_pair_confidence_degrades_to_the_weakest_contributing_answer():
    """A single tagged answer must not launder a pair the rest only matched by text."""
    rows = [
        answer(rid="a1", disease=PSO, competitor_focus=["Stelara"]),
        answer(rid="a2", disease=None, therapeutic_area="Immunology",
               text="Is Skyrizi better than Stelara?"),
    ]
    buckets, _ = h2h.group_by_pair(rows)
    weakest = [
        h2h.summarize_pair(b) for b in buckets.values()
        if b.pair.competitor == "Stelara" and b.pair.disease is None
    ]
    assert weakest, "the indication-less pair should exist as its own comparison"
    assert weakest[0]["pair_source"] == pairs_mod.ORIGIN_TEXT_ONLY
    assert weakest[0]["indication_known"] is False
    assert weakest[0]["pair_source_note"]


def test_sentiment_gap_is_none_when_the_rival_was_never_scored():
    """A gap computed against a missing number would be an invention."""
    rows = [answer(rid="a1", ours=0.5, theirs=None)]
    assert h2h.summarize_pair(only_bucket(rows))["sentiment_gap"] is None


def test_rival_sentiment_is_matched_through_aliases_not_exact_strings():
    """The scorer echoes whatever spelling the model used, e.g. the generic name."""
    rows = [answer(rid="a1", ours=0.2, theirs=None)]
    rows[0].brand_mentions = [{"brand": "ustekinumab", "sentiment": 0.9}]
    assert h2h.summarize_pair(only_bucket(rows))["their_sentiment"] == 0.9


# --- trend and disagreement --------------------------------------------------------
def test_a_single_run_reports_no_trend_rather_than_drawing_a_fake_one():
    rows = [answer(rid="a1", run="run1", position="NOT_MENTIONED")]
    trend = h2h.summarize_pair(only_bucket(rows))["trend"]
    assert trend["available"] is False
    assert trend["note"]


def test_a_trend_appears_once_a_second_run_exists():
    rows = [
        answer(rid="a1", run="run1", ts=1, position=verdict_mod.STRONG_POSITION),
        answer(rid="a2", run="run2", ts=2, position="NOT_MENTIONED"),
    ]
    trend = h2h.summarize_pair(only_bucket(rows))["trend"]
    assert trend["available"] is True
    assert trend["direction"] == "worse"
    assert trend["latest_loss_rate"] == 1.0
    assert trend["previous_loss_rate"] == 0.0


def test_disagreement_only_counts_models_answering_the_same_question():
    same = [
        answer(rid="a1", llm="gpt", qid="q1", position=verdict_mod.STRONG_POSITION),
        answer(rid="a2", llm="claude", qid="q1", position="NOT_MENTIONED"),
    ]
    d = h2h.summarize_pair(only_bucket(same))["disagreement"]
    assert d["questions_compared"] == 1
    assert d["questions_with_disagreement"] == 1

    different = [
        answer(rid="a1", llm="gpt", qid="q1", position=verdict_mod.STRONG_POSITION),
        answer(rid="a2", llm="claude", qid="q2", position="NOT_MENTIONED"),
    ]
    d = h2h.summarize_pair(only_bucket(different))["disagreement"]
    assert d["questions_with_disagreement"] == 0


def test_per_model_rows_cover_every_answer_worst_platform_first():
    rows = [
        answer(rid="a1", llm="gpt", position="NOT_MENTIONED"),
        answer(rid="a2", llm="claude", position=verdict_mod.STRONG_POSITION),
        answer(rid="a3", llm="claude", position="NOT_MENTIONED"),
    ]
    summary = h2h.summarize_pair(only_bucket(rows))
    assert sum(m["answers"] for m in summary["by_model"]) == 3
    assert summary["by_model"][0]["llm_name"] == "gpt"
    assert summary["by_model"][0]["loss_rate"] == 1.0


def test_per_persona_rows_cover_every_answer_worst_audience_first():
    rows = [
        answer(rid="a1", persona="Patient", position="NOT_MENTIONED"),
        answer(rid="a2", persona="Patient", position="SECOND_LINE"),
        answer(rid="a3", persona="Provider", position=verdict_mod.STRONG_POSITION),
    ]
    summary = h2h.summarize_pair(only_bucket(rows))
    assert sum(p["answers"] for p in summary["by_persona"]) == 3
    assert summary["by_persona"][0]["persona"] == "Patient"
    assert summary["by_persona"][0]["loss_rate"] == 1.0


def test_the_audience_and_platform_slices_grade_the_same_answers():
    """Two views of one comparison must never disagree about how much of it was lost."""
    rows = [
        answer(rid="a1", persona="Patient", llm="gpt", position="NOT_MENTIONED"),
        answer(rid="a2", persona="Provider", llm="gpt", position=verdict_mod.STRONG_POSITION),
        answer(rid="a3", persona="Provider", llm="claude", position="SECOND_LINE"),
    ]
    summary = h2h.summarize_pair(only_bucket(rows))
    for slice_name in ("by_model", "by_persona"):
        assert sum(r["answers"] for r in summary[slice_name]) == summary["answers"]
        assert sum(r["losing"] for r in summary[slice_name]) == summary["losing_answers"]


# --- board timeline ----------------------------------------------------------------
def dated(day, **kw):
    """An answer stamped with a real date — which the helper's default ``ts`` is not."""
    return answer(ts=f"{day}T12:00:00", **kw)


def board_timeline(rows):
    buckets, _ = h2h.group_by_pair(rows)
    return h2h.timeline(buckets)


def test_the_timeline_accounts_for_every_comparison_answer():
    """Dated or not, an answer the board used lands somewhere in the timeline totals."""
    rows = [
        dated("2026-06-13", rid="a1", position="NOT_MENTIONED"),
        dated("2026-06-13", rid="a2", position=verdict_mod.STRONG_POSITION),
        dated("2026-06-14", rid="a3", position="SECOND_LINE"),
        answer(rid="a4", position="AMONG_OPTIONS"),
    ]
    line = board_timeline(rows)
    assert sum(p["answers"] for p in line["periods"]) + line["undated"] == 4


def test_an_undated_answer_is_reported_rather_than_dated_to_today():
    """Placing it on today would move it onto a period it was never observed in."""
    line = board_timeline([answer(rid="a1", position="NOT_MENTIONED")])
    assert line["periods"] == []
    assert line["undated"] == 1


def test_the_timeline_counts_an_answer_once_per_comparison_it_informs():
    """The unit ``by_model`` counts, not distinct responses — and the note says so."""
    rows = [dated("2026-06-13", text="For plaque psoriasis: Skyrizi vs Stelara vs Cosentyx")]
    line = board_timeline(rows)
    assert [p["answers"] for p in line["periods"]] == [2]
    assert line["note"]


def test_two_days_report_no_line_rather_than_drawing_a_direction():
    """Two points joined up read as a trend when they are one observation each."""
    rows = [
        dated("2026-06-13", rid="a1", position="NOT_MENTIONED"),
        dated("2026-06-14", rid="a2", position=verdict_mod.STRONG_POSITION),
    ]
    line = board_timeline(rows)
    assert len(line["periods"]) == 2
    assert line["available"] is False
    assert line["min_periods"] == h2h.MIN_TIMELINE_PERIODS


def test_a_line_appears_once_enough_days_exist_and_reads_oldest_first():
    rows = [
        dated("2026-07-05", rid="a3", position="NOT_MENTIONED"),
        dated("2026-06-13", rid="a1", position="NOT_MENTIONED"),
        dated("2026-06-14", rid="a2", position=verdict_mod.STRONG_POSITION),
    ]
    line = board_timeline(rows)
    assert line["available"] is True
    assert [p["period"] for p in line["periods"]] == [
        "2026-06-13", "2026-06-14", "2026-07-05",
    ]
    assert [p["loss_rate"] for p in line["periods"]] == [1.0, 0.0, 1.0]


def test_each_period_splits_into_verdicts_that_add_up_to_its_total():
    rows = [
        dated("2026-06-13", rid="a1", position="NOT_MENTIONED"),
        dated("2026-06-13", rid="a2", position=verdict_mod.STRONG_POSITION),
        dated("2026-06-13", rid="a3", position=None),
    ]
    period = board_timeline(rows)["periods"][0]
    assert {v.lower() for v in verdict_mod.VERDICTS} <= set(period)
    assert sum(period[v.lower()] for v in verdict_mod.VERDICTS) == period["answers"] == 3


def test_several_runs_on_one_date_are_one_period_not_several():
    """A per-run axis would draw one day as three periods of a single answer each."""
    rows = [
        dated("2026-06-13", rid="a1", run="run1", position="NOT_MENTIONED"),
        dated("2026-06-13", rid="a2", run="run2", position="NOT_MENTIONED"),
        dated("2026-06-13", rid="a3", run="run2", position=verdict_mod.STRONG_POSITION),
    ]
    line = board_timeline(rows)
    assert len(line["periods"]) == 1
    assert line["periods"][0]["runs"] == 2
    assert line["runs"] == 2


def dated_corpus():
    """One losing comparison and one winning one, on different days."""
    return [
        dated("2026-06-13", rid="a1", brand="Skyrizi", competitor="Stelara", disease=PSO,
              text="Skyrizi vs Stelara for plaque psoriasis", position="NOT_MENTIONED"),
        dated("2026-06-14", rid="a2", brand="Skyrizi", competitor="Cosentyx", disease=PSO,
              text="Skyrizi vs Cosentyx for plaque psoriasis",
              position=verdict_mod.STRONG_POSITION),
    ]


def test_the_board_timeline_only_covers_comparisons_the_filters_kept():
    board = h2h.assemble(dated_corpus(), h2h.Selection.of(competitors=["Stelara"]))
    periods = board["timeline"]["periods"]
    assert [p["period"] for p in periods] == ["2026-06-13"]
    assert sum(p["answers"] for p in periods) == 1


def test_the_verdict_filter_narrows_the_timeline_with_the_board():
    """A line drawn over rows the reader cannot see would contradict the board above it."""
    board = h2h.assemble(dated_corpus(), h2h.Selection.of(verdicts=[verdict_mod.LOSING]))
    assert sum(p["answers"] for p in board["timeline"]["periods"]) == 1
    assert board["pairs_total"] == 1


# --- multi-select filters ----------------------------------------------------------
def mixed_corpus():
    """Two brands, two rivals, two personas, two platforms — enough to tell filters apart."""
    return [
        answer(rid="a1", brand="Skyrizi", competitor="Stelara", disease=PSO,
               text="Skyrizi vs Stelara for plaque psoriasis",
               persona="Patient", llm="gpt", position="NOT_MENTIONED"),
        answer(rid="a2", brand="Skyrizi", competitor="Cosentyx", disease=PSO,
               text="Skyrizi vs Cosentyx for plaque psoriasis",
               persona="Provider", llm="claude", position=verdict_mod.STRONG_POSITION),
        answer(rid="a3", brand="Rinvoq", competitor="Dupixent", disease="Atopic Dermatitis",
               text="Rinvoq vs Dupixent for atopic dermatitis",
               persona="Patient", llm="claude", position="NOT_MENTIONED"),
    ]


def brands_on(board):
    return {row["brand"] for row in board["pairs"]}


def test_an_empty_selection_is_no_filter_rather_than_no_match():
    """Unticking everything in the picker has to mean the same as ticking everything."""
    board = h2h.assemble(mixed_corpus(), h2h.Selection.of(brands=[], personas=None))
    assert board["pairs_total"] == 3


def test_a_selection_keeps_every_value_that_was_picked():
    board = h2h.assemble(
        mixed_corpus(), h2h.Selection.of(brands=["Skyrizi", "Rinvoq"]),
    )
    assert brands_on(board) == {"Skyrizi", "Rinvoq"}

    one = h2h.assemble(mixed_corpus(), h2h.Selection.of(brands=["Rinvoq"]))
    assert brands_on(one) == {"Rinvoq"}


def test_filter_values_are_matched_case_insensitively():
    """A value typed into the URL by hand must not silently return an empty board."""
    board = h2h.assemble(mixed_corpus(), h2h.Selection.of(brands=["rinvoq"]))
    assert brands_on(board) == {"Rinvoq"}


def test_an_area_selection_accepts_the_broad_name_and_the_stored_key():
    """Stored rows carry either spelling — a TA key or its parent — and both reach here."""
    # Endometriosis rolls up to Women's Health in brands.yaml; most keys are their own area,
    # so the roll-up case has to be picked deliberately or this asserts nothing.
    parent = area_for("Endometriosis")
    assert parent != "Endometriosis", "fixture assumes this key sits under a broader area"
    rows = [answer(rid="a1", brand="Lupron Depot", competitor="Orilissa",
                   therapeutic_area="Endometriosis", disease=None,
                   text="Lupron Depot vs Orilissa")]
    assert h2h.assemble(rows, h2h.Selection.of(areas=["Endometriosis"]))["pairs_total"] == 1
    assert h2h.assemble(rows, h2h.Selection.of(areas=[parent]))["pairs_total"] == 1
    assert h2h.assemble(rows, h2h.Selection.of(areas=["Oncology"]))["pairs_total"] == 0


def test_the_rival_and_indication_filters_narrow_the_comparison_not_the_answer():
    corpus = mixed_corpus()
    by_rival = h2h.assemble(corpus, h2h.Selection.of(competitors=["Cosentyx"]))
    assert {p["competitor"] for p in by_rival["pairs"]} == {"Cosentyx"}

    by_indication = h2h.assemble(corpus, h2h.Selection.of(diseases=[PSO]))
    assert {p["disease"] for p in by_indication["pairs"]} == {PSO}


def test_the_verdict_filter_hides_rows_without_rewriting_them():
    corpus = mixed_corpus()
    losing = h2h.assemble(corpus, h2h.Selection.of(verdicts=[verdict_mod.LOSING]))
    assert losing["pairs_total"] == 2
    assert {p["verdict"] for p in losing["pairs"]} == {verdict_mod.LOSING}
    unfiltered = {p["key"]: p for p in h2h.assemble(corpus, h2h.Selection())["pairs"]}
    for row in losing["pairs"]:
        assert row["losing_answers"] == unfiltered[row["key"]]["losing_answers"]


# --- accounting under a filter -----------------------------------------------------
def test_no_answer_is_lost_when_a_filter_narrows_the_board():
    """The same invariant as the unfiltered board: on the board, or excluded with a reason."""
    corpus = mixed_corpus() + [answer(rid="a4", text="Skyrizi is effective for psoriasis")]
    board = h2h.assemble(corpus, h2h.Selection.of(competitors=["Stelara"]))
    counted = board["answers_on_the_board"] + sum(e["answers"] for e in board["exclusions"])
    assert counted == board["answers_examined"]
    assert board["answers_excluded"] == board["answers_examined"] - board["answers_on_the_board"]


def test_an_answer_a_filter_removed_is_reported_as_a_choice_not_as_unusable_data():
    """Folding it into the resolution reasons would blame the corpus for the reader's filter."""
    board = h2h.assemble(mixed_corpus(), h2h.Selection.of(competitors=["Stelara"]))
    filtered = [e for e in board["exclusions"] if e["reason"] == h2h.FILTERED_OUT]
    assert filtered and filtered[0]["answers"] == 2
    assert filtered[0]["explanation"] == h2h.FILTERED_OUT_LABEL
    assert board["exclusions"][-1] is filtered[0], "the reader's own filter reads last"
    assert h2h.FILTERED_OUT not in pairs_mod.REASON_LABELS


def test_an_unfiltered_board_reports_no_filtered_out_line():
    board = h2h.assemble(mixed_corpus(), h2h.Selection())
    assert all(e["reason"] != h2h.FILTERED_OUT for e in board["exclusions"])
    assert board["filters_applied"] == {dim: [] for dim in (*h2h.FILTER_DIMENSIONS, "verdicts")}


# --- facet lists -------------------------------------------------------------------
def test_a_picker_never_narrows_its_own_options():
    """Otherwise picking one brand deletes every other brand and there is no way back."""
    options = h2h.facet_options(mixed_corpus(), h2h.Selection.of(brands=["Skyrizi"]))
    assert options[h2h.DIM_BRAND] == ["Rinvoq", "Skyrizi"]


def test_a_picker_is_narrowed_by_the_other_filters():
    options = h2h.facet_options(mixed_corpus(), h2h.Selection.of(brands=["Skyrizi"]))
    assert options[h2h.DIM_COMPETITOR] == ["Cosentyx", "Stelara"]
    assert options[h2h.DIM_PERSONA] == ["Patient", "Provider"]

    narrowed = h2h.facet_options(
        mixed_corpus(), h2h.Selection.of(brands=["Skyrizi"], personas=["Provider"]),
    )
    assert narrowed[h2h.DIM_COMPETITOR] == ["Cosentyx"]


def test_only_values_with_a_comparison_behind_them_are_offered():
    """Every option has to lead somewhere, or the picker invites an empty board."""
    corpus = mixed_corpus() + [
        answer(rid="a9", brand="Humira", llm="llama",
               text="Humira is effective for rheumatoid arthritis",
               disease="Rheumatoid Arthritis"),
    ]
    options = h2h.facet_options(corpus, h2h.Selection())
    assert "Humira" not in options[h2h.DIM_BRAND]
    assert "llama" not in options[h2h.DIM_MODEL]


def test_the_verdict_filter_does_not_prune_the_other_pickers():
    """A reader clearing "we lose" has to still find the brands they were looking at."""
    options = h2h.facet_options(
        mixed_corpus(), h2h.Selection.of(verdicts=[verdict_mod.WINNING]),
    )
    assert options[h2h.DIM_BRAND] == ["Rinvoq", "Skyrizi"]


def test_every_offered_option_returns_a_non_empty_board():
    corpus = mixed_corpus()
    options = h2h.facet_options(corpus, h2h.Selection())
    for dim, values in options.items():
        for value in values:
            board = h2h.assemble(corpus, h2h.Selection.of(**{dim: [value]}))
            assert board["pairs_total"] > 0, f"{dim}={value} offered but leads nowhere"
