"""Fail-closed local Ollama transport with bounded concurrency and health state."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import threading
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import redis
import redis.asyncio as async_redis

from core.config import Settings, is_allowed_local_ollama_endpoint
from llm.contracts import (
    LLMReasonCode,
    ModelIdentity,
    TypedGenerationError,
)
from llm.execution_guard import assert_llm_generation_allowed
from llm.qualification_registry import (
    expected_qualified_model_digest,
    is_qualified_local_model_identity,
)

_LOCAL_INFERENCE_LOCK = threading.Lock()
_LEASE_KEY = "job-agent:llm:ollama:inference"
_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("del", KEYS[1])
end
return 0
"""
_CIRCUIT_FAILURE_SCRIPT = """
if redis.call("EXISTS", KEYS[2]) == 1 then
  return -1
end
local failures = redis.call("INCR", KEYS[1])
redis.call("PEXPIRE", KEYS[1], ARGV[3])
if failures >= tonumber(ARGV[1]) then
  redis.call("SET", KEYS[2], "1", "PX", ARGV[2])
  redis.call("DEL", KEYS[1])
  return 1
end
return 0
"""
_CIRCUIT_SUCCESS_SCRIPT = """
if redis.call("EXISTS", KEYS[2]) == 1 then
  return -1
end
redis.call("DEL", KEYS[1])
return 1
"""


@dataclass(slots=True)
class _CircuitState:
    failures: int = 0
    opened_at: float | None = None


_CIRCUITS: dict[tuple[str, str], _CircuitState] = {}
_CONTEXT_ESTIMATE_BYTES_PER_TOKEN = 2
_CONTEXT_ESTIMATE_OVERHEAD_TOKENS = 256
_OLLAMA_SERVER_VERSION_RE = re.compile(
    r"^[0-9]+(?:\.[0-9]+){1,3}"
    r"(?:[-+][0-9A-Za-z][0-9A-Za-z.-]{0,31})?$"
)


def _redis_client(settings: Settings) -> async_redis.Redis:
    timeout = min(settings.ollama_connect_timeout_seconds, 2.0)
    return async_redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
    )


def _circuit_redis_client(settings: Settings) -> async_redis.Redis:
    timeout = min(settings.ollama_connect_timeout_seconds, 2.0)
    return async_redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
    )


def _circuit_redis_client_sync(settings: Settings) -> redis.Redis:
    timeout = min(settings.ollama_connect_timeout_seconds, 2.0)
    return redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
    )


@dataclass(frozen=True, slots=True)
class OllamaReadiness:
    """Bounded readiness result safe to include in operational APIs."""

    ok: bool
    model_identity: ModelIdentity
    reason_code: LLMReasonCode | None = None
    ollama_server_version: str | None = None

    def as_check(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": self.ok,
            "provider": self.model_identity.provider,
            "model": self.model_identity.model,
            "local": self.model_identity.local,
            "digest": self.model_identity.digest,
        }
        if self.reason_code is not None:
            result["reason_code"] = self.reason_code.value
        if self.ollama_server_version is not None:
            result["ollama_server_version"] = self.ollama_server_version
        return result


def is_cloud_model(model: str) -> bool:
    """Recognize Ollama cloud model tags without accepting ambiguous aliases."""

    normalized = model.strip().lower()
    if not normalized:
        return False
    tag = normalized.rsplit(":", 1)[-1]
    return (
        normalized.endswith(":cloud")
        or normalized.startswith("cloud-")
        or tag.endswith("-cloud")
        or tag.startswith("cloud-")
    )


def is_allowed_local_ollama_url(base_url: str) -> bool:
    """Allow only loopback or the explicit Docker-to-host gateway."""

    return is_allowed_local_ollama_endpoint(base_url)


