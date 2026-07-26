"""Context-local guard that prohibits LLM calls during final external actions."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from llm.contracts import LLMReasonCode, TypedGenerationError

_LLM_PROHIBITION_DEPTH: ContextVar[int] = ContextVar(
    "job_agent_llm_prohibition_depth",
    default=0,
)


@contextmanager
def prohibit_llm_generation() -> Iterator[None]:
    """Prohibit generation in this task context, including child tasks."""

    token = _LLM_PROHIBITION_DEPTH.set(_LLM_PROHIBITION_DEPTH.get() + 1)
    try:
        yield
    finally:
        _LLM_PROHIBITION_DEPTH.reset(token)


def assert_llm_generation_allowed() -> None:
    """Fail with one stable code before prompt data reaches any provider."""

    if _LLM_PROHIBITION_DEPTH.get() > 0:
        raise TypedGenerationError(
            LLMReasonCode.STAGE_PROHIBITED,
            "LLM generation is prohibited during the final external-action stage",
        )
