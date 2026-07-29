"""Static contracts for truthful dashboard submission messaging."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "api/static/js/app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "api/templates/index.html").read_text(encoding="utf-8")


def test_green_toast_is_reserved_for_employer_verified_result() -> None:
    assert APP_JS.count(", 'success');") == 1
    assert "showToast('Employer confirmed this application submission', 'success')" in APP_JS
    assert "if (isEmployerVerified(result))" in APP_JS
    assert "Backend reported success without employer evidence — not counted" in APP_JS


def test_preparation_actions_do_not_claim_submission() -> None:
    assert "Prepare application" in INDEX_HTML
    assert "Prepare selected" in INDEX_HTML
    assert "Approve &amp; Submit" not in INDEX_HTML
    assert "queued for submission" not in APP_JS
    assert "accepted for preparation — not submitted" in APP_JS
    assert "/api/applications/${appId}/prepare" in APP_JS
    assert "apiCall('/api/applications/batch-prepare'" in APP_JS
    assert "isPreparedApplication(application)" in APP_JS
    assert "application?.status === 'draft' && Boolean(application?.approved_at)" in APP_JS
    assert (
        "Prepare a new retry? Nothing will be submitted. "
        "Review it, then use Send application separately."
    ) in APP_JS
    assert "retry? In live mode this may perform the final external action" not in APP_JS


def test_live_send_is_fail_closed_and_runtime_banner_is_persistent() -> None:
    assert 'id="runtime-mode-banner"' in INDEX_HTML
    assert 'id="btn-send-app"' in INDEX_HTML
    assert "submission.allowed === true" in APP_JS
    assert "mode.live_submit_enabled === true" in APP_JS
    assert "readiness.status === 'ready'" in APP_JS
    assert "runtimeMeta('job-agent-ui-digest')" in APP_JS
    assert "&& releaseMatches" in APP_JS
    assert "Dashboard and API releases do not match; reload this page" in APP_JS
    assert 'name="job-agent-ui-digest"' in INDEX_HTML
    assert "A current validated form plan is required" in APP_JS
    assert "function finalSendUiState(application)" in APP_JS
    assert "&& runtime.allowed" in APP_JS
    assert "&& hasValidFormPlan(application)" in APP_JS
    assert "&& blockers.length === 0" in APP_JS
    assert "const planId = plan.plan_id || application?.form_plan_id" in APP_JS
    assert "plan.plan_id !== application.form_plan_id" in APP_JS
    assert "plan.fingerprint !== application.form_plan_fingerprint" in APP_JS
    assert "sendBtn.style.display = sendUi.visible ? 'inline-flex' : 'none'" in APP_JS
    assert "sendBtn.disabled = !sendUi.visible" in APP_JS
    assert "form_plan_id: app.form_plan_id" in APP_JS
    assert "client_release: LOADED_DASHBOARD_RELEASE" in APP_JS
    assert "ACTIVE_SUBMISSION_STAGES" in APP_JS
    assert "A submission attempt is already in progress" in APP_JS
    assert "&& !hasActiveSubmissionAttempt(application)" in APP_JS
    assert 'role="status" aria-live="polite"' in INDEX_HTML
    assert "Send application is hidden until every live-readiness" in APP_JS
    assert "Open application details to review every blocker." in APP_JS
    assert "$('btn-send-app').style.display = 'none'" in APP_JS


def test_remote_send_permission_is_explicit_bound_and_never_claims_submission() -> None:
    assert 'id="btn-allow-remote-send"' in INDEX_HTML
    assert "async function handleAllowRemoteSend(appId)" in APP_JS
    assert "/api/applications/${appId}/control-plane-review-grant" in APP_JS
    assert "acknowledgement: 'ALLOW_REMOTE_SEND'" in APP_JS
    assert "application_revision: app.revision ?? app.application_revision" in APP_JS
    assert "form_plan_id: app.form_plan_id" in APP_JS
    assert "This does not submit now." in APP_JS
    assert "No application was submitted." in APP_JS
    assert "remoteSendBtn.style.display = sendUi.visible ? 'inline-flex' : 'none'" in APP_JS
    assert "remoteSendBtn.disabled = !sendUi.visible" in APP_JS


def test_attempt_status_is_polled_when_api_supplies_a_status_url() -> None:
    assert "result?.status_url || result?.attempt?.status_url" in APP_JS
    assert "/api/submission-attempts/${attemptId}" in APP_JS
    assert "await probeJson(statusUrl)" in APP_JS
    assert "isTerminalAttemptResult(probe.data)" in APP_JS


def test_multi_url_ingest_uses_truthful_per_url_endpoint() -> None:
    assert "apiCall('/api/dashboard/ingest'" in APP_JS
    assert "urls," in APP_JS
    assert "counts.accepted" in APP_JS
    assert "result.prepared_application_ids || result.queued_application_ids" in APP_JS


def test_structured_admission_errors_render_a_bounded_operator_message() -> None:
    assert "function boundedApiError(body, fallback)" in APP_JS
    assert "detail.message" in APP_JS
    assert "detail.code" in APP_JS
    assert "new Error(boundedApiError(err, `HTTP ${res.status}`))" in APP_JS
    assert "new Error(err.detail ||" not in APP_JS
