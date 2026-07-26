from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest
import structlog

from core.config import get_settings
from scripts.build_v4_local_model_material_fixtures import build_material_cases
from scripts.evaluate_v4_local_model_qualification import (
    _SOURCE_FILES,
    DEFAULT_JSON_OUTPUT,
    FIXTURES,
    REPORT_SCHEMA_VERSION,
    _blocked_report,
    _evaluate_malformed_boundaries,
    _exact_model_threshold_passes,
    _exception_reason_code,
    _execution_environment_threshold_passes,
    _form_threshold_passes,
    _InstrumentedOllamaClient,
    _load_form_rows,
    _material_threshold_passes,
    _normalized_text_sha256,
    _qualification_input_attestation,
    _qualification_purpose_key,
    _QualificationProgress,
    _require_qualification_inputs_unchanged,
    _serialized_inference_threshold_passes,
    _source_paths,
    _suppress_application_logs,
    _validate_malformed_task,
    render_markdown,
    validate_aggregate_report,
    write_report,
)
from scripts.evaluate_v4_local_model_qualification import (
    main as qualification_main,
)


def test_full_material_fixture_has_40_stable_synthetic_tasks() -> None:
    built = build_material_cases()
    committed = json.loads(
        (FIXTURES / "local_model_full_material_40.json").read_text(encoding="utf-8")
    )

    assert committed == built
    assert len(committed) == 40
    assert len({row["id"] for row in committed}) == 40
    assert len({row["family"] for row in committed}) == 10
    assert {row["synthetic_label"] for row in committed} == {"complete_non_sensitive_input"}
    assert all("llm_output" not in row for row in committed)


def test_application_log_suppression_is_supported_and_content_free(capsys) -> None:
    marker = "synthetic-private-log-marker"
    try:
        _suppress_application_logs()
        logger = structlog.get_logger("qualification-regression")
        logger.info(marker)
        logger.warning(marker)
        logger.error(marker)
        captured = capsys.readouterr()
        assert marker not in captured.out
        assert marker not in captured.err
    finally:
        structlog.reset_defaults()


def test_form_source_loader_discards_embedded_llm_output(tmp_path: Path) -> None:
    source = json.loads(
        (FIXTURES / "form_resolution_bilingual_240.json").read_text(encoding="utf-8")
    )
    source[0]["llm_output"] = {
        "value": "must-not-cross-source-boundary",
        "confidence": 1,
        "evidence_refs": ["must-not-cross-source-boundary"],
    }
    path = tmp_path / "forms.json"
    path.write_text(json.dumps(source), encoding="utf-8")

    rows = _load_form_rows(path)
    serialized = json.dumps(
        [row.model_dump(mode="json") for row in rows],
        sort_keys=True,
    )

    assert len(rows) == 240
    assert "llm_output" not in serialized
    assert "must-not-cross-source-boundary" not in serialized


@pytest.mark.parametrize(
    ("successful", "payloads", "safety_violations", "expected"),
    [
        (80, 80, 0, True),
        (76, 76, 0, True),
        (75, 80, 0, False),
        (80, 80, 1, False),
    ],
)
def test_form_threshold_allows_bounded_fail_closed_provider_abstention(
    successful: int,
    payloads: int,
    safety_violations: int,
    expected: bool,
) -> None:
    assert (
        _form_threshold_passes(
            precision=1.0,
            synthesis_precision=1.0,
            expected_llm_cases=80,
            typed_invocations=80,
            provider_cases=80,
            successful_generation_cases=successful,
            successful_generations=successful,
            provider_attempts=80,
            provider_payloads=payloads,
            safety_violations=safety_violations,
        )
        is expected
    )


def test_blocked_model_report_cannot_pass() -> None:
    report = _blocked_report(
        paths=_source_paths(FIXTURES),
        reason_code="LLM_MODEL_NOT_READY",
        runtime_seconds=0.5,
    )

    validate_aggregate_report(report)
    assert report["qualification_status"] == "blocked"
    assert report["overall_pass"] is False
    assert all(not gate["passed"] for gate in report["thresholds"].values())
    assert report["failure_stage"] == "preflight"
    assert "No completed phase can make" in render_markdown(report)


