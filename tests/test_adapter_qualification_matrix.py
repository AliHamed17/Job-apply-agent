from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from scripts.build_adapter_qualification_matrix import (
    JSON_OUTPUT,
    MARKDOWN_OUTPUT,
    QualificationMatrixError,
    build_matrix,
    render_json,
    render_markdown,
)

ROOT = Path(__file__).resolve().parents[1]


def test_generated_adapter_matrix_is_current_and_fixture_only() -> None:
    matrix = build_matrix(ROOT)

    assert matrix["totals"] == {
        "adapters": 5,
        "final_executors": 0,
        "live_canaries_completed": 0,
        "qualified_form_scopes": 0,
        "real_url_dry_runs_completed": 0,
        "sanitized_fixtures": 87,
    }
    assert [(row["adapter"], row["fixture_count"]) for row in matrix["adapters"]] == [
        ("workday", 9),
        ("greenhouse", 22),
        ("lever", 28),
        ("ashby", 13),
        ("smartrecruiters", 15),
    ]
    assert all(row["achieved_tier"] == "fixture_qualified" for row in matrix["adapters"])
    assert all(row["qualified_form_scope_count"] == 0 for row in matrix["adapters"])
    assert all(row["final_external_action_enabled"] is False for row in matrix["adapters"])
    assert JSON_OUTPUT.read_text(encoding="utf-8") == render_json(matrix)
    assert MARKDOWN_OUTPUT.read_text(encoding="utf-8") == render_markdown(matrix)


def test_matrix_rejects_elevated_report_claim(tmp_path: Path) -> None:
    qualification_dir = tmp_path / "docs" / "qualification"
    qualification_dir.mkdir(parents=True)
    source_dir = ROOT / "docs" / "qualification"
    for source in source_dir.glob("*-browser-v*.json"):
        target = qualification_dir / source.name
        target.write_bytes(source.read_bytes())
        markdown = source.with_suffix(".md")
        (qualification_dir / markdown.name).write_bytes(markdown.read_bytes())

    report_path = qualification_dir / "workday-browser-v2.json"
    report = __import__("json").loads(report_path.read_text(encoding="utf-8"))
    elevated = deepcopy(report)
    elevated["qualification_gates"]["final_external_action_enabled"] = True
    report_path.write_text(
        __import__("json").dumps(elevated),
        encoding="utf-8",
    )

    with pytest.raises(QualificationMatrixError, match="unsafe qualification gates"):
        build_matrix(tmp_path)


def test_matrix_rejects_stale_markdown_pair(tmp_path: Path) -> None:
    qualification_dir = tmp_path / "docs" / "qualification"
    qualification_dir.mkdir(parents=True)
    source_dir = ROOT / "docs" / "qualification"
    for source in source_dir.glob("*-browser-v*.json"):
        (qualification_dir / source.name).write_bytes(source.read_bytes())
        markdown = source.with_suffix(".md")
        (qualification_dir / markdown.name).write_bytes(markdown.read_bytes())
    (qualification_dir / "lever-browser-v1.md").write_text(
        "fixture_qualified\n",
        encoding="utf-8",
    )

    with pytest.raises(QualificationMatrixError, match="stale qualification summary"):
        build_matrix(tmp_path)
