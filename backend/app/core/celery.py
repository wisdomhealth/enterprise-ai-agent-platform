from celery import Celery  # type: ignore[import-untyped]

from app.core.config import Settings


def create_celery(settings: Settings | None = None) -> Celery:
    settings = settings or Settings()
    broker_url = str(settings.redis_url) if settings.redis_url is not None else "memory://"
    celery_app = Celery("enterprise_ai_agent_platform", broker=broker_url)
    celery_app.conf.update(
        result_backend=None,
        task_ignore_result=True,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        broker_connection_retry_on_startup=True,
    )
    return celery_app
