import pytest

from app.modules.email.models import EmailAction, EmailState
from app.modules.email.state_machine import InvalidEmailTransition, transition


def test_new_draft_invalidates_approved_state() -> None:
    assert transition(EmailState.APPROVED, EmailAction.DRAFT_READY) is EmailState.AWAITING_REVIEW


def test_regeneration_uses_explicit_drafting_round_trip() -> None:
    assert transition(EmailState.AWAITING_REVIEW, EmailAction.START_DRAFT) is EmailState.DRAFTING
    assert transition(EmailState.DRAFTING, EmailAction.DRAFT_READY) is EmailState.AWAITING_REVIEW


def test_approval_and_rejection_require_awaiting_review() -> None:
    assert transition(EmailState.AWAITING_REVIEW, EmailAction.APPROVE) is EmailState.APPROVED
    assert transition(EmailState.AWAITING_REVIEW, EmailAction.REJECT) is EmailState.REJECTED
    with pytest.raises(InvalidEmailTransition):
        transition(EmailState.INGESTED, EmailAction.APPROVE)
