"""Celery application configuration."""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from core.config import get_settings


def create_celery_app() -> Celery:
    """Create and configure the Celery application."""
    settings = get_settings()

    broker = settings.redis_url
    backend = settings.redis_url

    if settings.tasks_always_eager:
        broker = "memory://"
        backend = "cache+memory://"

    app = Celery(
        "job_apply_agent",
        broker=broker,
        backend=backend,
        # autodiscover_tasks(["worker"]) below only imports worker/tasks.py
        # (Celery's "related_name" convention) — it does not pick up sibling
        # task modules. Without an explicit import, a real worker process
        # never registers these, so beat's scheduled messages for them
        # arrive as unregistered tasks and silently never run.
        include=[
            "worker.drainer",
            "worker.discovery_tasks",
            "worker.digest",
        ],
    )

    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        task_always_eager=settings.tasks_always_eager,
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
            "worker.drainer.drain_apply_queue_task": {"queue": "submission"},
            "worker.drainer.expire_stale_jobs_task": {"queue": "submission"},
            "worker.drainer.reconcile_stale_attempts_task": {"queue": "submission"},
            "worker.discovery_tasks.discover_jobs_task": {"queue": "discovery"},
            # Routed onto "submission" (not a new queue) so the existing
            # `-Q ingestion,processing,llm,submission,discovery` worker
            # command already consumes it — otherwise the daily-digest
            # beat entry below enqueues a task nothing ever picks up.
            "worker.digest.send_daily_digest_task": {"queue": "submission"},
        },
    )

    # Beat schedule — priority apply-queue drainer + stale-job TTL expiry
    # (Task 3.6) + LinkedIn discovery (Task 4.4). Preserve any beat entries already registered elsewhere.
    app.conf.beat_schedule = getattr(app.conf, "beat_schedule", None) or {}
    app.conf.beat_schedule["drain-apply-queue"] = {
        "task": "worker.drainer.drain_apply_queue_task",
        "schedule": 300.0,  # every 5 min; governor enforces gaps/caps
    }
    app.conf.beat_schedule["expire-stale-jobs"] = {
        "task": "worker.drainer.expire_stale_jobs_task",
        "schedule": crontab(hour=3, minute=0),
    }
    app.conf.beat_schedule["reconcile-stale-submission-attempts"] = {
        "task": "worker.drainer.reconcile_stale_attempts_task",
        "schedule": 300.0,
    }
    from core.config import get_settings as _gs  # noqa: E402, F401
    _interval = _gs().discovery_interval_h * 3600
    app.conf.beat_schedule["discover-jobs"] = {
        "task": "worker.discovery_tasks.discover_jobs_task",
        "schedule": float(_interval),
    }
    app.conf.beat_schedule["daily-digest"] = {
        "task": "worker.digest.send_daily_digest_task",
        "schedule": crontab(hour=20, minute=0),
    }

    app.autodiscover_tasks(["worker"])
    return app


celery_app = create_celery_app()
