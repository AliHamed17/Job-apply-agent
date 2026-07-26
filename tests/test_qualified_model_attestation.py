"""Production qualification must require evidence, not a registry entry alone."""

from __future__ import annotations

import json
import platform
import sys
import tomllib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from profile.models import CVArtifact, SelectedCVArtifact
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from core.material_audit import persist_material_audit
from core.submission_domain import (
    AnswerDecisionV1,
    AnswerDisposition,
    AnswerProvenance,
    FormFieldV1,
    FormPlanV1,
)
from core.submission_service import SubmissionAdmissionError, _require_model_binding
from llm.claim_evidence import ClaimEvidenceRefV1
from llm.client import LLMClient
from llm.contracts import (
    FORM_RESOLUTION_PROMPT_VERSION,
    MATERIAL_PROMPT_VERSION,
    ModelIdentity,
    is_qualified_material_identity,
)
from llm.generation import (
    GeneratedApplication,
    MaterialPackageBlockedError,
    MaterialPackageV1,
    QAAnswerV1,
    _require_qualified_material_client,
)
from llm.qualification_registry import (
    QUALIFIED_EXECUTION_ENVIRONMENT_SCHEMA_VERSION,
    QUALIFIED_OLLAMA_SERVER_VERSION,
    QualificationExecutionEnvironmentV1,
    capture_qualification_execution_environment,
    is_qualified_local_model_identity,
    load_qualified_local_model,
    load_qualified_runtime_packages,
    matches_qualified_local_model_registry,
    qualification_execution_environment_is_qualified,
    qualified_model_report_is_current,
)

_CV_HASH = "c" * 64
_CLAIM_DIGEST = "d" * 64
_QUALIFIED_DISTRIBUTIONS = {
    "annotated_types": "annotated-types",
    "anyio": "anyio",
    "certifi": "certifi",
    "h11": "h11",
    "httpcore": "httpcore",
    "httpx": "httpx",
    "idna": "idna",
    "pydantic": "pydantic",
    "pydantic_core": "pydantic-core",
    "pydantic_settings": "pydantic-settings",
    "pyjwt": "PyJWT",
    "pyyaml": "PyYAML",
    "python_dotenv": "python-dotenv",
    "redis": "redis",
    "sniffio": "sniffio",
    "typing_extensions": "typing-extensions",
    "typing_inspection": "typing-inspection",
}


def _identity() -> ModelIdentity:
    registry = load_qualified_local_model()
    return ModelIdentity(
        provider=registry.provider,
        model=registry.model,
        local=True,
        digest=registry.digest,
    )


def _portable_qualified_environment() -> dict[str, object]:
    environment = capture_qualification_execution_environment(
        ollama_server_version=QUALIFIED_OLLAMA_SERVER_VERSION,
        ollama_reason_code=None,
        ollama_request_timeout_seconds=120.0,
        llm_generation_max_horizon_seconds=120.0,
        ollama_connect_timeout_seconds=3.0,
        ollama_lease_wait_seconds=10.0,
        ollama_lease_ttl_seconds=130,
        ollama_circuit_failure_threshold=3,
        ollama_circuit_reset_seconds=30.0,
        ollama_num_ctx=16_384,
        llm_max_prompt_chars=24_000,
        lease_mode="redis",
        ollama_no_cloud=True,
    ).model_dump(mode="json")
    environment["python"] = {
        "implementation": "CPython",
        "major": 3,
        "minor": 13,
    }
    return environment