@pytest.mark.asyncio
async def test_malformed_boundary_bridge_consumes_current_quality_schema() -> None:
    client = _InstrumentedOllamaClient()

    task = await _evaluate_malformed_boundaries(
        FIXTURES / "malformed_prompt_injection_30.json",
        FIXTURES / "cv_routing_eval_config.yaml",
        client,
    )

    assert task["correctly_blocked"] == 30
    assert task["eligible_for_preparation"] == 0
    assert task["typed_invocations"] == 0
    assert task["provider_attempts"] == 0
    assert task["provider_payloads"] == 0
    assert task["successful_generations"] == 0
    assert task["threshold"]["actual"]["blocked"] == 30
    assert task["threshold"]["passed"] is True
    assert _validate_malformed_task(task) is task


def test_material_purpose_counter_uses_report_safe_alias() -> None:
    assert _qualification_purpose_key("cover_letter") == "full_material"
    assert _qualification_purpose_key("cv_routing") == "cv_routing"
    assert _qualification_purpose_key("private-dynamic-purpose") == "other_bounded_purpose"


def test_partial_blocked_report_preserves_bounded_inference_without_qualifying() -> None:
    progress = _QualificationProgress(failure_stage="cv_routing")
    progress.inference.update(
        {
            "typed_invocations": 1,
            "provider_attempts": 1,
            "purpose_counts": {"cv_routing": 1},
            "failure_reason_counts": {"LLM_CIRCUIT_OPEN": 1},
            "maximum_concurrent_calls": 1,
        }
    )
    report = _blocked_report(
        paths=_source_paths(FIXTURES),
        reason_code="EVALUATOR_RESULT_SCHEMA_MISMATCH",
        runtime_seconds=0.5,
        progress=progress,
    )

    validate_aggregate_report(report)
    assert report["overall_pass"] is False
    assert report["failure_stage"] == "cv_routing"
    assert report["inference"]["typed_invocations"] == 1
    assert report["inference"]["failure_reason_counts"] == {"LLM_CIRCUIT_OPEN": 1}
    assert report["tasks"] == {}


def test_evaluator_failure_code_never_persists_exception_text() -> None:
    private_marker = "synthetic-private-exception-marker"

    schema_code = _exception_reason_code(KeyError(private_marker), "malformed_boundaries")
    unexpected_code = _exception_reason_code(RuntimeError(private_marker), "full_material")

    assert schema_code == "EVALUATOR_RESULT_SCHEMA_MISMATCH"
    assert unexpected_code == "UNEXPECTED_EVALUATOR_ERROR"
    assert private_marker not in schema_code
    assert private_marker not in unexpected_code


@pytest.mark.parametrize("mutation", ["source", "fixture"])
def test_qualification_input_drift_blocks_before_report(
    mutation: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    import scripts.evaluate_v4_local_model_qualification as evaluator

    source = tmp_path / "behavior.py"
    source.write_text("POLICY = 1\n", encoding="utf-8")
    paths = {
        key: tmp_path / filename
        for key, filename in {
            "routing": "routing.json",
            "routing_config": "routing.yaml",
            "forms": "forms.json",
            "materials": "materials.json",
            "malformed": "malformed.json",
        }.items()
    }
    for path in paths.values():
        path.write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(evaluator, "ROOT", tmp_path)
    monkeypatch.setattr(evaluator, "_SOURCE_FILES", {"behavior": source})
    initial = _qualification_input_attestation(paths)

    if mutation == "source":
        source.write_text("POLICY = 2\n", encoding="utf-8")
    else:
        paths["forms"].write_text("[{}]\n", encoding="utf-8")

    with pytest.raises(RuntimeError) as exc_info:
        _require_qualification_inputs_unchanged(initial, paths)

    assert (
        _exception_reason_code(exc_info.value, "aggregate_validation")
        == "QUALIFICATION_INPUT_DRIFT"
    )


def test_invalid_settings_write_bounded_preflight_report_without_details(
    tmp_path: Path,
    monkeypatch,
) -> None:
    private_marker = "synthetic-private-settings-marker"
    json_output = tmp_path / "blocked.json"
    markdown_output = tmp_path / "blocked.md"
    monkeypatch.setenv("OLLAMA_NUM_CTX", private_marker)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate_v4_local_model_qualification.py",
            "--json-output",
            str(json_output),
            "--markdown-output",
            str(markdown_output),
            "--check",
        ],
    )
    get_settings.cache_clear()
    try:
        assert qualification_main() == 1
    finally:
        get_settings.cache_clear()
        structlog.reset_defaults()

    report_text = json_output.read_text(encoding="utf-8")
    markdown_text = markdown_output.read_text(encoding="utf-8")
    report = json.loads(report_text)
    validate_aggregate_report(report)
    assert report["qualification_status"] == "blocked"
    assert report["failure_stage"] == "preflight"
    assert report["blocking_reason_code"] == "EVALUATOR_INPUT_VALIDATION_FAILED"
    assert report["execution_environment"]["inference_config"] == {
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
        "configuration_reason_code": "EVALUATOR_INPUT_VALIDATION_FAILED",
    }
    assert private_marker not in report_text
    assert private_marker not in markdown_text


