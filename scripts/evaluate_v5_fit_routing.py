"""Evaluate and report deterministic v5 routing/fit qualification."""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from profile.cv_routing import CVRoutingConfig, load_routing_config  # noqa: E402
from profile.models import (  # noqa: E402
    CVArtifact,
    Personal,
    Preferences,
    ProfileEvidence,
    SelectedCVArtifact,
    UserProfile,
)

from discovery.contracts import stable_digest  # noqa: E402
from jobs.models import JobData  # noqa: E402
from match.job_fit import (  # noqa: E402
    FitQualificationV1,
    FitThresholdsV1,
    cv_manifest_digest,
    evaluate_job_fit,
    routing_config_digest,
)

CONFIG_PATH = ROOT / "tests" / "fixtures" / "v5" / "fit_routing_config_12.yaml"
DATASET_PATH = ROOT / "tests" / "fixtures" / "v5" / "fit_routing_240.json"
REPORT_JSON = ROOT / "docs" / "qualification" / "v5-fit-routing.json"
REPORT_MD = ROOT / "docs" / "qualification" / "v5-fit-routing.md"


def _artifacts(config: CVRoutingConfig) -> dict[str, SelectedCVArtifact]:
    artifacts: dict[str, SelectedCVArtifact] = {}
    for cv in config.cvs:
        content = f"sanitized qualification artifact:{cv.id}".encode()
        artifacts[cv.id] = SelectedCVArtifact(
            cv_id=cv.id,
            resolved_path=f"C:/sanitized-fixtures/{cv.file}",
            artifact=CVArtifact(
                pdf_sha256=hashlib.sha256(content).hexdigest(),
                byte_size=len(content),
                extracted_text=content.decode(),
            ),
        )
    return artifacts


def _profile(case: dict) -> UserProfile:
    return UserProfile(
        personal=Personal(location="Tel Aviv, Israel"),
        preferences=Preferences(
            roles=[case["family"]],
            locations=["Israel", "Worldwide Remote"],
            remote_ok=True,
            hybrid_ok=True,
            onsite_ok=True,
        ),
        evidence=ProfileEvidence(
            user_confirmed=dict(case["confirmed_profile_facts"]),
        ),
    )


def _qualification(
    config: CVRoutingConfig,
    artifacts: dict[str, SelectedCVArtifact],
    dataset_digest: str,
    thresholds: FitThresholdsV1,
    *,
    holdout_precision: float = 1.0,
    holdout_coverage: float = 1.0,
    qualified: bool = True,
    labeled_cases: int = 240,
    holdout_cases: int = 48,
) -> FitQualificationV1:
    return FitQualificationV1(
        routing_config_digest=routing_config_digest(config),
        cv_manifest_digest=cv_manifest_digest(artifacts),
        dataset_digest=dataset_digest,
        thresholds=thresholds,
        labeled_cases=labeled_cases,
        holdout_cases=holdout_cases,
        holdout_precision=holdout_precision,
        holdout_coverage=holdout_coverage,
        qualified=qualified,
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )


