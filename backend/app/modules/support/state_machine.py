from app.modules.chat.models import ConversationState
from app.modules.support.models import SupportAction


class InvalidTransition(ValueError):
    pass


_TRANSITIONS: dict[tuple[ConversationState, SupportAction], ConversationState] = {
    (
        ConversationState.AI_ACTIVE,
        SupportAction.REQUEST_HANDOFF,
    ): ConversationState.HANDOFF_REQUESTED,
    (ConversationState.HANDOFF_REQUESTED, SupportAction.QUEUE): ConversationState.QUEUED,
    (ConversationState.QUEUED, SupportAction.CLAIM): ConversationState.HUMAN_ACTIVE,
    (ConversationState.HUMAN_ACTIVE, SupportAction.REPLY): ConversationState.HUMAN_ACTIVE,
    (ConversationState.HUMAN_ACTIVE, SupportAction.RESOLVE): ConversationState.RESOLVED,
    (ConversationState.HUMAN_ACTIVE, SupportAction.RESUME_AI): ConversationState.AI_ACTIVE,
}


def transition(state: ConversationState, action: SupportAction) -> ConversationState | None:
    if action is SupportAction.TIMEOUT:
        return None
    try:
        return _TRANSITIONS[(state, action)]
    except KeyError as error:
        raise InvalidTransition(f"{state}:{action}") from error
