"""Test discovery task scheduling."""

from unittest.mock import patch


def test_discover_task_skips_when_killed():
    from worker import discovery_tasks

    class _Gov:
        def can_act(self):
            return (False, "kill switch active")

    with patch.object(discovery_tasks, "get_governor", return_value=_Gov()):
        assert discovery_tasks.discover_jobs_task() == 0
