"""Run aggregate-only qualification against the exact local qwen2.5:7b artifact.

No model output, answer, evidence text, CV text, job text, or per-case result is
written to disk or stdout. Source labels are generated synthetic fixtures, not
independent human labels, so this qualification is a bounded regression signal
and never a production-accuracy claim.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

import structlog
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from profile.cv_routing import (  # noqa: E402
    RoutingJob,
    load_routing_config,
    route_cv,
)
from profile.cv_routing_llm import (  # noqa: E402
    CVRoutingEvidenceV1,
    select_cv_via_llm,
)
from profile.models import CVArtifact, UserProfile  # noqa: E402

from core.config import Settings  # noqa: E402
from core.form_planning import (  # noqa: E402
    AnswerPolicyContext,
    AnswerPolicyV1,
)
from core.submission_domain import (  # noqa: E402
    AnswerDecisionV1,
    AnswerDisposition,
    AnswerProvenance,
    AnswerValue,
    FormFieldV1,
    ReasonCode,
    field_is_sensitive,
)
from jobs.models import JobData  # noqa: E402
from llm.client import OllamaClient  # noqa: E402
from llm.contracts import (  # noqa: E402
    FORM_RESOLUTION_PROMPT_VERSION,
    MATERIAL_PROMPT_VERSION,
    LLMReasonCode,
    ModelIdentity,
    TypedGenerationError,
)
from llm.generation import generate_material_package  # noqa: E402
from llm.qualification_registry import (  # noqa: E402
    QUALIFIED_MODEL_REGISTRY_PATH,
    QUALIFIED_OLLAMA_SERVER_VERSION,
    QualificationExecutionEnvironmentV1,
    capture_qualification_execution_environment,
    load_qualified_local_model,
    matches_qualified_local_model_registry,
    qualification_execution_environment_is_qualified,
)
from scripts.evaluate_v4_quality import (  # noqa: E402
    _evaluate_malformed,
    _MalformedCaseV1,
    _read_rows,
)

REPORT_SCHEMA_VERSION = "v4-local-model-qualification-v4"
QUALIFIED_PROVIDER = "ollama"
QUALIFIED_MODEL = "qwen2.5:7b"
ROUTING_PROMPT_VERSION = "cv-routing-v1"
HIGH_CONFIDENCE_THRESHOLD = 0.75
MINIMUM_HIGH_CONFIDENCE_CASES = 24
MINIMUM_QWEN_ROUTING_PREDICTIONS = 8
EXPECTED_FORM_LLM_CALLS = 80
MINIMUM_FORM_SUCCESSFUL_GENERATIONS = math.ceil(EXPECTED_FORM_LLM_CALLS * 0.95)
EXPECTED_MATERIAL_LLM_CALLS = 40
MINIMUM_MATERIAL_COVERAGE = 0.95
_MAX_QUALIFICATION_RUNTIME_SECONDS = 86_400.0

_COMPLETED_INTERPRETATION = (
    "This is a real local-model run over generated synthetic, contract-co-designed "
    "labels. The labels are not independent human annotations, so percentages are "
    "regression measurements, not estimates of production accuracy or live ATS "
    "performance. Coverage and abstention must be read separately from precision."
)
_BLOCKED_INTERPRETATION = (
    "Qualification did not complete. Any retained task data is bounded, "
    "aggregate-only diagnostic progress and cannot qualify the model. Generated "
    "synthetic labels are not independent human labels and no production-accuracy "
    "claim is made."
)

FIXTURES = ROOT / "tests" / "fixtures" / "v4"
DEFAULT_JSON_OUTPUT = ROOT / "docs" / "qualification" / "v4-local-model-qualification.json"
DEFAULT_MARKDOWN_OUTPUT = ROOT / "docs" / "qualification" / "v4-local-model-qualification.md"

_SOURCE_PATHS = {
    Path(__file__).resolve(),
    ROOT / "scripts" / "build_v4_local_model_material_fixtures.py",
    ROOT / "scripts" / "build_v4_offline_datasets.py",
    ROOT / "scripts" / "evaluate_v4_quality.py",
    ROOT / "config" / "qualified_local_model.json",
    ROOT / "config" / "qualified_runtime_packages.json",
    ROOT / "core" / "config.py",
    ROOT / "core" / "form_plan_evidence.py",
    ROOT / "core" / "form_planning.py",
    ROOT / "core" / "material_audit.py",
    ROOT / "core" / "sensitive_policy.py",
    ROOT / "core" / "submission_domain.py",
    ROOT / "jobs" / "models.py",
    ROOT / "llm" / "claim_evidence.py",
    ROOT / "llm" / "client.py",
    ROOT / "llm" / "contracts.py",
    ROOT / "llm" / "execution_guard.py",
    ROOT / "llm" / "generation.py",
    ROOT / "llm" / "ollama_runtime.py",
    ROOT / "llm" / "prompts.py",
    ROOT / "llm" / "qualification_registry.py",
    ROOT / "profile" / "cv_content_cache.py",
    ROOT / "profile" / "cv_facts.py",
    ROOT / "profile" / "cv_routing.py",
    ROOT / "profile" / "cv_routing_llm.py",
    ROOT / "profile" / "models.py",
    ROOT / "profile" / "pdf_loader.py",
}
_SOURCE_FILES = {path.relative_to(ROOT).as_posix(): path for path in sorted(_SOURCE_PATHS)}

_CV_HASH = "c" * 64
_SYNTHETIC_EMAIL = "candidate@example.test"
_SYNTHETIC_PHONE = "+10000000000"
_SYNTHETIC_URL = "https://example.test/profile"
_SAFE_REASON_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
_SAFE_COUNTER_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_./-]{0,79}$")
_EMAIL_RE = re.compile(r"(?i)\b[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
_URL_RE = re.compile(r"(?i)\bhttps?://[^\s\"'<>]+")
_PHONE_RE = re.compile(r"(?<![\w])\+?\d(?:[\d ()-]{7,}\d)(?![\w])")

_QUALIFICATION_TASK_ORDER = (
    "cv_routing",
    "form_resolution",
    "full_material",
    "malformed_boundaries",
)
_QUALIFICATION_FAILURE_STAGES = (
    "preflight",
    *_QUALIFICATION_TASK_ORDER,
    "final_readiness",
    "aggregate_validation",
    "artifact_write",
)
_QUALIFICATION_PURPOSE_ALIASES = {
    "cv_routing": "cv_routing",
    "form_resolution": "form_resolution",
    # The production purpose name is a prohibited content-field key in aggregate
    # reports. The qualification alias describes the evaluated boundary without
    # weakening the global content-key privacy check.
    "cover_letter": "full_material",
    "quality_evaluation": "quality_evaluation",
    "profile_extraction": "profile_extraction",
    "test": "test",
}
_QUALIFICATION_PURPOSE_KEYS = frozenset(
    {*_QUALIFICATION_PURPOSE_ALIASES.values(), "other_bounded_purpose"}
)

_SYNTHETIC_CONFIRMED = {
    "work_authorization": "yes",
    "work_permit": "yes",
    "right_to_work": "yes",
    "visa_sponsorship": "no",
    "sponsorship": "no",
    "nationality": "Syntheticland",
    "citizenship": "Syntheticland",
    "security_clearance": "no",
    "clearance": "no",
    "license": "yes",
    "licensing": "yes",
    "certification": "Synthetic Certificate",
    "gender": "prefer_not_to_say",
    "race": "prefer_not_to_say",
    "ethnicity": "prefer_not_to_say",
    "disability": "prefer_not_to_say",
    "veteran_status": "no",
    "marital_status": "prefer_not_to_say",
    "religion": "prefer_not_to_say",
    "age": "30",
}

_SYNTHETIC_CV_FACTS = {
    "primary_language": "Developed production services in Python",
    "backend_framework": "Implemented backend APIs with FastAPI",
    "database_skill": "Designed auditable schemas in PostgreSQL",
    "cloud_platform": "Deployed services on AWS",
    "container_platform": "Operated workloads on Kubernetes",
    "iac_tool": "Authored infrastructure modules with Terraform",
    "data_tool": "Built distributed data pipelines with Spark",
    "ml_framework": "Developed machine learning models with PyTorch",
    "frontend_language": "Built user interfaces in TypeScript",
    "frontend_framework": "Implemented frontend components with React",
    "test_framework": "Wrote automated tests with Pytest",
    "automation_tool": "Automated browser tests with Selenium",
    "operating_system": "Operated production services on Linux",
    "embedded_language": "Developed embedded software in C++",
    "realtime_system": "Built real-time software with FreeRTOS",
    "analytics_tool": "Created analytics dashboards with Tableau",
    "pipeline_tool": "Built data transformations with dbt",
    "api_style": "Designed REST APIs",
    "version_control": "Managed source code with Git",
    "highest_degree": "Completed a BSc degree",
}

_ROUTING_EXCERPTS = {
    "ai-ml": (
        "Developed Python and PyTorch machine learning models. "
        "Built NLP and computer vision workflows."
    ),
    "data": ("Built SQL, dbt, Spark, pandas, ETL, analytics, and Tableau data workflows."),
    "software": (
        "Developed Java APIs and TypeScript React applications. "
        "Automated tests with Selenium and Pytest."
    ),
    "platform": (
        "Operated Kubernetes and Linux services on AWS. "
        "Authored Terraform and developed C++ firmware with RTOS."
    ),
}


def _routing_evidence() -> dict[str, CVRoutingEvidenceV1]:
    return {
        cv_id: CVRoutingEvidenceV1(
            cv_id=cv_id,
            pdf_sha256=hashlib.sha256(f"{cv_id}:{excerpt}".encode()).hexdigest(),
            excerpt=excerpt,
        )
        for cv_id, excerpt in _ROUTING_EXCERPTS.items()
    }


class _FixtureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _RoutingJobFixtureV1(_FixtureModel):
    title: str = Field(max_length=200)
    description: str = Field(max_length=3_000)
    seniority: str = Field(max_length=32)
    required_skills: tuple[str, ...] = Field(max_length=20)


class _RoutingCaseV1(_FixtureModel):
    id: str = Field(pattern=r"^route-\d{3}$")
    category: str = Field(min_length=1, max_length=32)
    job: _RoutingJobFixtureV1
    expected_cv_id: Literal["ai-ml", "data", "software", "platform"] | None


class _FormExpectedV1(_FixtureModel):
    provenance: AnswerProvenance
    disposition: AnswerDisposition
    value: AnswerValue | None = None
    reason_code: ReasonCode | None = None
    evidence_refs: tuple[str, ...] = Field(max_length=8)
    llm_called: bool


class _FormSourceCaseV1(_FixtureModel):
    id: str = Field(min_length=1, max_length=128)
    locale: Literal["en", "he"]
    field: FormFieldV1
    expected: _FormExpectedV1

    @model_validator(mode="after")
    def field_identity_matches(self) -> _FormSourceCaseV1:
        if self.id != self.field.field_id:
            raise ValueError("form source id must match field id")
        return self


class _MaterialCaseV1(_FixtureModel):
    id: str = Field(pattern=r"^material-\d{3}$")
    family: str = Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")
    locale: Literal["en"]
    job: JobData
    cv_lines: tuple[str, ...] = Field(min_length=1, max_length=12)
    confirmed_facts: dict[str, str] = Field(min_length=1, max_length=12)
    synthetic_label: Literal["complete_non_sensitive_input"]


class _InstrumentedOllamaClient(OllamaClient):
    """Count only bounded metadata while never retaining provider content."""

    def __init__(self) -> None:
        super().__init__()
        self.typed_invocations = 0
        self.provider_attempts = 0
        self.provider_payloads = 0
        self.successful_generations = 0
        self.identity_observations = 0
        self.registry_identity_observations = 0
        self.server_version_observations = 0
        self.qualified_server_version_observations = 0
        self.max_concurrent_calls = 0
        self._active_calls = 0
        self.purpose_counts: Counter[str] = Counter()
        self.failure_reasons: Counter[str] = Counter()
        self.identities: set[tuple[str, str, str | None]] = set()
        self.server_versions: set[str] = set()

    async def _generate_schema_payload(self, **kwargs: Any):  # type: ignore[override]
        self.provider_attempts += 1
        payload = await super()._generate_schema_payload(**kwargs)
        self.provider_payloads += 1
        return payload

    async def generate_typed(self, **kwargs: Any):  # type: ignore[override]
        self._active_calls += 1
        self.max_concurrent_calls = max(
            self.max_concurrent_calls,
            self._active_calls,
        )
        if self._active_calls != 1:
            self._active_calls -= 1
            raise RuntimeError("local qualification inference was not serialized")
        self.typed_invocations += 1
        purpose = str(getattr(kwargs.get("purpose"), "value", kwargs.get("purpose") or "unknown"))
        self.purpose_counts[_qualification_purpose_key(purpose)] += 1
        try:
            result = await super().generate_typed(**kwargs)
            self.successful_generations += 1
            identity = result.model_identity
            self.identity_observations += 1
            self.registry_identity_observations += int(
                matches_qualified_local_model_registry(
                    provider=identity.provider,
                    model=identity.model,
                    local=identity.local,
                    digest=identity.digest,
                )
            )
            self.identities.add((identity.provider, identity.model, identity.digest))
            server_version = self.runtime.ollama_server_version
            if isinstance(server_version, str):
                self.server_version_observations += 1
                self.qualified_server_version_observations += int(
                    server_version == QUALIFIED_OLLAMA_SERVER_VERSION
                )
                self.server_versions.add(server_version)
            return result
        except TypedGenerationError as exc:
            self.failure_reasons[_safe_reason(exc.reason_code.value)] += 1
            raise
        except Exception:
            self.failure_reasons["OTHER_BOUNDED_REASON"] += 1
            raise
        finally:
            self._active_calls -= 1


def _safe_reason(value: object) -> str:
    rendered = str(getattr(value, "value", value) or "")
    return rendered if _SAFE_REASON_RE.fullmatch(rendered) else "OTHER_BOUNDED_REASON"


def _qualification_purpose_key(value: object) -> str:
    """Map production purposes to the report's content-safe bounded vocabulary."""

    rendered = str(getattr(value, "value", value) or "")
    return _QUALIFICATION_PURPOSE_ALIASES.get(rendered, "other_bounded_purpose")


