"""Explicit Celery publication for FastAPI request handlers."""

from __future__ import annotations

from typing import Protocol

from worker.celery_app import celery_app


class NamedTask(Protocol):
    """Minimum task identity required for configured publication."""

    name: str


def publish_configured_task(task: NamedTask, *args: object, **kwargs: object) -> object:
    """Publish through the configured app instead of a shared-task proxy."""

    task_name = str(task.name or "").strip()
    if not task_name:
        raise ValueError("CELERY_TASK_NAME_REQUIRED")
    return celery_app.send_task(task_name, args=args, kwargs=kwargs)
