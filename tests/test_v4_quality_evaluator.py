from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.evaluate_v4_quality import (
    DEFAULT_FIXTURES,
    DEFAULT_JSON_OUTPUT,
    DEFAULT_MARKDOWN_OUTPUT,
    HIGH_CONFIDENCE_THRESHOLD,
    MAX_FIXTURE_STRING_CHARS,
    _normalized_text_sha256,
    evaluate_quality,
    render_markdown,
)


@pytest.mark.asyncio
async def test_quality_baseline_has_exact_case_counts_and_separate_thresholds() -> None:
    report = await evaluate_quality(DEFAULT_FIXTURES)

    assert {name: dataset["cases"] for name, dataset in report["datasets"].items()} == {
        "cv_routing": 120,
        "form_resolution": 240,
        "claim_evidence": 40,
        "malformed_output": 30,
    }
    assert set(report["thresholds"]) == {
        "dataset_case_counts",
        "routing_high_confidence_precision",
        "form_non_sensitive_precision",
        "form_unsafe_eligibility",
        "claim_unsafe_eligibility",
        "malformed_fail_closed",
    }
    assert all(gate["passed"] for gate in report["thresholds"].values())
    assert report["overall_pass"] is True


@pytest.mark.asyncio
async def test_precision_abstention_and_fail_closed_safety_counts() -> None:
    tasks = (await evaluate_quality(DEFAULT_FIXTURES))["tasks"]

    routing = tasks["cv_routing"]
    assert routing["high_confidence_threshold"] == HIGH_CONFIDENCE_THRESHOLD
    assert routing["high_confidence_precision"] >= 0.95
    assert routing["high_confidence_coverage"] < 1.0
    assert routing["coverage"] < 1.0
    assert routing["abstention_rate"] > 0.0
    assert routing["confusion_counts"]["high_confidence"]["incorrect"] == 0
    assert routing["confusion_counts"]["expected_abstained"]["abstained"] > 0
    assert routing["confusion_counts"]["expected_abstained"]["selected"] > 0

    forms = tasks["form_resolution"]
    assert forms["non_sensitive_precision"] >= 0.95
    assert forms["overall_exact_accuracy"] == 1.0
    assert forms["typed_local_cases"] == 80
    assert forms["typed_local_cases_exercised"] == 80
    assert forms["sensitive_llm_calls"] == 0
    assert forms["sensitive_automatic_eligible"] == 0
    assert forms["unsupported_eligible"] == 0
    assert forms["contract_mismatches"] == 0

    claims = tasks["claim_evidence"]
    assert claims["precision"] == 1.0
    assert claims["unsupported_or_sensitive_eligible"] == 0
    assert claims["blocker_mismatches"] == 0
    assert len(claims["blocker_counts"]) >= 5
    assert claims["confusion_counts"] == {
        "true_eligible": 20,
        "false_eligible": 0,
        "true_blocked": 20,
        "false_blocked": 0,
    }

    malformed = tasks["malformed_output"]
    assert malformed["precision"] == 1.0
    assert malformed["coverage"] == 0.0
    assert malformed["eligible_for_preparation"] == 0
    assert malformed["semantic_prompt_injection_cases"] == 18
    assert malformed["semantic_prompt_injections_blocked"] == 18
    assert malformed["boundary_counts"] == {
        "form": 10,
        "routing": 10,
        "material": 10,
    }
    assert malformed["confusion_counts"] == {
        "correctly_blocked": 30,
        "incorrectly_accepted_or_misclassified": 0,
        "typed_rejected": 12,
        "semantic_blocked": 18,
    }


@pytest.mark.asyncio
async def test_evaluator_makes_no_network_or_ollama_call() -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("offline evaluator attempted network/model access")

    with (
        patch("socket.socket.connect", side_effect=forbidden),
        patch("httpx.Client", side_effect=forbidden),
        patch("httpx.AsyncClient", side_effect=forbidden),
    ):
        report = await evaluate_quality(DEFAULT_FIXTURES)

    assert report["evaluation_mode"] == {
        "offline": True,
        "deterministic": True,
        "network_calls": 0,
        "ollama_calls": 0,
        "typed_fixture_provider": True,
        "private_data_used": False,
    }