def test_qualified_runtime_manifest_matches_installed_graph_and_direct_pins() -> None:
    manifest = load_qualified_runtime_packages()
    environment = _portable_qualified_environment()
    packages = environment["packages"]
    assert environment["schema_version"] == QUALIFIED_EXECUTION_ENVIRONMENT_SCHEMA_VERSION
    assert packages == manifest.packages.model_dump(mode="json")
    assert manifest.packages.idna == "3.15"

    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text("utf-8"))
    direct_dependencies = project["project"]["dependencies"]
    exact_pins = {
        dependency.split("==", maxsplit=1)[0].casefold(): dependency.split("==", maxsplit=1)[1]
        for dependency in direct_dependencies
        if "==" in dependency
    }
    for field, distribution in _QUALIFIED_DISTRIBUTIONS.items():
        assert exact_pins[distribution.casefold()] == getattr(manifest.packages, field)


@pytest.mark.parametrize(
    "manifest_payload",
    [
        None,
        "{not-json",
        (
            '{"schema_version":"qualified-runtime-packages-v1",'
            '"schema_version":"qualified-runtime-packages-v1","packages":{}}'
        ),
        json.dumps(
            {
                "schema_version": "qualified-runtime-packages-v1",
                "packages": {"idna": "3.15"},
            }
        ),
        json.dumps(
            {
                "schema_version": "qualified-runtime-packages-v1",
                "packages": {
                    **load_qualified_runtime_packages().packages.model_dump(mode="json"),
                    "urllib3": "2.6.3",
                },
            }
        ),
    ],
)
def test_missing_malformed_or_incomplete_runtime_manifest_fails_closed(
    tmp_path: Path,
    monkeypatch,
    manifest_payload: str | None,
) -> None:
    import llm.qualification_registry as registry_module

    environment = QualificationExecutionEnvironmentV1.model_validate(
        _portable_qualified_environment()
    )
    manifest_path = tmp_path / "qualified_runtime_packages.json"
    if manifest_payload is not None:
        manifest_path.write_text(manifest_payload, encoding="utf-8")
    monkeypatch.setattr(
        registry_module,
        "QUALIFIED_RUNTIME_PACKAGES_PATH",
        manifest_path,
    )
    registry_module.load_qualified_runtime_packages.cache_clear()
    try:
        assert not qualification_execution_environment_is_qualified(environment)
    finally:
        registry_module.load_qualified_runtime_packages.cache_clear()


def test_currentness_requires_report_packages_to_equal_committed_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    environment = _portable_qualified_environment()
    registry_module, _ = _install_synthetic_passing_report(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        environment=environment,
    )
    manifest_payload = load_qualified_runtime_packages().model_dump(mode="json")
    manifest_payload["packages"]["idna"] = "99.99.99"
    manifest_path = tmp_path / "different-qualified-runtime-packages.json"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    monkeypatch.setattr(
        registry_module,
        "QUALIFIED_RUNTIME_PACKAGES_PATH",
        manifest_path,
    )
    registry_module.load_qualified_runtime_packages.cache_clear()
    try:
        assert not qualified_model_report_is_current(
            ollama_server_version=QUALIFIED_OLLAMA_SERVER_VERSION,
            ollama_request_timeout_seconds=120.0,
            llm_generation_max_horizon_seconds=120.0,
            ollama_connect_timeout_seconds=3.0,
            ollama_lease_wait_seconds=10.0,
            ollama_lease_ttl_seconds=130,
            ollama_circuit_failure_threshold=3,
            ollama_circuit_reset_seconds=30.0,
            ollama_num_ctx=16_384,
            llm_max_prompt_chars=24_000,
            lease_mode="redis",
            ollama_no_cloud=True,
        )
    finally:
        registry_module.load_qualified_local_model.cache_clear()
        registry_module.load_qualified_runtime_packages.cache_clear()


def test_qualified_environment_rejects_lease_shorter_than_generation_horizon() -> None:
    with pytest.raises(ValidationError):
        capture_qualification_execution_environment(
            ollama_server_version=QUALIFIED_OLLAMA_SERVER_VERSION,
            ollama_reason_code=None,
            ollama_request_timeout_seconds=60.0,
            llm_generation_max_horizon_seconds=120.0,
            ollama_connect_timeout_seconds=3.0,
            ollama_lease_wait_seconds=10.0,
            ollama_lease_ttl_seconds=65,
            ollama_circuit_failure_threshold=3,
            ollama_circuit_reset_seconds=30.0,
            ollama_num_ctx=16_384,
            llm_max_prompt_chars=24_000,
            lease_mode="redis",
            ollama_no_cloud=True,
        )


