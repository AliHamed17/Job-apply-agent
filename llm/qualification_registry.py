"""Versioned allowlist for the one locally qualified Ollama artifact."""

from __future__ import annotations

import importlib.metadata
import json
import platform
import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[1]
QUALIFIED_MODEL_REGISTRY_PATH = ROOT / "config" / "qualified_local_model.json"
QUALIFIED_RUNTIME_PACKAGES_PATH = ROOT / "config" / "qualified_runtime_packages.json"
QUALIFIED_EXECUTION_ENVIRONMENT_SCHEMA_VERSION: Literal[
    "qualification-execution-environment-v3"
] = "qualification-execution-environment-v3"
QUALIFIED_PYTHON_IMPLEMENTATION = "CPython"
QUALIFIED_PYTHON_MAJOR = 3
QUALIFIED_PYTHON_MINOR = 13
QUALIFIED_OLLAMA_SERVER_VERSION = "0.31.1"
_RESOLVED_VERSION_PATTERN = (
    r"^[0-9]+(?:\.[0-9]+){1,3}"
    r"(?:(?:a|b|rc|dev|post)[0-9]+)?"
    r"(?:\+[0-9A-Za-z][0-9A-Za-z.-]{0,31})?$"
)
_OLLAMA_VERSION_PATTERN = (
    r"^[0-9]+(?:\.[0-9]+){1,3}"
    r"(?:[-+][0-9A-Za-z][0-9A-Za-z.-]{0,31})?$"
)
_REASON_CODE_PATTERN = r"^[A-Z][A-Z0-9_]{0,79}$"


class QualifiedPythonRuntimeV1(BaseModel):
    """Python ABI boundary used by the private qualified runner."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    implementation: str = Field(min_length=1, max_length=32)
    major: int = Field(ge=3, le=9)
    minor: int = Field(ge=0, le=99)


class QualifiedPackageVersionsV2(BaseModel):
    """Exact behavior-affecting dependency graph for the qualified runner."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    annotated_types: str = Field(
        min_length=3,
        max_length=64,
        pattern=_RESOLVED_VERSION_PATTERN,
    )
    anyio: str = Field(
        min_length=3,
        max_length=64,
        pattern=_RESOLVED_VERSION_PATTERN,
    )
    certifi: str = Field(
        min_length=3,
        max_length=64,
        pattern=_RESOLVED_VERSION_PATTERN,
    )
    h11: str = Field(
        min_length=3,
        max_length=64,
        pattern=_RESOLVED_VERSION_PATTERN,
    )
    httpcore: str = Field(
        min_length=3,
        max_length=64,
        pattern=_RESOLVED_VERSION_PATTERN,
    )
    httpx: str = Field(
        min_length=3,
        max_length=64,
        pattern=_RESOLVED_VERSION_PATTERN,
    )
    idna: str = Field(
        min_length=3,
        max_length=64,
        pattern=_RESOLVED_VERSION_PATTERN,
    )
    pydantic: str = Field(
        min_length=3,
        max_length=64,
        pattern=_RESOLVED_VERSION_PATTERN,
    )
    pydantic_core: str = Field(
        min_length=3,
        max_length=64,
        pattern=_RESOLVED_VERSION_PATTERN,
    )
    pydantic_settings: str = Field(
        min_length=3,
        max_length=64,
        pattern=_RESOLVED_VERSION_PATTERN,
    )
    pyjwt: str = Field(
        min_length=3,
        max_length=64,
        pattern=_RESOLVED_VERSION_PATTERN,
    )
    pyyaml: str = Field(
        min_length=3,
        max_length=64,
        pattern=_RESOLVED_VERSION_PATTERN,
    )
    python_dotenv: str = Field(
        min_length=3,
        max_length=64,
        pattern=_RESOLVED_VERSION_PATTERN,
    )
    redis: str = Field(
        min_length=3,
        max_length=64,
        pattern=_RESOLVED_VERSION_PATTERN,
    )
    sniffio: str = Field(
        min_length=3,
        max_length=64,
        pattern=_RESOLVED_VERSION_PATTERN,
    )
    typing_extensions: str = Field(
        min_length=3,
        max_length=64,
        pattern=_RESOLVED_VERSION_PATTERN,
    )
    typing_inspection: str = Field(
        min_length=3,
        max_length=64,
        pattern=_RESOLVED_VERSION_PATTERN,
    )