@pytest.mark.asyncio
async def test_committed_baseline_exactly_matches_fresh_evaluation() -> None:
    report = await evaluate_quality(DEFAULT_FIXTURES)
    committed = json.loads(DEFAULT_JSON_OUTPUT.read_text(encoding="utf-8"))

    assert committed == report
    assert DEFAULT_MARKDOWN_OUTPUT.read_text(encoding="utf-8") == render_markdown(report)
    assert DEFAULT_MARKDOWN_OUTPUT.read_text(encoding="utf-8") == render_markdown(committed)


def test_dataset_digest_is_stable_across_line_endings(tmp_path: Path) -> None:
    lf = tmp_path / "lf.json"
    crlf = tmp_path / "crlf.json"
    lf.write_bytes(b'{\n  "value": true\n}\n')
    crlf.write_bytes(b'{\r\n  "value": true\r\n}\r\n')

    assert _normalized_text_sha256(lf) == _normalized_text_sha256(crlf)


def _copied_fixtures(tmp_path: Path) -> Path:
    target = tmp_path / "fixtures"
    shutil.copytree(DEFAULT_FIXTURES, target)
    return target


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("injected", "error"),
    (
        ("Contact private.person@corp.example.com", "non-synthetic email"),
        ("Call +972501234567 for details", "non-synthetic phone"),
        ("Open https://corp.example.com/private", "non-synthetic URL"),
        ("x" * (MAX_FIXTURE_STRING_CHARS + 1), "oversized string"),
    ),
)
async def test_fixture_reader_rejects_private_or_unbounded_strings(
    tmp_path: Path,
    injected: str,
    error: str,
) -> None:
    fixtures = _copied_fixtures(tmp_path)
    path = fixtures / "cv_routing_120.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    rows[0]["job"]["description"] = injected
    path.write_text(json.dumps(rows), encoding="utf-8")

    with pytest.raises(ValueError, match=error):
        await evaluate_quality(fixtures)


@pytest.mark.asyncio
async def test_fixture_controlled_aggregate_key_is_rejected(tmp_path: Path) -> None:
    fixtures = _copied_fixtures(tmp_path)
    path = fixtures / "cv_routing_120.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    rows[0]["category"] = "private.person@corp.example.com"
    path.write_text(json.dumps(rows), encoding="utf-8")

    with pytest.raises(ValueError, match="non-synthetic email"):
        await evaluate_quality(fixtures)


def test_baseline_artifacts_are_aggregate_only_and_evidence_honest() -> None:
    content = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in (DEFAULT_JSON_OUTPUT, DEFAULT_MARKDOWN_OUTPUT)
    )
    forbidden = (
        "private.user.marker",
        "real-employer-marker",
        "real-ats.example/private",
        "social.example/private-profile",
        "private-company-marker",
        "route-001",
        "identity-email-en-1",
        "claim-supported-python_backend",
        "candidate@example.test",
        "+10000000000",
        "https://example.test/profile",
    )

    assert not any(value in content for value in forbidden)
    assert _EMAIL_PATTERN.search(content) is None
    assert _PHONE_PATTERN.search(content) is None
    assert "http://" not in content
    assert "https://" not in content
    assert "no improvement" in content
    assert "not independent labels" in content
    assert "real-world generalization claim" in content
    assert Path(DEFAULT_JSON_OUTPUT).is_file()
    assert Path(DEFAULT_MARKDOWN_OUTPUT).is_file()


_EMAIL_PATTERN = re.compile(r"\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
_PHONE_PATTERN = re.compile(r"(?<!\w)\+?\d[\d ()-]{7,}\d(?!\w)")
