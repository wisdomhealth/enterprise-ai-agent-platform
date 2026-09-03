from celery import Celery  # type: ignore[import-untyped]

from app.core.config import Settings


def create_celery(settings: Settings | None = None) -> Celery:
    settings = settings or Settings()
    broker_url = str(settings.redis_url) if settings.redis_url is not None else "memory://"
    celery_app = Celery("enterprise_ai_agent_platform", broker=broker_url)
    celery_app.conf.update(
        imports=(
            "app.modules.knowledge.tasks",
            "app.modules.chat.tasks",
            "app.modules.email.tasks",
            "app.modules.retention.tasks",
            "app.modules.webhooks.tasks",
        ),
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
            "chat-answer-intent-dispatch": {
                "task": "app.modules.chat.tasks.dispatch_pending_chat_answer_jobs",
                "schedule": 60,
            },
            "gmail-history-poll": {
                "task": "app.modules.email.tasks.gmail_history_poll",
                "schedule": 60,
            },
            "email-intent-dispatch": {
                "task": "app.modules.email.tasks.dispatch_pending_email_jobs",
                "schedule": 60,
            },
            "email-delivery-outbox-dispatch": {
                "task": (
                    "app.modules.email.tasks."
                    "dispatch_pending_email_delivery_outbox_events"
                ),
                "schedule": 60,
            },
            "retention-daily-schedule": {
                "task": "app.modules.retention.tasks.schedule_daily_retention",
                "schedule": 24 * 60 * 60,
            },
            "retention-job-recovery": {
                "task": "app.modules.retention.tasks.dispatch_pending_retention_jobs",
                "schedule": 60,
            },
            "webhook-event-dispatch": {
                "task": "app.modules.webhooks.tasks.dispatch_pending_webhook_events",
                "schedule": 30,
            },
            "webhook-job-recovery": {
                "task": "app.modules.webhooks.tasks.dispatch_pending_webhook_jobs",
                "schedule": 60,
            },
        },
    )
    return celery_app