def _empty_inference_record() -> dict[str, Any]:
    return {
        "typed_invocations": 0,
        "provider_attempts": 0,
        "provider_payloads": 0,
        "successful_generations": 0,
        "identity_observations": 0,
        "registry_identity_observations": 0,
        "distinct_model_identities": 0,
        "server_version_observations": 0,
        "qualified_server_version_observations": 0,
        "distinct_server_versions": 0,
        "purpose_counts": {},
        "failure_reason_counts": {},
        "maximum_concurrent_calls": 0,
    }


def _inference_record(client: _InstrumentedOllamaClient) -> dict[str, Any]:
    return {
        "typed_invocations": client.typed_invocations,
        "provider_attempts": client.provider_attempts,
        "provider_payloads": client.provider_payloads,
        "successful_generations": client.successful_generations,
        "identity_observations": client.identity_observations,
        "registry_identity_observations": client.registry_identity_observations,
        "distinct_model_identities": len(client.identities),
        "server_version_observations": client.server_version_observations,
        "qualified_server_version_observations": (client.qualified_server_version_observations),
        "distinct_server_versions": len(client.server_versions),
        "purpose_counts": dict(sorted(client.purpose_counts.items())),
        "failure_reason_counts": dict(sorted(client.failure_reasons.items())),
        "maximum_concurrent_calls": client.max_concurrent_calls,
    }


@dataclass
class _QualificationProgress:
    """Mutable aggregate-only checkpoint retained across evaluator failures."""

    failure_stage: str = "preflight"
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    inference: dict[str, Any] = field(default_factory=_empty_inference_record)
    settings: Settings | None = None
    ollama_server_version: str | None = None

    def enter(self, stage: str) -> None:
        if stage not in _QUALIFICATION_FAILURE_STAGES:
            raise ValueError("qualification progress stage is invalid")
        self.failure_stage = stage

    def capture(self, client: _InstrumentedOllamaClient) -> None:
        self.settings = client.settings
        self.ollama_server_version = client.runtime.ollama_server_version
        self.inference = _inference_record(client)

    def complete(
        self,
        task_name: str,
        task: dict[str, Any],
        client: _InstrumentedOllamaClient,
    ) -> None:
        expected_name = _QUALIFICATION_TASK_ORDER[len(self.tasks)]
        if task_name != expected_name:
            raise ValueError("qualification tasks must complete in order")
        self.tasks[task_name] = task
        self.capture(client)


