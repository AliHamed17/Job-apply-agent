"""Real Redis cross-process proof for the private Ollama inference lease."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import pytest
import redis

from core.config import Settings
from llm.ollama_runtime import _LEASE_KEY, _InferenceLease

_ACTIVE_KEY = "job-agent:test:llm-lease:active"
_MAX_KEY = "job-agent:test:llm-lease:maximum"
_ENTER_SCRIPT = """
local active = redis.call("INCR", KEYS[1])
local maximum = tonumber(redis.call("GET", KEYS[2]) or "0")
if active > maximum then
  redis.call("SET", KEYS[2], active)
end
return active
"""


def _test_redis_url(source: str) -> str:
    parsed = urlsplit(source)
    return urlunsplit((parsed.scheme, parsed.netloc, "/15", parsed.query, ""))


def _lease_process(redis_url: str, start: object, results: object) -> None:
    async def run() -> None:
        settings = Settings(
            _env_file=None,
            app_env="test",
            redis_url=redis_url,
            tasks_always_eager=False,
        )
        start.wait(timeout=10)  # type: ignore[attr-defined]
        async with _InferenceLease(
            settings,
            deadline=datetime.now(UTC) + timedelta(seconds=10),
        ):
            client = redis.Redis.from_url(redis_url, decode_responses=True)
            entered_at = time.monotonic()
            client.eval(_ENTER_SCRIPT, 2, _ACTIVE_KEY, _MAX_KEY)
            await asyncio.sleep(0.2)
            client.decr(_ACTIVE_KEY)
            exited_at = time.monotonic()
            client.close()
            results.put((entered_at, exited_at))  # type: ignore[attr-defined]

    asyncio.run(run())


def test_two_processes_never_hold_real_redis_inference_lease_together() -> None:
    redis_url = _test_redis_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"))
    client = redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    try:
        client.ping()
    except (redis.RedisError, OSError, ConnectionError):
        client.close()
        if os.getenv("CI"):
            pytest.fail("CI Redis service is required for cross-process inference proof")
        pytest.skip("private Redis is not available")

    client.delete(_LEASE_KEY, _ACTIVE_KEY, _MAX_KEY)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(target=_lease_process, args=(redis_url, start, results)) for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(timeout=15)
        if any(process.is_alive() for process in processes):
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            pytest.fail("cross-process inference proof timed out")
        assert [process.exitcode for process in processes] == [0, 0]
        intervals = sorted(results.get(timeout=2) for _ in range(2))
        assert int(client.get(_MAX_KEY) or 0) == 1
        assert int(client.get(_ACTIVE_KEY) or 0) == 0
        assert intervals[0][1] <= intervals[1][0]
    finally:
        client.delete(_LEASE_KEY, _ACTIVE_KEY, _MAX_KEY)
        client.close()
