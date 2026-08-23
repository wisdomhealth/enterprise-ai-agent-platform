from celery import Celery  # type: ignore[import-untyped]

from app.core.config import Settings


def create_celery(settings: Settings | None = None) -> Celery:
    settings = settings or Settings()
    broker_url = str(settings.redis_url) if settings.redis_url is not None else "memory://"
    celery_app = Celery("enterprise_ai_agent_platform", broker=broker_url)
    celery_app.conf.update(
        imports=("app.modules.knowledge.tasks",),
        result_backend=None,
        task_ignore_result=True,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        broker_connection_retry_on_startup=True,
        beat_schedule={
            "knowledge-drive-source-sync": {
                "task": "app.modules.knowledge.tasks.drive_source_sync",
                "schedule": 15 * 60,
            },
            "knowledge-drive-sync-outbox-dispatch": {
                "task": "app.modules.knowledge.tasks.dispatch_pending_drive_sync_outbox_events",
                "schedule": 60,
            },
        },
    )
    return celery_app