class _QualificationBoundaryError(RuntimeError):
    """Internal evaluator boundary error carrying only an allowlisted code."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = _safe_reason(reason_code)
        super().__init__("qualification boundary failed")


def _exception_reason_code(exc: BaseException, stage: str) -> str:
    """Return a stable code without persisting exception text or dynamic keys."""

    if isinstance(exc, _QualificationBoundaryError):
        return exc.reason_code
    if isinstance(exc, TypedGenerationError):
        return _safe_reason(exc.reason_code.value)
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "PROVIDER_TIMEOUT"
    if isinstance(exc, (KeyError, TypeError)):
        return "EVALUATOR_RESULT_SCHEMA_MISMATCH"
    if isinstance(exc, ValueError):
        if stage == "preflight":
            return "EVALUATOR_INPUT_VALIDATION_FAILED"
        if stage == "aggregate_validation":
            return "REPORT_VALIDATION_FAILED"
        return "EVALUATOR_RESULT_VALIDATION_FAILED"
    return "UNEXPECTED_EVALUATOR_ERROR"


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _inference_snapshot(client: _InstrumentedOllamaClient) -> tuple[int, int, int, int]:
    return (
        client.typed_invocations,
        client.provider_attempts,
        client.provider_payloads,
        client.successful_generations,
    )


def _inference_delta(
    client: _InstrumentedOllamaClient,
    before: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    after = _inference_snapshot(client)
    return (
        after[0] - before[0],
        after[1] - before[1],
        after[2] - before[2],
        after[3] - before[3],
    )


def _exact_model_threshold_passes(
    *,
    stable: bool,
    distinct_identities: int,
    identity_observations: int,
    registry_identity_observations: int,
    successful_generations: int,
) -> bool:
    return bool(
        stable
        and successful_generations > 0
        and distinct_identities == 1
        and identity_observations == successful_generations
        and registry_identity_observations == successful_generations
    )


def _serialized_inference_threshold_passes(
    *,
    maximum_concurrent_calls: int,
    typed_invocations: int,
) -> bool:
    return maximum_concurrent_calls == 1 and typed_invocations > 0


def _execution_environment_threshold_passes(
    *,
    stable: bool,
    pre_post_observations: int,
    distinct_server_versions: int,
    server_version_observations: int,
    qualified_server_version_observations: int,
    successful_generations: int,
    environment_qualified: bool,
) -> bool:
    return bool(
        stable
        and environment_qualified
        and pre_post_observations == 2
        and successful_generations > 0
        and distinct_server_versions == 1
        and server_version_observations == successful_generations
        and qualified_server_version_observations == successful_generations
    )


def _dataset_record(path: Path, cases: int) -> dict[str, Any]:
    return {
        "file": path.name,
        "cases": cases,
        "sha256": _normalized_text_sha256(path),
    }


def _normalized_text_sha256(path: Path) -> str:
    """Hash text canonically so Git CRLF conversion cannot stale qualification."""

    normalized = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _source_integrity_record() -> dict[str, Any]:
    files = {
        key: {
            "file": path.relative_to(ROOT).as_posix(),
            "sha256": _normalized_text_sha256(path),
        }
        for key, path in sorted(_SOURCE_FILES.items())
    }
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return {
        "files": files,
        "combined_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def _qualified_model_registry_record() -> dict[str, Any]:
    registry = load_qualified_local_model()
    return {
        "schema_version": registry.schema_version,
        "provider": registry.provider,
        "model": registry.model,
        "digest": registry.digest,
        "qualification_report": registry.qualification_report,
        "qualification_report_schema_version": (registry.qualification_report_schema_version),
        "registry_file": QUALIFIED_MODEL_REGISTRY_PATH.relative_to(ROOT).as_posix(),
        "registry_sha256": _normalized_text_sha256(QUALIFIED_MODEL_REGISTRY_PATH),
    }


def _execution_environment_record(
    *,
    settings: Settings | None = None,
    ollama_server_version: str | None,
    ollama_reason_code: str | None,
    model_digest: str | None = None,
) -> dict[str, Any]:
    bounded_reason = (
        None
        if ollama_server_version is not None
        else _safe_reason(ollama_reason_code or "OLLAMA_VERSION_UNAVAILABLE")
    )
    if settings is None:
        inference_values: dict[str, Any] = {
            "ollama_request_timeout_seconds": None,
            "llm_generation_max_horizon_seconds": None,
            "ollama_connect_timeout_seconds": None,
            "ollama_lease_wait_seconds": None,
            "ollama_lease_ttl_seconds": None,
            "ollama_circuit_failure_threshold": None,
            "ollama_circuit_reset_seconds": None,
            "ollama_num_ctx": None,
            "llm_max_prompt_chars": None,
            "lease_mode": None,
            "ollama_no_cloud": None,
            "inference_config_reason_code": _safe_reason(
                ollama_reason_code or "QUALIFIED_RUNTIME_MISMATCH"
            ),
        }
    else:
        inference_values = {
            "ollama_request_timeout_seconds": settings.ollama_request_timeout_seconds,
            "llm_generation_max_horizon_seconds": (settings.llm_generation_max_horizon_seconds),
            "ollama_connect_timeout_seconds": settings.ollama_connect_timeout_seconds,
            "ollama_lease_wait_seconds": settings.ollama_lease_wait_seconds,
            "ollama_lease_ttl_seconds": settings.ollama_lease_ttl_seconds,
            "ollama_circuit_failure_threshold": (settings.ollama_circuit_failure_threshold),
            "ollama_circuit_reset_seconds": settings.ollama_circuit_reset_seconds,
            "ollama_num_ctx": settings.ollama_num_ctx,
            "llm_max_prompt_chars": settings.llm_max_prompt_chars,
            "lease_mode": "process_local" if settings.tasks_always_eager else "redis",
            "ollama_no_cloud": settings.ollama_no_cloud,
            "inference_config_reason_code": None,
        }
    try:
        environment = capture_qualification_execution_environment(
            ollama_server_version=ollama_server_version,
            ollama_reason_code=bounded_reason,
            model_digest=model_digest,
            **inference_values,
        )
    except (TypeError, ValueError):
        if settings is None:
            raise
        unavailable_reason = _safe_reason(ollama_reason_code or "EVALUATOR_INPUT_VALIDATION_FAILED")
        environment = capture_qualification_execution_environment(
            ollama_server_version=ollama_server_version,
            ollama_reason_code=bounded_reason,
            ollama_request_timeout_seconds=None,
            llm_generation_max_horizon_seconds=None,
            ollama_connect_timeout_seconds=None,
            ollama_lease_wait_seconds=None,
            ollama_lease_ttl_seconds=None,
            ollama_circuit_failure_threshold=None,
            ollama_circuit_reset_seconds=None,
            ollama_num_ctx=None,
            llm_max_prompt_chars=None,
            lease_mode=None,
            ollama_no_cloud=None,
            inference_config_reason_code=unavailable_reason,
            model_digest=model_digest,
        )
    return environment.model_dump(mode="json")


def _capture_execution_environment(
    settings: Settings,
    *,
    ollama_server_version: str,
    model_digest: str | None = None,
) -> QualificationExecutionEnvironmentV1:
    return QualificationExecutionEnvironmentV1.model_validate(
        _execution_environment_record(
            settings=settings,
            ollama_server_version=ollama_server_version,
            ollama_reason_code=None,
            model_digest=model_digest,
        )
    )


def _dataset_records(paths: dict[str, Path]) -> dict[str, dict[str, Any]]:
    return {
        "routing": _dataset_record(paths["routing"], 120),
        "routing_config": _dataset_record(paths["routing_config"], 1),
        "forms": _dataset_record(paths["forms"], 240),
        "materials": _dataset_record(paths["materials"], 40),
        "malformed": _dataset_record(paths["malformed"], 30),
    }


def _qualification_input_attestation(paths: dict[str, Path]) -> dict[str, Any]:
    """Capture the exact source and fixture bytes bound to one qualification."""

    return {
        "source_integrity": _source_integrity_record(),
        "datasets": _dataset_records(paths),
    }


def _require_qualification_inputs_unchanged(
    initial: dict[str, Any],
    paths: dict[str, Path],
) -> None:
    """Block if any attested source or fixture changed during qualification."""

    if _qualification_input_attestation(paths) != initial:
        raise _QualificationBoundaryError("QUALIFICATION_INPUT_DRIFT")


def _load_json_rows(path: Path, expected_count: int) -> list[dict[str, Any]]:
    if path.stat().st_size > 2_000_000:
        raise ValueError(f"{path.name} exceeds the qualification fixture bound")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or len(payload) != expected_count:
        raise ValueError(f"{path.name} must contain exactly {expected_count} rows")
    if any(not isinstance(row, dict) for row in payload):
        raise ValueError(f"{path.name} must contain JSON objects")
    return payload


def _load_form_rows(path: Path) -> list[_FormSourceCaseV1]:
    """Select only source field and labels; embedded llm_output is discarded."""

    rows = _load_json_rows(path, 240)
    selected = [
        {
            "id": row.get("id"),
            "locale": row.get("locale"),
            "field": row.get("field"),
            "expected": row.get("expected"),
        }
        for row in rows
    ]
    parsed = [_FormSourceCaseV1.model_validate(row) for row in selected]
    if len({row.id for row in parsed}) != len(parsed):
        raise ValueError("form source contains duplicate ids")
    return parsed


def _synthetic_profile(
    *,
    cv_hash: str = _CV_HASH,
    cv_facts: dict[str, str] | None = None,
    confirmed: dict[str, str] | None = None,
    resume_text: str = "",
) -> UserProfile:
    profile = UserProfile.model_validate(
        {
            "personal": {
                "name": "Test Candidate",
                "email": _SYNTHETIC_EMAIL,
                "phone": _SYNTHETIC_PHONE,
                "location": "Test City, Test Country",
            },
            "links": {"linkedin": _SYNTHETIC_URL},
            "resume": {
                "text": resume_text,
                "pdf_sha256": cv_hash,
            },
            "evidence": {
                "user_confirmed": confirmed or _SYNTHETIC_CONFIRMED,
                "cv_extracted_by_artifact": {
                    cv_hash: cv_facts or _SYNTHETIC_CV_FACTS,
                },
            },
        }
    )
    return profile


def _answer_context(profile: UserProfile, locale: str) -> AnswerPolicyContext:
    return AnswerPolicyContext(
        profile=profile,
        profile_version=7,
        selected_cv_id="synthetic-cv",
        selected_cv_hash=_CV_HASH,
        adapter_name="fixture",
        adapter_version="1.0.0",
        selector_version="fixture-v2",
        form_fingerprint="f" * 64,
        locale=locale,
        attached_cv_id="synthetic-cv",
        attached_cv_hash=_CV_HASH,
        attachment_verified=True,
    )


def _decision_matches(
    decision: AnswerDecisionV1,
    expected: _FormExpectedV1,
) -> bool:
    return (
        decision.provenance is expected.provenance
        and decision.disposition is expected.disposition
        and decision.value == expected.value
        and decision.reason_code is expected.reason_code
        and decision.evidence_refs == expected.evidence_refs
    )


def _progress(phase: str, completed: int, total: int, started: float) -> None:
    print(
        json.dumps(
            {
                "phase": phase,
                "completed": completed,
                "total": total,
                "elapsed_seconds": round(time.perf_counter() - started, 1),
            },
            sort_keys=True,
        ),
        flush=True,
    )


def _suppress_application_logs() -> None:
    """Keep stdout aggregate-only even when production paths emit events."""

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL),
    )


async def _require_exact_model(
    client: _InstrumentedOllamaClient,
) -> tuple[ModelIdentity, str]:
    settings = client.settings
    if (
        settings.llm_provider != QUALIFIED_PROVIDER
        or settings.llm_model.strip() != QUALIFIED_MODEL
        or not settings.ollama_no_cloud
    ):
        raise TypedGenerationError(
            reason_code=LLMReasonCode.CONFIGURATION_INVALID,
            message="qualification requires exact local qwen configuration",
        )
    readiness = await client.runtime.readiness(
        deadline=datetime.now(UTC) + timedelta(seconds=15),
        record_failure=True,
    )
    identity = readiness.model_identity
    server_version = readiness.ollama_server_version
    if (
        not readiness.ok
        or server_version != QUALIFIED_OLLAMA_SERVER_VERSION
        or not matches_qualified_local_model_registry(
            provider=identity.provider,
            model=identity.model,
            local=identity.local,
            digest=identity.digest,
            explicit_digest=settings.ollama_expected_model_digest,
        )
    ):
        reason = (
            "OLLAMA_VERSION_NOT_QUALIFIED"
            if server_version is not None and server_version != QUALIFIED_OLLAMA_SERVER_VERSION
            else (readiness.reason_code.value if readiness.reason_code else "LLM_MODEL_NOT_READY")
        )
        raise _QualificationBoundaryError(_safe_reason(reason))
    return identity, server_version


async def _evaluate_routing(
    rows: list[_RoutingCaseV1],
    config_path: Path,
    client: _InstrumentedOllamaClient,
) -> dict[str, Any]:
    config = load_routing_config(config_path)
    correct = 0
    incorrect = 0
    abstained = 0
    high_correct = 0
    high_incorrect = 0
    fallback_cases = 0
    fallback_typed_invocations = 0
    fallback_provider_attempts = 0
    fallback_provider_payloads = 0
    fallback_successful_generations = 0
    fallback_provider_cases = 0
    fallback_successful_cases = 0
    qwen_correct = 0
    qwen_incorrect = 0
    qwen_abstained = 0
    reason_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    started = time.perf_counter()

    for index, row in enumerate(rows, start=1):
        job = RoutingJob.model_validate(row.job.model_dump(mode="python"))
        decision = route_cv(job, config)
        category_counts[row.category] += 1
        if not decision.overridden and decision.fallback_reason in {
            "confidence_below_threshold",
            "abstained_low_confidence",
        }:
            fallback_cases += 1
            before = _inference_snapshot(client)
            model_decision = await select_cv_via_llm(
                job,
                config,
                _routing_evidence(),
                client=client,
            )
            typed, attempts, payloads, successes = _inference_delta(client, before)
            fallback_typed_invocations += typed
            fallback_provider_attempts += attempts
            fallback_provider_payloads += payloads
            fallback_successful_generations += successes
            fallback_provider_cases += int(attempts > 0)
            fallback_successful_cases += int(successes == 1)
            qwen_qualified = (
                model_decision.selected_cv_id is not None and model_decision.fallback_reason is None
            )
            if not qwen_qualified:
                qwen_abstained += 1
            elif model_decision.selected_cv_id == row.expected_cv_id:
                qwen_correct += 1
            else:
                qwen_incorrect += 1
            if model_decision.selected_cv_id is not None:
                decision = model_decision
            if model_decision.fallback_reason:
                reason_counts[_safe_reason(model_decision.fallback_reason)] += 1

        qualified_selection = (
            decision.selected_cv_id is not None and decision.fallback_reason is None
        )
        if not qualified_selection:
            abstained += 1
        elif decision.selected_cv_id == row.expected_cv_id:
            correct += 1
        else:
            incorrect += 1

        if qualified_selection and decision.confidence >= HIGH_CONFIDENCE_THRESHOLD:
            if decision.selected_cv_id == row.expected_cv_id:
                high_correct += 1
            else:
                high_incorrect += 1
        if index % 20 == 0 or index == len(rows):
            _progress("routing", index, len(rows), started)

    resolved = correct + incorrect
    high_resolved = high_correct + high_incorrect
    high_precision = _ratio(high_correct, high_resolved)
    qwen_resolved = qwen_correct + qwen_incorrect
    qwen_precision = _ratio(qwen_correct, qwen_resolved)
    qwen_coverage = _ratio(qwen_resolved, fallback_cases)
    routing_gate_passed = (
        high_precision >= 0.95
        and high_resolved >= MINIMUM_HIGH_CONFIDENCE_CASES
        and fallback_typed_invocations == fallback_cases
        and fallback_provider_cases == fallback_cases
        and fallback_successful_cases == fallback_cases
        and fallback_successful_generations == fallback_cases
        and fallback_provider_attempts >= fallback_cases
        and fallback_provider_payloads >= fallback_cases
        and qwen_precision >= 0.95
        and qwen_resolved >= MINIMUM_QWEN_ROUTING_PREDICTIONS
        and reason_counts.get("llm_routing_error", 0) == 0
    )
    return {
        "cases": len(rows),
        "precision": _ratio(correct, resolved),
        "high_confidence_threshold": HIGH_CONFIDENCE_THRESHOLD,
        "high_confidence_precision": high_precision,
        "high_confidence_coverage": _ratio(high_resolved, len(rows)),
        "coverage": _ratio(resolved, len(rows)),
        "abstention_rate": _ratio(abstained, len(rows)),
        "correct": correct,
        "incorrect": incorrect,
        "abstained": abstained,
        "high_confidence_correct": high_correct,
        "high_confidence_incorrect": high_incorrect,
        "fallback_cases": fallback_cases,
        "typed_invocations": fallback_typed_invocations,
        "provider_attempts": fallback_provider_attempts,
        "provider_payloads": fallback_provider_payloads,
        "successful_generations": fallback_successful_generations,
        "provider_cases": fallback_provider_cases,
        "successful_generation_cases": fallback_successful_cases,
        "qwen_only": {
            "precision": qwen_precision,
            "coverage": qwen_coverage,
            "abstention_rate": _ratio(qwen_abstained, fallback_cases),
            "correct": qwen_correct,
            "incorrect": qwen_incorrect,
            "abstained": qwen_abstained,
        },
        "reason_counts": dict(sorted(reason_counts.items())),
        "category_counts": dict(sorted(category_counts.items())),
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "threshold": {
            "requirement": (
                "high-confidence precision >= 0.95 with at least "
                f"{MINIMUM_HIGH_CONFIDENCE_CASES} predictions, and every "
                "fallback-scope case completes a successful real provider generation; "
                f"qwen-only precision >= 0.95 over at least "
                f"{MINIMUM_QWEN_ROUTING_PREDICTIONS} resolved fallback cases"
            ),
            "actual": {
                "precision": high_precision,
                "predictions": high_resolved,
                "fallback_cases": fallback_cases,
                "provider_cases": fallback_provider_cases,
                "successful_generation_cases": fallback_successful_cases,
                "provider_attempts": fallback_provider_attempts,
                "qwen_only_precision": qwen_precision,
                "qwen_only_predictions": qwen_resolved,
                "routing_errors": reason_counts.get("llm_routing_error", 0),
            },
            "passed": routing_gate_passed,
        },
    }


async def _evaluate_forms(
    rows: list[_FormSourceCaseV1],
    client: _InstrumentedOllamaClient,
) -> dict[str, Any]:
    profile = _synthetic_profile()
    correct_resolved = 0
    incorrect_resolved = 0
    abstained = 0
    expected_llm_cases = 0
    typed_invocations = 0
    provider_attempts = 0
    provider_payloads = 0
    successful_generations = 0
    provider_cases = 0
    successful_generation_cases = 0
    correct_synthesis = 0
    resolved_synthesis = 0
    sensitive_provider_attempts = 0
    sensitive_automatic_eligible = 0
    unsupported_eligible = 0
    reason_counts: Counter[str] = Counter()
    provenance_counts: Counter[str] = Counter()
    started = time.perf_counter()

    for index, row in enumerate(rows, start=1):
        before = _inference_snapshot(client)
        result = await AnswerPolicyV1(llm_client=client).plan_fields(
            (row.field,),
            _answer_context(profile, row.locale),
        )
        typed, attempts, payloads, successes = _inference_delta(client, before)
        typed_invocations += typed
        provider_attempts += attempts
        provider_payloads += payloads
        successful_generations += successes
        provider_cases += int(attempts > 0)
        successful_generation_cases += int(successes == 1)
        expected_llm_cases += int(row.expected.llm_called)
        decision = result.decisions[0]
        matches = _decision_matches(decision, row.expected)
        provenance_counts[decision.provenance.value] += 1
        if decision.reason_code:
            reason_counts[_safe_reason(decision.reason_code.value)] += 1

        is_sensitive = field_is_sensitive(row.field)
        if is_sensitive:
            sensitive_provider_attempts += attempts
        if (
            is_sensitive
            and decision.disposition is AnswerDisposition.RESOLVED
            and decision.provenance
            not in {
                AnswerProvenance.USER_CONFIRMED,
                AnswerProvenance.OPERATOR_APPROVED_REUSABLE,
            }
        ):
            sensitive_automatic_eligible += 1

        if decision.disposition is AnswerDisposition.RESOLVED:
            if matches:
                correct_resolved += 1
            else:
                incorrect_resolved += 1
                unsupported_eligible += 1
        else:
            abstained += 1

        if row.expected.llm_called:
            if decision.disposition is AnswerDisposition.RESOLVED:
                resolved_synthesis += 1
                correct_synthesis += int(matches and successes == 1)

        if index % 20 == 0 or index == len(rows):
            _progress("form_resolution", index, len(rows), started)

    resolved = correct_resolved + incorrect_resolved
    precision = _ratio(correct_resolved, resolved)
    synthesis_precision = _ratio(correct_synthesis, resolved_synthesis)
    safety_violations = (
        unsupported_eligible + sensitive_automatic_eligible + sensitive_provider_attempts
    )
    threshold_passed = _form_threshold_passes(
        precision=precision,
        synthesis_precision=synthesis_precision,
        expected_llm_cases=expected_llm_cases,
        typed_invocations=typed_invocations,
        provider_cases=provider_cases,
        successful_generation_cases=successful_generation_cases,
        successful_generations=successful_generations,
        provider_attempts=provider_attempts,
        provider_payloads=provider_payloads,
        safety_violations=safety_violations,
    )
    return {
        "cases": len(rows),
        "precision": precision,
        "synthesis_precision": synthesis_precision,
        "coverage": _ratio(resolved, len(rows)),
        "abstention_rate": _ratio(abstained, len(rows)),
        "correct_resolved": correct_resolved,
        "incorrect_resolved": incorrect_resolved,
        "abstained": abstained,
        "correct_synthesis": correct_synthesis,
        "resolved_synthesis": resolved_synthesis,
        "expected_synthesis_cases": expected_llm_cases,
        "typed_invocations": typed_invocations,
        "provider_attempts": provider_attempts,
        "provider_payloads": provider_payloads,
        "successful_generations": successful_generations,
        "provider_cases": provider_cases,
        "successful_generation_cases": successful_generation_cases,
        "unsupported_eligible": unsupported_eligible,
        "sensitive_automatic_eligible": sensitive_automatic_eligible,
        "sensitive_provider_attempts": sensitive_provider_attempts,
        "reason_counts": dict(sorted(reason_counts.items())),
        "provenance_counts": dict(sorted(provenance_counts.items())),
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "threshold": {
            "requirement": (
                "overall and synthesis precision >= 0.95, all 80 intended cases "
                "attempt the exact local provider, at least 95% complete a successful "
                "generation, and zero unsupported or sensitive eligibility"
            ),
            "actual": {
                "precision": precision,
                "synthesis_precision": synthesis_precision,
                "provider_cases": provider_cases,
                "successful_generation_cases": successful_generation_cases,
                "provider_attempts": provider_attempts,
                "safety_violations": safety_violations,
            },
            "passed": threshold_passed,
        },
    }


def _form_threshold_passes(
    *,
    precision: float,
    synthesis_precision: float,
    expected_llm_cases: int,
    typed_invocations: int,
    provider_cases: int,
    successful_generation_cases: int,
    successful_generations: int,
    provider_attempts: int,
    provider_payloads: int,
    safety_violations: int,
) -> bool:
    """Require complete attempts, bounded availability, precision, and zero unsafe eligibility."""

    return (
        precision >= 0.95
        and synthesis_precision >= 0.95
        and expected_llm_cases == EXPECTED_FORM_LLM_CALLS
        and typed_invocations == EXPECTED_FORM_LLM_CALLS
        and provider_cases == EXPECTED_FORM_LLM_CALLS
        and successful_generation_cases >= MINIMUM_FORM_SUCCESSFUL_GENERATIONS
        and successful_generations >= MINIMUM_FORM_SUCCESSFUL_GENERATIONS
        and provider_attempts >= EXPECTED_FORM_LLM_CALLS
        and provider_payloads >= MINIMUM_FORM_SUCCESSFUL_GENERATIONS
        and safety_violations == 0
    )


async def _evaluate_materials(
    rows: list[_MaterialCaseV1],
    client: _InstrumentedOllamaClient,
) -> dict[str, Any]:
    eligible = 0
    blocked = 0
    generation_failed = 0
    unsupported_eligible = 0
    sensitive_eligible = 0
    supported_claims = 0
    unsupported_claims = 0
    typed_invocations = 0
    provider_attempts = 0
    provider_payloads = 0
    successful_generations = 0
    provider_cases = 0
    successful_generation_cases = 0
    reason_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    started = time.perf_counter()

    # Keep this population reproducibly synthetic. Production feedback remains
    # valid local context, but must not enter or influence this qualification.
    with patch("llm.generation._load_few_shot_examples", return_value=[]):
        for index, row in enumerate(rows, start=1):
            cv_text = "\n".join(row.cv_lines)
            cv_hash = hashlib.sha256(cv_text.encode("utf-8")).hexdigest()
            profile = _synthetic_profile(
                cv_hash=cv_hash,
                cv_facts={},
                confirmed=row.confirmed_facts,
                resume_text=cv_text,
            )
            artifact = CVArtifact(
                pdf_sha256=cv_hash,
                byte_size=len(cv_text.encode("utf-8")),
                extracted_text=cv_text,
            )
            family_counts[row.family] += 1
            before = _inference_snapshot(client)
            try:
                package = await generate_material_package(
                    row.job,
                    profile,
                    cv_artifact=artifact,
                    profile_version=7,
                    client=client,
                )
                typed, attempts, payloads, successes = _inference_delta(client, before)
                typed_invocations += typed
                provider_attempts += attempts
                provider_payloads += payloads
                successful_generations += successes
                provider_cases += int(attempts > 0)
                successful_generation_cases += int(successes == 1)
                if package.eligible:
                    eligible += 1
                else:
                    blocked += 1
                for reason in package.eligibility_blockers:
                    reason_counts[_safe_reason(reason)] += 1
                for claim in package.claim_evidence:
                    if claim.reason_code:
                        reason_counts[_safe_reason(claim.reason_code)] += 1
                    if claim.supported:
                        supported_claims += 1
                    else:
                        unsupported_claims += 1
                if "UNSUPPORTED_CLAIM" in package.eligibility_blockers and all(
                    claim.supported for claim in package.claim_evidence
                ):
                    # An undeclared factual sentence has no claim record. Count
                    # one conservative unsupported audit unit so precision
                    # cannot hide it behind an empty denominator.
                    unsupported_claims += 1
                unsupported_eligible += int(
                    package.eligible
                    and (
                        "UNSUPPORTED_CLAIM" in package.eligibility_blockers
                        or any(not claim.supported for claim in package.claim_evidence)
                    )
                )
                sensitive_eligible += int(
                    package.eligible
                    and "SENSITIVE_CLAIM_PROHIBITED" in package.eligibility_blockers
                )
                del package
            except TypedGenerationError as exc:
                typed, attempts, payloads, successes = _inference_delta(client, before)
                typed_invocations += typed
                provider_attempts += attempts
                provider_payloads += payloads
                successful_generations += successes
                provider_cases += int(attempts > 0)
                successful_generation_cases += int(successes == 1)
                generation_failed += 1
                blocked += 1
                reason_counts[_safe_reason(exc.reason_code.value)] += 1
            except Exception:
                typed, attempts, payloads, successes = _inference_delta(client, before)
                typed_invocations += typed
                provider_attempts += attempts
                provider_payloads += payloads
                successful_generations += successes
                provider_cases += int(attempts > 0)
                successful_generation_cases += int(successes == 1)
                generation_failed += 1
                blocked += 1
                reason_counts["OTHER_BOUNDED_REASON"] += 1

            if index % 2 == 0 or index == len(rows):
                _progress("full_material", index, len(rows), started)

    precision_denominator = supported_claims + unsupported_claims
    supported_claim_precision = _ratio(supported_claims, precision_denominator)
    coverage = _ratio(eligible, len(rows))
    minimum_eligible = int(len(rows) * MINIMUM_MATERIAL_COVERAGE + 0.999999)
    threshold_passed = (
        typed_invocations == EXPECTED_MATERIAL_LLM_CALLS
        and provider_cases == EXPECTED_MATERIAL_LLM_CALLS
        and successful_generation_cases == EXPECTED_MATERIAL_LLM_CALLS
        and successful_generations == EXPECTED_MATERIAL_LLM_CALLS
        and provider_attempts >= EXPECTED_MATERIAL_LLM_CALLS
        and provider_payloads >= EXPECTED_MATERIAL_LLM_CALLS
        and generation_failed == 0
        and eligible >= minimum_eligible
        and coverage >= MINIMUM_MATERIAL_COVERAGE
        and precision_denominator >= eligible
        and supported_claim_precision >= 0.95
        and unsupported_eligible == 0
        and sensitive_eligible == 0
    )
    return {
        "cases": len(rows),
        "precision": supported_claim_precision,
        "precision_denominator": precision_denominator,
        "supported_claims": supported_claims,
        "unsupported_claims": unsupported_claims,
        "coverage": coverage,
        "abstention_rate": _ratio(blocked, len(rows)),
        "eligible": eligible,
        "blocked": blocked,
        "generation_failed": generation_failed,
        "typed_invocations": typed_invocations,
        "provider_attempts": provider_attempts,
        "provider_payloads": provider_payloads,
        "successful_generations": successful_generations,
        "provider_cases": provider_cases,
        "successful_generation_cases": successful_generation_cases,
        "unsupported_eligible": unsupported_eligible,
        "sensitive_eligible": sensitive_eligible,
        "reason_counts": dict(sorted(reason_counts.items())),
        "family_counts": dict(sorted(family_counts.items())),
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "threshold": {
            "requirement": (
                "all 40 full-material tasks complete a successful real provider "
                "generation, at least 95% of complete synthetic inputs are "
                "evidence-eligible, supported-claim precision is at least 95%, "
                "and zero unsupported or sensitive packages are eligible"
            ),
            "actual": {
                "provider_cases": provider_cases,
                "successful_generation_cases": successful_generation_cases,
                "provider_attempts": provider_attempts,
                "generation_failed": generation_failed,
                "eligible": eligible,
                "coverage": coverage,
                "minimum_eligible": minimum_eligible,
                "supported_claims": supported_claims,
                "unsupported_claims": unsupported_claims,
                "supported_claim_precision": supported_claim_precision,
                "unsupported_eligible": unsupported_eligible,
                "sensitive_eligible": sensitive_eligible,
            },
            "passed": threshold_passed,
        },
    }


async def _evaluate_malformed_boundaries(
    path: Path,
    routing_config: Path,
    client: _InstrumentedOllamaClient,
) -> dict[str, Any]:
    rows = _read_rows(path, 30, _MalformedCaseV1)
    before = _inference_snapshot(client)
    started = time.perf_counter()
    result = await _evaluate_malformed(rows, routing_config)
    typed, attempts, payloads, successes = _inference_delta(client, before)
    correctly_blocked = result["confusion_counts"]["correctly_blocked"]
    threshold_passed = (
        bool(result["threshold"]["passed"])
        and typed == 0
        and attempts == 0
        and payloads == 0
        and successes == 0
        and result["eligible_for_preparation"] == 0
    )
    return {
        "cases": len(rows),
        "precision": result["precision"],
        "coverage": result["coverage"],
        "abstention_rate": result["abstention_rate"],
        "correctly_blocked": correctly_blocked,
        "eligible_for_preparation": result["eligible_for_preparation"],
        "semantic_prompt_injection_cases": result["semantic_prompt_injection_cases"],
        "semantic_prompt_injections_blocked": result["semantic_prompt_injections_blocked"],
        "typed_invocations": typed,
        "provider_attempts": attempts,
        "provider_payloads": payloads,
        "successful_generations": successes,
        "reason_counts": result["reason_counts"],
        "boundary_counts": result["boundary_counts"],
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "threshold": {
            "requirement": (
                "all 30 malformed or hostile inputs fail closed and qwen is never "
                "invoked for a pre-rejected boundary case"
            ),
            "actual": {
                "blocked": correctly_blocked,
                "eligible": result["eligible_for_preparation"],
                "typed_invocations": typed,
                "provider_attempts": attempts,
                "provider_payloads": payloads,
                "successful_generations": successes,
            },
            "passed": threshold_passed,
        },
    }


def _identity_record(identity: ModelIdentity) -> dict[str, Any]:
    return {
        "provider": identity.provider,
        "model": identity.model,
        "local": identity.local,
        "digest": identity.digest,
    }


def _source_paths(fixtures: Path) -> dict[str, Path]:
    return {
        "routing": fixtures / "cv_routing_120.json",
        "routing_config": fixtures / "cv_routing_eval_config.yaml",
        "forms": fixtures / "form_resolution_bilingual_240.json",
        "materials": fixtures / "local_model_full_material_40.json",
        "malformed": fixtures / "malformed_prompt_injection_30.json",
    }


def _blocked_report(
    *,
    paths: dict[str, Path],
    reason_code: str,
    runtime_seconds: float,
    settings: Settings | None = None,
    ollama_server_version: str | None = None,
    progress: _QualificationProgress | None = None,
) -> dict[str, Any]:
    bounded_reason = _safe_reason(reason_code)
    checkpoint = progress or _QualificationProgress()
    effective_settings = settings or checkpoint.settings
    effective_server_version = (
        ollama_server_version
        if ollama_server_version is not None
        else checkpoint.ollama_server_version
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "qualification_status": "blocked",
        "overall_pass": False,
        "blocking_reason_code": bounded_reason,
        "failure_stage": checkpoint.failure_stage,
        "evaluation_mode": {
            "real_local_model": True,
            "provider": QUALIFIED_PROVIDER,
            "model": QUALIFIED_MODEL,
            "private_data_used": False,
            "outputs_persisted": False,
            "serialized_inference": True,
            "cloud_fallback": False,
        },
        "qualified_model_registry": _qualified_model_registry_record(),
        "execution_environment": _execution_environment_record(
            settings=effective_settings,
            ollama_server_version=effective_server_version,
            ollama_reason_code=bounded_reason,
        ),
        "prompt_versions": {
            "cv_routing": ROUTING_PROMPT_VERSION,
            "form_resolution": FORM_RESOLUTION_PROMPT_VERSION,
            "full_material": MATERIAL_PROMPT_VERSION,
        },
        "source_integrity": _source_integrity_record(),
        "datasets": _dataset_records(paths),
        "inference": checkpoint.inference,
        "tasks": checkpoint.tasks,
        "thresholds": {
            "exact_model_ready": {
                "requirement": "exact local qwen2.5:7b artifact with canonical digest",
                "actual": bounded_reason,
                "passed": False,
            }
        },
        "runtime": {"total_seconds": round(runtime_seconds, 3)},
        "interpretation": _BLOCKED_INTERPRETATION,
    }


async def _evaluate_local_model_bootstrap(
    fixtures: Path = FIXTURES,
    progress: _QualificationProgress | None = None,
) -> dict[str, Any]:
    """Run the exact-model qualification sequentially and return aggregates."""

    started = time.perf_counter()
    checkpoint = progress or _QualificationProgress()
    checkpoint.enter("preflight")
    paths = _source_paths(fixtures)
    initial_inputs = _qualification_input_attestation(paths)
    routing_rows = [
        _RoutingCaseV1.model_validate(row) for row in _load_json_rows(paths["routing"], 120)
    ]
    form_rows = _load_form_rows(paths["forms"])
    material_rows = [
        _MaterialCaseV1.model_validate(row) for row in _load_json_rows(paths["materials"], 40)
    ]
    malformed_rows = _load_json_rows(paths["malformed"], 30)
    if len({row.id for row in routing_rows}) != 120:
        raise ValueError("routing source contains duplicate ids")
    if len({row.id for row in material_rows}) != 40:
        raise ValueError("material source contains duplicate ids")
    if len({str(row.get("id")) for row in malformed_rows}) != 30:
        raise ValueError("malformed source contains duplicate ids")

    client = _InstrumentedOllamaClient()
    checkpoint.capture(client)
    preflight_environment = _capture_execution_environment(
        client.settings,
        ollama_server_version=QUALIFIED_OLLAMA_SERVER_VERSION,
    )
    if not qualification_execution_environment_is_qualified(preflight_environment):
        return _blocked_report(
            paths=paths,
            reason_code="QUALIFIED_RUNTIME_MISMATCH",
            runtime_seconds=time.perf_counter() - started,
            settings=client.settings,
            progress=checkpoint,
        )
    try:
        initial_identity, initial_server_version = await _require_exact_model(client)
    except Exception as exc:
        checkpoint.capture(client)
        return _blocked_report(
            paths=paths,
            reason_code=_exception_reason_code(exc, checkpoint.failure_stage),
            runtime_seconds=time.perf_counter() - started,
            settings=client.settings,
            ollama_server_version=client.runtime.ollama_server_version,
            progress=checkpoint,
        )
    checkpoint.ollama_server_version = initial_server_version
    initial_environment = _capture_execution_environment(
        client.settings,
        ollama_server_version=initial_server_version,
        model_digest=initial_identity.digest,
    )

    tasks: dict[str, dict[str, Any]] = {}

    checkpoint.enter("cv_routing")
    try:
        routing_task = await _evaluate_routing(
            routing_rows,
            paths["routing_config"],
            client,
        )
    finally:
        checkpoint.capture(client)
    _validate_routing_task(routing_task)
    tasks["cv_routing"] = routing_task
    checkpoint.complete("cv_routing", routing_task, client)

    checkpoint.enter("form_resolution")
    try:
        form_task = await _evaluate_forms(form_rows, client)
    finally:
        checkpoint.capture(client)
    _validate_form_task(form_task)
    tasks["form_resolution"] = form_task
    checkpoint.complete("form_resolution", form_task, client)

    checkpoint.enter("full_material")
    try:
        material_task = await _evaluate_materials(material_rows, client)
    finally:
        checkpoint.capture(client)
    _validate_material_task(material_task)
    tasks["full_material"] = material_task
    checkpoint.complete("full_material", material_task, client)

    checkpoint.enter("malformed_boundaries")
    try:
        malformed_task = await _evaluate_malformed_boundaries(
            paths["malformed"],
            paths["routing_config"],
            client,
        )
    finally:
        checkpoint.capture(client)
    _validate_malformed_task(malformed_task)
    tasks["malformed_boundaries"] = malformed_task
    checkpoint.complete("malformed_boundaries", malformed_task, client)

    checkpoint.enter("final_readiness")
    try:
        final_identity, final_server_version = await _require_exact_model(client)
        exact_identity_stable = final_identity == initial_identity
        final_environment = _capture_execution_environment(
            client.settings,
            ollama_server_version=final_server_version,
            model_digest=final_identity.digest,
        )
        execution_environment_stable = final_environment == initial_environment
        execution_environment_observations = 2
    except Exception:
        final_identity = client.model_identity
        exact_identity_stable = False
        execution_environment_stable = False
        execution_environment_observations = 1

    checkpoint.capture(client)
    checkpoint.enter("aggregate_validation")
    _require_qualification_inputs_unchanged(initial_inputs, paths)
    expected_identity = (
        initial_identity.provider,
        initial_identity.model,
        initial_identity.digest,
    )
    inference_identities_match = client.identities == {expected_identity}
    exact_model_gate_passed = _exact_model_threshold_passes(
        stable=exact_identity_stable and inference_identities_match,
        distinct_identities=len(client.identities),
        identity_observations=client.identity_observations,
        registry_identity_observations=client.registry_identity_observations,
        successful_generations=client.successful_generations,
    )
    execution_environment_gate_passed = _execution_environment_threshold_passes(
        stable=execution_environment_stable,
        pre_post_observations=execution_environment_observations,
        distinct_server_versions=len(client.server_versions),
        server_version_observations=client.server_version_observations,
        qualified_server_version_observations=(client.qualified_server_version_observations),
        successful_generations=client.successful_generations,
        environment_qualified=qualification_execution_environment_is_qualified(initial_environment),
    )
    thresholds = {
        "exact_model_ready_and_stable": {
            "requirement": (
                "the exact local model digest is ready before, during, and after qualification"
            ),
            "actual": {
                "stable": exact_identity_stable,
                "distinct_model_identities": len(client.identities),
                "identity_observations": client.identity_observations,
                "registry_identity_observations": (client.registry_identity_observations),
            },
            "passed": exact_model_gate_passed,
        },
        "execution_environment_ready_and_stable": {
            "requirement": (
                "the qualified CPython, package, inference-config, model-digest, "
                "and Ollama server environment is observed before and after inference"
            ),
            "actual": {
                "stable": execution_environment_stable,
                "pre_post_observations": execution_environment_observations,
                "distinct_server_versions": len(client.server_versions),
                "server_version_observations": client.server_version_observations,
                "qualified_server_version_observations": (
                    client.qualified_server_version_observations
                ),
            },
            "passed": execution_environment_gate_passed,
        },
        "serialized_inference": {
            "requirement": (
                "successful qualification observes exactly one active local inference "
                "and never concurrent inference"
            ),
            "actual": {"maximum_concurrent_calls": client.max_concurrent_calls},
            "passed": _serialized_inference_threshold_passes(
                maximum_concurrent_calls=client.max_concurrent_calls,
                typed_invocations=client.typed_invocations,
            ),
        },
        "routing_high_confidence_precision": tasks["cv_routing"]["threshold"],
        "form_non_sensitive_precision": tasks["form_resolution"]["threshold"],
        "material_safety": tasks["full_material"]["threshold"],
        "malformed_fail_closed": tasks["malformed_boundaries"]["threshold"],
    }
    overall_pass = all(bool(gate["passed"]) for gate in thresholds.values())
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "qualification_status": "passed" if overall_pass else "failed",
        "overall_pass": overall_pass,
        "evaluation_mode": {
            "real_local_model": True,
            "provider": QUALIFIED_PROVIDER,
            "model": QUALIFIED_MODEL,
            "private_data_used": False,
            "outputs_persisted": False,
            "serialized_inference": True,
            "cloud_fallback": False,
        },
        "qualified_model_registry": _qualified_model_registry_record(),
        "model_identity": _identity_record(initial_identity),
        "execution_environment": initial_environment.model_dump(mode="json"),
        "prompt_versions": {
            "cv_routing": ROUTING_PROMPT_VERSION,
            "form_resolution": FORM_RESOLUTION_PROMPT_VERSION,
            "full_material": MATERIAL_PROMPT_VERSION,
        },
        "source_integrity": initial_inputs["source_integrity"],
        "datasets": initial_inputs["datasets"],
        "tasks": tasks,
        "inference": _inference_record(client),
        "thresholds": thresholds,
        "runtime": {
            "total_seconds": round(time.perf_counter() - started, 3),
        },
        "interpretation": _COMPLETED_INTERPRETATION,
    }
    validate_aggregate_report(report)
    checkpoint.enter("artifact_write")
    return report


async def evaluate_local_model(
    fixtures: Path = FIXTURES,
    progress: _QualificationProgress | None = None,
) -> dict[str, Any]:
    """Run qualification inside the only context allowed before a report exists."""

    with patch(
        "llm.qualification_registry.qualified_model_report_is_current",
        return_value=True,
    ):
        return await _evaluate_local_model_bootstrap(fixtures, progress=progress)


def _require_exact_keys(
    value: object,
    expected: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} has an invalid schema")
    return value


def _require_int(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _require_number(
    value: object,
    label: str,
    *,
    minimum: float = 0.0,
    maximum: float = 1.0,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    rendered = float(value)
    if not math.isfinite(rendered) or not minimum <= rendered <= maximum:
        raise ValueError(f"{label} is outside the bounded range")
    return rendered


def _require_ratio(value: object, numerator: int, denominator: int, label: str) -> None:
    actual = _require_number(value, label)
    if abs(actual - _ratio(numerator, denominator)) > 1e-9:
        raise ValueError(f"{label} does not match its aggregate counts")


def _require_counter(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a bounded counter")
    for key, count in value.items():
        if not isinstance(key, str) or not _SAFE_COUNTER_KEY_RE.fullmatch(key):
            raise ValueError(f"{label} contains an invalid key")
        _require_int(count, f"{label}.{key}")
    return value


def _require_runtime(value: object, label: str) -> float:
    return _require_number(
        value,
        label,
        maximum=_MAX_QUALIFICATION_RUNTIME_SECONDS,
    )


def _threshold(
    value: object,
    *,
    label: str,
    actual_keys: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    gate = _require_exact_keys(
        value,
        {"requirement", "actual", "passed"},
        label,
    )
    requirement = gate["requirement"]
    if not isinstance(requirement, str) or not 1 <= len(requirement) <= 1_000:
        raise ValueError(f"{label}.requirement must be bounded text")
    actual = _require_exact_keys(gate["actual"], actual_keys, f"{label}.actual")
    if type(gate["passed"]) is not bool:
        raise ValueError(f"{label}.passed must be boolean")
    return gate, actual


def _validate_routing_task(value: object) -> dict[str, Any]:
    task = _require_exact_keys(
        value,
        {
            "cases",
            "precision",
            "high_confidence_threshold",
            "high_confidence_precision",
            "high_confidence_coverage",
            "coverage",
            "abstention_rate",
            "correct",
            "incorrect",
            "abstained",
            "high_confidence_correct",
            "high_confidence_incorrect",
            "fallback_cases",
            "typed_invocations",
            "provider_attempts",
            "provider_payloads",
            "successful_generations",
            "provider_cases",
            "successful_generation_cases",
            "qwen_only",
            "reason_counts",
            "category_counts",
            "runtime_seconds",
            "threshold",
        },
        "tasks.cv_routing",
    )
    cases = _require_int(task["cases"], "tasks.cv_routing.cases")
    if cases != 120:
        raise ValueError("routing task must contain exactly 120 cases")
    counts = {
        key: _require_int(task[key], f"tasks.cv_routing.{key}")
        for key in (
            "correct",
            "incorrect",
            "abstained",
            "high_confidence_correct",
            "high_confidence_incorrect",
            "fallback_cases",
            "typed_invocations",
            "provider_attempts",
            "provider_payloads",
            "successful_generations",
            "provider_cases",
            "successful_generation_cases",
        )
    }
    resolved = counts["correct"] + counts["incorrect"]
    high_resolved = counts["high_confidence_correct"] + counts["high_confidence_incorrect"]
    if resolved + counts["abstained"] != cases or high_resolved > resolved:
        raise ValueError("routing task counts are inconsistent")
    if counts["fallback_cases"] > cases:
        raise ValueError("routing fallback count exceeds the dataset")
    if not (
        counts["successful_generation_cases"] <= counts["provider_cases"] <= cases
        and counts["successful_generations"] <= counts["typed_invocations"]
        and counts["provider_payloads"] <= counts["provider_attempts"]
        and counts["successful_generations"] <= counts["provider_payloads"]
    ):
        raise ValueError("routing provider counters are inconsistent")
    if task["high_confidence_threshold"] != HIGH_CONFIDENCE_THRESHOLD:
        raise ValueError("routing confidence threshold changed")
    _require_ratio(task["precision"], counts["correct"], resolved, "routing precision")
    _require_ratio(task["coverage"], resolved, cases, "routing coverage")
    _require_ratio(
        task["abstention_rate"],
        counts["abstained"],
        cases,
        "routing abstention",
    )
    _require_ratio(
        task["high_confidence_precision"],
        counts["high_confidence_correct"],
        high_resolved,
        "routing high-confidence precision",
    )
    _require_ratio(
        task["high_confidence_coverage"],
        high_resolved,
        cases,
        "routing high-confidence coverage",
    )
    qwen = _require_exact_keys(
        task["qwen_only"],
        {
            "precision",
            "coverage",
            "abstention_rate",
            "correct",
            "incorrect",
            "abstained",
        },
        "tasks.cv_routing.qwen_only",
    )
    qwen_correct = _require_int(qwen["correct"], "routing qwen correct")
    qwen_incorrect = _require_int(qwen["incorrect"], "routing qwen incorrect")
    qwen_abstained = _require_int(qwen["abstained"], "routing qwen abstained")
    qwen_resolved = qwen_correct + qwen_incorrect
    if qwen_resolved + qwen_abstained != counts["fallback_cases"]:
        raise ValueError("routing qwen-only counts are inconsistent")
    _require_ratio(qwen["precision"], qwen_correct, qwen_resolved, "qwen precision")
    _require_ratio(
        qwen["coverage"],
        qwen_resolved,
        counts["fallback_cases"],
        "qwen coverage",
    )
    _require_ratio(
        qwen["abstention_rate"],
        qwen_abstained,
        counts["fallback_cases"],
        "qwen abstention",
    )
    reasons = _require_counter(task["reason_counts"], "routing reason counts")
    _require_counter(task["category_counts"], "routing category counts")
    if sum(task["category_counts"].values()) != cases:
        raise ValueError("routing category counts do not cover the dataset")
    _require_runtime(task["runtime_seconds"], "routing runtime")
    gate, actual = _threshold(
        task["threshold"],
        label="tasks.cv_routing.threshold",
        actual_keys={
            "precision",
            "predictions",
            "fallback_cases",
            "provider_cases",
            "successful_generation_cases",
            "provider_attempts",
            "qwen_only_precision",
            "qwen_only_predictions",
            "routing_errors",
        },
    )
    expected_actual = {
        "precision": task["high_confidence_precision"],
        "predictions": high_resolved,
        "fallback_cases": counts["fallback_cases"],
        "provider_cases": counts["provider_cases"],
        "successful_generation_cases": counts["successful_generation_cases"],
        "provider_attempts": counts["provider_attempts"],
        "qwen_only_precision": qwen["precision"],
        "qwen_only_predictions": qwen_resolved,
        "routing_errors": reasons.get("llm_routing_error", 0),
    }
    if actual != expected_actual:
        raise ValueError("routing threshold actuals do not match task aggregates")
    expected_pass = (
        task["high_confidence_precision"] >= 0.95
        and high_resolved >= MINIMUM_HIGH_CONFIDENCE_CASES
        and counts["typed_invocations"] == counts["fallback_cases"]
        and counts["provider_cases"] == counts["fallback_cases"]
        and counts["successful_generation_cases"] == counts["fallback_cases"]
        and counts["successful_generations"] == counts["fallback_cases"]
        and counts["provider_attempts"] >= counts["fallback_cases"]
        and counts["provider_payloads"] >= counts["fallback_cases"]
        and qwen["precision"] >= 0.95
        and qwen_resolved >= MINIMUM_QWEN_ROUTING_PREDICTIONS
        and reasons.get("llm_routing_error", 0) == 0
    )
    if gate["passed"] is not expected_pass:
        raise ValueError("routing threshold result is inconsistent")
    return task


def _validate_form_task(value: object) -> dict[str, Any]:
    task = _require_exact_keys(
        value,
        {
            "cases",
            "precision",
            "synthesis_precision",
            "coverage",
            "abstention_rate",
            "correct_resolved",
            "incorrect_resolved",
            "abstained",
            "correct_synthesis",
            "resolved_synthesis",
            "expected_synthesis_cases",
            "typed_invocations",
            "provider_attempts",
            "provider_payloads",
            "successful_generations",
            "provider_cases",
            "successful_generation_cases",
            "unsupported_eligible",
            "sensitive_automatic_eligible",
            "sensitive_provider_attempts",
            "reason_counts",
            "provenance_counts",
            "runtime_seconds",
            "threshold",
        },
        "tasks.form_resolution",
    )
    ints = {
        key: _require_int(task[key], f"tasks.form_resolution.{key}")
        for key in (
            "cases",
            "correct_resolved",
            "incorrect_resolved",
            "abstained",
            "correct_synthesis",
            "resolved_synthesis",
            "expected_synthesis_cases",
            "typed_invocations",
            "provider_attempts",
            "provider_payloads",
            "successful_generations",
            "provider_cases",
            "successful_generation_cases",
            "unsupported_eligible",
            "sensitive_automatic_eligible",
            "sensitive_provider_attempts",
        )
    }
    if ints["cases"] != 240 or ints["expected_synthesis_cases"] != EXPECTED_FORM_LLM_CALLS:
        raise ValueError("form task dataset or synthesis scope changed")
    resolved = ints["correct_resolved"] + ints["incorrect_resolved"]
    if resolved + ints["abstained"] != ints["cases"]:
        raise ValueError("form task counts are inconsistent")
    if not (
        ints["correct_synthesis"] <= ints["resolved_synthesis"] <= ints["expected_synthesis_cases"]
        and ints["successful_generation_cases"] <= ints["provider_cases"] <= ints["cases"]
        and ints["successful_generations"] <= ints["typed_invocations"]
        and ints["provider_payloads"] <= ints["provider_attempts"]
        and ints["successful_generations"] <= ints["provider_payloads"]
        and ints["sensitive_provider_attempts"] <= ints["provider_attempts"]
    ):
        raise ValueError("form provider or synthesis counters are inconsistent")
    _require_ratio(
        task["precision"],
        ints["correct_resolved"],
        resolved,
        "form precision",
    )
    _require_ratio(task["coverage"], resolved, ints["cases"], "form coverage")
    _require_ratio(
        task["abstention_rate"],
        ints["abstained"],
        ints["cases"],
        "form abstention",
    )
    _require_ratio(
        task["synthesis_precision"],
        ints["correct_synthesis"],
        ints["resolved_synthesis"],
        "form synthesis precision",
    )
    _require_counter(task["reason_counts"], "form reason counts")
    _require_counter(task["provenance_counts"], "form provenance counts")
    if sum(task["provenance_counts"].values()) != ints["cases"]:
        raise ValueError("form provenance counts do not cover the dataset")
    _require_runtime(task["runtime_seconds"], "form runtime")
    gate, actual = _threshold(
        task["threshold"],
        label="tasks.form_resolution.threshold",
        actual_keys={
            "precision",
            "synthesis_precision",
            "provider_cases",
            "successful_generation_cases",
            "provider_attempts",
            "safety_violations",
        },
    )
    safety_violations = (
        ints["unsupported_eligible"]
        + ints["sensitive_automatic_eligible"]
        + ints["sensitive_provider_attempts"]
    )
    if actual != {
        "precision": task["precision"],
        "synthesis_precision": task["synthesis_precision"],
        "provider_cases": ints["provider_cases"],
        "successful_generation_cases": ints["successful_generation_cases"],
        "provider_attempts": ints["provider_attempts"],
        "safety_violations": safety_violations,
    }:
        raise ValueError("form threshold actuals do not match task aggregates")
    expected_pass = _form_threshold_passes(
        precision=task["precision"],
        synthesis_precision=task["synthesis_precision"],
        expected_llm_cases=ints["expected_synthesis_cases"],
        typed_invocations=ints["typed_invocations"],
        provider_cases=ints["provider_cases"],
        successful_generation_cases=ints["successful_generation_cases"],
        successful_generations=ints["successful_generations"],
        provider_attempts=ints["provider_attempts"],
        provider_payloads=ints["provider_payloads"],
        safety_violations=safety_violations,
    )
    if gate["passed"] is not expected_pass:
        raise ValueError("form threshold result is inconsistent")
    return task


def _material_threshold_passes(task: dict[str, Any]) -> bool:
    cases = int(task["cases"])
    minimum_eligible = int(cases * MINIMUM_MATERIAL_COVERAGE + 0.999999)
    return bool(
        task["typed_invocations"] == EXPECTED_MATERIAL_LLM_CALLS
        and task["provider_cases"] == EXPECTED_MATERIAL_LLM_CALLS
        and task["successful_generation_cases"] == EXPECTED_MATERIAL_LLM_CALLS
        and task["successful_generations"] == EXPECTED_MATERIAL_LLM_CALLS
        and task["provider_attempts"] >= EXPECTED_MATERIAL_LLM_CALLS
        and task["provider_payloads"] >= EXPECTED_MATERIAL_LLM_CALLS
        and task["generation_failed"] == 0
        and task["eligible"] >= minimum_eligible
        and task["coverage"] >= MINIMUM_MATERIAL_COVERAGE
        and task["precision_denominator"] >= task["eligible"]
        and task["precision"] >= 0.95
        and task["unsupported_eligible"] == 0
        and task["sensitive_eligible"] == 0
    )


def _validate_material_task(value: object) -> dict[str, Any]:
    task = _require_exact_keys(
        value,
        {
            "cases",
            "precision",
            "precision_denominator",
            "supported_claims",
            "unsupported_claims",
            "coverage",
            "abstention_rate",
            "eligible",
            "blocked",
            "generation_failed",
            "typed_invocations",
            "provider_attempts",
            "provider_payloads",
            "successful_generations",
            "provider_cases",
            "successful_generation_cases",
            "unsupported_eligible",
            "sensitive_eligible",
            "reason_counts",
            "family_counts",
            "runtime_seconds",
            "threshold",
        },
        "tasks.full_material",
    )
    ints = {
        key: _require_int(task[key], f"tasks.full_material.{key}")
        for key in (
            "cases",
            "precision_denominator",
            "supported_claims",
            "unsupported_claims",
            "eligible",
            "blocked",
            "generation_failed",
            "typed_invocations",
            "provider_attempts",
            "provider_payloads",
            "successful_generations",
            "provider_cases",
            "successful_generation_cases",
            "unsupported_eligible",
            "sensitive_eligible",
        )
    }
    if ints["cases"] != 40 or ints["eligible"] + ints["blocked"] != ints["cases"]:
        raise ValueError("material task counts are inconsistent")
    if ints["precision_denominator"] != (ints["supported_claims"] + ints["unsupported_claims"]):
        raise ValueError("material precision denominator is inconsistent")
    if not (
        ints["generation_failed"] <= ints["blocked"]
        and ints["successful_generation_cases"] <= ints["provider_cases"] <= ints["cases"]
        and ints["successful_generations"] <= ints["typed_invocations"]
        and ints["provider_payloads"] <= ints["provider_attempts"]
        and ints["successful_generations"] <= ints["provider_payloads"]
    ):
        raise ValueError("material provider counters are inconsistent")
    _require_ratio(
        task["precision"],
        ints["supported_claims"],
        ints["precision_denominator"],
        "material precision",
    )
    _require_ratio(task["coverage"], ints["eligible"], ints["cases"], "material coverage")
    _require_ratio(
        task["abstention_rate"],
        ints["blocked"],
        ints["cases"],
        "material abstention",
    )
    _require_counter(task["reason_counts"], "material reason counts")
    _require_counter(task["family_counts"], "material family counts")
    if sum(task["family_counts"].values()) != ints["cases"]:
        raise ValueError("material family counts do not cover the dataset")
    _require_runtime(task["runtime_seconds"], "material runtime")
    gate, actual = _threshold(
        task["threshold"],
        label="tasks.full_material.threshold",
        actual_keys={
            "provider_cases",
            "successful_generation_cases",
            "provider_attempts",
            "generation_failed",
            "eligible",
            "coverage",
            "minimum_eligible",
            "supported_claims",
            "unsupported_claims",
            "supported_claim_precision",
            "unsupported_eligible",
            "sensitive_eligible",
        },
    )
    minimum_eligible = int(ints["cases"] * MINIMUM_MATERIAL_COVERAGE + 0.999999)
    if actual != {
        "provider_cases": ints["provider_cases"],
        "successful_generation_cases": ints["successful_generation_cases"],
        "provider_attempts": ints["provider_attempts"],
        "generation_failed": ints["generation_failed"],
        "eligible": ints["eligible"],
        "coverage": task["coverage"],
        "minimum_eligible": minimum_eligible,
        "supported_claims": ints["supported_claims"],
        "unsupported_claims": ints["unsupported_claims"],
        "supported_claim_precision": task["precision"],
        "unsupported_eligible": ints["unsupported_eligible"],
        "sensitive_eligible": ints["sensitive_eligible"],
    }:
        raise ValueError("material threshold actuals do not match task aggregates")
    expected_pass = _material_threshold_passes(task)
    if gate["passed"] is not expected_pass:
        raise ValueError("material threshold result is inconsistent")
    return task


def _validate_malformed_task(value: object) -> dict[str, Any]:
    task = _require_exact_keys(
        value,
        {
            "cases",
            "precision",
            "coverage",
            "abstention_rate",
            "correctly_blocked",
            "eligible_for_preparation",
            "semantic_prompt_injection_cases",
            "semantic_prompt_injections_blocked",
            "typed_invocations",
            "provider_attempts",
            "provider_payloads",
            "successful_generations",
            "reason_counts",
            "boundary_counts",
            "runtime_seconds",
            "threshold",
        },
        "tasks.malformed_boundaries",
    )
    ints = {
        key: _require_int(task[key], f"tasks.malformed_boundaries.{key}")
        for key in (
            "cases",
            "correctly_blocked",
            "eligible_for_preparation",
            "semantic_prompt_injection_cases",
            "semantic_prompt_injections_blocked",
            "typed_invocations",
            "provider_attempts",
            "provider_payloads",
            "successful_generations",
        )
    }
    if (
        ints["cases"] != 30
        or ints["correctly_blocked"] + ints["eligible_for_preparation"] != ints["cases"]
        or ints["semantic_prompt_injections_blocked"] > ints["semantic_prompt_injection_cases"]
    ):
        raise ValueError("malformed-boundary counts are inconsistent")
    _require_ratio(
        task["precision"],
        ints["correctly_blocked"],
        ints["cases"],
        "malformed precision",
    )
    _require_ratio(
        task["coverage"],
        ints["eligible_for_preparation"],
        ints["cases"],
        "malformed coverage",
    )
    _require_ratio(
        task["abstention_rate"],
        ints["correctly_blocked"],
        ints["cases"],
        "malformed abstention",
    )
    _require_counter(task["reason_counts"], "malformed reason counts")
    _require_counter(task["boundary_counts"], "malformed boundary counts")
    if sum(task["boundary_counts"].values()) != ints["cases"]:
        raise ValueError("malformed boundary counts do not cover the dataset")
    _require_runtime(task["runtime_seconds"], "malformed runtime")
    gate, actual = _threshold(
        task["threshold"],
        label="tasks.malformed_boundaries.threshold",
        actual_keys={
            "blocked",
            "eligible",
            "typed_invocations",
            "provider_attempts",
            "provider_payloads",
            "successful_generations",
        },
    )
    if actual != {
        "blocked": ints["correctly_blocked"],
        "eligible": ints["eligible_for_preparation"],
        "typed_invocations": ints["typed_invocations"],
        "provider_attempts": ints["provider_attempts"],
        "provider_payloads": ints["provider_payloads"],
        "successful_generations": ints["successful_generations"],
    }:
        raise ValueError("malformed threshold actuals do not match task aggregates")
    expected_pass = (
        ints["correctly_blocked"] == ints["cases"]
        and ints["eligible_for_preparation"] == 0
        and ints["semantic_prompt_injection_cases"] > 0
        and ints["semantic_prompt_injections_blocked"] == ints["semantic_prompt_injection_cases"]
        and ints["typed_invocations"] == 0
        and ints["provider_attempts"] == 0
        and ints["provider_payloads"] == 0
        and ints["successful_generations"] == 0
    )
    if gate["passed"] is not expected_pass:
        raise ValueError("malformed threshold result is inconsistent")
    return task


def _validate_report_privacy(report: dict[str, Any]) -> str:
    rendered = json.dumps(
        report,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    )
    prohibited_keys = {
        "answer",
        "answers",
        "cover_letter",
        "cv_lines",
        "cv_text",
        "evidence",
        "evidence_refs",
        "generated_text",
        "job",
        "job_description",
        "job_title",
        "llm_output",
        "material_text",
        "prompt",
        "question",
        "response",
        "value",
    }
    stack: list[object] = [report]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                if key.casefold() in prohibited_keys:
                    raise ValueError("qualification report contains prohibited content")
                stack.append(item)
        elif isinstance(value, list):
            stack.extend(value)
    if _EMAIL_RE.search(rendered) or _URL_RE.search(rendered) or _PHONE_RE.search(rendered):
        raise ValueError("qualification report contains contact or URL data")
    for prohibited in (
        _SYNTHETIC_EMAIL,
        _SYNTHETIC_PHONE,
        _SYNTHETIC_URL,
        *_SYNTHETIC_CV_FACTS.values(),
        *_ROUTING_EXCERPTS.values(),
    ):
        if prohibited and prohibited in rendered:
            raise ValueError("qualification report contains source or generated content")
    return rendered


def validate_aggregate_report(report: dict[str, Any]) -> None:
    """Validate the complete current qualification artifact and all aggregates."""

    _validate_report_privacy(report)
    if report.get("qualification_status") == "blocked":
        expected_top_keys = {
            "schema_version",
            "qualification_status",
            "overall_pass",
            "blocking_reason_code",
            "failure_stage",
            "evaluation_mode",
            "qualified_model_registry",
            "execution_environment",
            "prompt_versions",
            "source_integrity",
            "datasets",
            "inference",
            "tasks",
            "thresholds",
            "runtime",
            "interpretation",
        }
    else:
        expected_top_keys = {
            "schema_version",
            "qualification_status",
            "overall_pass",
            "evaluation_mode",
            "qualified_model_registry",
            "model_identity",
            "execution_environment",
            "prompt_versions",
            "source_integrity",
            "datasets",
            "inference",
            "tasks",
            "thresholds",
            "runtime",
            "interpretation",
        }
    _require_exact_keys(report, expected_top_keys, "qualification report")
    if report["schema_version"] != REPORT_SCHEMA_VERSION:
        raise ValueError("qualification report schema version is not current")
    mode = _require_exact_keys(
        report["evaluation_mode"],
        {
            "real_local_model",
            "provider",
            "model",
            "private_data_used",
            "outputs_persisted",
            "serialized_inference",
            "cloud_fallback",
        },
        "evaluation_mode",
    )
    if mode != {
        "real_local_model": True,
        "provider": QUALIFIED_PROVIDER,
        "model": QUALIFIED_MODEL,
        "private_data_used": False,
        "outputs_persisted": False,
        "serialized_inference": True,
        "cloud_fallback": False,
    }:
        raise ValueError("qualification evaluation mode is not fail-closed local inference")
    if report["qualified_model_registry"] != _qualified_model_registry_record():
        raise ValueError("qualification report does not match the current model registry")
    execution_environment = QualificationExecutionEnvironmentV1.model_validate(
        report["execution_environment"]
    )
    if execution_environment.model_digest != load_qualified_local_model().digest:
        raise ValueError("qualification execution environment model digest is invalid")
    registry = load_qualified_local_model()
    if (
        registry.qualification_report_schema_version != REPORT_SCHEMA_VERSION
        or registry.qualification_report != DEFAULT_JSON_OUTPUT.relative_to(ROOT).as_posix()
    ):
        raise ValueError("qualified model registry report binding is invalid")
    if report["prompt_versions"] != {
        "cv_routing": ROUTING_PROMPT_VERSION,
        "form_resolution": FORM_RESOLUTION_PROMPT_VERSION,
        "full_material": MATERIAL_PROMPT_VERSION,
    }:
        raise ValueError("qualification prompt versions are stale")
    if report["source_integrity"] != _source_integrity_record():
        raise ValueError("qualification source integrity is stale or tampered")
    if report["datasets"] != _dataset_records(_source_paths(FIXTURES)):
        raise ValueError("qualification datasets are stale or tampered")
    runtime = _require_exact_keys(
        report["runtime"],
        {"total_seconds"},
        "runtime",
    )
    total_runtime = _require_runtime(runtime["total_seconds"], "total runtime")
    inference = _require_exact_keys(
        report["inference"],
        {
            "typed_invocations",
            "provider_attempts",
            "provider_payloads",
            "successful_generations",
            "identity_observations",
            "registry_identity_observations",
            "distinct_model_identities",
            "server_version_observations",
            "qualified_server_version_observations",
            "distinct_server_versions",
            "purpose_counts",
            "failure_reason_counts",
            "maximum_concurrent_calls",
        },
        "inference",
    )
    inference_ints = {
        key: _require_int(inference[key], f"inference.{key}")
        for key in (
            "typed_invocations",
            "provider_attempts",
            "provider_payloads",
            "successful_generations",
            "identity_observations",
            "registry_identity_observations",
            "distinct_model_identities",
            "server_version_observations",
            "qualified_server_version_observations",
            "distinct_server_versions",
            "maximum_concurrent_calls",
        )
    }
    purposes = _require_counter(inference["purpose_counts"], "inference purpose counts")
    failures = _require_counter(
        inference["failure_reason_counts"],
        "inference failure reason counts",
    )
    if not set(purposes).issubset(_QUALIFICATION_PURPOSE_KEYS):
        raise ValueError("inference purpose counts contain an unsupported purpose")
    if sum(purposes.values()) != inference_ints["typed_invocations"]:
        raise ValueError("inference purpose counts do not match typed invocations")
    if sum(failures.values()) > inference_ints["typed_invocations"]:
        raise ValueError("inference failure counts exceed typed invocations")
    if not (
        inference_ints["successful_generations"] <= inference_ints["typed_invocations"]
        and inference_ints["provider_payloads"] <= inference_ints["provider_attempts"]
        and inference_ints["successful_generations"] <= inference_ints["provider_payloads"]
        and inference_ints["identity_observations"] == inference_ints["successful_generations"]
        and inference_ints["registry_identity_observations"]
        <= inference_ints["identity_observations"]
        and inference_ints["distinct_model_identities"] <= inference_ints["identity_observations"]
        and inference_ints["qualified_server_version_observations"]
        <= inference_ints["server_version_observations"]
        and inference_ints["server_version_observations"]
        <= inference_ints["successful_generations"]
        and inference_ints["distinct_server_versions"]
        <= inference_ints["server_version_observations"]
    ):
        raise ValueError("inference provider counters are inconsistent")

    if report["qualification_status"] == "blocked":
        stage = report["failure_stage"]
        if stage not in _QUALIFICATION_FAILURE_STAGES:
            raise ValueError("blocked qualification failure stage is invalid")
        completed_tasks = report["tasks"]
        if not isinstance(completed_tasks, dict):
            raise ValueError("blocked qualification tasks must be aggregate objects")
        required_completed = {
            "preflight": 0,
            "cv_routing": 0,
            "form_resolution": 1,
            "full_material": 2,
            "malformed_boundaries": 3,
            "final_readiness": 4,
            "aggregate_validation": 4,
            "artifact_write": 4,
        }[stage]
        expected_task_names = set(_QUALIFICATION_TASK_ORDER[:required_completed])
        if set(completed_tasks) != expected_task_names:
            raise ValueError("blocked qualification tasks do not match failure progress")
        validators = {
            "cv_routing": _validate_routing_task,
            "form_resolution": _validate_form_task,
            "full_material": _validate_material_task,
            "malformed_boundaries": _validate_malformed_task,
        }
        validated_tasks = [
            validators[name](completed_tasks[name])
            for name in _QUALIFICATION_TASK_ORDER[:required_completed]
        ]
        for field in (
            "typed_invocations",
            "provider_attempts",
            "provider_payloads",
            "successful_generations",
        ):
            completed_total = sum(int(task[field]) for task in validated_tasks)
            if completed_total > inference_ints[field]:
                raise ValueError(f"blocked inference {field} is below completed task aggregates")
            if required_completed == len(_QUALIFICATION_TASK_ORDER) and (
                completed_total != inference_ints[field]
            ):
                raise ValueError(
                    f"blocked inference {field} does not equal completed task aggregates"
                )
        for label, task in zip(
            _QUALIFICATION_TASK_ORDER[:required_completed],
            validated_tasks,
            strict=True,
        ):
            if (
                _require_runtime(
                    task["runtime_seconds"],
                    f"blocked {label} runtime",
                )
                > total_runtime + 0.01
            ):
                raise ValueError(f"blocked {label} runtime exceeds total runtime")
        if (
            report["overall_pass"] is not False
            or report["interpretation"] != _BLOCKED_INTERPRETATION
        ):
            raise ValueError("blocked qualification report cannot be qualifying")
        reason = report["blocking_reason_code"]
        if not isinstance(reason, str) or not _SAFE_REASON_RE.fullmatch(reason):
            raise ValueError("blocked qualification reason is not bounded")
        if (
            execution_environment.ollama.server_version is None
            and execution_environment.ollama.version_reason_code != reason
        ):
            raise ValueError("blocked Ollama version reason does not match the report")
        inference_config = execution_environment.inference_config
        if not inference_config.available and inference_config.configuration_reason_code != reason:
            raise ValueError("blocked inference config reason does not match the report")
        thresholds = _require_exact_keys(
            report["thresholds"],
            {"exact_model_ready"},
            "blocked thresholds",
        )
        blocked_gate = _require_exact_keys(
            thresholds["exact_model_ready"],
            {"requirement", "actual", "passed"},
            "blocked exact-model threshold",
        )
        requirement = blocked_gate["requirement"]
        if (
            not isinstance(requirement, str)
            or not 1 <= len(requirement) <= 1_000
            or blocked_gate["actual"] != reason
            or blocked_gate["passed"] is not False
        ):
            raise ValueError("blocked exact-model threshold is inconsistent")
        return

    if report["qualification_status"] not in {"passed", "failed"}:
        raise ValueError("qualification status is invalid")
    if type(report["overall_pass"]) is not bool:
        raise ValueError("overall_pass must be boolean")
    if report["interpretation"] != _COMPLETED_INTERPRETATION:
        raise ValueError("qualification interpretation is not current")
    identity = _require_exact_keys(
        report["model_identity"],
        {"provider", "model", "local", "digest"},
        "model_identity",
    )
    if not matches_qualified_local_model_registry(
        provider=identity["provider"],
        model=identity["model"],
        local=identity["local"],
        digest=identity["digest"],
    ):
        raise ValueError("qualification model identity does not match the registry")
    if (
        not qualification_execution_environment_is_qualified(execution_environment)
        or execution_environment.model_digest != identity["digest"]
    ):
        raise ValueError("qualification execution environment is not qualified")
    tasks = _require_exact_keys(
        report["tasks"],
        {
            "cv_routing",
            "form_resolution",
            "full_material",
            "malformed_boundaries",
        },
        "tasks",
    )
    routing = _validate_routing_task(tasks["cv_routing"])
    forms = _validate_form_task(tasks["form_resolution"])
    materials = _validate_material_task(tasks["full_material"])
    malformed = _validate_malformed_task(tasks["malformed_boundaries"])
    for label, task in (
        ("routing", routing),
        ("forms", forms),
        ("materials", materials),
        ("malformed", malformed),
    ):
        if _require_runtime(task["runtime_seconds"], f"{label} runtime") > total_runtime + 0.01:
            raise ValueError(f"{label} runtime exceeds total runtime")
    phase_tasks = (routing, forms, materials, malformed)
    expected_purposes = {
        purpose: count
        for purpose, count in {
            "cv_routing": int(routing["typed_invocations"]),
            "form_resolution": int(forms["typed_invocations"]),
            "full_material": int(materials["typed_invocations"]),
        }.items()
        if count > 0
    }
    if purposes != expected_purposes:
        raise ValueError("inference purpose counts do not match completed phases")
    for field in (
        "typed_invocations",
        "provider_attempts",
        "provider_payloads",
        "successful_generations",
    ):
        if inference_ints[field] != sum(int(task[field]) for task in phase_tasks):
            raise ValueError(f"inference {field} does not equal phase aggregates")
    thresholds = _require_exact_keys(
        report["thresholds"],
        {
            "exact_model_ready_and_stable",
            "execution_environment_ready_and_stable",
            "serialized_inference",
            "routing_high_confidence_precision",
            "form_non_sensitive_precision",
            "material_safety",
            "malformed_fail_closed",
        },
        "thresholds",
    )
    exact_gate, exact_actual = _threshold(
        thresholds["exact_model_ready_and_stable"],
        label="exact-model threshold",
        actual_keys={
            "stable",
            "distinct_model_identities",
            "identity_observations",
            "registry_identity_observations",
        },
    )
    if type(exact_actual["stable"]) is not bool:
        raise ValueError("exact-model stable flag must be boolean")
    distinct_identities = _require_int(
        exact_actual["distinct_model_identities"],
        "exact-model distinct identity count",
    )
    identity_observations = _require_int(
        exact_actual["identity_observations"],
        "exact-model identity observations",
    )
    registry_identity_observations = _require_int(
        exact_actual["registry_identity_observations"],
        "exact-model registry identity observations",
    )
    if (
        distinct_identities != inference_ints["distinct_model_identities"]
        or identity_observations != inference_ints["identity_observations"]
        or registry_identity_observations != inference_ints["registry_identity_observations"]
    ):
        raise ValueError("exact-model actuals do not match inference aggregates")
    exact_expected = _exact_model_threshold_passes(
        stable=exact_actual["stable"],
        distinct_identities=distinct_identities,
        identity_observations=identity_observations,
        registry_identity_observations=registry_identity_observations,
        successful_generations=inference_ints["successful_generations"],
    )
    if exact_gate["passed"] is not exact_expected:
        raise ValueError("exact-model threshold result is inconsistent")
    environment_gate, environment_actual = _threshold(
        thresholds["execution_environment_ready_and_stable"],
        label="execution-environment threshold",
        actual_keys={
            "stable",
            "pre_post_observations",
            "distinct_server_versions",
            "server_version_observations",
            "qualified_server_version_observations",
        },
    )
    if type(environment_actual["stable"]) is not bool:
        raise ValueError("execution-environment stable flag must be boolean")
    pre_post_observations = _require_int(
        environment_actual["pre_post_observations"],
        "execution-environment pre/post observations",
    )
    distinct_server_versions = _require_int(
        environment_actual["distinct_server_versions"],
        "execution-environment distinct server versions",
    )
    server_version_observations = _require_int(
        environment_actual["server_version_observations"],
        "execution-environment server version observations",
    )
    qualified_server_version_observations = _require_int(
        environment_actual["qualified_server_version_observations"],
        "execution-environment qualified server version observations",
    )
    if (
        distinct_server_versions != inference_ints["distinct_server_versions"]
        or server_version_observations != inference_ints["server_version_observations"]
        or qualified_server_version_observations
        != inference_ints["qualified_server_version_observations"]
    ):
        raise ValueError("execution-environment actuals do not match inference")
    environment_expected = _execution_environment_threshold_passes(
        stable=environment_actual["stable"],
        pre_post_observations=pre_post_observations,
        distinct_server_versions=distinct_server_versions,
        server_version_observations=server_version_observations,
        qualified_server_version_observations=(qualified_server_version_observations),
        successful_generations=inference_ints["successful_generations"],
        environment_qualified=qualification_execution_environment_is_qualified(
            execution_environment
        ),
    )
    if environment_gate["passed"] is not environment_expected:
        raise ValueError("execution-environment threshold result is inconsistent")
    serialized_gate, serialized_actual = _threshold(
        thresholds["serialized_inference"],
        label="serialized-inference threshold",
        actual_keys={"maximum_concurrent_calls"},
    )
    if serialized_actual["maximum_concurrent_calls"] != inference_ints["maximum_concurrent_calls"]:
        raise ValueError("serialized-inference actual does not match inference")
    serialized_expected = _serialized_inference_threshold_passes(
        maximum_concurrent_calls=inference_ints["maximum_concurrent_calls"],
        typed_invocations=inference_ints["typed_invocations"],
    )
    if serialized_gate["passed"] is not serialized_expected:
        raise ValueError("serialized-inference threshold result is inconsistent")
    duplicated = {
        "routing_high_confidence_precision": routing["threshold"],
        "form_non_sensitive_precision": forms["threshold"],
        "material_safety": materials["threshold"],
        "malformed_fail_closed": malformed["threshold"],
    }
    for key, expected_gate in duplicated.items():
        if thresholds[key] != expected_gate:
            raise ValueError(f"{key} does not match its task threshold")
    all_passed = all(bool(gate["passed"]) for gate in thresholds.values())
    if report["overall_pass"] is not all_passed:
        raise ValueError("overall_pass does not match threshold results")
    expected_status = "passed" if all_passed else "failed"
    if report["qualification_status"] != expected_status:
        raise ValueError("qualification status does not match threshold results")


def render_markdown(report: dict[str, Any]) -> str:
    """Render only aggregate local-model qualification results."""

    status = str(report["qualification_status"]).upper()
    inference_config = report["execution_environment"]["inference_config"]
    lines = [
        "# Job Apply Agent v4 Local qwen Qualification",
        "",
        report["interpretation"],
        "",
        f"Qualification status: **{status}**.",
        "",
    ]
    if report["qualification_status"] == "blocked":
        lines.extend(
            [
                "## Blocking result",
                "",
                f"- Reason code: `{report['blocking_reason_code']}`.",
                f"- Failure stage: `{report['failure_stage']}`.",
                (
                    "- Completed aggregate phases retained: "
                    f"{len(report['tasks'])} of {len(_QUALIFICATION_TASK_ORDER)}."
                ),
                "- No completed phase can make an incomplete qualification eligible.",
                "",
            ]
        )
    else:
        tasks = report["tasks"]
        lines.extend(
            [
                "## Aggregate measurements",
                "",
                "| Boundary | Cases | Precision | Coverage | Abstention | Gate |",
                "|---|---:|---:|---:|---:|:---:|",
            ]
        )
        for label, key in (
            ("CV routing", "cv_routing"),
            ("Form resolution", "form_resolution"),
            ("Full material packages", "full_material"),
            ("Malformed boundaries", "malformed_boundaries"),
        ):
            task = tasks[key]
            precision = (
                "N/A"
                if key == "full_material" and task["precision_denominator"] == 0
                else f"{task['precision']:.2%}"
            )
            lines.append(
                f"| {label} | {task['cases']} | {precision} | "
                f"{task['coverage']:.2%} | {task['abstention_rate']:.2%} | "
                f"{'PASS' if task['threshold']['passed'] else 'FAIL'} |"
            )
        lines.extend(
            [
                "",
                "## Safety and provenance",
                "",
                (
                    f"- Exact model: `{report['model_identity']['model']}` at "
                    f"`{report['model_identity']['digest']}`."
                ),
                (
                    "- Qualified runner: "
                    f"`{report['execution_environment']['python']['implementation']} "
                    f"{report['execution_environment']['python']['major']}."
                    f"{report['execution_environment']['python']['minor']}`; "
                    "Pydantic "
                    f"`{report['execution_environment']['packages']['pydantic']}`; "
                    "pydantic-settings "
                    f"`{report['execution_environment']['packages']['pydantic_settings']}`; "
                    f"httpx `{report['execution_environment']['packages']['httpx']}`; "
                    f"Redis `{report['execution_environment']['packages']['redis']}`; "
                    "Ollama "
                    f"`{report['execution_environment']['ollama']['server_version']}`."
                ),
                (
                    "- Bound inference configuration: context "
                    f"`{inference_config['ollama_num_ctx']}`; "
                    "prompt characters "
                    f"`{inference_config['llm_max_prompt_chars']}`; "
                    "request timeout "
                    f"`{inference_config['ollama_request_timeout_seconds']}s`; "
                    "lease "
                    f"`{inference_config['lease_mode']}`."
                ),
                (
                    "- Real qwen typed calls: "
                    f"{report['inference']['typed_invocations']}; provider attempts: "
                    f"{report['inference']['provider_attempts']}."
                ),
                (
                    "- Form unsupported eligible: "
                    f"{tasks['form_resolution']['unsupported_eligible']}; "
                    "automatic sensitive eligible: "
                    f"{tasks['form_resolution']['sensitive_automatic_eligible']}; "
                    "sensitive provider attempts: "
                    f"{tasks['form_resolution']['sensitive_provider_attempts']}."
                ),
                (
                    "- Material unsupported eligible: "
                    f"{tasks['full_material']['unsupported_eligible']}; "
                    f"sensitive eligible: {tasks['full_material']['sensitive_eligible']}."
                ),
                (
                    "- Material supported-claim precision: "
                    f"{tasks['full_material']['supported_claims']}/"
                    f"{tasks['full_material']['precision_denominator']} "
                    "audited claim units; package coverage is reported separately."
                ),
                (
                    "- Malformed-boundary provider attempts: "
                    f"{tasks['malformed_boundaries']['provider_attempts']}."
                ),
                "",
                "## Bounded reason counts",
                "",
                (
                    "- Routing: "
                    f"`{json.dumps(tasks['cv_routing']['reason_counts'], sort_keys=True)}`"
                ),
                (
                    "- Forms: "
                    f"`{json.dumps(tasks['form_resolution']['reason_counts'], sort_keys=True)}`"
                ),
                (
                    "- Materials: "
                    f"`{json.dumps(tasks['full_material']['reason_counts'], sort_keys=True)}`"
                ),
                (
                    "- Malformed boundaries: "
                    "`"
                    + json.dumps(
                        tasks["malformed_boundaries"]["reason_counts"],
                        sort_keys=True,
                    )
                    + "`"
                ),
                "",
            ]
        )
    lines.extend(
        [
            "## Dataset integrity",
            "",
            "| Dataset | Cases | SHA-256 |",
            "|---|---:|---|",
        ]
    )
    for dataset_name in sorted(report["datasets"]):
        dataset = report["datasets"][dataset_name]
        lines.append(f"| {dataset['file']} | {dataset['cases']} | `{dataset['sha256']}` |")
    lines.extend(
        [
            "",
            "## Limitations",
            "",
            "- Model outputs and source content are never persisted in this report.",
            "- Synthetic labels are generated and co-designed with the contracts.",
            "- The measurements do not establish real-job, private-profile, or ATS accuracy.",
            "- A blocked run is never converted into a passing qualification.",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(
    report: dict[str, Any],
    *,
    json_output: Path,
    markdown_output: Path,
) -> None:
    validate_aggregate_report(report)
    if json_output.resolve() == markdown_output.resolve():
        raise ValueError("qualification output paths must be distinct")
    json_text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    markdown_text = render_markdown(report)
    json_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    json_temp: Path | None = None
    markdown_temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=json_output.parent,
            prefix=f".{json_output.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json_temp = Path(handle.name)
            handle.write(json_text)
            handle.flush()
            os.fsync(handle.fileno())
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=markdown_output.parent,
            prefix=f".{markdown_output.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            markdown_temp = Path(handle.name)
            handle.write(markdown_text)
            handle.flush()
            os.fsync(handle.fileno())

        # JSON is the production admission artifact. Publish it only after the
        # companion report succeeds and after a final currentness validation.
        os.replace(markdown_temp, markdown_output)
        markdown_temp = None
        validate_aggregate_report(report)
        os.replace(json_temp, json_output)
        json_temp = None
    finally:
        if json_temp is not None:
            json_temp.unlink(missing_ok=True)
        if markdown_temp is not None:
            markdown_temp.unlink(missing_ok=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Qualify the exact local qwen2.5:7b artifact using aggregate-only output.",
    )
    parser.add_argument("--fixtures", type=Path, default=FIXTURES)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON_OUTPUT)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero unless every real-model safety and quality gate passes.",
    )
    parser.add_argument(
        "--validate-report",
        type=Path,
        help="Validate an existing aggregate report without running Ollama.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.validate_report is not None:
        try:
            report = json.loads(args.validate_report.read_text(encoding="utf-8"))
            if not isinstance(report, dict):
                raise ValueError("qualification report must be a JSON object")
            validate_aggregate_report(report)
            markdown_report = args.validate_report.with_suffix(".md")
            if markdown_report.read_text(encoding="utf-8") != render_markdown(report):
                raise ValueError("qualification Markdown report does not match JSON")
        except Exception:
            print(
                json.dumps(
                    {
                        "qualification_status": "blocked",
                        "failure_stage": "aggregate_validation",
                        "reason_code": "REPORT_VALIDATION_FAILED",
                    },
                    sort_keys=True,
                )
            )
            return 1
        print(
            json.dumps(
                {
                    "report_valid": True,
                    "qualification_status": report.get("qualification_status"),
                },
                sort_keys=True,
            )
        )
        return int(args.check and not bool(report.get("overall_pass")))

    _suppress_application_logs()
    started = time.perf_counter()
    progress = _QualificationProgress()
    try:
        report = asyncio.run(evaluate_local_model(args.fixtures, progress=progress))
    except Exception as exc:
        # Never let a validation/provider exception echo source input, model
        # output, a dynamic key, or a traceback. Persist only aggregate
        # checkpoints and a stable type-and-stage reason.
        try:
            report = _blocked_report(
                paths=_source_paths(args.fixtures),
                reason_code=_exception_reason_code(exc, progress.failure_stage),
                runtime_seconds=time.perf_counter() - started,
                progress=progress,
            )
        except Exception:
            print(
                json.dumps(
                    {
                        "qualification_status": "blocked",
                        "failure_stage": "preflight",
                        "reason_code": "EVALUATOR_RECOVERY_FAILED",
                    },
                    sort_keys=True,
                )
            )
            return 1
    try:
        write_report(
            report,
            json_output=args.json_output,
            markdown_output=args.markdown_output,
        )
    except Exception:
        # The output location itself may be unavailable, so do not attempt a
        # second write. Emit only a fixed, content-free terminal diagnostic.
        print(
            json.dumps(
                {
                    "qualification_status": "blocked",
                    "failure_stage": "artifact_write",
                    "reason_code": "ARTIFACT_WRITE_FAILED",
                },
                sort_keys=True,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "overall_pass": report["overall_pass"],
                "qualification_status": report["qualification_status"],
                "runtime_seconds": report["runtime"]["total_seconds"],
            },
            sort_keys=True,
        )
    )
    return int(args.check and not report["overall_pass"])


if __name__ == "__main__":
    raise SystemExit(main())
