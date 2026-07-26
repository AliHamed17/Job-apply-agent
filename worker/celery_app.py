"""Celery application configuration."""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab
from celery.signals import beat_init, worker_init

from core.config import Settings, get_settings


def validate_celery_runtime(settings: Settings | None = None) -> Settings:
    """Fail closed before a worker or Beat process can start."""

    resolved = settings or get_settings()
    resolved.validate_runtime()
    return resolved


@worker_init.connect
def validate_worker_startup(**_kwargs: object) -> None:
    """Revalidate when Celery initializes a worker process."""

    validate_celery_runtime()


@beat_init.connect
def validate_beat_startup(**_kwargs: object) -> None:
    """Revalidate when Celery initializes the scheduler process."""

    validate_celery_runtime()


def create_celery_app(settings: Settings | None = None) -> Celery:
    """Validate runtime safety, then create and configure Celery."""

    settings = validate_celery_runtime(settings)

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
            "worker.health",
            "worker.submission_commands",
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
            "worker.submission_commands.execute_submission_command_task": {"queue": "submission"},
            "worker.submission_commands.drain_submission_commands_task": {"queue": "submission"},
            "worker.submission_commands.reconcile_stale_commands_task": {"queue": "submission"},
            "worker.drainer.drain_apply_queue_task": {"queue": "submission"},
            "worker.drainer.expire_stale_jobs_task": {"queue": "submission"},
            "worker.drainer.reconcile_stale_attempts_task": {"queue": "submission"},
            "worker.discovery_tasks.discover_jobs_task": {"queue": "discovery"},
            # Routed onto "submission" (not a new queue) so the existing
            # `-Q ingestion,processing,llm,submission,discovery` worker
            # command already consumes it — otherwise the daily-digest
            # beat entry below enqueues a task nothing ever picks up.
            "worker.digest.send_daily_digest_task": {"queue": "submission"},
            "worker.health.beat_heartbeat_task": {"queue": "submission"},
        },
    )

    # Beat schedule — priority apply-queue drainer + stale-job TTL expiry
    # (Task 3.6) + LinkedIn discovery (Task 4.4).
    # Preserve any beat entries already registered elsewhere.
    app.conf.beat_schedule = getattr(app.conf, "beat_schedule", None) or {}
    app.conf.beat_schedule["drain-submission-commands"] = {
        "task": "worker.submission_commands.drain_submission_commands_task",
        "schedule": float(
            max(
                1,
                min(
                    settings.submission_command_drain_interval_seconds,
                    max(1, settings.submit_permit_ttl_seconds // 4),
                ),
            )
        ),
    }
    app.conf.beat_schedule["expire-stale-jobs"] = {
        "task": "worker.drainer.expire_stale_jobs_task",
        "schedule": crontab(hour=3, minute=0),
    }
    app.conf.beat_schedule["reconcile-stale-submission-commands"] = {
        "task": "worker.submission_commands.reconcile_stale_commands_task",
        "schedule": 300.0,
    }
    _interval = settings.discovery_interval_h * 3600
    app.conf.beat_schedule["discover-jobs"] = {
        "task": "worker.discovery_tasks.discover_jobs_task",
        "schedule": float(_interval),
    }
    app.conf.beat_schedule["daily-digest"] = {
        "task": "worker.digest.send_daily_digest_task",
        "schedule": crontab(hour=20, minute=0),
    }
    app.conf.beat_schedule["beat-heartbeat"] = {
        "task": "worker.health.beat_heartbeat_task",
        "schedule": 30.0,
    }

    app.autodiscover_tasks(["worker"])
    return app


celery_app = create_celery_app()
