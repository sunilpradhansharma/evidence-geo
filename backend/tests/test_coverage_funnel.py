"""The coverage funnel's state precedence.

`coverage_report` counts a comparison as covered the moment a question exists — including one
still sitting unreviewed. On the real bank that reads 15.9% covered while only 1.8% of
comparisons have actually been answered by a model. These tests pin the distinction so the
two numbers cannot silently collapse back into one.
"""
import pytest

from app.curation import service as svc


def state(**kw):
    base = {"answered": False, "approved": False, "pending": False, "staged_status": None}
    base.update(kw)
    return svc.cell_state(**base)


def test_nothing_written_is_not_asked():
    assert state() == svc.STATE_NOT_ASKED


def test_a_question_awaiting_review_is_not_yet_monitored():
    """The defect this whole funnel exists to expose."""
    assert state(pending=True) == svc.STATE_IN_REVIEW


@pytest.mark.parametrize("staged", ["CLASSIFIED", "QUARANTINED_AE"])
def test_a_staged_candidate_counts_as_in_review(staged):
    assert state(staged_status=staged) == svc.STATE_IN_REVIEW


def test_approved_but_never_run_is_its_own_state():
    """Approval is not monitoring: nobody has asked a model yet."""
    assert state(approved=True) == svc.STATE_APPROVED_NOT_RUN


def test_only_a_scored_answer_counts_as_monitored():
    assert state(answered=True) == svc.STATE_ANSWERED


def test_a_declined_cell_is_not_reported_as_backlog():
    """`_stage_one` refuses to overwrite a decided row, so a declined cell can never be
    generated again — reporting it as a gap would invite work the generator will refuse."""
    assert state(staged_status="REJECTED") == svc.STATE_DECLINED


def test_answered_outranks_every_weaker_state():
    """One answered question means the comparison IS being watched, whatever else exists."""
    assert state(
        answered=True, approved=True, pending=True, staged_status="CLASSIFIED",
    ) == svc.STATE_ANSWERED


def test_approved_outranks_in_review():
    assert state(approved=True, pending=True) == svc.STATE_APPROVED_NOT_RUN


def test_a_live_candidate_outranks_an_earlier_rejection():
    """A rejected row plus a pending bank question means someone is looking again."""
    assert state(pending=True, staged_status="REJECTED") == svc.STATE_IN_REVIEW


def test_every_state_has_a_plain_language_label():
    """A count is never rendered without the sentence that explains it."""
    for s in svc.FUNNEL_STATES:
        assert svc.STATE_LABELS[s]
        assert not svc.STATE_LABELS[s].isupper()


def test_states_are_ordered_most_monitored_first():
    assert svc.FUNNEL_STATES[0] == svc.STATE_ANSWERED
    assert svc.FUNNEL_STATES[-1] == svc.STATE_NOT_ASKED