def test_relationally_invalid_timing_writes_bounded_preflight_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    private_marker = "synthetic-private-relational-marker"
    json_output = tmp_path / "blocked.json"
    markdown_output = tmp_path / "blocked.md"
    monkeypatch.setenv("OLLAMA_REQUEST_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("LLM_GENERATION_MAX_HORIZON_SECONDS", "120")
    monkeypatch.setenv("OLLAMA_LEASE_TTL_SECONDS", "65")
    monkeypatch.setenv("OPENAI_API_KEY", private_marker)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate_v4_local_model_qualification.py",
            "--json-output",
            str(json_output),
            "--markdown-output",
            str(markdown_output),
            "--check",
        ],
    )
    get_settings.cache_clear()
    try:
        assert qualification_main() == 1
    finally:
        get_settings.cache_clear()
        structlog.reset_defaults()

    report_text = json_output.read_text(encoding="utf-8")
    markdown_text = markdown_output.read_text(encoding="utf-8")
    report = json.loads(report_text)
    validate_aggregate_report(report)
    assert report["qualification_status"] == "blocked"
    assert report["failure_stage"] == "preflight"
    assert report["blocking_reason_code"] == "EVALUATOR_INPUT_VALIDATION_FAILED"
    assert (
        report["execution_environment"]["inference_config"]["configuration_reason_code"]
        == "EVALUATOR_INPUT_VALIDATION_FAILED"
    )
    assert private_marker not in report_text
    assert private_marker not in markdown_text


def test_report_publication_never_replaces_json_before_markdown(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import scripts.evaluate_v4_local_model_qualification as evaluator

    report = _blocked_report(
        paths=_source_paths(FIXTURES),
        reason_code="LLM_MODEL_NOT_READY",
        runtime_seconds=0.5,
    )
    json_output = tmp_path / "qualification.json"
    markdown_output = tmp_path / "qualification.md"
    sentinel = '{"qualification_status":"previous"}\n'
    json_output.write_text(sentinel, encoding="utf-8")
    private_marker = "synthetic-private-write-marker"
    real_replace = evaluator.os.replace

    def fail_markdown_replace(source, destination):
        if Path(destination) == markdown_output:
            raise OSError(private_marker)
        return real_replace(source, destination)

    monkeypatch.setattr(evaluator.os, "replace", fail_markdown_replace)

    with pytest.raises(OSError):
        write_report(
            report,
            json_output=json_output,
            markdown_output=markdown_output,
        )

    assert json_output.read_text(encoding="utf-8") == sentinel
    assert private_marker not in json_output.read_text(encoding="utf-8")
    assert not tuple(tmp_path.glob("*.tmp"))


def test_artifact_write_failure_returns_fixed_content_free_diagnostic(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    import scripts.evaluate_v4_local_model_qualification as evaluator

    private_marker = "synthetic-private-write-marker"
    report = _blocked_report(
        paths=_source_paths(FIXTURES),
        reason_code="LLM_MODEL_NOT_READY",
        runtime_seconds=0.5,
    )

    async def fake_evaluation(*_args, **_kwargs):
        return report

    def fail_write(*_args, **_kwargs):
        raise OSError(private_marker)

    monkeypatch.setattr(evaluator, "evaluate_local_model", fake_evaluation)
    monkeypatch.setattr(evaluator, "write_report", fail_write)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate_v4_local_model_qualification.py",
            "--json-output",
            str(tmp_path / "qualification.json"),
            "--markdown-output",
            str(tmp_path / "qualification.md"),
            "--check",
        ],
    )

    assert qualification_main() == 1
    captured = capsys.readouterr()
    assert private_marker not in captured.out
    assert private_marker not in captured.err
    diagnostic = json.loads(captured.out)
    assert diagnostic == {
        "failure_stage": "artifact_write",
        "qualification_status": "blocked",
        "reason_code": "ARTIFACT_WRITE_FAILED",
    }
    structlog.reset_defaults()


