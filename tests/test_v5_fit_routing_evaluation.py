from __future__ import annotations

import json
from pathlib import Path

from scripts.build_v5_fit_routing_dataset import build_dataset
from scripts.evaluate_v5_fit_routing import evaluate

ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "tests" / "fixtures" / "v5" / "fit_routing_240.json"
REPORT_PATH = ROOT / "docs" / "qualification" / "v5-fit-routing.json"


def test_dataset_is_deterministic_bilingual_and_spans_all_twelve_cv_families():
    committed = json.loads(DATASET_PATH.read_text(encoding="utf-8"))

    assert committed == build_dataset()
    assert len(committed["cases"]) == 240
    assert len(committed["families"]) == 12
    assert len(set(committed["families"])) == 12
    assert {case["language"] for case in committed["cases"]} == {"en", "he"}
    assert sum(case["split"] == "holdout" for case in committed["cases"]) == 48
    for family in committed["families"]:
        family_cases = [case for case in committed["cases"] if case["family"] == family]
        assert len(family_cases) == 20
        assert {case["language"] for case in family_cases} == {"en", "he"}
        assert {case["expected_disposition"] for case in family_cases} == {
            "eligible",
            "excluded",
            "needs_review",
        }


def test_committed_report_matches_evaluator_and_meets_precision_gate_only_for_fixture():
    first, qualification = evaluate()
    second, _ = evaluate()
    committed = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    assert first == second == committed
    assert committed["qualified"] is True
    assert committed["runtime_authority"] is False
    assert committed["dataset"]["holdout_cases"] == 48
    assert committed["holdout"]["auto_eligible_precision"] >= 0.95
    assert committed["holdout"]["routing_precision"] >= 0.95
    assert committed["holdout"]["fit_precision"] >= 0.95
    assert committed["holdout"]["abstention_rate"] > 0
    assert len(committed["holdout"]["cv_selection_confusion_matrix"]) == 12
    assert qualification.qualified is True
    assert qualification.thresholds.minimum_fit_score >= 85
