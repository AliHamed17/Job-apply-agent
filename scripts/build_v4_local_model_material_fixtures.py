"""Build 40 sanitized full-material tasks for local qwen qualification."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tests" / "fixtures" / "v4" / "local_model_full_material_40.json"

_FAMILIES: tuple[dict[str, Any], ...] = (
    {
        "family": "ai_ml",
        "title": "Machine Learning Engineer",
        "description": "Develop tested machine learning workflows using Python and PyTorch.",
        "evidence": (
            "Developed machine learning models with PyTorch.",
            "Built Python feature pipelines.",
            "Evaluated model performance with offline datasets.",
        ),
    },
    {
        "family": "data",
        "title": "Data Engineer",
        "description": "Build auditable data pipelines using Spark, dbt, and SQL.",
        "evidence": (
            "Built distributed data pipelines with Spark.",
            "Built data transformations with dbt.",
            "Designed auditable schemas in PostgreSQL.",
        ),
    },
    {
        "family": "software",
        "title": "Backend Software Engineer",
        "description": "Implement reliable Python services, APIs, and automated tests.",
        "evidence": (
            "Developed production services in Python.",
            "Implemented backend APIs with FastAPI.",
            "Wrote automated tests with Pytest.",
        ),
    },
    {
        "family": "qa",
        "title": "Quality Automation Engineer",
        "description": "Build browser automation and maintain reliable regression coverage.",
        "evidence": (
            "Automated browser tests with Selenium.",
            "Wrote regression suites with Pytest.",
            "Investigated reproducible software defects.",
        ),
    },
    {
        "family": "devops",
        "title": "DevOps Engineer",
        "description": "Operate cloud workloads using Kubernetes, Terraform, and AWS.",
        "evidence": (
            "Operated workloads on Kubernetes.",
            "Authored infrastructure modules with Terraform.",
            "Deployed services on AWS.",
        ),
    },
    {
        "family": "infrastructure",
        "title": "Infrastructure Engineer",
        "description": "Maintain observable Linux services and automated infrastructure.",
        "evidence": (
            "Operated production services on Linux.",
            "Automated infrastructure monitoring.",
            "Maintained reliable service configurations.",
        ),
    },
    {
        "family": "embedded",
        "title": "Embedded Software Engineer",
        "description": "Develop and test embedded C++ software for real-time systems.",
        "evidence": (
            "Developed embedded software in C++.",
            "Built real-time software with FreeRTOS.",
            "Tested device interfaces.",
        ),
    },
    {
        "family": "frontend",
        "title": "Frontend Software Engineer",
        "description": "Build tested web interfaces using TypeScript and React.",
        "evidence": (
            "Built user interfaces in TypeScript.",
            "Implemented frontend components with React.",
            "Wrote frontend tests.",
        ),
    },
    {
        "family": "analytics",
        "title": "Analytics Engineer",
        "description": "Create governed SQL models and decision-ready analytics views.",
        "evidence": (
            "Designed auditable schemas in PostgreSQL.",
            "Built SQL reports.",
            "Created analytics dashboards with Tableau.",
        ),
    },
    {
        "family": "junior",
        "title": "Junior Software Engineer",
        "description": "Contribute tested Python changes with version-control workflows.",
        "evidence": (
            "Completed software projects in Python.",
            "Managed source code with Git.",
            "Wrote automated tests with Pytest.",
        ),
    },
)

_VARIANT_EVIDENCE = (
    "Collaborated with engineering teams on documented software changes.",
    "Maintained version-controlled technical documentation.",
    "Reviewed automated test results before releases.",
    "Investigated production issues using structured logs.",
)


def build_material_cases() -> list[dict[str, Any]]:
    """Return stable generated inputs; no model outputs or private data."""

    cases: list[dict[str, Any]] = []
    for variant, extra_evidence in enumerate(_VARIANT_EVIDENCE, start=1):
        for family_index, family in enumerate(_FAMILIES, start=1):
            case_number = (variant - 1) * len(_FAMILIES) + family_index
            cases.append(
                {
                    "id": f"material-{case_number:03d}",
                    "family": family["family"],
                    "locale": "en",
                    "job": {
                        "title": family["title"],
                        "company": f"Synthetic Engineering Group {case_number:02d}",
                        "location": "Synthetic City",
                        "description": (
                            f"{family['description']} "
                            f"Scenario variant {variant} emphasizes documented review."
                        ),
                    },
                    "cv_lines": [*family["evidence"], extra_evidence],
                    "confirmed_facts": {
                        "notice_period": "Available after 30 days",
                        "salary_expectations": "Open to the approved role band",
                    },
                    "synthetic_label": "complete_non_sensitive_input",
                }
            )
    if len(cases) != 40 or len({case["id"] for case in cases}) != 40:
        raise AssertionError("local-model material fixture must contain 40 unique cases")
    return cases


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(
            build_material_cases(),
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"cases": 40, "output": OUTPUT.name}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
