"""Static dashboard contracts for fixture-only Greenhouse inspection."""

from __future__ import annotations

from pathlib import Path

from submitters.platforms import (
    TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    QualificationTier,
    adapter_for_platform,
)

APP_JS = Path("api/static/js/app.js").read_text(encoding="utf-8")


def _function(name: str, next_name: str) -> str:
    return APP_JS.split(f"function {name}", maxsplit=1)[1].split(
        f"function {next_name}",
        maxsplit=1,
    )[0]


def test_fixture_only_greenhouse_capability_disables_real_url_inspection() -> None:
    descriptor = adapter_for_platform("greenhouse")

    assert descriptor is not None
    assert descriptor.adapter_version == "1.0.0"
    assert descriptor.selector_version == "greenhouse-candidate-v9"
    assert descriptor.execution_contract_version == TWO_PHASE_EXECUTION_CONTRACT_VERSION
    assert descriptor.qualification is QualificationTier.FIXTURE_QUALIFIED
    assert descriptor.qualified_form_scope == ()
    assert descriptor.allows_final_execution is False

    inspection_gate = _function(
        "adapterInspectionBlockers(application)",
        "adapterQualificationBlockers(application)",
    )
    assert "adapterCapabilityForPlatform(application?.platform)" in inspection_gate
    assert "capability.execution_contract_version !== 'two-phase-v2'" in inspection_gate
    assert "'dry_run_qualified', 'live_canary_qualified'" in inspection_gate
    assert "capability.qualification_tier" in inspection_gate
    assert "capability.qualified_form_scope.length === 0" in inspection_gate
    assert "real-URL inspection is disabled" in inspection_gate
    assert "greenhouse" not in inspection_gate.casefold()


def test_greenhouse_prepare_and_send_use_capabilities_without_platform_bypass() -> None:
    requires_plan = _function(
        "requiresVersionedFormPlan(application)",
        "adapterInspectionBlockers(application)",
    )
    assert "adapterCapabilityForPlatform(application?.platform)" in requires_plan
    assert "capability?.execution_contract_version === 'two-phase-v2'" in requires_plan
    assert "application?.requires_versioned_form_plan === true" in requires_plan
    assert "if (capability) return" not in requires_plan
    assert "greenhouse" not in requires_plan.casefold()

    modal_actions = APP_JS.split(
        "const isPending = isReviewableApplication(app);",
        maxsplit=1,
    )[1].split(
        "const retryBtn = $('btn-retry-app');",
        maxsplit=1,
    )[0]
    assert "const requiresPlan = requiresVersionedFormPlan(app);" in modal_actions
    assert "const inspectionBlockers = adapterInspectionBlockers(app);" in modal_actions
    assert "isPending && requiresPlan" in modal_actions
    assert "inspectionBlockers.length > 0" in modal_actions
    assert "requiresPlan" in modal_actions
    assert "app.form_plan_review_ready !== true" in modal_actions
    assert "greenhouse" not in modal_actions.casefold()
    assert "app.platform ===" not in modal_actions

    send_gate = _function(
        "liveSendBlockers(application)",
        "renderApplications()",
    )
    assert "!hasValidFormPlan(application)" in send_gate
    assert "adapterQualificationBlockers(application)" in send_gate
    assert "greenhouse" not in send_gate.casefold()

    qualification_gate = _function(
        "adapterQualificationBlockers(application)",
        "liveSendBlockers(application)",
    )
    assert "capability.final_execution_enabled !== true" in qualification_gate
    assert "capability.qualified_form_scope" in qualification_gate
    assert "!qualifiedScope.includes(fingerprint)" in qualification_gate
    assert "greenhouse" not in qualification_gate.casefold()