def test_missing_fixture_recovery_emits_only_fixed_diagnostic(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    private_marker = "synthetic-private-missing-fixture-marker"
    missing_fixtures = tmp_path / private_marker
    json_output = tmp_path / "qualification.json"
    markdown_output = tmp_path / "qualification.md"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate_v4_local_model_qualification.py",
            "--fixtures",
            str(missing_fixtures),
            "--json-output",
            str(json_output),
            "--markdown-output",
            str(markdown_output),
            "--check",
        ],
    )

    assert qualification_main() == 1
    captured = capsys.readouterr()
    assert private_marker not in captured.out
    assert private_marker not in captured.err
    assert "Traceback" not in captured.err
    assert json.loads(captured.out) == {
        "failure_stage": "preflight",
        "qualification_status": "blocked",
        "reason_code": "EVALUATOR_RECOVERY_FAILED",
    }
    assert not json_output.exists()
    assert not markdown_output.exists()


def test_minimal_passing_report_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid schema"):
        validate_aggregate_report(
            {
                "schema_version": REPORT_SCHEMA_VERSION,
                "qualification_status": "passed",
                "overall_pass": True,
            }
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "dataset_hash",
        "source_hash",
        "registry_digest",
        "threshold_pass",
    ],
)
def test_blocked_report_rejects_stale_or_tampered_attestation(mutation: str) -> None:
    report = _blocked_report(
        paths=_source_paths(FIXTURES),
        reason_code="LLM_MODEL_NOT_READY",
        runtime_seconds=0.5,
    )
    tampered = deepcopy(report)
    if mutation == "dataset_hash":
        tampered["datasets"]["routing"]["sha256"] = "0" * 64
    elif mutation == "source_hash":
        tampered["source_integrity"]["combined_sha256"] = "0" * 64
    elif mutation == "registry_digest":
        tampered["qualified_model_registry"]["digest"] = f"sha256:{'0' * 64}"
    else:
        tampered["thresholds"]["exact_model_ready"]["passed"] = True

    with pytest.raises(ValueError):
        validate_aggregate_report(tampered)


@pytest.mark.parametrize(
    ("eligible", "expected"),
    [
        (0, False),
        (37, False),
        (38, True),
        (40, True),
    ],
)
def test_material_threshold_requires_real_generation_and_95_percent_coverage(
    eligible: int,
    expected: bool,
) -> None:
    task = {
        "cases": 40,
        "typed_invocations": 40,
        "provider_cases": 40,
        "successful_generation_cases": 40,
        "successful_generations": 40,
        "provider_attempts": 40,
        "provider_payloads": 40,
        "generation_failed": 0,
        "eligible": eligible,
        "coverage": round(eligible / 40, 6),
        "precision": 1.0,
        "precision_denominator": 40,
        "supported_claims": 40,
        "unsupported_claims": 0,
        "unsupported_eligible": 0,
        "sensitive_eligible": 0,
    }

    assert _material_threshold_passes(task) is expected


def test_material_threshold_measures_claim_precision_independently_from_coverage() -> None:
    task = {
        "cases": 40,
        "typed_invocations": 40,
        "provider_cases": 40,
        "successful_generation_cases": 40,
        "successful_generations": 40,
        "provider_attempts": 40,
        "provider_payloads": 40,
        "generation_failed": 0,
        "eligible": 40,
        "coverage": 1.0,
        "precision": 0.5,
        "precision_denominator": 80,
        "supported_claims": 40,
        "unsupported_claims": 40,
        "unsupported_eligible": 0,
        "sensitive_eligible": 0,
    }

    assert not _material_threshold_passes(task)


