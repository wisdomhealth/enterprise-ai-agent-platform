from types import SimpleNamespace

import pytest

from app.modules.support.models import HandoffTrigger, SensitiveTopic
from app.modules.support.triggers import (
    AnthropicStructuredSafetyClassifier,
    SensitiveTopicClassification,
    StructuredSafetyClassifierResponseError,
    choose_handoff_trigger,
)


def test_two_consecutive_refusals_trigger_repeated_failure() -> None:
    history = [{"refused": True}, {"refused": True}]
    assert choose_handoff_trigger(history) is HandoffTrigger.REPEATED_FAILURE


def test_safety_topic_wins_over_other_automatic_trigger() -> None:
    assert (
        choose_handoff_trigger([{"refused": True}], sensitive_topic=SensitiveTopic.SAFETY)
        is HandoffTrigger.SENSITIVE_TOPIC
    )


def test_no_supported_material_claim_is_low_confidence() -> None:
    assert (
        choose_handoff_trigger([{"refused": False, "supported_material_claims": 0}])
        is HandoffTrigger.LOW_CONFIDENCE
    )


class _FakeMessages:
    def __init__(self, payload: str) -> None:
        self._payload = payload

    async def create(self, **_kwargs: object) -> object:
        return SimpleNamespace(content=[SimpleNamespace(text=self._payload)])


class _FakeClient:
    def __init__(self, payload: str) -> None:
        self.messages = _FakeMessages(payload)


@pytest.mark.asyncio
async def test_anthropic_structured_classifier_accepts_only_defined_sensitive_topics() -> None:
    classifier = AnthropicStructuredSafetyClassifier(
        "not-a-real-key",
        client=_FakeClient('{"sensitive_topic":"SAFETY"}'),
    )

    result = await classifier.classify("customer text")

    assert result == SensitiveTopicClassification(sensitive_topic=SensitiveTopic.SAFETY)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        '{"sensitive_topic":"UNKNOWN_TOPIC"}',
        '{"sensitive_topic":"SAFETY","provider_detail":"must not be trusted"}',
        '{"topic":"SAFETY"}',
        "not-json",
    ],
)
async def test_anthropic_structured_classifier_rejects_malformed_or_unknown_results(
    payload: str,
) -> None:
    classifier = AnthropicStructuredSafetyClassifier("not-a-real-key", client=_FakeClient(payload))

    with pytest.raises(StructuredSafetyClassifierResponseError):
        await classifier.classify("customer text")
