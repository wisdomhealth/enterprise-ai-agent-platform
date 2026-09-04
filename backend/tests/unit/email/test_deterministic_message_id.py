from uuid import UUID

from app.modules.email.delivery import deterministic_message_id


def test_message_id_is_stable_for_delivery_intent() -> None:
    intent_id = UUID("12345678-1234-5678-1234-567812345678")

    assert deterministic_message_id(intent_id, "mail.example.com") == (
        "<delivery-12345678-1234-5678-1234-567812345678@mail.example.com>"
    )


def test_message_id_rejects_unsafe_domain() -> None:
    intent_id = UUID("12345678-1234-5678-1234-567812345678")

    try:
        deterministic_message_id(intent_id, "mail.example.com\r\nBcc: attacker@example.test")
    except ValueError as error:
        assert str(error) == "invalid Message-ID domain"
    else:
        raise AssertionError("unsafe Message-ID domain was accepted")
