from __future__ import annotations

import json
from profile.models import Personal, Preferences, ProfileEvidence, UserProfile

from core.automation_readiness import build_automation_readiness
from core.config import Settings
from submitters.inspector import inspect_submitter_health
from submitters.platforms import (
    TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    AdapterDescriptor,
    QualificationTier,
)


def _dependencies(ok: bool = True) -> dict[str, object]:
    return {
        "status": "ready" if ok else "degraded",
        "checks": {
            name: {"ok": ok}
            for name in (
                "database",
                "migration",
                "redis",
                "worker",
                "beat",
                "shared_storage",
                "browser",
                "llm",
            )
        },
    }


def _profile(*, placeholder_identity: bool = False) -> UserProfile:
    return UserProfile(
        personal=Personal(
            name="Jane Doe" if placeholder_identity else "Candidate Name",
            email="jane@example.com" if placeholder_identity else "candidate@domain.test",
            phone="+972 50 000 0000",
            location="Israel",
        ),
        preferences=Preferences(
            roles=["Machine Learning Engineer"],
            locations=["Israel", "Worldwide Remote"],
        ),
        evidence=ProfileEvidence(
            user_confirmed={
                "work_authorization": "operator-confirmed",
                "visa_sponsorship": "operator-confirmed",
                "nationality": "operator-confirmed",
            }
        ),
    )


def _settings(tmp_path) -> Settings:
    cv_dir = tmp_path / "cvs"
    cv_dir.mkdir()
    (cv_dir / "ml.pdf").write_bytes(b"%PDF-1.4\nqualified fixture")
    routing = tmp_path / "cv_routing.yaml"
    routing.write_text(
        """
minimum_confidence: 0.5
fallback_cv_id: null
cvs:
  - id: ml
    file: ml.pdf
    title_terms: [machine learning]
    skills: [python]
overrides: []
""".strip(),
        encoding="utf-8",
    )
    browser_root = tmp_path / "browser"
    browser_root.mkdir()
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="postgresql://jobagent:test@localhost/jobagent",
        application_data_dir=str(tmp_path),
        cv_routing_path=str(routing),
        cv_directory=str(cv_dir),
        portal_browser_profile_root=str(browser_root),
        llm_provider="mock",
        dry_run=False,
        draft_only=False,
        portal_final_submit_enabled=True,
        live_automation_acknowledged=True,
        secret_key="operator-auth-test-secret-" + "x" * 32,
    )


def _qualified_adapter() -> AdapterDescriptor:
    return AdapterDescriptor(
        platform="test-ats",
        adapter_version="1.0.0",
        selector_version="candidate-v1",
        transport="browser",
        authentication_mode="public_candidate_flow",
        supported_controls=("text", "file"),
        qualification=QualificationTier.LIVE_CANARY_QUALIFIED,
        qualified_form_scope=("a" * 64,),
        domains=("jobs.example.test",),
        execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    )


def test_placeholder_identity_allows_discovery_but_blocks_later_stages(tmp_path):
    report = build_automation_readiness(
        settings=_settings(tmp_path),
        dependency_report=_dependencies(),
        profile=_profile(placeholder_identity=True),
        profile_version=1,
        adapters=(_qualified_adapter(),),
    )

    assert report["discovery_ready"] is True
    assert report["preparation_ready"] is False
    assert report["submission_ready"] is False
    assert report["stages"]["preparation"]["reason_codes"] == [
        "PROFILE_NAME_PLACEHOLDER",
        "PROFILE_EMAIL_PLACEHOLDER",
    ]


def test_all_three_stages_are_independently_ready_with_exact_prerequisites(tmp_path):
    report = build_automation_readiness(
        settings=_settings(tmp_path),
        dependency_report=_dependencies(),
        profile=_profile(),
        profile_version=7,
        adapters=(_qualified_adapter(),),
    )

    assert report == {
        "discovery_ready": True,
        "preparation_ready": True,
        "submission_ready": True,
        "stages": {
            "discovery": {"ready": True, "reason_codes": []},
            "preparation": {"ready": True, "reason_codes": []},
            "submission": {"ready": True, "reason_codes": []},
        },
    }


def test_readiness_is_bounded_and_contains_no_candidate_values(tmp_path):
    profile = _profile()
    report = build_automation_readiness(
        settings=_settings(tmp_path),
        dependency_report=_dependencies(),
        profile=profile,
        profile_version=None,
        adapters=(),
    )

    serialized = json.dumps(report)
    assert "PROFILE_VERSION_MISSING" in serialized
    assert "ADAPTER_NOT_QUALIFIED" in serialized
    for private_value in (
        profile.personal.name,
        profile.personal.email,
        profile.personal.phone,
    ):
        assert private_value not in serialized


def test_legacy_auto_apply_never_reports_live_autopilot(monkeypatch):
    monkeypatch.setenv("AUTO_APPLY", "true")
    from core.config import get_settings

    get_settings.cache_clear()
    try:
        report = inspect_submitter_health()
        assert report.auto_prepare_active is True
        assert report.qualified_autopilot_active is False
        assert report.live_auto_apply_active is False
    finally:
        get_settings.cache_clear()
