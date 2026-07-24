"""Write a measured offline CV-routing qualification report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from profile.cv_routing import load_routing_config  # noqa: E402
from profile.routing_evaluation import evaluate_dataset  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--dataset", default="tests/fixtures/cv_routing_evaluation.json"
    )
    parser.add_argument("--output", default="routing-evaluation-report.json")
    args = parser.parse_args()
    report = evaluate_dataset(args.dataset, load_routing_config(args.config))
    Path(args.output).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
