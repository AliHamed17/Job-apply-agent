"""Runtime assembly for deterministic fit evaluation over private local CVs."""

from __future__ import annotations

import os
from pathlib import Path
from profile.cv_content_cache import load_configured_cv_artifacts
from profile.cv_routing import load_routing_config
from profile.models import UserProfile

from dotenv import dotenv_values

from jobs.models import JobData
from match.job_fit import (
    JobFitDecisionV1,
    evaluate_job_fit,
    load_fit_qualification,
    unavailable_job_fit_decision,
)


def configured_fit_qualification_path() -> str:
    """Read the private artifact path without expanding the qualified LLM config surface."""

    direct = os.environ.get("FIT_ROUTING_QUALIFICATION_PATH", "").strip()
    if direct:
        return direct
    env_file = Path(os.environ.get("JOB_AGENT_ENV_FILE", ".env"))
    if env_file.is_file():
        configured = str(
            dotenv_values(env_file).get("FIT_ROUTING_QUALIFICATION_PATH") or ""
        ).strip()
        if configured:
            return configured
    return "fit_routing_qualification.json"


def evaluate_configured_job_fit(
    job: JobData,
    profile: UserProfile,
    *,
    profile_version: int | None,
    cv_routing_path: str | Path,
    cv_directory: str | Path,
    qualification_path: str | Path,
) -> JobFitDecisionV1:
    """Fail closed when any local routing or qualification input is unavailable."""

    routing_path = Path(cv_routing_path)
    if not routing_path.is_file():
        return unavailable_job_fit_decision(
            job,
            profile_version=profile_version,
            reason_code="FIT_ROUTING_CONFIG_MISSING",
        )
    try:
        routing_config = load_routing_config(routing_path)
    except Exception:
        return unavailable_job_fit_decision(
            job,
            profile_version=profile_version,
            reason_code="FIT_ROUTING_CONFIG_INVALID",
        )

    artifacts = load_configured_cv_artifacts(routing_config, cv_directory)
    qualification_file = Path(qualification_path)
    qualification = None
    if qualification_file.is_file():
        try:
            qualification = load_fit_qualification(qualification_file)
        except Exception:
            return unavailable_job_fit_decision(
                job,
                profile_version=profile_version,
                reason_code="FIT_QUALIFICATION_INVALID",
            )

    try:
        return evaluate_job_fit(
            job,
            profile,
            profile_version=profile_version,
            routing_config=routing_config,
            artifacts=artifacts,
            qualification=qualification,
        )
    except Exception:
        return unavailable_job_fit_decision(
            job,
            profile_version=profile_version,
            reason_code="FIT_EVALUATION_UNAVAILABLE",
        )
