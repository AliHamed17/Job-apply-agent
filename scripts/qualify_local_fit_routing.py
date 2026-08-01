"""Create a private fit qualification bound to exact local CV bytes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from profile.cv_content_cache import load_configured_cv_artifacts  # noqa: E402
from profile.cv_routing import load_routing_config  # noqa: E402

from scripts.evaluate_v5_fit_routing import calibrate  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate a labeled local dataset and bind the resulting quality qualification "
            "to the exact routing config and CV hashes. This never grants submission authority."
        )
    )
    parser.add_argument("--config", default="cv_routing.yaml")
    parser.add_argument("--cv-directory", default="cvs")
    parser.add_argument(
        "--dataset",
        default="tests/fixtures/v5/fit_routing_240.json",
    )
    parser.add_argument("--output", default="fit_routing_qualification.json")
    parser.add_argument("--report", default="routing-evaluation-report.json")
    args = parser.parse_args()

    config = load_routing_config(args.config)
    artifacts = dict(load_configured_cv_artifacts(config, args.cv_directory))
    configured_ids = {cv.id for cv in config.cvs}
    if set(artifacts) != configured_ids:
        missing = len(configured_ids - set(artifacts))
        raise SystemExit(f"qualification refused: {missing} configured CV artifacts unavailable")
    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    report, qualification = calibrate(
        dataset,
        config,
        artifacts,
        scope="private local routing and CV manifest",
        runtime_note=(
            "This artifact enables only calibrated quality eligibility. A separately signed "
            "and unexpired policy is still required for any later submission authority."
        ),
    )

    Path(args.output).write_text(
        qualification.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    Path(args.report).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    result = "qualified" if qualification.qualified else "not-qualified"
    print(
        f"fit routing {result}: holdout precision "
        f"{qualification.holdout_precision:.3f}, coverage "
        f"{qualification.holdout_coverage:.3f}"
    )


if __name__ == "__main__":
    main()
