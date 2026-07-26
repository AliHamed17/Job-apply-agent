from pathlib import Path

APP_JS = Path("api/static/js/app.js").read_text(encoding="utf-8")
INDEX_HTML = Path("api/templates/index.html").read_text(encoding="utf-8")


def test_dashboard_exposes_an_explicit_non_submitting_inspection_action() -> None:
    assert 'id="btn-inspect-app"' in INDEX_HTML
    assert "Inspect application form" in INDEX_HTML
    assert "async function inspectApplicationForm(appId)" in APP_JS
    assert "`/api/applications/${appId}/inspect`" in APP_JS
    assert "{ application_revision: application.revision }" in APP_JS
    assert "may upload or replace the" in INDEX_HTML
    assert "never clicks the final submit control" in INDEX_HTML


def test_inspection_ui_does_not_relabel_the_action_as_submission() -> None:
    inspection = APP_JS.split("async function inspectApplicationForm(appId)", 1)[1].split(
        "window.copyCoverLetter",
        1,
    )[0]

    assert "Send application" not in inspection
    assert "submitted" not in inspection.casefold()
    assert "exact cv upload evidence recorded" in inspection.casefold()
    assert "'success'" not in inspection


def test_dashboard_requires_persisted_redacted_attachment_evidence() -> None:
    assert "function formPlanHasAttachmentEvidence(plan)" in APP_JS
    assert "candidate_browser_upload_complete" in APP_JS
    assert "plan.selected_cv_ref === plan.attached_cv_ref" in APP_JS
    assert "plan.selected_cv_hash === plan.attached_cv_hash" in APP_JS
    assert "plan.attachment_verified_at" in APP_JS
    assert "Selected CV (redacted)" in APP_JS
    assert "Attached CV (redacted)" in APP_JS
    assert "Browser-observed upload-complete marker" in APP_JS
    assert "${app.selected_cv_id}" not in APP_JS
