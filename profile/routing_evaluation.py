"""Offline CV-routing and answer-provenance evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from profile.cv_routing import CVRoutingConfig, RoutingJob, route_cv
from typing import Any


def evaluate_dataset(
    dataset_path: str | Path, config: CVRoutingConfig
) -> dict[str, Any]:
    cases = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    correct = 0
    abstained = 0
    unsupported_required = 0
    provenance_counts: dict[str, int] = {}
    results = []
    for case in cases:
        decision = route_cv(RoutingJob.model_validate(case["job"]), config)
        expected = case["expected_cv_id"]
        is_abstained = decision.selected_cv_id is None
        correct += int(decision.selected_cv_id == expected)
        abstained += int(is_abstained)
        unsupported_required += int(case.get("unsupported_required_field", False))
        provenance = case.get("answer_provenance", "not_applicable")
        provenance_counts[provenance] = provenance_counts.get(provenance, 0) + 1
        results.append(
            {
                "id": case["id"],
                "category": case["category"],
                "expected_cv_id": expected,
                "selected_cv_id": decision.selected_cv_id,
                "confidence": decision.confidence,
                "correct": decision.selected_cv_id == expected,
                "abstained": is_abstained,
            }
        )
    count = len(cases)
    return {
        "dataset_size": count,
        "routing_accuracy": correct / count if count else 0,
        "abstention_rate": abstained / count if count else 0,
        "unsupported_required_field_rate": unsupported_required / count if count else 0,
        "answer_provenance": provenance_counts,
        "results": results,
        "claim": "Measured results only; no automatic profile or model changes were made.",
    }
