"""Stable API-to-Celery dispatch boundaries.

Celery ``shared_task`` objects are process-global proxies. Importing another
Celery app can change which concrete task the proxy resolves to, so API tests
and callers should replace this explicit boundary rather than task internals.
"""

from __future__ import annotations


def dispatch_url_processing(url_id: int, *, tasks_always_eager: bool) -> None:
    """Dispatch URL processing in the configured execution mode."""

    from worker.tasks import process_url_task

    if tasks_always_eager:
        process_url_task.apply(args=[url_id])
    else:
        process_url_task.delay(url_id)
