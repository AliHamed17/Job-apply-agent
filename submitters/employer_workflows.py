"""Typed employer-specific workflow policy with safe generic fallbacks."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, field_validator

from submitters.platforms import detect_platform, supported_platforms


class EmployerWorkflow(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    domains: list[str] = Field(min_length=1, max_length=20)
    platform: str
    prefer_last_application: bool = True
    source_path: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("domains")
    @classmethod
    def _domains_are_hosts(cls, domains: list[str]) -> list[str]:
        normalized: list[str] = []
        for domain in domains:
            value = domain.strip().lower().rstrip(".")
            if not value or "://" in value or "/" in value:
                raise ValueError("workflow domains must be hostnames")
            normalized.append(value)
        return normalized

    @field_validator("source_path")
    @classmethod
    def _source_path_is_bounded(cls, path: list[str]) -> list[str]:
        values = [item.strip() for item in path if item.strip()]
        if any(len(item) > 80 for item in values):
            raise ValueError("source path items must be 80 characters or fewer")
        return values

    @field_validator("platform")
    @classmethod
    def _platform_has_an_adapter(cls, platform: str) -> str:
        normalized = platform.strip().lower()
        if normalized not in {*supported_platforms(), "generic_portal"}:
            raise ValueError("workflow platform must name a supported adapter")
        return normalized


class EmployerWorkflowConfig(BaseModel):
    version: int = 1
    employers: list[EmployerWorkflow] = Field(default_factory=list, max_length=100)


_BUILTIN_WORKFLOWS = EmployerWorkflowConfig(
    employers=[
        EmployerWorkflow(
            id="nvidia_workday",
            domains=["nvidia.wd5.myworkdayjobs.com"],
            platform="workday",
            prefer_last_application=True,
            source_path=["Website", "NVIDIA.COM"],
        )
    ]
)


def load_employer_workflows(path: str | Path | None) -> EmployerWorkflowConfig:
    """Load local overrides and retain sanitized built-in known workflows."""
    workflows = list(_BUILTIN_WORKFLOWS.employers)
    candidate = Path(path) if path else None
    if candidate and candidate.is_file():
        raw = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        configured = EmployerWorkflowConfig.model_validate(raw)
        configured_ids = {item.id for item in configured.employers}
        workflows = [
            item for item in workflows if item.id not in configured_ids
        ] + configured.employers
    return EmployerWorkflowConfig(version=1, employers=workflows)


def workflow_for_url(
    url: str,
    config: EmployerWorkflowConfig,
) -> EmployerWorkflow:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    for workflow in config.employers:
        if any(host == domain or host.endswith(f".{domain}") for domain in workflow.domains):
            return workflow
    return EmployerWorkflow(
        id="generic",
        domains=[host or "invalid.local"],
        platform=detect_platform(url),
        prefer_last_application=True,
    )