class QualifiedRuntimePackageManifestV1(BaseModel):
    """Committed dependency graph allowed to produce qualified artifacts."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["qualified-runtime-packages-v1"]
    packages: QualifiedPackageVersionsV2


class QualifiedOllamaRuntimeV1(BaseModel):
    """Observed local Ollama server identity, or a bounded unavailable reason."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    server_version: str | None = Field(
        default=None,
        min_length=3,
        max_length=64,
        pattern=_OLLAMA_VERSION_PATTERN,
    )
    version_reason_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=_REASON_CODE_PATTERN,
    )

    @model_validator(mode="after")
    def version_or_reason(self) -> QualifiedOllamaRuntimeV1:
        if (self.server_version is None) == (self.version_reason_code is None):
            raise ValueError("Ollama runtime requires exactly one version or reason")
        return self


class QualifiedInferenceConfigV2(BaseModel):
    """Inference-affecting environment values not fixed in source."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    ollama_request_timeout_seconds: float | None = Field(default=None, ge=1.0, le=120.0)
    llm_generation_max_horizon_seconds: float | None = Field(
        default=None,
        ge=1.0,
        le=120.0,
    )
    ollama_connect_timeout_seconds: float | None = Field(default=None, ge=0.1, le=15.0)
    ollama_lease_wait_seconds: float | None = Field(default=None, ge=0.1, le=60.0)
    ollama_lease_ttl_seconds: int | None = Field(default=None, ge=5, le=300)
    ollama_circuit_failure_threshold: int | None = Field(default=None, ge=1, le=10)
    ollama_circuit_reset_seconds: float | None = Field(default=None, ge=1.0, le=300.0)
    ollama_num_ctx: int | None = Field(default=None, ge=8_192, le=32_768)
    llm_max_prompt_chars: int | None = Field(default=None, ge=1_000, le=100_000)
    lease_mode: Literal["process_local", "redis"] | None = None
    ollama_no_cloud: bool | None = None
    configuration_reason_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=_REASON_CODE_PATTERN,
    )

    @model_validator(mode="after")
    def values_or_bounded_reason(self) -> QualifiedInferenceConfigV2:
        values = (
            self.ollama_request_timeout_seconds,
            self.llm_generation_max_horizon_seconds,
            self.ollama_connect_timeout_seconds,
            self.ollama_lease_wait_seconds,
            self.ollama_lease_ttl_seconds,
            self.ollama_circuit_failure_threshold,
            self.ollama_circuit_reset_seconds,
            self.ollama_num_ctx,
            self.llm_max_prompt_chars,
            self.lease_mode,
            self.ollama_no_cloud,
        )
        if self.configuration_reason_code is None:
            if any(value is None for value in values):
                raise ValueError("qualified inference config requires every resolved value")
            request_timeout = self.ollama_request_timeout_seconds
            maximum_horizon = self.llm_generation_max_horizon_seconds
            lease_ttl = self.ollama_lease_ttl_seconds
            assert request_timeout is not None
            assert maximum_horizon is not None
            assert lease_ttl is not None
            if request_timeout > maximum_horizon:
                raise ValueError("request timeout exceeds the maximum generation horizon")
            if lease_ttl < maximum_horizon + 5:
                raise ValueError("inference lease TTL does not cover the generation horizon")
        elif any(value is not None for value in values):
            raise ValueError("unavailable inference config cannot contain resolved values")
        return self

    @property
    def available(self) -> bool:
        return self.configuration_reason_code is None


class QualificationExecutionEnvironmentV1(BaseModel):
    """Bounded, non-secret fingerprint for one qualification execution."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["qualification-execution-environment-v3"]
    python: QualifiedPythonRuntimeV1
    packages: QualifiedPackageVersionsV2
    ollama: QualifiedOllamaRuntimeV1
    inference_config: QualifiedInferenceConfigV2
    model_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class QualifiedLocalModelV1(BaseModel):
    """Committed identity that can change only with a new qualification report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["qualified-local-model-v1"]
    provider: Literal["ollama"]
    model: Literal["qwen2.5:7b"]
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    qualification_report: Literal["docs/qualification/v4-local-model-qualification.json"]
    qualification_report_schema_version: Literal["v4-local-model-qualification-v4"]


@lru_cache(maxsize=1)
def load_qualified_local_model() -> QualifiedLocalModelV1:
    """Load the committed registry; malformed or missing data fails closed."""

    return QualifiedLocalModelV1.model_validate_json(
        QUALIFIED_MODEL_REGISTRY_PATH.read_text(encoding="utf-8")
    )


@lru_cache(maxsize=1)
def load_qualified_runtime_packages() -> QualifiedRuntimePackageManifestV1:
    """Load the exact qualified dependency graph; malformed data fails closed."""

    payload = json.loads(
        QUALIFIED_RUNTIME_PACKAGES_PATH.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_json_keys,
    )
    return QualifiedRuntimePackageManifestV1.model_validate(payload)


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """Reject ambiguous manifests rather than accepting the final duplicate key."""

    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("qualified runtime package manifest contains duplicate keys")
        value[key] = item
    return value


def _capture_runtime_packages() -> QualifiedPackageVersionsV2:
    """Capture installed distribution versions without package or path metadata."""

    return QualifiedPackageVersionsV2(
        annotated_types=importlib.metadata.version("annotated-types"),
        anyio=importlib.metadata.version("anyio"),
        certifi=importlib.metadata.version("certifi"),
        h11=importlib.metadata.version("h11"),
        httpcore=importlib.metadata.version("httpcore"),
        httpx=importlib.metadata.version("httpx"),
        idna=importlib.metadata.version("idna"),
        pydantic=importlib.metadata.version("pydantic"),
        pydantic_core=importlib.metadata.version("pydantic-core"),
        pydantic_settings=importlib.metadata.version("pydantic-settings"),
        pyjwt=importlib.metadata.version("PyJWT"),
        pyyaml=importlib.metadata.version("PyYAML"),
        python_dotenv=importlib.metadata.version("python-dotenv"),
        redis=importlib.metadata.version("redis"),
        sniffio=importlib.metadata.version("sniffio"),
        typing_extensions=importlib.metadata.version("typing-extensions"),
        typing_inspection=importlib.metadata.version("typing-inspection"),
    )


def expected_qualified_model_digest(explicit_digest: str = "") -> str:
    """Return the registry digest and reject a contradictory environment pin."""

    registry = load_qualified_local_model()
    normalized = explicit_digest.strip().casefold()
    if normalized and normalized != registry.digest:
        raise ValueError("OLLAMA_EXPECTED_MODEL_DIGEST must equal the qualified registry")
    return registry.digest


def matches_qualified_local_model_registry(
    *,
    provider: object,
    model: object,
    local: object,
    digest: object,
    explicit_digest: str = "",
) -> bool:
    """Match the raw committed artifact identity without asserting qualification."""

    try:
        registry = load_qualified_local_model()
        expected_digest = expected_qualified_model_digest(explicit_digest)
    except (OSError, ValueError):
        return False
    return bool(
        provider == registry.provider
        and model == registry.model
        and local is True
        and digest == expected_digest
    )


def capture_qualification_execution_environment(
    *,
    ollama_server_version: str | None,
    ollama_reason_code: str | None,
    ollama_request_timeout_seconds: float | None,
    llm_generation_max_horizon_seconds: float | None,
    ollama_connect_timeout_seconds: float | None,
    ollama_lease_wait_seconds: float | None,
    ollama_lease_ttl_seconds: int | None,
    ollama_circuit_failure_threshold: int | None,
    ollama_circuit_reset_seconds: float | None,
    ollama_num_ctx: int | None,
    llm_max_prompt_chars: int | None,
    lease_mode: Literal["process_local", "redis"] | None,
    ollama_no_cloud: bool | None,
    inference_config_reason_code: str | None = None,
    model_digest: str | None = None,
) -> QualificationExecutionEnvironmentV1:
    """Capture only resolved values that can change qualification behavior."""

    registry = load_qualified_local_model()
    return QualificationExecutionEnvironmentV1(
        schema_version=QUALIFIED_EXECUTION_ENVIRONMENT_SCHEMA_VERSION,
        python=QualifiedPythonRuntimeV1(
            implementation=platform.python_implementation(),
            major=sys.version_info.major,
            minor=sys.version_info.minor,
        ),
        packages=_capture_runtime_packages(),
        ollama=QualifiedOllamaRuntimeV1(
            server_version=ollama_server_version,
            version_reason_code=ollama_reason_code,
        ),
        inference_config=QualifiedInferenceConfigV2(
            ollama_request_timeout_seconds=ollama_request_timeout_seconds,
            llm_generation_max_horizon_seconds=llm_generation_max_horizon_seconds,
            ollama_connect_timeout_seconds=ollama_connect_timeout_seconds,
            ollama_lease_wait_seconds=ollama_lease_wait_seconds,
            ollama_lease_ttl_seconds=ollama_lease_ttl_seconds,
            ollama_circuit_failure_threshold=ollama_circuit_failure_threshold,
            ollama_circuit_reset_seconds=ollama_circuit_reset_seconds,
            ollama_num_ctx=ollama_num_ctx,
            llm_max_prompt_chars=llm_max_prompt_chars,
            lease_mode=lease_mode,
            ollama_no_cloud=ollama_no_cloud,
            configuration_reason_code=inference_config_reason_code,
        ),
        model_digest=model_digest or registry.digest,
    )


def qualification_execution_environment_is_qualified(
    environment: QualificationExecutionEnvironmentV1,
) -> bool:
    """Return whether a captured environment is eligible to produce a pass."""

    try:
        registry = load_qualified_local_model()
        package_manifest = load_qualified_runtime_packages()
    except (OSError, ValueError):
        return False
    return bool(
        environment.python.implementation == QUALIFIED_PYTHON_IMPLEMENTATION
        and environment.python.major == QUALIFIED_PYTHON_MAJOR
        and environment.python.minor == QUALIFIED_PYTHON_MINOR
        and environment.ollama.server_version == QUALIFIED_OLLAMA_SERVER_VERSION
        and environment.ollama.version_reason_code is None
        and environment.inference_config.available
        and environment.inference_config.lease_mode == "redis"
        and environment.inference_config.ollama_no_cloud is True
        and environment.model_digest == registry.digest
        and environment.packages == package_manifest.packages
    )


def _current_environment_matches(
    environment: QualificationExecutionEnvironmentV1,
    *,
    ollama_server_version: str | None,
    ollama_request_timeout_seconds: float | None,
    llm_generation_max_horizon_seconds: float | None,
    ollama_connect_timeout_seconds: float | None,
    ollama_lease_wait_seconds: float | None,
    ollama_lease_ttl_seconds: int | None,
    ollama_circuit_failure_threshold: int | None,
    ollama_circuit_reset_seconds: float | None,
    ollama_num_ctx: int | None,
    llm_max_prompt_chars: int | None,
    lease_mode: Literal["process_local", "redis"] | None,
    ollama_no_cloud: bool | None,
) -> bool:
    resolved = (
        ollama_request_timeout_seconds,
        llm_generation_max_horizon_seconds,
        ollama_connect_timeout_seconds,
        ollama_lease_wait_seconds,
        ollama_lease_ttl_seconds,
        ollama_circuit_failure_threshold,
        ollama_circuit_reset_seconds,
        ollama_num_ctx,
        llm_max_prompt_chars,
        lease_mode,
        ollama_no_cloud,
    )
    if any(value is None for value in resolved):
        from core.config import get_settings

        settings = get_settings()
        if ollama_request_timeout_seconds is None:
            ollama_request_timeout_seconds = settings.ollama_request_timeout_seconds
        if llm_generation_max_horizon_seconds is None:
            llm_generation_max_horizon_seconds = settings.llm_generation_max_horizon_seconds
        if ollama_connect_timeout_seconds is None:
            ollama_connect_timeout_seconds = settings.ollama_connect_timeout_seconds
        if ollama_lease_wait_seconds is None:
            ollama_lease_wait_seconds = settings.ollama_lease_wait_seconds
        if ollama_lease_ttl_seconds is None:
            ollama_lease_ttl_seconds = settings.ollama_lease_ttl_seconds
        if ollama_circuit_failure_threshold is None:
            ollama_circuit_failure_threshold = settings.ollama_circuit_failure_threshold
        if ollama_circuit_reset_seconds is None:
            ollama_circuit_reset_seconds = settings.ollama_circuit_reset_seconds
        if ollama_num_ctx is None:
            ollama_num_ctx = settings.ollama_num_ctx
        if llm_max_prompt_chars is None:
            llm_max_prompt_chars = settings.llm_max_prompt_chars
        if lease_mode is None:
            lease_mode = "process_local" if settings.tasks_always_eager else "redis"
        if ollama_no_cloud is None:
            ollama_no_cloud = settings.ollama_no_cloud
    observed_server_version = (
        environment.ollama.server_version
        if ollama_server_version is None
        else ollama_server_version
    )
    current = capture_qualification_execution_environment(
        ollama_server_version=observed_server_version,
        ollama_reason_code=None,
        ollama_request_timeout_seconds=ollama_request_timeout_seconds,
        llm_generation_max_horizon_seconds=llm_generation_max_horizon_seconds,
        ollama_connect_timeout_seconds=ollama_connect_timeout_seconds,
        ollama_lease_wait_seconds=ollama_lease_wait_seconds,
        ollama_lease_ttl_seconds=ollama_lease_ttl_seconds,
        ollama_circuit_failure_threshold=ollama_circuit_failure_threshold,
        ollama_circuit_reset_seconds=ollama_circuit_reset_seconds,
        ollama_num_ctx=ollama_num_ctx,
        llm_max_prompt_chars=llm_max_prompt_chars,
        lease_mode=lease_mode,
        ollama_no_cloud=ollama_no_cloud,
    )
    return current == environment


def is_qualified_local_model_identity(
    *,
    provider: object,
    model: object,
    local: object,
    digest: object,
    explicit_digest: str = "",
    ollama_server_version: str | None = None,
    ollama_request_timeout_seconds: float | None = None,
    llm_generation_max_horizon_seconds: float | None = None,
    ollama_connect_timeout_seconds: float | None = None,
    ollama_lease_wait_seconds: float | None = None,
    ollama_lease_ttl_seconds: int | None = None,
    ollama_circuit_failure_threshold: int | None = None,
    ollama_circuit_reset_seconds: float | None = None,
    ollama_num_ctx: int | None = None,
    llm_max_prompt_chars: int | None = None,
    lease_mode: Literal["process_local", "redis"] | None = None,
    ollama_no_cloud: bool | None = None,
) -> bool:
    """Require both the exact artifact and a current passing qualification report."""

    if not matches_qualified_local_model_registry(
        provider=provider,
        model=model,
        local=local,
        digest=digest,
        explicit_digest=explicit_digest,
    ):
        return False
    currentness_overrides = (
        ollama_server_version,
        ollama_request_timeout_seconds,
        llm_generation_max_horizon_seconds,
        ollama_connect_timeout_seconds,
        ollama_lease_wait_seconds,
        ollama_lease_ttl_seconds,
        ollama_circuit_failure_threshold,
        ollama_circuit_reset_seconds,
        ollama_num_ctx,
        llm_max_prompt_chars,
        lease_mode,
        ollama_no_cloud,
    )
    if all(value is None for value in currentness_overrides):
        return qualified_model_report_is_current()
    return qualified_model_report_is_current(
        ollama_server_version=ollama_server_version,
        ollama_request_timeout_seconds=ollama_request_timeout_seconds,
        llm_generation_max_horizon_seconds=llm_generation_max_horizon_seconds,
        ollama_connect_timeout_seconds=ollama_connect_timeout_seconds,
        ollama_lease_wait_seconds=ollama_lease_wait_seconds,
        ollama_lease_ttl_seconds=ollama_lease_ttl_seconds,
        ollama_circuit_failure_threshold=ollama_circuit_failure_threshold,
        ollama_circuit_reset_seconds=ollama_circuit_reset_seconds,
        ollama_num_ctx=ollama_num_ctx,
        llm_max_prompt_chars=llm_max_prompt_chars,
        lease_mode=lease_mode,
        ollama_no_cloud=ollama_no_cloud,
    )


def qualified_model_report_is_current(
    *,
    ollama_server_version: str | None = None,
    ollama_request_timeout_seconds: float | None = None,
    llm_generation_max_horizon_seconds: float | None = None,
    ollama_connect_timeout_seconds: float | None = None,
    ollama_lease_wait_seconds: float | None = None,
    ollama_lease_ttl_seconds: int | None = None,
    ollama_circuit_failure_threshold: int | None = None,
    ollama_circuit_reset_seconds: float | None = None,
    ollama_num_ctx: int | None = None,
    llm_max_prompt_chars: int | None = None,
    lease_mode: Literal["process_local", "redis"] | None = None,
    ollama_no_cloud: bool | None = None,
) -> bool:
    """Return whether the registry points to a current, strictly passing report."""

    try:
        registry = load_qualified_local_model()
        report_path = (ROOT / registry.qualification_report).resolve()
        report_path.relative_to(ROOT.resolve())
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return False
        # Import lazily to keep the runtime transport free of evaluator imports
        # until a readiness probe actually needs production qualification evidence.
        from scripts.evaluate_v4_local_model_qualification import (
            validate_aggregate_report,
        )

        validate_aggregate_report(payload)
        identity = payload.get("model_identity")
        environment = QualificationExecutionEnvironmentV1.model_validate(
            payload.get("execution_environment")
        )
        return bool(
            payload.get("qualification_status") == "passed"
            and payload.get("overall_pass") is True
            and isinstance(identity, dict)
            and qualification_execution_environment_is_qualified(environment)
            and environment.model_digest == identity.get("digest")
            and matches_qualified_local_model_registry(
                provider=identity.get("provider"),
                model=identity.get("model"),
                local=identity.get("local"),
                digest=identity.get("digest"),
            )
            and _current_environment_matches(
                environment,
                ollama_server_version=ollama_server_version,
                ollama_request_timeout_seconds=ollama_request_timeout_seconds,
                llm_generation_max_horizon_seconds=llm_generation_max_horizon_seconds,
                ollama_connect_timeout_seconds=ollama_connect_timeout_seconds,
                ollama_lease_wait_seconds=ollama_lease_wait_seconds,
                ollama_lease_ttl_seconds=ollama_lease_ttl_seconds,
                ollama_circuit_failure_threshold=ollama_circuit_failure_threshold,
                ollama_circuit_reset_seconds=ollama_circuit_reset_seconds,
                ollama_num_ctx=ollama_num_ctx,
                llm_max_prompt_chars=llm_max_prompt_chars,
                lease_mode=lease_mode,
                ollama_no_cloud=ollama_no_cloud,
            )
        )
    except (
        ImportError,
        importlib.metadata.PackageNotFoundError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return False
