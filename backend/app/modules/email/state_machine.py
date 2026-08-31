from app.modules.email.models import EmailAction, EmailState


class InvalidEmailTransition(ValueError):
    pass


_TRANSITIONS: dict[tuple[EmailState, EmailAction], EmailState] = {
    (EmailState.INGESTED, EmailAction.START_DRAFT): EmailState.DRAFTING,
    (EmailState.INGESTED, EmailAction.CLASSIFICATION_FAILED): EmailState.DRAFT_RETRY_WAIT,
    (EmailState.DRAFTING, EmailAction.CLASSIFIED_NO_DRAFT): EmailState.INGESTED,
    (EmailState.DRAFTING, EmailAction.DRAFT_READY): EmailState.AWAITING_REVIEW,
    (EmailState.DRAFTING, EmailAction.DRAFT_FAILED): EmailState.DRAFT_RETRY_WAIT,
    (EmailState.DRAFT_RETRY_WAIT, EmailAction.RETRY_DRAFT): EmailState.DRAFTING,
}


def transition(state: EmailState, action: EmailAction) -> EmailState:
    try:
        return _TRANSITIONS[(state, action)]
    except KeyError as error:
        raise InvalidEmailTransition(f"{state.value}:{action.value}") from error
