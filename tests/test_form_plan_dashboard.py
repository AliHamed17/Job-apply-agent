"""Static browser contracts for private form review and evidence-locked drafts."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "api/static/js/app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "api/templates/index.html").read_text(encoding="utf-8")


def test_dashboard_renders_exact_observed_fields_and_confirmation_controls() -> None:
    assert "function renderFormPlanPanel(appId, plan)" in APP_JS
    assert "field.label" in APP_JS
    assert "field.options || []" in APP_JS
    assert "formConstraintSummary(field)" in APP_JS
    assert "field.sensitive_category" in APP_JS
    assert "data-confirm-field-index" in APP_JS
    assert "Reuse only for this exact field and form version" in APP_JS
    assert "encodeURIComponent(field.field_id)" in APP_JS
    assert (
        "/api/applications/${appId}/answers/${encodeURIComponent(field.field_id)}/confirm"
    ) in APP_JS
    assert "application_revision: plan.application_revision" in APP_JS
    assert "plan_id: plan.plan_id" in APP_JS


def test_confirmation_refreshes_a_stale_plan_without_logging_the_answer() -> None:
    confirmation_flow = APP_JS.split(
        "async function confirmFormAnswer(appId, plan, index)",
        maxsplit=1,
    )[1].split(
        "window.openReviewModal",
        maxsplit=1,
    )[0]
    assert "if (result.status === 409)" in confirmation_flow
    assert "Loading the latest plan" in confirmation_flow
    assert "renderFormPlanPanel(appId, refreshed.data)" in confirmation_flow
    assert "showToast(value" not in confirmation_flow
    assert "console." not in confirmation_flow
    assert "Answer confirmed. Review the updated plan, then prepare again." in confirmation_flow


def test_browser_constraints_supplement_authoritative_server_validation() -> None:
    answer_reader = APP_JS.split(
        "function readFormAnswer(field, index)",
        maxsplit=1,
    )[1].split(
        "async function confirmFormAnswer",
        maxsplit=1,
    )[0]
    assert "control.checkValidity()" in answer_reader
    assert "control.reportValidity()" in answer_reader
    assert "satisfies the observed form constraints" in answer_reader


def test_evidence_checked_cover_letter_cannot_be_silently_edited_or_prepared() -> None:
    assert 'id="modal-cover-letter"' in INDEX_HTML
    textarea = INDEX_HTML.split('id="modal-cover-letter"', maxsplit=1)[1].split(
        "</textarea>",
        maxsplit=1,
    )[0]
    assert "readonly" in textarea
    assert "This evidence-checked draft is read-only." in INDEX_HTML
    assert "$('modal-cover-letter').readOnly = true;" in APP_JS

    prepare_flow = APP_JS.split(
        "async function handlePrepare(appId)",
        maxsplit=1,
    )[1].split(
        "async function handleSend(appId)",
        maxsplit=1,
    )[0]
    assert "modal-cover-letter" not in prepare_flow
    assert "/feedback" not in prepare_flow


def test_modal_requests_are_bound_to_the_exact_application() -> None:
    assert "function beginReviewModalRequest(applicationId)" in APP_JS
    assert "function isCurrentReviewModalRequest(applicationId, requestToken)" in APP_JS
    assert "function invalidateReviewModalRequest()" in APP_JS
    assert "if (modal.id === 'review-modal') invalidateReviewModalRequest();" in APP_JS

    review_flow = APP_JS.split(
        "window.openReviewModal = async appId =>",
        maxsplit=1,
    )[1].split(
        "async function previewCvRoute",
        maxsplit=1,
    )[0]
    assert "const requestToken = beginReviewModalRequest(appId);" in review_flow
    assert "actionButtons.forEach" in review_flow
    assert "button.disabled = true;" in review_flow
    assert "button.onclick = null;" in review_flow
    assert "if (!isCurrentReviewModalRequest(app.id, requestToken)) return;" in review_flow
    assert "app.form_plan_expires_at = planResult.data?.expires_at || null;" in review_flow
    assert "app.form_plan_invalidated_at = planResult.data?.invalidated_at || null;" in review_flow
    assert "planResult.data?.valid === true" in review_flow
    assert "app.form_plan_valid = false;" in review_flow
    assert "No current form inspection is available." in review_flow

    for flow_name in (
        "confirmFormAnswer",
        "previewCvRoute",
        "overrideCvRoute",
        "handlePrepare",
        "handleSend",
        "handleReject",
    ):
        assert f"function {flow_name}" in APP_JS
    assert "reviewModalState.requestToken" in APP_JS


def test_send_requires_exact_audited_model_identities() -> None:
    blockers = APP_JS.split(
        "function liveSendBlockers(application)",
        maxsplit=1,
    )[1].split(
        "function renderApplications",
        maxsplit=1,
    )[0]
    assert "application?.material_eligible !== true" in blockers
    assert "runtimeModel.ready !== true" in blockers
    assert "runtimeModel.local !== true" in blockers
    assert "application.material_prompt_version !== QUALIFIED_MATERIAL_PROMPT_VERSION" in blockers
    assert "application.material_model_provider !== runtimeModel.provider" in blockers
    assert "application.material_model_name !== runtimeModel.model" in blockers
    assert "application.material_model_digest !== runtimeModel.digest" in blockers
    assert "application?.form_plan_uses_local_llm === true" in blockers
    assert "application.form_plan_llm_prompt_version !== QUALIFIED_FORM_PROMPT_VERSION" in blockers
    assert "application.form_plan_llm_model_digest !== runtimeModel.digest" in blockers


def test_send_rechecks_form_plan_expiry_and_invalidation_at_click_time() -> None:
    assert "function parseServerTimestamp(rawValue)" in APP_JS
    validity = APP_JS.split(
        "function hasValidFormPlan(application)",
        maxsplit=1,
    )[1].split(
        "const ACTIVE_SUBMISSION_STAGES",
        maxsplit=1,
    )[0]
    assert "application?.form_plan_invalidated_at" in validity
    assert "application?.form_plan_expires_at" in validity
    assert "parseServerTimestamp(expiresAt)" in validity
    assert "Number.isFinite(expiresAtMs) && expiresAtMs > Date.now()" in validity
    assert "return !expiresAt" not in validity

    send_flow = APP_JS.split(
        "async function handleSend(appId)",
        maxsplit=1,
    )[1].split(
        "async function handleReject",
        maxsplit=1,
    )[0]
    assert "const blockers = liveSendBlockers(app);" in send_flow


def test_send_reuses_request_key_until_the_result_is_definitive() -> None:
    key_contract = APP_JS.split(
        "function sendIdempotencyStorageKey(application)",
        maxsplit=1,
    )[1].split(
        "async function handleSend(appId)",
        maxsplit=1,
    )[0]
    assert "sessionStorage.getItem(storageKey)" in key_contract
    assert "sessionStorage.setItem(storageKey, value)" in key_contract
    assert "state.sendIdempotencyKeys.set(storageKey, value)" in key_contract
    assert "previousAttemptIds" in key_contract
    assert "No new attempt is visible yet; retry will reuse the same request key." in key_contract

    send_flow = APP_JS.split(
        "async function handleSend(appId)",
        maxsplit=1,
    )[1].split(
        "async function handleReject",
        maxsplit=1,
    )[0]
    assert "const idempotency = getOrCreateSendIdempotencyKey(app);" in send_flow
    assert "idempotency_key: idempotency.value" in send_flow
    assert "if (response.status === 0 || response.status >= 500)" in send_flow
    ambiguous_branch = send_flow.split(
        "if (response.status === 0 || response.status >= 500)",
        maxsplit=1,
    )[1].split(
        "clearSendIdempotencyKey(idempotency.storageKey);",
        maxsplit=1,
    )[0]
    assert "reconcileAmbiguousSend(" in ambiguous_branch
    assert "clearSendIdempotencyKey" not in ambiguous_branch
