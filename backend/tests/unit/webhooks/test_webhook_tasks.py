from app.core.celery import create_celery
from app.modules.webhooks.tasks import (
    WEBHOOK_DELIVERY_TASK_NAME,
    dispatch_pending_webhook_events,
    dispatch_pending_webhook_jobs,
    webhook_delivery_job,
)


def test_webhook_tasks_are_registered_for_dispatch_delivery_and_recovery() -> None:
    assert webhook_delivery_job.name == WEBHOOK_DELIVERY_TASK_NAME
    assert (
        dispatch_pending_webhook_events.name
        == "app.modules.webhooks.tasks.dispatch_pending_webhook_events"
    )
    assert (
        dispatch_pending_webhook_jobs.name
        == "app.modules.webhooks.tasks.dispatch_pending_webhook_jobs"
    )

    celery = create_celery()
    assert "app.modules.webhooks.tasks" in celery.conf.imports
    assert celery.conf.beat_schedule["webhook-event-dispatch"] == {
        "task": "app.modules.webhooks.tasks.dispatch_pending_webhook_events",
        "schedule": 30,
    }
    assert celery.conf.beat_schedule["webhook-job-recovery"] == {
        "task": "app.modules.webhooks.tasks.dispatch_pending_webhook_jobs",
        "schedule": 60,
    }
