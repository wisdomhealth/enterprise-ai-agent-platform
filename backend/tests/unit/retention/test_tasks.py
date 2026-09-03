from app.core.celery import create_celery
from app.modules.retention.tasks import (
    RETENTION_JOB_TASK_NAME,
    dispatch_pending_retention_jobs,
    retention_job,
    schedule_daily_retention,
)


def test_retention_tasks_are_registered_for_daily_work_and_recovery() -> None:
    assert retention_job.name == RETENTION_JOB_TASK_NAME
    assert schedule_daily_retention.name == "app.modules.retention.tasks.schedule_daily_retention"
    assert (
        dispatch_pending_retention_jobs.name
        == "app.modules.retention.tasks.dispatch_pending_retention_jobs"
    )

    schedule = create_celery().conf.beat_schedule
    assert schedule["retention-daily-schedule"] == {
        "task": "app.modules.retention.tasks.schedule_daily_retention",
        "schedule": 24 * 60 * 60,
    }
    assert schedule["retention-job-recovery"] == {
        "task": "app.modules.retention.tasks.dispatch_pending_retention_jobs",
        "schedule": 60,
    }
