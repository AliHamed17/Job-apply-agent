"""Build the deterministic, fixture-only first-five ATS qualification matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from submitters.platforms import QualificationTier, registered_adapters  # noqa: E402

QUALIFICATION_DIR = ROOT / "docs" / "qualification"
JSON_OUTPUT = QUALIFICATION_DIR / "adapter-matrix.json"
MARKDOWN_OUTPUT = QUALIFICATION_DIR / "adapter-matrix.md"
REPORT_STEMS = (
    "workday-browser-v2",
    "greenhouse-browser-v1",
    "lever-browser-v1",
    "ashby-browser-v1",
    "smartrecruiters-browser-v1",
)
EXPECTED_SCHEMA = "ats-browser-qualification-report-v1"
MATRIX_SCHEMA = "ats-adapter-qualification-matrix-v1"


class QualificationMatrixError(ValueError):
    """Raised when source evidence cannot support the aggregate matrix."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QualificationMatrixError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise QualificationMatrixError(f"{label} must be an object")
    return value


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _load_report(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                QualificationMatrixError(f"non-finite JSON number: {token}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QualificationMatrixError(f"invalid qualification report: {path.name}") from exc
    return _require_mapping(value, path.name)


def _validate_markdown_pair(path: Path, *, fixture_count: int) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise QualificationMatrixError(f"invalid qualification summary: {path.name}") from exc
    required = (
        "fixture_qualified",
        "Real-URL dry run: pending",
        "Live canary: pending",
        "Final external action: disabled",
    )
    if any(marker not in text for marker in required):
        raise QualificationMatrixError(f"stale qualification summary: {path.name}")
    fixture_words = {
        9: "Nine",
        13: "Thirteen",
        15: "Fifteen",
        22: "Twenty-two",
        28: "Twenty-eight",
    }
    expected_word = fixture_words.get(fixture_count)
    if expected_word is None or expected_word.casefold() not in text.casefold():
        raise QualificationMatrixError(f"fixture count missing from summary: {path.name}")


def build_matrix(root: Path = ROOT) -> dict[str, object]:
    """Return a validated aggregate derived only from committed report pairs."""

    qualification_dir = root / "docs" / "qualification"
    descriptors = {item.platform: item for item in registered_adapters()}
    adapters: list[dict[str, object]] = []

    for stem in REPORT_STEMS:
        json_path = qualification_dir / f"{stem}.json"
        markdown_path = qualification_dir / f"{stem}.md"
        report = _load_report(json_path)
        adapter = _require_mapping(report.get("adapter"), f"{stem}.adapter")
        gates = _require_mapping(
            report.get("qualification_gates"),
            f"{stem}.qualification_gates",
        )
        evidence = _require_mapping(
            report.get("fixture_evidence"),
            f"{stem}.fixture_evidence",
        )
        safety = _require_mapping(
            report.get("safety_observations"),
            f"{stem}.safety_observations",
        )

        platform = str(adapter.get("adapter_name") or "")
        descriptor = descriptors.get(platform)
        if descriptor is None:
            raise QualificationMatrixError(f"unregistered adapter: {platform}")
        expected_adapter = {
            "adapter_name": descriptor.platform,
            "adapter_version": descriptor.adapter_version,
            "execution_contract_version": descriptor.execution_contract_version,
            "selector_version": descriptor.selector_version,
            "transport": descriptor.transport,
        }
        if dict(adapter) != expected_adapter:
            raise QualificationMatrixError(f"registry/report mismatch: {stem}")
        if report.get("schema_version") != EXPECTED_SCHEMA:
            raise QualificationMatrixError(f"unsupported report schema: {stem}")
        if (
            descriptor.qualification is not QualificationTier.FIXTURE_QUALIFIED
            or report.get("achieved_tier") != QualificationTier.FIXTURE_QUALIFIED.value
        ):
            raise QualificationMatrixError(f"non-fixture qualification claim: {stem}")

        expected_gates = {
            "final_external_action_enabled": False,
            "fixture_contract": "passed",
            "live_canary": "pending",
            "qualified_form_scope": [],
            "real_url_dry_run": "pending",
        }
        if dict(gates) != expected_gates or descriptor.qualified_form_scope:
            raise QualificationMatrixError(f"unsafe qualification gates: {stem}")
        for flag in (
            "external_network_used",
            "final_action_performed",
            "private_data_used",
            "real_application_used",
        ):
            if safety.get(flag) is not False:
                raise QualificationMatrixError(f"unsafe evidence flag {flag}: {stem}")

        cases = evidence.get("cases")
        fixture_count = evidence.get("fixture_count")
        fixture_digest = evidence.get("fixture_digest")
        if (
            not isinstance(cases, list)
            or not isinstance(fixture_count, int)
            or fixture_count <= 0
            or fixture_count != len(cases)
            or not isinstance(fixture_digest, str)
            or len(fixture_digest) != 64
            or any(character not in "0123456789abcdef" for character in fixture_digest)
        ):
            raise QualificationMatrixError(f"invalid fixture evidence: {stem}")
        _validate_markdown_pair(markdown_path, fixture_count=fixture_count)

        adapters.append(
            {
                "adapter": platform,
                "adapter_version": descriptor.adapter_version,
                "achieved_tier": descriptor.qualification.value,
                "execution_contract_version": descriptor.execution_contract_version,
                "final_external_action_enabled": descriptor.allows_final_execution,
                "fixture_count": fixture_count,
                "fixture_digest": fixture_digest,
                "live_canary_completed": gates["live_canary"] == "passed",
                "qualified_form_scope_count": len(descriptor.qualified_form_scope),
                "real_url_dry_run_completed": gates["real_url_dry_run"] == "passed",
                "recorded_on": str(report.get("recorded_on") or ""),
                "selector_version": descriptor.selector_version,
                "source_json": json_path.relative_to(root).as_posix(),
                "source_markdown": markdown_path.relative_to(root).as_posix(),
                "source_report_digest": _canonical_digest(report),
                "transport": descriptor.transport,
            }
        )

    totals = {
        "adapters": len(adapters),
        "final_executors": sum(bool(item["final_external_action_enabled"]) for item in adapters),
        "live_canaries_completed": sum(bool(item["live_canary_completed"]) for item in adapters),
        "qualified_form_scopes": sum(
            cast(int, item["qualified_form_scope_count"]) for item in adapters
        ),
        "real_url_dry_runs_completed": sum(
            bool(item["real_url_dry_run_completed"]) for item in adapters
        ),
        "sanitized_fixtures": sum(cast(int, item["fixture_count"]) for item in adapters),
    }
    return {
        "adapters": adapters,
        "interpretation": (
            "Offline sanitized fixture qualification only. No real URL, live canary, "
            "qualified form scope, or final executor is represented."
        ),
        "schema_version": MATRIX_SCHEMA,
        "totals": totals,
    }


def render_json(matrix: Mapping[str, object]) -> str:
    """Render stable UTF-8 JSON with no wall-clock field."""

    return (
        json.dumps(
            matrix,
            ensure_ascii=True,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def render_markdown(matrix: Mapping[str, object]) -> str:
    """Render the human-readable view from the same validated aggregate."""

    totals = _require_mapping(matrix.get("totals"), "matrix.totals")
    adapters = matrix.get("adapters")
    if not isinstance(adapters, list):
        raise QualificationMatrixError("matrix.adapters must be an array")
    lines = [
        "# First-five ATS qualification matrix",
        "",
        "This matrix is generated from the five committed sanitized report pairs and",
        "the central adapter registry. It records offline fixture qualification only.",
        "",
        "| Adapter | Version | Selector | Tier | Fixtures | Real URL dry run | "
        "Live canary | Qualified scopes | Final executor |",
        "|---|---:|---|---|---:|---|---|---:|---|",
    ]
    for item_value in adapters:
        item = _require_mapping(item_value, "matrix.adapter")
        lines.append(
            "| {adapter} | {adapter_version} | {selector_version} | "
            "`{achieved_tier}` | {fixture_count} | {dry_run} | {canary} | "
            "{qualified_form_scope_count} | {executor} |".format(
                **item,
                dry_run="completed" if item["real_url_dry_run_completed"] else "pending",
                canary="completed" if item["live_canary_completed"] else "pending",
                executor="enabled" if item["final_external_action_enabled"] else "disabled",
            )
        )
    lines.extend(
        [
            "",
            "## Current evidence boundary",
            "",
            f"- Sanitized fixtures: **{totals['sanitized_fixtures']}**.",
            f"- Real-URL dry runs completed: **{totals['real_url_dry_runs_completed']}**.",
            f"- Live canaries completed: **{totals['live_canaries_completed']}**.",
            f"- Qualified form scopes: **{totals['qualified_form_scopes']}**.",
            f"- Final executors enabled: **{totals['final_executors']}**.",
            "",
            "No row proves current tenant compatibility or authorizes an employer-side",
            "submission. Any selector, protocol, form, attachment, request, or evidence",
            "change requires a new sanitized fixture report and a later qualification cycle.",
            "",
            "The machine-readable source of this table is",
            "[`adapter-matrix.json`](adapter-matrix.json). Regenerate or validate both",
            "artifacts with:",
            "",
            "```powershell",
            "python scripts/build_adapter_qualification_matrix.py --check",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _write_artifacts(matrix: Mapping[str, object]) -> None:
    QUALIFICATION_DIR.mkdir(parents=True, exist_ok=True)
    JSON_OUTPUT.write_text(render_json(matrix), encoding="utf-8", newline="\n")
    MARKDOWN_OUTPUT.write_text(render_markdown(matrix), encoding="utf-8", newline="\n")


def _check_artifacts(matrix: Mapping[str, object]) -> bool:
    expected = {
        JSON_OUTPUT: render_json(matrix),
        MARKDOWN_OUTPUT: render_markdown(matrix),
    }
    return all(
        path.exists() and path.read_text(encoding="utf-8") == content
        for path, content in expected.items()
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="fail if artifacts are stale")
    mode.add_argument("--write", action="store_true", help="replace generated artifacts")
    args = parser.parse_args(argv)
    try:
        matrix = build_matrix()
        if args.write:
            _write_artifacts(matrix)
            result = {"status": "written", "totals": matrix["totals"]}
            exit_code = 0
        else:
            current = _check_artifacts(matrix)
            result = {
                "status": "current" if current else "stale",
                "totals": matrix["totals"],
            }
            exit_code = 0 if current else 1
    except QualificationMatrixError as exc:
        result = {"status": "invalid_source", "reason": str(exc)}
        exit_code = 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