def test_material_threshold_rejects_preflight_only_or_failed_provider_cases() -> None:
    task = {
        "cases": 40,
        "typed_invocations": 40,
        "provider_cases": 40,
        "successful_generation_cases": 0,
        "successful_generations": 0,
        "provider_attempts": 40,
        "provider_payloads": 0,
        "generation_failed": 40,
        "eligible": 0,
        "coverage": 0.0,
        "unsupported_eligible": 0,
        "sensitive_eligible": 0,
    }

    assert not _material_threshold_passes(task)


@pytest.mark.parametrize(
    (
        "distinct_identities",
        "identity_observations",
        "registry_identity_observations",
        "successful_generations",
        "expected",
    ),
    [
        (0, 40, 40, 40, False),
        (2, 40, 40, 40, False),
        (1, 40, 39, 40, False),
        (1, 39, 39, 40, False),
        (1, 40, 40, 40, True),
    ],
)
def test_exact_model_gate_rejects_zero_mixed_or_unobserved_identities(
    distinct_identities: int,
    identity_observations: int,
    registry_identity_observations: int,
    successful_generations: int,
    expected: bool,
) -> None:
    assert (
        _exact_model_threshold_passes(
            stable=True,
            distinct_identities=distinct_identities,
            identity_observations=identity_observations,
            registry_identity_observations=registry_identity_observations,
            successful_generations=successful_generations,
        )
        is expected
    )


@pytest.mark.parametrize(
    ("maximum_concurrent_calls", "typed_invocations", "expected"),
    [(0, 40, False), (1, 0, False), (2, 40, False), (1, 40, True)],
)
def test_serialized_gate_requires_observed_nonconcurrent_inference(
    maximum_concurrent_calls: int,
    typed_invocations: int,
    expected: bool,
) -> None:
    assert (
        _serialized_inference_threshold_passes(
            maximum_concurrent_calls=maximum_concurrent_calls,
            typed_invocations=typed_invocations,
        )
        is expected
    )


@pytest.mark.parametrize(
    (
        "stable",
        "pre_post_observations",
        "distinct_server_versions",
        "server_version_observations",
        "qualified_server_version_observations",
        "successful_generations",
        "environment_qualified",
        "expected",
    ),
    [
        (True, 2, 1, 40, 40, 40, True, True),
        (False, 2, 1, 40, 40, 40, True, False),
        (True, 1, 1, 40, 40, 40, True, False),
        (True, 2, 2, 40, 40, 40, True, False),
        (True, 2, 1, 39, 39, 40, True, False),
        (True, 2, 1, 40, 39, 40, True, False),
        (True, 2, 1, 0, 0, 0, True, False),
        (True, 2, 1, 40, 40, 40, False, False),
    ],
)
def test_execution_environment_gate_requires_stable_per_call_server_attestation(
    stable: bool,
    pre_post_observations: int,
    distinct_server_versions: int,
    server_version_observations: int,
    qualified_server_version_observations: int,
    successful_generations: int,
    environment_qualified: bool,
    expected: bool,
) -> None:
    assert (
        _execution_environment_threshold_passes(
            stable=stable,
            pre_post_observations=pre_post_observations,
            distinct_server_versions=distinct_server_versions,
            server_version_observations=server_version_observations,
            qualified_server_version_observations=(qualified_server_version_observations),
            successful_generations=successful_generations,
            environment_qualified=environment_qualified,
        )
        is expected
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "extra",
        "malformed_pydantic",
        "malformed_httpx",
        "malformed_ollama",
        "model_digest",
    ],
)
def test_report_validator_rejects_missing_or_malformed_execution_environment(
    mutation: str,
) -> None:
    report = _blocked_report(
        paths=_source_paths(FIXTURES),
        reason_code="LLM_MODEL_NOT_READY",
        runtime_seconds=0.5,
    )
    tampered = deepcopy(report)
    environment = tampered["execution_environment"]
    if mutation == "missing":
        del tampered["execution_environment"]
    elif mutation == "extra":
        environment["packages"]["urllib3"] = "2.0.0"
    elif mutation == "malformed_pydantic":
        environment["packages"]["pydantic"] = "latest\nspoof"
    elif mutation == "malformed_httpx":
        environment["packages"]["httpx"] = "https://example.test/version"
    elif mutation == "malformed_ollama":
        environment["ollama"] = {
            "server_version": "../../spoof",
            "version_reason_code": None,
        }
    else:
        environment["model_digest"] = f"sha256:{'0' * 64}"

    with pytest.raises(ValueError):
        validate_aggregate_report(tampered)


