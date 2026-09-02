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
    (EmailState.AWAITING_REVIEW, EmailAction.START_DRAFT): EmailState.DRAFTING,
    (EmailState.AWAITING_REVIEW, EmailAction.DRAFT_READY): EmailState.AWAITING_REVIEW,
    (EmailState.APPROVED, EmailAction.DRAFT_READY): EmailState.AWAITING_REVIEW,
    (EmailState.SEND_PENDING, EmailAction.DRAFT_READY): EmailState.AWAITING_REVIEW,
    (EmailState.AWAITING_REVIEW, EmailAction.APPROVE): EmailState.APPROVED,
    (EmailState.AWAITING_REVIEW, EmailAction.REJECT): EmailState.REJECTED,
    (EmailState.APPROVED, EmailAction.QUEUE_SEND): EmailState.SEND_PENDING,
    (EmailState.SEND_PENDING, EmailAction.CLAIM_SEND): EmailState.SENDING,
    (EmailState.SENDING, EmailAction.SEND_SUCCEEDED): EmailState.SENT,
    (EmailState.SENDING, EmailAction.SEND_FAILED): EmailState.SEND_RETRY_WAIT,
    (EmailState.SEND_RETRY_WAIT, EmailAction.RETRY_SEND): EmailState.SEND_PENDING,
    (EmailState.SENDING, EmailAction.DELIVERY_AMBIGUOUS): EmailState.DELIVERY_UNKNOWN,
    (EmailState.DELIVERY_UNKNOWN, EmailAction.RECONCILE_SENT): EmailState.SENT,
    (EmailState.DELIVERY_UNKNOWN, EmailAction.RECONCILE_ABSENT): EmailState.SEND_PENDING,
}


def transition(state: EmailState, action: EmailAction) -> EmailState:
    try:
        return _TRANSITIONS[(state, action)]
    except KeyError as error:
        raise InvalidEmailTransition(f"{state.value}:{action.value}") from error
