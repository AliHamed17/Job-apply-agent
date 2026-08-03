"""Regression coverage for API-to-Celery task publication."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from api import task_publication


def test_api_publication_uses_the_configured_celery_application() -> None:
    task = SimpleNamespace(name="worker.discovery_tasks.discover_jobs_task")

    with patch.object(task_publication.celery_app, "send_task") as send_task:
        task_publication.publish_configured_task(task, 7, force=True)

    send_task.assert_called_once_with(
        "worker.discovery_tasks.discover_jobs_task",
        args=(7,),
        kwargs={"force": True},
    )


def test_api_routes_never_publish_through_shared_task_delay() -> None:
    offenders = [
        str(path)
        for path in Path("api/routes").glob("*.py")
        if ".delay(" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
