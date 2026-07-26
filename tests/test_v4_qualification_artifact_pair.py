from __future__ import annotations

import json
import sys
from pathlib import Path

from scripts.evaluate_v4_local_model_qualification import (
    FIXTURES,
    _blocked_report,
    _source_paths,
    write_report,
)
from scripts.evaluate_v4_local_model_qualification import (
    main as qualification_main,
)


def _write_valid_pair(tmp_path: Path) -> tuple[Path, Path]:
    report = _blocked_report(
        paths=_source_paths(FIXTURES),
        reason_code="LLM_MODEL_NOT_READY",
        runtime_seconds=0.5,
    )
    json_path = tmp_path / "qualification.json"
    markdown_path = tmp_path / "qualification.md"
    write_report(
        report,
        json_output=json_path,
        markdown_output=markdown_path,
    )
    return json_path, markdown_path


def _validate_report(
    json_path: Path,
    *,
    monkeypatch,
    capsys,
) -> tuple[int, dict[str, object]]:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate_v4_local_model_qualification.py",
            "--validate-report",
            str(json_path),
        ],
    )
    return qualification_main(), json.loads(capsys.readouterr().out)


def test_validate_report_accepts_exact_json_markdown_pair(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    json_path, _ = _write_valid_pair(tmp_path)

    exit_code, diagnostic = _validate_report(
        json_path,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )

    assert exit_code == 0
    assert diagnostic == {
        "qualification_status": "blocked",
        "report_valid": True,
    }


def test_validate_report_rejects_missing_sibling_markdown(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    json_path, markdown_path = _write_valid_pair(tmp_path)
    markdown_path.unlink()

    exit_code, diagnostic = _validate_report(
        json_path,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )

    assert exit_code == 1
    assert diagnostic == {
        "failure_stage": "aggregate_validation",
        "qualification_status": "blocked",
        "reason_code": "REPORT_VALIDATION_FAILED",
    }


def test_validate_report_rejects_mismatched_sibling_markdown(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    json_path, markdown_path = _write_valid_pair(tmp_path)
    markdown_path.write_text("stale qualification summary\n", encoding="utf-8")

    exit_code, diagnostic = _validate_report(
        json_path,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )

    assert exit_code == 1
    assert diagnostic == {
        "failure_stage": "aggregate_validation",
        "qualification_status": "blocked",
        "reason_code": "REPORT_VALIDATION_FAILED",
    }


def test_validate_report_rejects_non_utf8_sibling_markdown(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    json_path, markdown_path = _write_valid_pair(tmp_path)
    markdown_path.write_bytes(b"\xff\xfe")

    exit_code, diagnostic = _validate_report(
        json_path,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )

    assert exit_code == 1
    assert diagnostic == {
        "failure_stage": "aggregate_validation",
        "qualification_status": "blocked",
        "reason_code": "REPORT_VALIDATION_FAILED",
    }
