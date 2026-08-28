from collections.abc import Mapping, Sequence

from app.modules.support.models import HandoffTrigger, SensitiveTopic


def choose_handoff_trigger(
    history: Sequence[Mapping[str, object]],
    *,
    sensitive_topic: SensitiveTopic | None = None,
    system_error: bool = False,
) -> HandoffTrigger | None:
    """Choose only a durable, explainable automatic escalation reason."""
    if sensitive_topic is not None:
        return HandoffTrigger.SENSITIVE_TOPIC
    if system_error:
        return HandoffTrigger.SYSTEM_ERROR
    recent = list(history[-2:])
    if len(recent) == 2 and all(turn.get("refused") is True for turn in recent):
        return HandoffTrigger.REPEATED_FAILURE
    if history:
        latest = history[-1]
        if latest.get("refused") is True or latest.get("supported_material_claims") == 0:
            return HandoffTrigger.LOW_CONFIDENCE
    return None


def detect_sensitive_topic(text: str) -> SensitiveTopic | None:
    normalized = text.casefold()
    phrases = {
        SensitiveTopic.ACCOUNT_SECURITY: ("password", "account hacked", "security code"),
        SensitiveTopic.PAYMENT_DATA: ("card number", "cvv", "payment data"),
        SensitiveTopic.LEGAL_THREAT: ("lawsuit", "legal action", "sue"),
        SensitiveTopic.SAFETY: ("self harm", "suicide", "immediate danger"),
        SensitiveTopic.PRIVACY_REQUEST: ("delete my data", "privacy request", "gdpr"),
    }
    return next(
        (topic for topic, values in phrases.items() if any(item in normalized for item in values)),
        None,
    )
