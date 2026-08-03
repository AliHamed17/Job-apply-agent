"""Regression coverage for API-to-Celery task publication."""

from api.main import app  # noqa: F401
from worker.celery_app import celery_app as configured_celery_app
from worker.discovery_tasks import discover_jobs_task
from worker.tasks import process_url_task


def test_api_shared_tasks_use_the_configured_celery_application() -> None:
    """API imports must never leave queued tasks on Celery's localhost app."""

    assert discover_jobs_task.app is configured_celery_app
    assert process_url_task.app is configured_celery_app
    assert discover_jobs_task.app.main == "job_apply_agent"
    assert process_url_task.app.conf.broker_url == configured_celery_app.conf.broker_url
