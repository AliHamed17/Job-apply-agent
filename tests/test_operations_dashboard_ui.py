"""Static safety contracts for the operations and qualification dashboard."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "api/static/js/app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "api/templates/index.html").read_text(encoding="utf-8")
STYLES = (ROOT / "api/static/css/style.css").read_text(encoding="utf-8")


def test_operations_dashboard_consumes_the_protected_bounded_snapshot() -> None:
    assert 'id="operations-dashboard"' in INDEX_HTML
    assert "Operations &amp; Qualification" in INDEX_HTML
    assert "Employer, job, question, answer, and candidate details are never shown" in INDEX_HTML
    assert "probeJson('/api/dashboard/operations')" in APP_JS
    assert "state.operationsProbeStatus === 'authentication_required'" in APP_JS
    assert "Enter the API Secret to load protected operational evidence." in APP_JS
    assert "const OPERATIONS_ARRAY_LIMIT = 16" in APP_JS
    assert ".slice(0, Math.max(0, Math.min(limit, OPERATIONS_ARRAY_LIMIT)))" in APP_JS


def test_operations_dashboard_uses_structured_fields_and_safe_fallbacks() -> None:
    for field in (
        "dependencies",
        "last_successful_discovery",
        "adapter_matrix",
        "failure_clusters",
        "queue_depth",
        "attempt_stages",
        "attempt_outcomes",
        "form_resolution",
        "attachment_results",
        "evidence_types",
        "runtime_identity",
    ):
        assert f"data.{field}" in APP_JS

    assert "dependencyFallbackFromReadiness()" in APP_JS
    assert "'shared_storage'," in APP_JS
    assert "'shared_profile_storage'," not in APP_JS
    assert "adapterFallbackFromRuntime()" in APP_JS
    assert "runtimeIdentityFallback()" in APP_JS
    assert "Object.entries(d.selector_failure_clusters" not in APP_JS
    assert "item.diagnostic_details" not in APP_JS
    assert "item.error_message" not in APP_JS
    assert "JSON.stringify(state.operationsData)" not in APP_JS


def test_operations_values_are_escaped_and_status_classes_are_fixed() -> None:
    assert "${esc(operationalToken(labelValue))}" in APP_JS
    assert "${esc(operationalToken(adapter.ats))}" in APP_JS
    assert "${esc(shortOperationalToken(runtimeIdentity.build_sha, 12))}" in APP_JS
    assert "${esc(fmtDateTime(lastDiscoveryAt))}" in APP_JS
    assert "return 'is-danger'" in APP_JS
    assert "return 'is-warning'" in APP_JS
    assert "return 'is-ready'" in APP_JS
    assert "return 'is-neutral'" in APP_JS
    assert ".operations-status-dot.is-ready { background: var(--primary); }" in STYLES


def test_review_timeline_exposes_only_redacted_attempt_audit_fields() -> None:
    assert "...attempts.map(attemptHistoryItem)" in APP_JS
    for field in (
        "attempt.created_at",
        "attempt.reconciled_at",
        "attempt.adapter_version",
        "attempt.selector_version",
        "attempt.form_plan_fingerprint",
        "attempt.attachment_verified",
        "attempt.verification_kind",
        "attempt.runner_release",
    ):
        assert field in APP_JS
    assert "shortOperationalToken(attempt.form_plan_fingerprint, 14)" in APP_JS
    assert "attachment verified" in APP_JS
    assert "reconciled ${fmtDateTime(attempt.reconciled_at)}" in APP_JS


def test_toasts_render_api_messages_as_text_not_markup() -> None:
    assert "toast.querySelector('span').textContent = String(message)" in APP_JS
    assert "<span>${message}</span>" not in APP_JS


def test_hidden_send_control_keeps_escaped_card_and_text_only_detail_guidance() -> None:
    assert "${esc(summarizeSendBlockers(sendUi.blockers))}" in APP_JS
    assert "sendReason.textContent = sendUi.candidate && !sendUi.visible" in APP_JS
    assert "sendReason.innerHTML" not in APP_JS
    assert ".app-action-guidance" in STYLES
