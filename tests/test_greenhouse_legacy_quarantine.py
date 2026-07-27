"""Safety contract for the retired one-step Greenhouse adapter."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import SubmitterRegistry
from submitters.greenhouse import GreenhouseSubmitter


def _job() -> JobData:
    return JobData(
        title="Safety Engineer",
        apply_url="https://boards.greenhouse.io/example/jobs/12345",
        source_url="https://boards.greenhouse.io/example/jobs/12345",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("api_key", ["", "credential-must-not-be-used"])
async def test_legacy_submitter_refuses_before_using_credentials_or_private_inputs(
    api_key: str,
) -> None:
    submitter = GreenhouseSubmitter(api_key=api_key)
    private_marker = "PRIVATE-MARKER-MUST-NOT-LEAK"

    result = await submitter.submit(
        _job(),
        GeneratedApplication(cover_letter=private_marker),
        {"personal": {"email": f"{private_marker}@example.test"}},
        resume_path=f"C:/{private_marker}.pdf",
    )

    assert submitter.can_submit(_job()) is False
    assert result.success is False
    assert result.status == "failed"
    assert result.reason_code == "ADAPTER_NOT_QUALIFIED"
    assert result.error == "ADAPTER_NOT_QUALIFIED"
    assert result.diagnostic_details == {"external_action_started": False}
    assert private_marker not in repr(result)


@pytest.mark.asyncio
async def test_historical_browser_method_is_also_network_incapable() -> None:
    result = await GreenhouseSubmitter()._submit_via_browser(
        _job(),
        GeneratedApplication(),
        {},
        resume_path=None,
    )

    assert result.success is False
    assert result.reason_code == "ADAPTER_NOT_QUALIFIED"
    assert result.diagnostic_details["external_action_started"] is False


def test_legacy_module_has_no_http_browser_or_file_action_calls() -> None:
    module_path = Path(__file__).resolve().parents[1] / "submitters" / "greenhouse.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert imported_roots.isdisjoint({"httpx", "playwright"})
    assert called_names.isdisjoint({"open", "request", "urlopen"})
    assert called_attributes.isdisjoint(
        {
            "click",
            "fill",
            "goto",
            "open",
            "post",
            "request",
            "set_input_files",
        }
    )


def test_legacy_class_cannot_enter_two_phase_registry() -> None:
    with pytest.raises(TypeError):
        SubmitterRegistry().register_two_phase(GreenhouseSubmitter())


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://boards.greenhouse.io/example/jobs/12345", "12345"),
        ("https://job-boards.greenhouse.io/example?gh_jid=67890", "67890"),
        ("https://untrusted.example/?next=greenhouse.io/jobs/12345", None),
    ],
)
def test_historical_job_id_helper_is_exact(url: str, expected: str | None) -> None:
    assert GreenhouseSubmitter._extract_job_id(url) == expected