def _evaluate_cases(
    cases: list[dict],
    *,
    config: CVRoutingConfig,
    artifacts: dict[str, SelectedCVArtifact],
    dataset_digest: str,
    thresholds: FitThresholdsV1,
) -> dict:
    qualification = _qualification(config, artifacts, dataset_digest, thresholds)
    auto_predictions = 0
    auto_correct = 0
    routing_predictions = 0
    routing_correct = 0
    fit_predictions = 0
    fit_correct = 0
    needs_review = 0
    unsupported = 0
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    disposition_confusion: dict[str, Counter[str]] = defaultdict(Counter)

    for case in cases:
        decision = evaluate_job_fit(
            JobData.model_validate(case["job"]),
            _profile(case),
            profile_version=1,
            routing_config=config,
            artifacts=artifacts,
            qualification=qualification,
        )
        expected_cv = case["expected_cv_id"]
        selected = decision.selected_cv_id or "abstained"
        confusion[expected_cv][selected] += 1
        disposition_confusion[case["expected_disposition"]][decision.disposition.value] += 1
        if decision.disposition.value == "needs_review":
            needs_review += 1
        if decision.unsupported_required_skills or any(
            reason.endswith("_EVIDENCE_MISSING") for reason in decision.uncertainty
        ):
            unsupported += 1

        high_confidence_route = bool(
            decision.selected_cv_id
            and decision.routing_fallback_reason is None
            and decision.routing_confidence >= thresholds.minimum_routing_confidence
            and decision.routing_margin >= thresholds.minimum_routing_margin
        )
        if high_confidence_route:
            routing_predictions += 1
            routing_correct += int(decision.selected_cv_id == expected_cv)
        if decision.disposition.value == "eligible":
            fit_predictions += 1
            fit_correct += int(case["expected_disposition"] == "eligible")
        if decision.quality_eligible:
            auto_predictions += 1
            auto_correct += int(
                case["expected_quality_eligible"] and decision.selected_cv_id == expected_cv
            )

    total = len(cases)

    def ratio(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 6) if denominator else 0.0

    return {
        "cases": total,
        "auto_eligible_predictions": auto_predictions,
        "auto_eligible_precision": ratio(auto_correct, auto_predictions),
        "auto_eligible_coverage": ratio(auto_predictions, total),
        "routing_predictions": routing_predictions,
        "routing_precision": ratio(routing_correct, routing_predictions),
        "fit_eligible_predictions": fit_predictions,
        "fit_precision": ratio(fit_correct, fit_predictions),
        "abstention_rate": ratio(needs_review, total),
        "unsupported_required_field_rate": ratio(unsupported, total),
        "cv_selection_confusion_matrix": {
            expected: dict(sorted(observed.items()))
            for expected, observed in sorted(confusion.items())
        },
        "fit_disposition_confusion_matrix": {
            expected: dict(sorted(observed.items()))
            for expected, observed in sorted(disposition_confusion.items())
        },
    }


def calibrate(
    dataset: dict,
    config: CVRoutingConfig,
    artifacts: dict[str, SelectedCVArtifact],
    *,
    scope: str,
    runtime_note: str,
) -> tuple[dict, FitQualificationV1]:
    """Select thresholds on train cases, then evaluate once on held-out cases."""

    cases = dataset.get("cases")
    families = dataset.get("families")
    if not isinstance(cases, list) or len(cases) < 240:
        raise ValueError("FIT_DATASET_REQUIRES_AT_LEAST_240_CASES")
    if not isinstance(families, list) or len(set(families)) < 12:
        raise ValueError("FIT_DATASET_REQUIRES_AT_LEAST_12_FAMILIES")
    configured_ids = {cv.id for cv in config.cvs}
    expected_ids = {str(case.get("expected_cv_id") or "") for case in cases}
    if expected_ids != configured_ids or set(families) != configured_ids:
        raise ValueError("FIT_DATASET_CV_FAMILIES_DO_NOT_MATCH_CONFIG")

    dataset_digest = stable_digest(dataset)
    train = [case for case in cases if case["split"] == "train"]
    holdout = [case for case in cases if case["split"] == "holdout"]
    if len(holdout) < 48:
        raise ValueError("FIT_DATASET_REQUIRES_AT_LEAST_48_HOLDOUT_CASES")

    selected: tuple[FitThresholdsV1, dict] | None = None
    candidates: list[dict] = []
    for fit_score in (85.0, 88.0, 90.0):
        for confidence in (0.55, 0.6, 0.65, 0.7, 0.75):
            for margin in (0.08, 0.12, 0.2):
                thresholds = FitThresholdsV1(
                    minimum_fit_score=fit_score,
                    minimum_routing_confidence=confidence,
                    minimum_routing_margin=margin,
                )
                metrics = _evaluate_cases(
                    train,
                    config=config,
                    artifacts=artifacts,
                    dataset_digest=dataset_digest,
                    thresholds=thresholds,
                )
                candidates.append(
                    {
                        "thresholds": thresholds.model_dump(mode="json"),
                        "precision": metrics["auto_eligible_precision"],
                        "coverage": metrics["auto_eligible_coverage"],
                    }
                )
                meets_gate = (
                    metrics["auto_eligible_precision"] >= 0.95
                    and metrics["routing_precision"] >= 0.95
                    and metrics["fit_precision"] >= 0.95
                )
                if meets_gate and (
                    selected is None
                    or metrics["auto_eligible_coverage"] > selected[1]["auto_eligible_coverage"]
                ):
                    selected = (thresholds, metrics)

    thresholds = selected[0] if selected is not None else FitThresholdsV1()
    train_metrics = (
        selected[1]
        if selected is not None
        else _evaluate_cases(
            train,
            config=config,
            artifacts=artifacts,
            dataset_digest=dataset_digest,
            thresholds=thresholds,
        )
    )
    holdout_metrics = _evaluate_cases(
        holdout,
        config=config,
        artifacts=artifacts,
        dataset_digest=dataset_digest,
        thresholds=thresholds,
    )
    qualified = bool(
        selected is not None
        and holdout_metrics["auto_eligible_precision"] >= 0.95
        and holdout_metrics["routing_precision"] >= 0.95
        and holdout_metrics["fit_precision"] >= 0.95
    )
    qualification = _qualification(
        config,
        artifacts,
        dataset_digest,
        thresholds,
        holdout_precision=holdout_metrics["auto_eligible_precision"],
        holdout_coverage=holdout_metrics["auto_eligible_coverage"],
        qualified=qualified,
        labeled_cases=len(cases),
        holdout_cases=len(holdout),
    )
    report = {
        "schema_version": "fit-routing-report.v1",
        "scope": scope,
        "runtime_authority": False,
        "runtime_note": runtime_note,
        "dataset": {
            "digest": dataset_digest,
            "cases": len(cases),
            "train_cases": len(train),
            "holdout_cases": len(holdout),
            "families": dataset["families"],
            "languages": sorted({case["language"] for case in dataset["cases"]}),
        },
        "selected_thresholds": thresholds.model_dump(mode="json"),
        "train": train_metrics,
        "holdout": holdout_metrics,
        "qualified": qualified,
        "qualification": qualification.model_dump(mode="json"),
        "candidate_count": len(candidates),
    }
    return report, qualification


