from app.modules.jobs.service import RetryPolicy


def test_exponential_backoff_is_capped_and_jittered():
    policy = RetryPolicy(base_seconds=10, max_seconds=45, jitter=lambda: 0.5)

    assert policy.delay_seconds(attempts=1) == 15
    assert policy.delay_seconds(attempts=2) == 30
    assert policy.delay_seconds(attempts=3) == 45
    assert policy.delay_seconds(attempts=20) == 45


def test_retry_after_is_a_minimum_delay():
    policy = RetryPolicy(base_seconds=10, max_seconds=120, jitter=lambda: 0.0)

    assert policy.delay_seconds(attempts=1, retry_after_seconds=75) == 75
    assert policy.delay_seconds(attempts=5, retry_after_seconds=30) == 120
