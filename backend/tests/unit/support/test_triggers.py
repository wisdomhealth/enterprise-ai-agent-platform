from app.modules.support.models import HandoffTrigger, SensitiveTopic
from app.modules.support.triggers import choose_handoff_trigger


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
