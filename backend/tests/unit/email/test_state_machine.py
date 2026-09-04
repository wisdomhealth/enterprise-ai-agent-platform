import pytest

from app.modules.email.models import EmailAction, EmailState
from app.modules.email.state_machine import InvalidEmailTransition, transition


def test_email_state_machine_requires_review_before_approval() -> None:
    assert transition(EmailState.INGESTED, EmailAction.START_DRAFT) is EmailState.DRAFTING
    assert transition(EmailState.DRAFTING, EmailAction.DRAFT_READY) is EmailState.AWAITING_REVIEW

    with pytest.raises(InvalidEmailTransition):
        transition(EmailState.INGESTED, EmailAction.APPROVE)


def test_draft_failure_can_only_resume_through_explicit_retry() -> None:
    assert transition(EmailState.DRAFTING, EmailAction.DRAFT_FAILED) is EmailState.DRAFT_RETRY_WAIT
    assert transition(EmailState.DRAFT_RETRY_WAIT, EmailAction.RETRY_DRAFT) is EmailState.DRAFTING


def test_task17_states_have_no_send_transition() -> None:
    with pytest.raises(InvalidEmailTransition):
        transition(EmailState.AWAITING_REVIEW, EmailAction.SEND)
