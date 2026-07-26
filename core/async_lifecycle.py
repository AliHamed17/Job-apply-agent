"""Run related async operations on one durable event-loop lifecycle.

Browser adapters prepare an ephemeral page during preflight and use that exact
page for the final action.  Creating a new ``asyncio.run`` loop for each call
would invalidate loop-bound browser objects, so this module keeps one
``asyncio.Runner`` on one dedicated thread while synchronous callers perform
durable database work between async operations.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import threading
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

_T = TypeVar("_T")


def _create_runner() -> asyncio.Runner:
    runner = asyncio.Runner()
    # Initialize the loop on the worker thread that will own every run/close.
    runner.get_loop()
    return runner


def _run_on_runner(
    runner: asyncio.Runner,
    coroutine: Coroutine[Any, Any, _T],
    lifecycle_context: contextvars.Context,
    caller_context: contextvars.Context,
) -> _T:
    async def run_with_caller_context() -> _T:
        tokens: list[tuple[contextvars.ContextVar[Any], contextvars.Token[Any]]] = []
        try:
            # Caller controls such as the final-stage LLM guard apply to this
            # operation only. Adapter-owned ContextVars that are set by the
            # coroutine remain in ``lifecycle_context`` for the next operation.
            for variable, value in caller_context.items():
                tokens.append((variable, variable.set(value)))
            return await coroutine
        finally:
            for variable, token in reversed(tokens):
                variable.reset(token)

    return runner.run(
        run_with_caller_context(),
        context=lifecycle_context,
    )


class SameEventLoopLifecycle:
    """Synchronously drive several coroutines on one loop and one thread."""

    def __init__(self, *, thread_name: str = "submission-async-lifecycle") -> None:
        self._thread_name = thread_name
        self._executor: ThreadPoolExecutor | None = None
        self._runner: asyncio.Runner | None = None
        self._context: contextvars.Context | None = None
        self._call_lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        return self._executor is not None and self._runner is not None and self._context is not None

    def open(self) -> SameEventLoopLifecycle:
        if self.is_open:
            raise RuntimeError("async lifecycle is already open")
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=self._thread_name)
        try:
            runner = executor.submit(_create_runner).result()
        except BaseException:
            executor.shutdown(wait=True, cancel_futures=True)
            raise
        self._executor = executor
        self._runner = runner
        self._context = contextvars.Context()
        return self

    def run(self, coroutine: Coroutine[Any, Any, _T]) -> _T:
        """Run one coroutine with caller ContextVars on the lifecycle loop."""

        executor = self._executor
        runner = self._runner
        lifecycle_context = self._context
        if executor is None or runner is None or lifecycle_context is None:
            coroutine.close()
            raise RuntimeError("async lifecycle is not open")

        caller_context = contextvars.copy_context()
        with self._call_lock:
            try:
                future = executor.submit(
                    _run_on_runner,
                    runner,
                    coroutine,
                    lifecycle_context,
                    caller_context,
                )
            except BaseException:
                coroutine.close()
                raise
            return future.result()

    def close(self) -> None:
        """Close the runner on its owner thread and release the worker."""

        executor = self._executor
        runner = self._runner
        self._executor = None
        self._runner = None
        self._context = None
        if executor is None:
            return
        try:
            if runner is not None:
                with self._call_lock:
                    executor.submit(runner.close).result()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    def __enter__(self) -> SameEventLoopLifecycle:
        return self.open()

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


async def cleanup_prepared_action_if_supported(
    executor: object,
    *,
    action: object | None,
) -> bool:
    """Invoke an adapter's optional cleanup hook on the active lifecycle loop."""

    cleanup = getattr(executor, "cleanup_prepared_action", None)
    if not callable(cleanup):
        return False
    result = cleanup(action=action)
    if inspect.isawaitable(result):
        await result
    return True
