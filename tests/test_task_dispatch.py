"""Tests for stable API-to-Celery dispatch boundaries."""

from unittest.mock import MagicMock, patch

import pytest

from worker.task_dispatch import dispatch_url_processing


@pytest.mark.parametrize("eager", [True, False])
def test_dispatch_url_processing_uses_exactly_one_execution_mode(eager: bool) -> None:
    task = MagicMock()
    with patch("worker.tasks.process_url_task", task):
        dispatch_url_processing(41, tasks_always_eager=eager)

    if eager:
        task.apply.assert_called_once_with(args=[41])
        task.delay.assert_not_called()
    else:
        task.apply.assert_not_called()
        task.delay.assert_called_once_with(41)
