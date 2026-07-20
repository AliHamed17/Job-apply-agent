"""IMPORTANT #3 regression coverage — the daily-digest beat entry enqueues
worker.digest.send_daily_digest_task, but a worker only consumes queues it
was started with (`-Q ingestion,processing,llm,submission,discovery`). Without
a task_routes entry the task defaults to Celery's "celery" queue, which no
running worker consumes, so the beat entry silently does nothing forever.

Note: instantiating a Celery app (``create_celery_app()``, run at import
time by ``worker.celery_app``) calls ``set_as_current`` by default, which
changes the process-global "current app" that every other module's
``@shared_task`` proxies resolve against — this would silently break
``unittest.mock.patch("worker.tasks.<task>.apply", ...)`` in *other* test
files that run afterward in the same session. The fixture below restores
whatever app was current before this module ran its import, so this test
file's side effect doesn't leak into the rest of the suite.
"""

from __future__ import annotations

import celery._state as _celery_state
import pytest


@pytest.fixture(autouse=True)
def _restore_current_celery_app():
    previous = _celery_state.get_current_app()
    yield
    previous.set_current()


def test_daily_digest_task_routed_to_consumed_queue():
    from worker.celery_app import celery_app

    routes = celery_app.conf.task_routes
    assert routes["worker.digest.send_daily_digest_task"]["queue"] == "submission"


def test_daily_digest_beat_entry_still_registered():
    from worker.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    assert schedule["daily-digest"]["task"] == "worker.digest.send_daily_digest_task"
