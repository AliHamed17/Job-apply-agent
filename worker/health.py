"""Worker and scheduler heartbeat publishing."""

from __future__ import annotations

from celery import shared_task
from celery.signals import beat_init, heartbeat_sent, worker_ready

from core.operations import browser_available, record_heartbeat


@worker_ready.connect
@heartbeat_sent.connect
def worker_heartbeat(**_kwargs) -> None:
    record_heartbeat("worker")
    if browser_available():
        record_heartbeat("browser")


@beat_init.connect
def beat_heartbeat(**_kwargs) -> None:
    record_heartbeat("beat")


@shared_task(name="worker.health.beat_heartbeat_task")
def beat_heartbeat_task() -> None:
    record_heartbeat("beat")