def evaluate() -> tuple[dict, FitQualificationV1]:
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    config = load_routing_config(CONFIG_PATH)
    artifacts = _artifacts(config)
    return calibrate(
        dataset,
        config,
        artifacts,
        scope="sanitized synthetic fixture only",
        runtime_note=(
            "Personal autopilot remains disabled until a private qualification is bound "
            "to the exact local CV hashes and routing configuration."
        ),
    )


def _markdown(report: dict) -> str:
    holdout = report["holdout"]
    thresholds = report["selected_thresholds"]
    status = "PASS" if report["qualified"] else "FAIL"
    cases = report["dataset"]["cases"]
    train_cases = report["dataset"]["train_cases"]
    holdout_cases = report["dataset"]["holdout_cases"]
    return f"""# v5 Fit and Routing Qualification

Status: **{status}** for the sanitized synthetic fixture only.

This report grants no runtime or submission authority. A private local qualification must
match the exact personal CV hashes and routing configuration before quality eligibility can
be consumed by a later, separately signed autopilot policy.

- Cases: {cases} ({train_cases} train, {holdout_cases} held out)
- CV families: {len(report["dataset"]["families"])}
- Languages: {", ".join(report["dataset"]["languages"])}
- Minimum fit score: {thresholds["minimum_fit_score"]}
- Minimum routing confidence: {thresholds["minimum_routing_confidence"]}
- Minimum routing margin: {thresholds["minimum_routing_margin"]}
- Held-out auto-eligible precision: {holdout["auto_eligible_precision"]:.3f}
- Held-out coverage: {holdout["auto_eligible_coverage"]:.3f}
- Held-out routing precision: {holdout["routing_precision"]:.3f}
- Held-out fit precision: {holdout["fit_precision"]:.3f}
- Held-out abstention rate: {holdout["abstention_rate"]:.3f}
- Unsupported required-field rate: {holdout["unsupported_required_field_rate"]:.3f}

The machine-readable report contains the CV-selection and fit-disposition confusion matrices.
"""


def main() -> None:
    report, _ = evaluate()
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    REPORT_MD.write_text(_markdown(report), encoding="utf-8")


if __name__ == "__main__":
    main()
