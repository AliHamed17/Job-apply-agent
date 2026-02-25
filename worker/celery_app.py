"""Celery application configuration."""

from __future__ import annotations

from celery import Celery

from core.config import get_settings


def create_celery_app() -> Celery:
    """Create and configure the Celery application."""
    settings = get_settings()

    app = Celery(
        "job_apply_agent",
        broker=settings.redis_url,
        backend=settings.redis_url,
    )

    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        # Rate limiting
        task_default_rate_limit=f"{settings.rate_limit_requests_per_minute}/m",
        # Retry policy
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        # Task routing
        task_routes={
            "worker.tasks.process_message_task": {"queue": "ingestion"},
            "worker.tasks.process_url_task": {"queue": "processing"},
            "worker.tasks.score_job_task": {"queue": "processing"},
            "worker.tasks.generate_application_task": {"queue": "llm"},
            "worker.tasks.submit_application_task": {"queue": "submission"},
        },
    )

    app.autodiscover_tasks(["worker"])
    return app


celery_app = create_celery_app()
