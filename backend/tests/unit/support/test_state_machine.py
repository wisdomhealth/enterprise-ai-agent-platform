import pytest

from app.modules.chat.models import ConversationState
from app.modules.support.state_machine import InvalidTransition, SupportAction, transition


def test_human_active_returns_to_ai_only_by_explicit_resume() -> None:
    assert transition(ConversationState.HUMAN_ACTIVE, SupportAction.TIMEOUT) is None
    assert (
        transition(ConversationState.HUMAN_ACTIVE, SupportAction.RESUME_AI)
        is ConversationState.AI_ACTIVE
    )


def test_required_handoff_transition_path_is_explicit() -> None:
    assert (
        transition(ConversationState.AI_ACTIVE, SupportAction.REQUEST_HANDOFF)
        is ConversationState.HANDOFF_REQUESTED
    )
    assert (
        transition(ConversationState.HANDOFF_REQUESTED, SupportAction.QUEUE)
        is ConversationState.QUEUED
    )
    assert (
        transition(ConversationState.QUEUED, SupportAction.CLAIM) is ConversationState.HUMAN_ACTIVE
    )


def test_invalid_transition_raises() -> None:
    with pytest.raises(InvalidTransition):
        transition(ConversationState.AI_ACTIVE, SupportAction.RESUME_AI)