def test_blocked_report_records_bounded_unavailable_ollama_version() -> None:
    report = _blocked_report(
        paths=_source_paths(FIXTURES),
        reason_code="LLM_MODEL_NOT_READY",
        runtime_seconds=0.5,
    )

    assert report["execution_environment"]["ollama"] == {
        "server_version": None,
        "version_reason_code": "LLM_MODEL_NOT_READY",
    }
    validate_aggregate_report(report)


def test_integrity_hash_is_identical_for_lf_and_crlf(tmp_path: Path) -> None:
    lf = tmp_path / "lf.py"
    crlf = tmp_path / "crlf.py"
    lf.write_bytes(b"line = 1\nother = 2\n")
    crlf.write_bytes(b"line = 1\r\nother = 2\r\n")

    assert _normalized_text_sha256(lf) == _normalized_text_sha256(crlf)


def test_source_attestation_covers_behavior_closure_without_unrelated_modules() -> None:
    attested = {
        path.relative_to(Path(__file__).resolve().parents[1]).as_posix()
        for path in _SOURCE_FILES.values()
    }
    assert {
        "llm/prompts.py",
        "llm/ollama_runtime.py",
        "llm/generation.py",
        "core/form_planning.py",
        "core/sensitive_policy.py",
        "profile/cv_facts.py",
        "profile/models.py",
        "profile/cv_content_cache.py",
        "jobs/models.py",
    }.issubset(attested)
    assert "api/routes/dashboard.py" not in attested


def test_source_integrity_invalidates_behavior_changes_not_unrelated_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import scripts.evaluate_v4_local_model_qualification as evaluator

    behavior = tmp_path / "behavior.py"
    unrelated = tmp_path / "unrelated.py"
    behavior.write_text("POLICY = 1\n", encoding="utf-8")
    unrelated.write_text("DASHBOARD = 1\n", encoding="utf-8")
    monkeypatch.setattr(evaluator, "ROOT", tmp_path)
    monkeypatch.setattr(evaluator, "_SOURCE_FILES", {"behavior": behavior})

    initial = evaluator._source_integrity_record()
    unrelated.write_text("DASHBOARD = 2\n", encoding="utf-8")
    assert evaluator._source_integrity_record() == initial
    behavior.write_text("POLICY = 2\n", encoding="utf-8")
    assert evaluator._source_integrity_record() != initial


@pytest.mark.parametrize(
    "key",
    [
        "answer",
        "cover_letter",
        "cv_text",
        "evidence",
        "job_title",
        "llm_output",
        "prompt",
        "question",
        "response",
    ],
)
def test_aggregate_report_validator_rejects_content_fields(key: str) -> None:
    with pytest.raises(ValueError, match="prohibited content"):
        validate_aggregate_report(
            {
                "schema_version": REPORT_SCHEMA_VERSION,
                key: "synthetic private content",
            }
        )


def test_committed_local_model_report_is_aggregate_only() -> None:
    assert DEFAULT_JSON_OUTPUT.exists(), "run the real local-model qualification"
    report = json.loads(DEFAULT_JSON_OUTPUT.read_text(encoding="utf-8"))

    validate_aggregate_report(report)
    assert report["schema_version"] == REPORT_SCHEMA_VERSION
    assert report["qualification_status"] == "passed"
    assert report["overall_pass"] is True
    assert report["evaluation_mode"]["real_local_model"] is True
    assert report["evaluation_mode"]["outputs_persisted"] is False
    assert "not independent human" in report["interpretation"]
    assert report["datasets"]["routing"]["cases"] == 120
    assert report["datasets"]["forms"]["cases"] == 240
    assert report["datasets"]["materials"]["cases"] == 40
    assert report["datasets"]["malformed"]["cases"] == 30

    tampered = deepcopy(report)
    total = tampered["inference"]["typed_invocations"]
    tampered["inference"]["purpose_counts"] = {"test": total}
    with pytest.raises(ValueError, match="purpose counts"):
        validate_aggregate_report(tampered)