def estimate_context_tokens(
    *,
    prompt: str,
    system: str,
    response_format: str | dict[str, Any] | None,
    max_tokens: int,
) -> int:
    """Conservatively estimate the complete context before inference.

    The JSON-schema transport contract and requested output budget are part of
    the estimate. Treating every two UTF-8 bytes as a token makes bilingual
    and punctuation-heavy inputs fail closed before Ollama can truncate them.
    """

    if isinstance(response_format, dict):
        format_text = json.dumps(
            response_format,
            separators=(",", ":"),
            ensure_ascii=True,
        )
    else:
        format_text = response_format or ""
    contract_bytes = len("\n".join((system, prompt, format_text)).encode("utf-8"))
    input_tokens = (
        contract_bytes + _CONTEXT_ESTIMATE_BYTES_PER_TOKEN - 1
    ) // _CONTEXT_ESTIMATE_BYTES_PER_TOKEN
    return input_tokens + max_tokens + _CONTEXT_ESTIMATE_OVERHEAD_TOKENS


class _InferenceLease:
    """Serialize inference locally and, when available, across processes."""

    def __init__(self, settings: Settings, deadline: datetime) -> None:
        self.settings = settings
        self.deadline = deadline
        self.token = secrets.token_urlsafe(24)
        self.redis: async_redis.Redis | None = None
        self.redis_acquired = False
        self.local_acquired = False

    def _remaining_seconds(self) -> float:
        return max(0.0, (self.deadline - datetime.now(UTC)).total_seconds())

    async def __aenter__(self) -> _InferenceLease:
        wait_budget = min(self.settings.ollama_lease_wait_seconds, self._remaining_seconds())
        if wait_budget <= 0:
            raise TypedGenerationError(
                LLMReasonCode.DEADLINE_EXCEEDED,
                "generation deadline elapsed before inference",
            )
        local_deadline = time.monotonic() + wait_budget
        while time.monotonic() < local_deadline:
            if _LOCAL_INFERENCE_LOCK.acquire(blocking=False):
                self.local_acquired = True
                break
            await asyncio.sleep(0.02)
        if not self.local_acquired:
            raise TypedGenerationError(
                LLMReasonCode.CONCURRENCY_LIMIT,
                "local inference concurrency limit exceeded",
            )
        if self.settings.tasks_always_eager:
            return self

        # Redis is the cross-process authority. If it is unavailable, retaining
        # the local lock is the safe single-process fallback.
        try:
            self.redis = _redis_client(self.settings)
            redis_deadline = time.monotonic() + min(
                self.settings.ollama_lease_wait_seconds,
                self._remaining_seconds(),
            )
            while time.monotonic() < redis_deadline:
                remaining = min(
                    self._remaining_seconds(),
                    max(0.0, redis_deadline - time.monotonic()),
                )
                if remaining <= 0:
                    await self._release()
                    raise TypedGenerationError(
                        LLMReasonCode.DEADLINE_EXCEEDED,
                        "generation deadline elapsed while acquiring inference lease",
                    )
                try:
                    acquired = await asyncio.wait_for(
                        self.redis.set(
                            _LEASE_KEY,
                            self.token,
                            nx=True,
                            px=self.settings.ollama_lease_ttl_seconds * 1000,
                        ),
                        timeout=remaining,
                    )
                except TimeoutError:
                    await self._release()
                    raise TypedGenerationError(
                        LLMReasonCode.DEADLINE_EXCEEDED,
                        "generation deadline elapsed while acquiring inference lease",
                    ) from None
                if acquired:
                    self.redis_acquired = True
                    if self._remaining_seconds() <= 0:
                        await self._release()
                        raise TypedGenerationError(
                            LLMReasonCode.DEADLINE_EXCEEDED,
                            "generation deadline elapsed while acquiring inference lease",
                        )
                    return self
                await asyncio.sleep(min(0.05, self._remaining_seconds()))
        except asyncio.CancelledError:
            await self._release()
            raise
        except (redis.RedisError, OSError, ConnectionError, TimeoutError):
            await self._release()
            raise TypedGenerationError(
                LLMReasonCode.CONCURRENCY_LIMIT,
                "distributed inference lease is unavailable",
            ) from None
        except BaseException:
            await self._release()
            raise

        await self._release()
        raise TypedGenerationError(
            LLMReasonCode.CONCURRENCY_LIMIT,
            "distributed inference concurrency limit exceeded",
        )

    async def _release(self) -> None:
        connection = self.redis
        try:
            if connection is not None and self.redis_acquired:
                await asyncio.wait_for(
                    cast(
                        Awaitable[Any],
                        connection.eval(
                            _RELEASE_SCRIPT,
                            1,
                            _LEASE_KEY,
                            self.token,
                        ),
                    ),
                    timeout=1.0,
                )
        except (redis.RedisError, OSError, ConnectionError, TimeoutError):
            # The short TTL is the final ownership boundary if release fails.
            pass
        finally:
            self.redis_acquired = False
            self.redis = None
            if self.local_acquired:
                self.local_acquired = False
                _LOCAL_INFERENCE_LOCK.release()
            if connection is not None:
                try:
                    await asyncio.wait_for(connection.aclose(), timeout=1.0)
                except (redis.RedisError, OSError, ConnectionError, TimeoutError):
                    pass

    async def __aexit__(self, *_args: object) -> None:
        await self._release()