def _install_synthetic_passing_report(
    *,
    tmp_path: Path,
    monkeypatch,
    environment: dict[str, object],
) -> tuple[object, Path]:
    import llm.qualification_registry as registry_module
    import scripts.evaluate_v4_local_model_qualification as evaluator

    registry = load_qualified_local_model()
    registry_root = tmp_path / "qualified-runtime"
    registry_path = registry_root / "config" / "qualified_local_model.json"
    report_path = registry_root / registry.qualification_report
    registry_path.parent.mkdir(parents=True)
    report_path.parent.mkdir(parents=True)
    registry_path.write_text(registry.model_dump_json(), encoding="utf-8")
    report_path.write_text(
        json.dumps(
            {
                "qualification_status": "passed",
                "overall_pass": True,
                "model_identity": {
                    "provider": registry.provider,
                    "model": registry.model,
                    "local": True,
                    "digest": registry.digest,
                },
                "execution_environment": environment,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(registry_module, "ROOT", registry_root)
    monkeypatch.setattr(
        registry_module,
        "QUALIFIED_MODEL_REGISTRY_PATH",
        registry_path,
    )
    monkeypatch.setattr(evaluator, "validate_aggregate_report", lambda _report: None)
    registry_module.load_qualified_local_model.cache_clear()
    return registry_module, report_path


def _package() -> MaterialPackageV1:
    return MaterialPackageV1(
        cv_sha256=_CV_HASH,
        profile_version=1,
        model_identity=_identity(),
        cover_letter="I developed Python services.",
        recruiter_message="I am interested in the role.",
        qa_answers=(
            QAAnswerV1(
                field_id="relevant_experience",
                answer="I developed Python services.",
            ),
        ),
        claim_evidence=(
            ClaimEvidenceRefV1(
                claim_id="claim_" + "c" * 24,
                claim_digest=_CLAIM_DIGEST,
                evidence_ids=("ev_" + "e" * 24,),
                evidence_quote_digests=("f" * 64,),
                supported=True,
            ),
        ),
        relevant_experience_claim_digests=(_CLAIM_DIGEST,),
    )


class _ExactIdentityClient(LLMClient):
    @property
    def model_identity(self) -> ModelIdentity:
        return _identity()

    async def generate(self, *args, **kwargs):
        raise AssertionError("generation must not begin without qualification evidence")

    async def generate_json(self, *args, **kwargs):
        raise AssertionError("generation must not begin without qualification evidence")


def test_registry_match_is_not_production_qualification(monkeypatch) -> None:
    identity = _identity()
    monkeypatch.setattr(
        "llm.qualification_registry.qualified_model_report_is_current",
        lambda: False,
    )

    assert matches_qualified_local_model_registry(
        provider=identity.provider,
        model=identity.model,
        local=identity.local,
        digest=identity.digest,
    )
    assert not is_qualified_local_model_identity(
        provider=identity.provider,
        model=identity.model,
        local=identity.local,
        digest=identity.digest,
    )
    assert not is_qualified_material_identity(
        provider=identity.provider,
        model=identity.model,
        local=identity.local,
        digest=identity.digest,
        prompt_version=MATERIAL_PROMPT_VERSION,
    )


@pytest.mark.asyncio
async def test_exact_prefilled_client_cannot_skip_missing_attestation(monkeypatch) -> None:
    monkeypatch.setattr(
        "llm.qualification_registry.qualified_model_report_is_current",
        lambda: False,
    )

    with pytest.raises(MaterialPackageBlockedError, match="MATERIAL_MODEL_NOT_QUALIFIED"):
        await _require_qualified_material_client(_ExactIdentityClient())


def test_material_package_and_persistence_fail_closed_without_report(monkeypatch) -> None:
    package = _package()
    selected = SelectedCVArtifact(
        cv_id="synthetic-cv",
        resolved_path="C:/private/synthetic.pdf",
        artifact=CVArtifact(
            pdf_sha256=_CV_HASH,
            byte_size=10,
            extracted_text="Developed Python services.",
        ),
    )
    application = SimpleNamespace(
        selected_cv_hash=None,
        material_eligible=None,
        material_blockers_json=None,
        material_claims_json=None,
        material_model_provider=None,
        material_model_name=None,
        material_model_digest=None,
        material_prompt_version=None,
    )
    generated = GeneratedApplication(
        cover_letter=package.cover_letter,
        recruiter_message=package.recruiter_message,
        qa_answers=package.qa_answer_dict(),
        cv_sha256=_CV_HASH,
        profile_version=1,
        claim_evidence=list(package.claim_evidence),
        material_package=package,
    )
    monkeypatch.setattr(
        "llm.qualification_registry.qualified_model_report_is_current",
        lambda: False,
    )

    assert not package.eligible
    blockers = persist_material_audit(application, generated, selected)
    assert application.material_eligible is False
    assert "MATERIAL_MODEL_NOT_QUALIFIED" in blockers


def test_form_plan_and_submission_admission_fail_closed_without_report(
    monkeypatch,
) -> None:
    now = datetime.now(UTC)
    field = FormFieldV1(
        field_id="primary_language",
        canonical_name="primary_language",
        label="Primary programming language",
        field_type="text",
        required=True,
        position=0,
    )
    decision = AnswerDecisionV1(
        field_id=field.field_id,
        disposition=AnswerDisposition.RESOLVED,
        provenance=AnswerProvenance.LOCAL_LLM,
        value="Python",
        evidence_refs=(f"cv:{_CV_HASH}:primary_language",),
    )
    identity = _identity()
    monkeypatch.setattr(
        "llm.qualification_registry.qualified_model_report_is_current",
        lambda: False,
    )
    with pytest.raises(ValueError, match="qualified prompt and model identity"):
        FormPlanV1(
            plan_id=uuid4(),
            application_id=1,
            application_revision=1,
            adapter_name="fixture",
            adapter_version="1.0.0",
            selector_version="fixture-v1",
            form_fingerprint="f" * 64,
            selected_cv_id="synthetic-cv",
            selected_cv_hash=_CV_HASH,
            attached_cv_id="synthetic-cv",
            attached_cv_hash=_CV_HASH,
            attachment_verified=True,
            profile_version=1,
            session_verified_at=now,
            created_at=now,
            expires_at=now + timedelta(minutes=30),
            fields=(field,),
            decisions=(decision,),
            llm_prompt_version=FORM_RESOLUTION_PROMPT_VERSION,
            llm_model_provider=identity.provider,
            llm_model_name=identity.model,
            llm_model_digest=identity.digest,
        )

    confirmed = decision.model_copy(
        update={
            "provenance": AnswerProvenance.USER_CONFIRMED,
            "evidence_refs": ("operator_confirmation:synthetic",),
        }
    )
    plan = FormPlanV1(
        plan_id=uuid4(),
        application_id=1,
        application_revision=1,
        adapter_name="fixture",
        adapter_version="1.0.0",
        selector_version="fixture-v1",
        form_fingerprint="f" * 64,
        selected_cv_id="synthetic-cv",
        selected_cv_hash=_CV_HASH,
        attached_cv_id="synthetic-cv",
        attached_cv_hash=_CV_HASH,
        attachment_verified=True,
        profile_version=1,
        session_verified_at=now,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        fields=(field,),
        decisions=(confirmed,),
    )
    application = SimpleNamespace(
        material_model_provider=identity.provider,
        material_model_name=identity.model,
        material_model_digest=identity.digest,
    )
    capabilities = {
        "llm": {
            "ready": True,
            "provider": identity.provider,
            "model": identity.model,
            "local": True,
            "digest": identity.digest,
        }
    }
    with pytest.raises(SubmissionAdmissionError, match="RUNTIME_NOT_READY"):
        _require_model_binding(application, plan, capabilities)


@pytest.mark.parametrize("report_payload", [None, "{not-json", json.dumps({"overall_pass": True})])
def test_missing_or_invalid_registry_report_never_qualifies(
    tmp_path: Path,
    monkeypatch,
    report_payload: str | None,
) -> None:
    import llm.qualification_registry as registry_module

    registry = load_qualified_local_model()
    registry_root = tmp_path / "registry-root"
    registry_path = registry_root / "config" / "qualified_local_model.json"
    report_path = registry_root / registry.qualification_report
    registry_path.parent.mkdir(parents=True)
    report_path.parent.mkdir(parents=True)
    registry_path.write_text(
        registry.model_dump_json(),
        encoding="utf-8",
    )
    if report_payload is not None:
        report_path.write_text(report_payload, encoding="utf-8")
    monkeypatch.setattr(registry_module, "ROOT", registry_root)
    monkeypatch.setattr(
        registry_module,
        "QUALIFIED_MODEL_REGISTRY_PATH",
        registry_path,
    )
    registry_module.load_qualified_local_model.cache_clear()
    try:
        assert not qualified_model_report_is_current()
    finally:
        registry_module.load_qualified_local_model.cache_clear()


def test_structural_report_remains_portable_but_currentness_requires_cpython_313(
    tmp_path: Path,
    monkeypatch,
) -> None:
    environment = _portable_qualified_environment()
    registry_module, _ = _install_synthetic_passing_report(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        environment=environment,
    )
    try:
        expected = (
            platform.python_implementation() == "CPython"
            and sys.version_info.major == 3
            and sys.version_info.minor == 13
        )
        assert (
            qualified_model_report_is_current(
                ollama_server_version=QUALIFIED_OLLAMA_SERVER_VERSION,
                ollama_request_timeout_seconds=120.0,
                llm_generation_max_horizon_seconds=120.0,
                ollama_connect_timeout_seconds=3.0,
                ollama_lease_wait_seconds=10.0,
                ollama_lease_ttl_seconds=130,
                ollama_circuit_failure_threshold=3,
                ollama_circuit_reset_seconds=30.0,
                ollama_num_ctx=16_384,
                llm_max_prompt_chars=24_000,
                lease_mode="redis",
                ollama_no_cloud=True,
            )
            is expected
        )
    finally:
        registry_module.load_qualified_local_model.cache_clear()


@pytest.mark.skipif(
    platform.python_implementation() != "CPython" or sys.version_info[:2] != (3, 13),
    reason="production qualification currentness is asserted only on CPython 3.13",
)
def test_committed_report_is_current_for_pinned_production_runner() -> None:
    assert qualified_model_report_is_current(
        ollama_server_version=QUALIFIED_OLLAMA_SERVER_VERSION,
        ollama_request_timeout_seconds=120.0,
        llm_generation_max_horizon_seconds=120.0,
        ollama_connect_timeout_seconds=3.0,
        ollama_lease_wait_seconds=10.0,
        ollama_lease_ttl_seconds=130,
        ollama_circuit_failure_threshold=3,
        ollama_circuit_reset_seconds=30.0,
        ollama_num_ctx=16_384,
        llm_max_prompt_chars=24_000,
        lease_mode="redis",
        ollama_no_cloud=True,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_environment",
        "extra_environment_key",
        "missing_package_key",
        "malformed_version",
        *[f"{field}_version_changed" for field in _QUALIFIED_DISTRIBUTIONS],
        "python_311",
        "model_digest_changed",
        "ollama_version_changed",
        "request_timeout_changed",
        "maximum_horizon_changed",
        "connect_timeout_changed",
        "lease_wait_changed",
        "lease_ttl_changed",
        "circuit_threshold_changed",
        "circuit_reset_changed",
        "num_ctx_changed",
        "prompt_bound_changed",
        "lease_mode_changed",
        "cloud_policy_changed",
    ],
)
def test_currentness_rejects_missing_changed_or_spoofed_runtime_fingerprint(
    tmp_path: Path,
    monkeypatch,
    mutation: str,
) -> None:
    environment = deepcopy(_portable_qualified_environment())
    runtime_server_version = QUALIFIED_OLLAMA_SERVER_VERSION
    runtime_request_timeout = 120.0
    runtime_maximum_horizon = 120.0
    runtime_connect_timeout = 3.0
    runtime_lease_wait = 10.0
    runtime_lease_ttl = 130
    runtime_circuit_threshold = 3
    runtime_circuit_reset = 30.0
    runtime_num_ctx = 16_384
    runtime_prompt_bound = 24_000
    runtime_lease_mode = "redis"
    runtime_no_cloud = True
    if mutation == "missing_environment":
        environment = {}
    elif mutation == "extra_environment_key":
        environment["operating_system"] = "spoof"
    elif mutation == "missing_package_key":
        del environment["packages"]["typing_inspection"]
    elif mutation == "malformed_version":
        environment["packages"]["pydantic"] = "latest\nspoof"
    elif mutation.endswith("_version_changed"):
        field = mutation.removesuffix("_version_changed")
        environment["packages"][field] = "99.99.99"
    elif mutation == "python_311":
        environment["python"]["minor"] = 11
    elif mutation == "model_digest_changed":
        environment["model_digest"] = f"sha256:{'0' * 64}"
    elif mutation == "ollama_version_changed":
        runtime_server_version = "0.31.2"
    elif mutation == "request_timeout_changed":
        runtime_request_timeout = 119.0
    elif mutation == "maximum_horizon_changed":
        runtime_maximum_horizon = 119.0
        runtime_request_timeout = 119.0
    elif mutation == "connect_timeout_changed":
        runtime_connect_timeout = 2.0
    elif mutation == "lease_wait_changed":
        runtime_lease_wait = 9.0
    elif mutation == "lease_ttl_changed":
        runtime_lease_ttl = 129
    elif mutation == "circuit_threshold_changed":
        runtime_circuit_threshold = 4
    elif mutation == "circuit_reset_changed":
        runtime_circuit_reset = 31.0
    elif mutation == "num_ctx_changed":
        runtime_num_ctx = 8_192
    elif mutation == "prompt_bound_changed":
        runtime_prompt_bound = 12_000
    elif mutation == "lease_mode_changed":
        runtime_lease_mode = "process_local"
    else:
        runtime_no_cloud = False

    registry_module, _ = _install_synthetic_passing_report(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        environment=environment,
    )
    try:
        assert not qualified_model_report_is_current(
            ollama_server_version=runtime_server_version,
            ollama_request_timeout_seconds=runtime_request_timeout,
            llm_generation_max_horizon_seconds=runtime_maximum_horizon,
            ollama_connect_timeout_seconds=runtime_connect_timeout,
            ollama_lease_wait_seconds=runtime_lease_wait,
            ollama_lease_ttl_seconds=runtime_lease_ttl,
            ollama_circuit_failure_threshold=runtime_circuit_threshold,
            ollama_circuit_reset_seconds=runtime_circuit_reset,
            ollama_num_ctx=runtime_num_ctx,
            llm_max_prompt_chars=runtime_prompt_bound,
            lease_mode=runtime_lease_mode,
            ollama_no_cloud=runtime_no_cloud,
        )
    finally:
        registry_module.load_qualified_local_model.cache_clear()
