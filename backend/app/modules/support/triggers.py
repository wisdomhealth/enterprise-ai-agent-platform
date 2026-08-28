from collections.abc import Mapping, Sequence
from typing import Protocol

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


class StructuredSafetyClassifier(Protocol):
    """A typed safety boundary; unstructured keyword matching is forbidden."""

    async def classify(self, text: str) -> SensitiveTopic | None: ...


class NoSensitiveTopicClassifier:
    """Explicit default used until a configured structured classifier is supplied."""

    async def classify(self, _text: str) -> SensitiveTopic | None:
        return None
