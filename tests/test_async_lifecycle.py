from __future__ import annotations

import asyncio
import contextvars
import threading

import pytest

from core.async_lifecycle import (
    SameEventLoopLifecycle,
    cleanup_prepared_action_if_supported,
)


def test_lifecycle_reuses_loop_thread_and_copies_each_call_context():
    marker = contextvars.ContextVar("same_loop_test_marker", default="unset")
    caller_thread = threading.get_ident()

    async def observe():
        return id(asyncio.get_running_loop()), threading.get_ident(), marker.get()

    with SameEventLoopLifecycle() as lifecycle:
        first_token = marker.set("first")
        try:
            first = lifecycle.run(observe())
        finally:
            marker.reset(first_token)

        second_token = marker.set("second")
        try:
            second = lifecycle.run(observe())
        finally:
            marker.reset(second_token)

    assert first[:2] == second[:2]
    assert first[1] != caller_thread
    assert first[2] == "first"
    assert second[2] == "second"


@pytest.mark.asyncio
async def test_lifecycle_is_safe_when_caller_event_loop_is_already_running():
    caller_loop = id(asyncio.get_running_loop())

    async def observe():
        return id(asyncio.get_running_loop())

    with SameEventLoopLifecycle() as lifecycle:
        first = lifecycle.run(observe())
        second = lifecycle.run(observe())

    assert first == second
    assert first != caller_loop


def test_optional_cleanup_runs_on_the_existing_lifecycle_loop():
    class Executor:
        cleanup_observation = None

        async def cleanup_prepared_action(self, *, action):
            self.cleanup_observation = (
                id(asyncio.get_running_loop()),
                threading.get_ident(),
                action,
            )

    async def observe():
        return id(asyncio.get_running_loop()), threading.get_ident()

    executor = Executor()
    action = object()
    with SameEventLoopLifecycle() as lifecycle:
        loop_and_thread = lifecycle.run(observe())
        cleaned = lifecycle.run(
            cleanup_prepared_action_if_supported(
                executor,
                action=action,
            )
        )

    assert cleaned is True
    assert executor.cleanup_observation == (*loop_and_thread, action)


def test_adapter_context_survives_calls_while_caller_context_is_scoped():
    adapter_state = contextvars.ContextVar("adapter_lifecycle_state", default=None)
    caller_guard = contextvars.ContextVar("caller_lifecycle_guard", default=False)

    async def prepare():
        adapter_state.set("prepared")
        return caller_guard.get()

    async def cleanup():
        return adapter_state.get(), caller_guard.get()

    with SameEventLoopLifecycle() as lifecycle:
        first_token = caller_guard.set(True)
        try:
            assert lifecycle.run(prepare()) is True
        finally:
            caller_guard.reset(first_token)

        second_token = caller_guard.set(True)
        try:
            assert lifecycle.run(cleanup()) == ("prepared", True)
        finally:
            caller_guard.reset(second_token)

    assert caller_guard.get() is False
