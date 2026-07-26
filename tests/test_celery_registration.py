"""Regression guard: every task Celery Beat schedules (and every task the
pipeline routes) must actually be registered by a real worker.

The failure this prevents: ``autodiscover_tasks(["worker"])`` only imports
``worker/tasks.py`` (Celery's related-name convention), so sibling task
modules (``worker.drainer``, ``worker.discovery_tasks``, ``worker.digest``)
were NOT registered. Beat would happily enqueue their messages and no worker
would ever pick them up — auto-approved applications stuck APPROVED,
discovery/digests silently never running. The fix is an explicit
``include=[...]`` on the Celery app; this test fails if that regresses.

``loader.import_default_modules()`` is exactly what a worker calls at
bootstrap to import ``conf.include`` + ``conf.imports``, so asserting against
the registry afterwards reproduces the real worker's view (a bare
``celery_app.tasks`` before bootstrap does not).
"""

from __future__ import annotations

import celery._state as _celery_state
import pytest


@pytest.fixture(autouse=True)
def _restore_current_celery_app():
    # create_celery_app() (run at import time) calls set_as_current(), which
    # changes the process-global current app that other modules' @shared_task
    # proxies resolve against. Restore it so this file's import side effect
    # doesn't leak into tests that patch worker.tasks.<task>.apply later.
    previous = _celery_state.get_current_app()
    yield
    previous.set_current()


def _registered_task_names() -> set[str]:
    from worker.celery_app import celery_app

    celery_app.loader.import_default_modules()
    return set(celery_app.tasks.keys())


def test_every_beat_scheduled_task_is_registered():
    from worker.celery_app import celery_app

    registered = _registered_task_names()
    schedule = celery_app.conf.beat_schedule or {}
    assert schedule, "no beat schedule configured — expected drain/expire/discover/digest"

    missing = {
        entry: cfg["task"] for entry, cfg in schedule.items() if cfg["task"] not in registered
    }
    assert not missing, (
        f"Beat schedules tasks that no worker registers: {missing}. "
        "Add the owning module to the Celery app's include=[...]."
    )


def test_every_routed_task_is_registered():
    """Every task_routes entry must also be a real, registered task."""
    from worker.celery_app import celery_app

    registered = _registered_task_names()
    routes = celery_app.conf.task_routes or {}
    missing = [name for name in routes if name not in registered]
    assert not missing, f"task_routes references unregistered tasks: {missing}"


def test_core_and_v2_task_modules_all_present():
    """Explicit belt-and-suspenders: the specific task names the full-auto
    pipeline depends on must all be registered by a real worker."""
    registered = _registered_task_names()
    required = {
        "worker.tasks.process_message_task",
        "worker.tasks.process_url_task",
        "worker.tasks.score_job_task",
        "worker.tasks.generate_application_task",
        "worker.tasks.submit_application_task",
        "worker.submission_commands.execute_submission_command_task",
        "worker.submission_commands.drain_submission_commands_task",
        "worker.submission_commands.reconcile_stale_commands_task",
        "worker.drainer.drain_apply_queue_task",
        "worker.drainer.expire_stale_jobs_task",
        "worker.discovery_tasks.discover_jobs_task",
        "worker.digest.send_daily_digest_task",
    }
    missing = sorted(required - registered)
    assert not missing, f"expected pipeline tasks not registered by worker: {missing}"


def test_database_outbox_recovery_runs_well_before_permit_expiry():
    from core.config import get_settings
    from worker.celery_app import celery_app

    settings = get_settings()
    schedule = celery_app.conf.beat_schedule["drain-submission-commands"]["schedule"]
    assert float(schedule) < settings.submit_permit_ttl_seconds
    assert settings.submission_command_drain_batch_size > 1
