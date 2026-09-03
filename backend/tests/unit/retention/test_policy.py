from uuid import uuid4

import pytest

from app.modules.retention.models import RetentionPolicy
from app.modules.retention.service import subject_key_hash


def test_product_defaults_are_configurable_not_compliance_flags() -> None:
    policy = RetentionPolicy.default(organization_id=uuid4())

    assert policy.chat_days == 90
    assert policy.email_days == 90
    assert policy.audit_days == 365
    assert policy.version == 1
    assert not hasattr(policy, "gdpr_compliant")
    assert not hasattr(policy, "hipaa_compliant")


@pytest.mark.parametrize("field", ["chat_days", "email_days", "audit_days"])
def test_retention_periods_must_be_positive(field: str) -> None:
    values = {
        "organization_id": uuid4(),
        "chat_days": 90,
        "email_days": 90,
        "audit_days": 365,
    }
    values[field] = 0

    with pytest.raises(ValueError, match="positive"):
        RetentionPolicy.configured(**values)


def test_subject_hash_is_keyed_normalized_and_never_contains_subject() -> None:
    first = subject_key_hash(b"tenant-erasure-key", " Customer@Example.Test ")
    same = subject_key_hash(b"tenant-erasure-key", "customer@example.test")
    different_key = subject_key_hash(b"other-erasure-key", "customer@example.test")

    assert first == same
    assert first != different_key
    assert len(first) == 64
    assert "customer" not in first