class OllamaRuntime:
    """Local-only Ollama access with exact-model readiness and circuit breaking."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.ollama_base_url.rstrip("/")
        self.model = settings.llm_model.strip() or "qwen2.5:7b"
        self.identity = ModelIdentity(provider="ollama", model=self.model, local=True)
        self.ollama_server_version: str | None = None
        self._circuit = _CIRCUITS.setdefault((self.base_url, self.model), _CircuitState())

    def validate_local_configuration(self) -> None:
        if not is_allowed_local_ollama_url(self.base_url):
            raise TypedGenerationError(
                LLMReasonCode.MODEL_NOT_LOCAL,
                "Ollama must use an allowed local endpoint",
            )
        if not self.settings.ollama_no_cloud or is_cloud_model(self.model):
            raise TypedGenerationError(
                LLMReasonCode.MODEL_NOT_LOCAL,
                "Ollama cloud models are disabled",
            )
        if (
            self.settings.ollama_lease_ttl_seconds
            < self.settings.llm_generation_max_horizon_seconds + 5
        ):
            raise TypedGenerationError(
                LLMReasonCode.CONFIGURATION_INVALID,
                "inference lease TTL must cover the generation horizon",
            )

    def _ensure_local_circuit_closed(self) -> None:
        opened_at = self._circuit.opened_at
        if opened_at is None:
            return
        if time.monotonic() - opened_at >= self.settings.ollama_circuit_reset_seconds:
            self._circuit.opened_at = None
            self._circuit.failures = 0
            return
        raise TypedGenerationError(
            LLMReasonCode.CIRCUIT_OPEN,
            "Ollama circuit breaker is open",
        )

    def _record_local_success(self) -> None:
        self._circuit.failures = 0
        self._circuit.opened_at = None

    def _record_local_failure(self) -> None:
        self._circuit.failures += 1
        if self._circuit.failures >= self.settings.ollama_circuit_failure_threshold:
            self._circuit.opened_at = time.monotonic()

    def _circuit_keys(self) -> tuple[str, str]:
        expected_digest = expected_qualified_model_digest(
            self.settings.ollama_expected_model_digest
        )
        scope = hashlib.sha256(
            "\0".join(
                (
                    self.settings.app_env,
                    self.base_url,
                    self.model,
                    expected_digest,
                    str(self.settings.ollama_circuit_failure_threshold),
                    str(self.settings.ollama_circuit_reset_seconds),
                )
            ).encode("utf-8")
        ).hexdigest()[:24]
        prefix = f"job-agent:llm:ollama:{{{scope}}}:circuit"
        return f"{prefix}:failures", f"{prefix}:open"

    def _circuit_operation_timeout(self, deadline: datetime | None) -> float:
        timeout = min(self.settings.ollama_connect_timeout_seconds, 2.0)
        if deadline is not None:
            timeout = min(timeout, (deadline - datetime.now(UTC)).total_seconds())
        return max(0.0, timeout)

    async def _close_circuit_client(self, client: async_redis.Redis) -> None:
        try:
            await asyncio.wait_for(client.aclose(), timeout=1.0)
        except (redis.RedisError, OSError, ConnectionError, TimeoutError):
            pass

    async def _ensure_circuit_closed_async(
        self,
        deadline: datetime | None,
    ) -> None:
        if self.settings.tasks_always_eager:
            self._ensure_local_circuit_closed()
            return
        timeout = self._circuit_operation_timeout(deadline)
        if timeout <= 0:
            raise TypedGenerationError(
                LLMReasonCode.DEADLINE_EXCEEDED,
                "generation deadline elapsed before circuit check",
            )
        client = _circuit_redis_client(self.settings)
        try:
            _, open_key = self._circuit_keys()
            is_open = await asyncio.wait_for(client.exists(open_key), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except (redis.RedisError, OSError, ConnectionError, TimeoutError, ValueError):
            raise TypedGenerationError(
                LLMReasonCode.PROVIDER_UNAVAILABLE,
                "distributed inference circuit is unavailable",
            ) from None
        finally:
            await self._close_circuit_client(client)
        if is_open:
            raise TypedGenerationError(
                LLMReasonCode.CIRCUIT_OPEN,
                "Ollama circuit breaker is open",
            )

    def _ensure_circuit_closed_sync(self) -> None:
        if self.settings.tasks_always_eager:
            self._ensure_local_circuit_closed()
            return
        client = _circuit_redis_client_sync(self.settings)
        try:
            _, open_key = self._circuit_keys()
            is_open = client.exists(open_key)
        except (redis.RedisError, OSError, ConnectionError, TimeoutError, ValueError):
            raise TypedGenerationError(
                LLMReasonCode.PROVIDER_UNAVAILABLE,
                "distributed inference circuit is unavailable",
            ) from None
        finally:
            try:
                client.close()
            except (redis.RedisError, OSError, ConnectionError):
                pass
        if is_open:
            raise TypedGenerationError(
                LLMReasonCode.CIRCUIT_OPEN,
                "Ollama circuit breaker is open",
            )

    async def _record_failure_async(self, deadline: datetime | None) -> bool:
        if self.settings.tasks_always_eager:
            self._record_local_failure()
            return True
        timeout = self._circuit_operation_timeout(deadline)
        if timeout <= 0:
            return False
        client = _circuit_redis_client(self.settings)
        try:
            failures_key, open_key = self._circuit_keys()
            threshold = self.settings.ollama_circuit_failure_threshold
            reset_ms = max(1, round(self.settings.ollama_circuit_reset_seconds * 1000))
            counter_ttl_ms = max(
                reset_ms,
                round(
                    (
                        threshold
                        * (
                            self.settings.llm_generation_max_horizon_seconds
                            + self.settings.ollama_lease_wait_seconds
                        )
                        + self.settings.ollama_circuit_reset_seconds
                    )
                    * 1000
                ),
            )
            result = await asyncio.wait_for(
                cast(
                    Awaitable[Any],
                    client.eval(
                        _CIRCUIT_FAILURE_SCRIPT,
                        2,
                        failures_key,
                        open_key,
                        str(threshold),
                        str(reset_ms),
                        str(counter_ttl_ms),
                    ),
                ),
                timeout=timeout,
            )
            return isinstance(result, int) and result in {-1, 0, 1}
        except asyncio.CancelledError:
            raise
        except (redis.RedisError, OSError, ConnectionError, TimeoutError, ValueError):
            return False
        finally:
            await self._close_circuit_client(client)

    async def _record_success_async(self, deadline: datetime | None) -> bool:
        if self.settings.tasks_always_eager:
            self._record_local_success()
            return True
        timeout = self._circuit_operation_timeout(deadline)
        if timeout <= 0:
            return False
        client = _circuit_redis_client(self.settings)
        try:
            failures_key, open_key = self._circuit_keys()
            result = await asyncio.wait_for(
                cast(
                    Awaitable[Any],
                    client.eval(
                        _CIRCUIT_SUCCESS_SCRIPT,
                        2,
                        failures_key,
                        open_key,
                    ),
                ),
                timeout=timeout,
            )
            return result == 1
        except asyncio.CancelledError:
            raise
        except (redis.RedisError, OSError, ConnectionError, TimeoutError, ValueError):
            return False
        finally:
            await self._close_circuit_client(client)

    def _http_timeout(self, remaining_seconds: float) -> httpx.Timeout:
        total = min(self.settings.ollama_request_timeout_seconds, remaining_seconds)
        connect = min(self.settings.ollama_connect_timeout_seconds, total)
        return httpx.Timeout(timeout=total, connect=connect)

    def _identity_is_current(
        self,
        identity: ModelIdentity,
        server_version: str,
    ) -> bool:
        return is_qualified_local_model_identity(
            provider=identity.provider,
            model=identity.model,
            local=identity.local,
            digest=identity.digest,
            explicit_digest=self.settings.ollama_expected_model_digest,
            ollama_server_version=server_version,
            ollama_request_timeout_seconds=(self.settings.ollama_request_timeout_seconds),
            llm_generation_max_horizon_seconds=(self.settings.llm_generation_max_horizon_seconds),
            ollama_connect_timeout_seconds=(self.settings.ollama_connect_timeout_seconds),
            ollama_lease_wait_seconds=self.settings.ollama_lease_wait_seconds,
            ollama_lease_ttl_seconds=self.settings.ollama_lease_ttl_seconds,
            ollama_circuit_failure_threshold=(self.settings.ollama_circuit_failure_threshold),
            ollama_circuit_reset_seconds=(self.settings.ollama_circuit_reset_seconds),
            ollama_num_ctx=self.settings.ollama_num_ctx,
            llm_max_prompt_chars=self.settings.llm_max_prompt_chars,
            lease_mode=("process_local" if self.settings.tasks_always_eager else "redis"),
            ollama_no_cloud=self.settings.ollama_no_cloud,
        )

    async def _readiness_failure(
        self,
        reason_code: LLMReasonCode,
        *,
        deadline: datetime | None,
        record_failure: bool,
        server_version: str | None = None,
    ) -> OllamaReadiness:
        if record_failure and not await self._record_failure_async(deadline):
            reason_code = LLMReasonCode.PROVIDER_UNAVAILABLE
        return OllamaReadiness(
            False,
            self.identity,
            reason_code,
            server_version,
        )

    def _identity_from_tags(self, payload: object) -> ModelIdentity | None:
        models = payload.get("models") if isinstance(payload, dict) else None
        match = next(
            (
                item
                for item in models or ()
                if isinstance(item, dict) and item.get("name") == self.model
            ),
            None,
        )
        if match is None:
            return None
        raw_digest = match.get("digest")
        size = match.get("size")
        details = match.get("details")
        model_format = details.get("format") if isinstance(details, dict) else None
        digest = None
        if isinstance(raw_digest, str):
            normalized_digest = raw_digest.lower()
            hex_digest = (
                normalized_digest.removeprefix("sha256:")
                if normalized_digest.startswith("sha256:")
                else normalized_digest
            )
            if len(hex_digest) == 64 and all(
                character in "0123456789abcdef" for character in hex_digest
            ):
                digest = f"sha256:{hex_digest}"
        if (
            digest is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or model_format != "gguf"
        ):
            raise TypedGenerationError(
                LLMReasonCode.MODEL_NOT_LOCAL,
                "Ollama model metadata does not prove a local artifact",
            )
        return ModelIdentity(
            provider="ollama",
            model=self.model,
            local=True,
            digest=digest,
        )

    @staticmethod
    def _server_version_from_payload(payload: object) -> str:
        if not isinstance(payload, dict) or set(payload) != {"version"}:
            raise ValueError("invalid Ollama version response")
        version = payload.get("version")
        if (
            not isinstance(version, str)
            or not 3 <= len(version) <= 64
            or version != version.strip()
            or _OLLAMA_SERVER_VERSION_RE.fullmatch(version) is None
        ):
            raise ValueError("invalid Ollama server version")
        return version

    async def readiness(
        self,
        *,
        deadline: datetime | None = None,
        record_failure: bool = False,
        circuit_deadline: datetime | None = None,
    ) -> OllamaReadiness:
        """Verify the local server version and exact configured model artifact."""

        try:
            self.validate_local_configuration()
            expected_qualified_model_digest(self.settings.ollama_expected_model_digest)
            await self._ensure_circuit_closed_async(deadline)
        except (OSError, ValueError):
            return OllamaReadiness(
                False,
                self.identity,
                LLMReasonCode.CONFIGURATION_INVALID,
            )
        except TypedGenerationError as exc:
            return OllamaReadiness(False, self.identity, exc.reason_code)

        timeout_seconds = self.settings.ollama_connect_timeout_seconds
        if deadline is not None:
            remaining = (deadline - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                return OllamaReadiness(
                    False,
                    self.identity,
                    LLMReasonCode.DEADLINE_EXCEEDED,
                )
            timeout_seconds = min(timeout_seconds, remaining)
        try:
            timeout = httpx.Timeout(
                timeout=timeout_seconds,
                connect=timeout_seconds,
            )

            async def fetch_readiness_payloads(
                client: httpx.AsyncClient,
            ) -> tuple[object, object]:
                version_response = await client.get(f"{self.base_url}/api/version")
                version_response.raise_for_status()
                version_payload = version_response.json()
                tags_response = await client.get(f"{self.base_url}/api/tags")
                tags_response.raise_for_status()
                return version_payload, tags_response.json()

            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                version_payload, payload = await asyncio.wait_for(
                    fetch_readiness_payloads(client),
                    timeout=timeout_seconds,
                )
            server_version = self._server_version_from_payload(version_payload)
            self.ollama_server_version = server_version
            identity = self._identity_from_tags(payload)
            if identity is None:
                return await self._readiness_failure(
                    LLMReasonCode.MODEL_NOT_READY,
                    deadline=circuit_deadline or deadline,
                    record_failure=record_failure,
                    server_version=server_version,
                )
            self.identity = identity
            currentness_timeout = self.settings.ollama_connect_timeout_seconds
            if deadline is not None:
                remaining = (deadline - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    return OllamaReadiness(
                        False,
                        self.identity,
                        LLMReasonCode.DEADLINE_EXCEEDED,
                        server_version,
                    )
                currentness_timeout = min(currentness_timeout, remaining)
            try:
                identity_is_current = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._identity_is_current,
                        identity,
                        server_version,
                    ),
                    timeout=currentness_timeout,
                )
            except TimeoutError:
                return await self._readiness_failure(
                    (
                        LLMReasonCode.DEADLINE_EXCEEDED
                        if deadline is not None
                        else LLMReasonCode.PROVIDER_UNAVAILABLE
                    ),
                    deadline=circuit_deadline or deadline,
                    record_failure=record_failure,
                    server_version=server_version,
                )
            if deadline is not None and datetime.now(UTC) >= deadline:
                return await self._readiness_failure(
                    LLMReasonCode.DEADLINE_EXCEEDED,
                    deadline=circuit_deadline or deadline,
                    record_failure=record_failure,
                    server_version=server_version,
                )
            if not identity_is_current:
                return await self._readiness_failure(
                    LLMReasonCode.MODEL_NOT_READY,
                    deadline=circuit_deadline or deadline,
                    record_failure=record_failure,
                    server_version=server_version,
                )
            return OllamaReadiness(
                True,
                self.identity,
                ollama_server_version=server_version,
            )
        except TypedGenerationError as exc:
            return await self._readiness_failure(
                exc.reason_code,
                deadline=circuit_deadline or deadline,
                record_failure=record_failure,
            )
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, json.JSONDecodeError):
            return await self._readiness_failure(
                LLMReasonCode.PROVIDER_UNAVAILABLE,
                deadline=circuit_deadline or deadline,
                record_failure=record_failure,
            )

    def readiness_sync(self) -> OllamaReadiness:
        """Synchronous exact-model readiness for the existing operations probe."""

        try:
            self.validate_local_configuration()
            expected_qualified_model_digest(self.settings.ollama_expected_model_digest)
            self._ensure_circuit_closed_sync()
        except (OSError, ValueError):
            return OllamaReadiness(
                False,
                self.identity,
                LLMReasonCode.CONFIGURATION_INVALID,
            )
        except TypedGenerationError as exc:
            return OllamaReadiness(False, self.identity, exc.reason_code)
        try:
            timeout = httpx.Timeout(
                timeout=self.settings.ollama_connect_timeout_seconds,
                connect=self.settings.ollama_connect_timeout_seconds,
            )
            with httpx.Client(timeout=timeout, trust_env=False) as client:
                version_response = client.get(f"{self.base_url}/api/version")
                version_response.raise_for_status()
                version_payload = version_response.json()
                tags_response = client.get(f"{self.base_url}/api/tags")
                tags_response.raise_for_status()
                payload = tags_response.json()
            server_version = self._server_version_from_payload(version_payload)
            self.ollama_server_version = server_version
            identity = self._identity_from_tags(payload)
            if identity is None:
                return OllamaReadiness(
                    False,
                    self.identity,
                    LLMReasonCode.MODEL_NOT_READY,
                    server_version,
                )
            self.identity = identity
            if not self._identity_is_current(identity, server_version):
                return OllamaReadiness(
                    False,
                    self.identity,
                    LLMReasonCode.MODEL_NOT_READY,
                    server_version,
                )
            return OllamaReadiness(
                True,
                self.identity,
                ollama_server_version=server_version,
            )
        except TypedGenerationError as exc:
            return OllamaReadiness(False, self.identity, exc.reason_code)
        except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError):
            return OllamaReadiness(
                False,
                self.identity,
                LLMReasonCode.PROVIDER_UNAVAILABLE,
            )

    async def chat(
        self,
        *,
        prompt: str,
        system: str,
        max_tokens: int,
        temperature: float,
        response_format: str | dict[str, Any] | None,
        deadline: datetime,
        require_ready_model: bool,
    ) -> str:
        """Run one bounded inference and return only its unlogged response text."""

        assert_llm_generation_allowed()
        self.validate_local_configuration()
        await self._ensure_circuit_closed_async(deadline)
        estimated_context_tokens = estimate_context_tokens(
            prompt=prompt,
            system=system,
            response_format=response_format,
            max_tokens=max_tokens,
        )
        if estimated_context_tokens > self.settings.ollama_num_ctx:
            raise TypedGenerationError(
                LLMReasonCode.CONFIGURATION_INVALID,
                "typed generation contract exceeds the configured context window",
            )
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise TypedGenerationError(
                LLMReasonCode.DEADLINE_EXCEEDED,
                "generation deadline elapsed before inference",
            )
        circuit_tail_seconds = min(1.0, remaining / 10)
        work_deadline = deadline - timedelta(seconds=circuit_tail_seconds)
        verified_identity: ModelIdentity | None = None

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "num_ctx": self.settings.ollama_num_ctx,
            },
        }
        if response_format is not None:
            payload["format"] = response_format

        async with _InferenceLease(self.settings, deadline):
            # A waiting caller must recheck after acquiring the cross-process
            # lease; a preceding caller may have opened the circuit.
            await self._ensure_circuit_closed_async(work_deadline)
            if require_ready_model:
                status = await self.readiness(
                    deadline=work_deadline,
                    record_failure=True,
                    circuit_deadline=deadline,
                )
                if not status.ok:
                    raise TypedGenerationError(
                        status.reason_code or LLMReasonCode.PROVIDER_UNAVAILABLE,
                        "configured Ollama model is not ready",
                    )
                verified_identity = status.model_identity

            remaining = (work_deadline - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                raise TypedGenerationError(
                    LLMReasonCode.DEADLINE_EXCEEDED,
                    "generation deadline elapsed before provider request",
                )

            async def post_and_decode(client: httpx.AsyncClient) -> object:
                response = await client.post(
                    f"{self.base_url}/api/chat",
                    json=payload,
                )
                response.raise_for_status()
                return response.json()

            absolute_request_timeout = min(
                self.settings.ollama_request_timeout_seconds,
                remaining,
            )
            try:
                async with httpx.AsyncClient(
                    timeout=self._http_timeout(absolute_request_timeout),
                    trust_env=False,
                ) as client:
                    # httpx timeouts are per network phase. A peer that keeps
                    # slowly yielding bytes can therefore stay below every
                    # phase timeout while exceeding the caller's absolute
                    # generation deadline. Bound the complete request, body
                    # read, and JSON decode with the remaining deadline too.
                    data = await asyncio.wait_for(
                        post_and_decode(client),
                        timeout=absolute_request_timeout,
                    )
                if not isinstance(data, dict):
                    raise ValueError("invalid response shape")
                content = data.get("message", {}).get("content", "")
                if not isinstance(content, str):
                    raise ValueError("invalid response shape")
                if require_ready_model:
                    final_status = await self.readiness(
                        deadline=work_deadline,
                        record_failure=True,
                        circuit_deadline=deadline,
                    )
                    if not final_status.ok or final_status.model_identity != verified_identity:
                        if final_status.ok:
                            recorded = await self._record_failure_async(deadline)
                            if not recorded:
                                raise TypedGenerationError(
                                    LLMReasonCode.PROVIDER_UNAVAILABLE,
                                    "distributed inference circuit update failed",
                                )
                        raise TypedGenerationError(
                            final_status.reason_code or LLMReasonCode.MODEL_NOT_READY,
                            "Ollama model identity changed during inference",
                        )
                if datetime.now(UTC) >= work_deadline:
                    recorded = await self._record_failure_async(deadline)
                    raise TypedGenerationError(
                        (
                            LLMReasonCode.DEADLINE_EXCEEDED
                            if recorded
                            else LLMReasonCode.PROVIDER_UNAVAILABLE
                        ),
                        "generation deadline elapsed before result acceptance",
                    )
                if not await self._record_success_async(deadline):
                    raise TypedGenerationError(
                        LLMReasonCode.PROVIDER_UNAVAILABLE,
                        "distributed inference circuit update failed",
                    )
                return content
            except TypedGenerationError:
                raise
            except (httpx.TimeoutException, TimeoutError):
                recorded = await self._record_failure_async(deadline)
                raise TypedGenerationError(
                    (
                        LLMReasonCode.DEADLINE_EXCEEDED
                        if recorded
                        else LLMReasonCode.PROVIDER_UNAVAILABLE
                    ),
                    "Ollama request exceeded its bounded deadline",
                ) from None
            except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError):
                await self._record_failure_async(deadline)
                raise TypedGenerationError(
                    LLMReasonCode.PROVIDER_UNAVAILABLE,
                    "Ollama request failed",
                ) from None
