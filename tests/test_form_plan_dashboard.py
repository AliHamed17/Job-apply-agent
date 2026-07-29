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


def test_confirmation_preserves_api_detail_and_refreshes_only_stale_plans() -> None:
    confirmation_flow = APP_JS.split(
        "async function confirmFormAnswer(appId, plan, index)",
        maxsplit=1,
    )[1].split(
        "window.openReviewModal",
        maxsplit=1,
    )[0]
    assert "if (result.status === 409)" in confirmation_flow
    assert "boundedApiError(" in confirmation_flow
    assert "['FORM_CHANGED', 'ANSWER_POLICY_CHANGED'].includes(reasonCode)" in confirmation_flow
    assert "renderFormPlanPanel(appId, refreshed.data)" in confirmation_flow
    assert "showToast(value" not in confirmation_flow
    assert "console." not in confirmation_flow
    assert "Answer confirmed. Review the updated plan, then prepare again." in confirmation_flow
    assert "Reusable answer saved. Reinspect the employer form" in confirmation_flow
    assert "The form changed while you were reviewing it" not in confirmation_flow


def test_partial_plan_requires_scoped_reuse_then_reinspection() -> None:
    control = APP_JS.split(
        "function formAnswerControl(field, decision, index, reviewable, partialPlan)",
        maxsplit=1,
    )[1].split(
        "function renderFormPlanPanel",
        maxsplit=1,
    )[0]
    confirmation = APP_JS.split(
        "async function confirmFormAnswer(appId, plan, index)",
        maxsplit=1,
    )[1].split(
        "window.openReviewModal",
        maxsplit=1,
    )[0]
    panel = APP_JS.split(
        "function renderFormPlanPanel(appId, plan)",
        maxsplit=1,
    )[1].split(
        "function readFormAnswer",
        maxsplit=1,
    )[0]

    assert "checked required" in control
    assert "save as a scoped reusable answer" in control
    assert "partialPlan && !field.canonical_name" in control
    assert "Partial plans require a scoped reusable answer" in confirmation
    assert "this partial plan cannot be prepared" in confirmation
    assert "FORM_PLAN_INCOMPLETE" in panel
    assert "Preparation is unavailable for this partial plan" in panel
    assert "run Inspect application form again" in panel
    assert "A partial inspection cannot be prepared" in INDEX_HTML


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
        "function liveSendBlockers(application, runtime = runtimeSubmissionState())",
        maxsplit=1,
    )[1].split(
        "function finalSendUiState",
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


def test_send_requires_exact_live_qualified_adapter_and_form_scope() -> None:
    assert "probeJson('/api/ats/adapters')" in APP_JS
    assert "adapterCapabilities: null" in APP_JS
    assert "function cacheFormPlanIdentity(application, plan)" in APP_JS
    qualification = APP_JS.split(
        "function adapterQualificationBlockers(application)",
        maxsplit=1,
    )[1].split(
        "function liveSendBlockers",
        maxsplit=1,
    )[0]
    assert "adapter.adapter_version === adapterVersion" in qualification
    assert "adapter.selector_version === selectorVersion" in qualification
    assert "capability.final_execution_enabled !== true" in qualification
    assert "capability.qualified_form_scope" in qualification
    assert "!qualifiedScope.includes(fingerprint)" in qualification
    assert "outside the qualified live scope" in qualification

    blockers = APP_JS.split(
        "function liveSendBlockers(application, runtime = runtimeSubmissionState())",
        maxsplit=1,
    )[1].split(
        "function finalSendUiState",
        maxsplit=1,
    )[0]
    assert "blockers.push(...adapterQualificationBlockers(application));" in blockers


def test_inspection_and_preparation_use_adapter_capabilities_not_platform_names() -> None:
    assert "function adapterCapabilityForPlatform(platform)" in APP_JS
    assert "function requiresVersionedFormPlan(application)" in APP_JS
    assert "application?.requires_versioned_form_plan === true" in APP_JS
    assert "capability?.execution_contract_version === 'two-phase-v2'" in APP_JS
    assert "function adapterInspectionBlockers(application)" in APP_JS
    inspection = APP_JS.split(
        "function adapterInspectionBlockers(application)",
        maxsplit=1,
    )[1].split(
        "function adapterQualificationBlockers",
        maxsplit=1,
    )[0]
    assert "capability.execution_contract_version !== 'two-phase-v2'" in inspection
    assert "'dry_run_qualified', 'live_canary_qualified'" in inspection
    assert "capability.qualified_form_scope.length === 0" in inspection
    assert "real-URL inspection is disabled" in inspection

    modal = APP_JS.split(
        "const isPending = isReviewableApplication(app);",
        maxsplit=1,
    )[1].split(
        "const retryBtn = $('btn-retry-app');",
        maxsplit=1,
    )[0]
    assert "const requiresPlan = requiresVersionedFormPlan(app);" in modal
    assert "const inspectionBlockers = adapterInspectionBlockers(app);" in modal
    assert "isPending && requiresPlan" in modal
    assert "inspectionBlockers.length > 0" in modal
    assert "app.platform === 'workday'" not in modal
    assert "current employer form" in modal


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
