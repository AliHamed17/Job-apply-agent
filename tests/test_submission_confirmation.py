from __future__ import annotations

from submitters.confirmation import (
    browser_submission_result,
    detect_submission_confirmation,
)


def test_generic_success_words_do_not_confirm_an_application():
    html = """
    <html><body>
      <h1>Apply for our Success Engineering team</h1>
      <p>Successful candidates will be contacted. Thank you for your interest.</p>
    </body></html>
    """
    evidence = detect_submission_confirmation(
        "https://careers.example.test/jobs/1/apply",
        html,
    )
    assert evidence.confirmed is False


def test_explicit_application_confirmation_is_authoritative():
    html = """
    <html><body>
      <main data-testid="application-confirmation">
        <h1>Application submitted</h1>
        <p>We have received your application.</p>
      </main>
    </body></html>
    """
    result = browser_submission_result(
        platform="example",
        page_url="https://careers.example.test/application-confirmation",
        html=html,
    )
    assert result.status == "submitted"
    assert result.reason_code == "SUBMITTED"


def test_post_click_without_confirmation_is_unknown_not_failed():
    result = browser_submission_result(
        platform="example",
        page_url="https://careers.example.test/jobs/1/apply",
        html="<html><body><h1>Application form</h1></body></html>",
    )
    assert result.status == "unknown"
    assert result.reason_code == "SUBMIT_UNCONFIRMED"
    assert result.success is False


def test_challenge_after_click_is_indeterminate():
    result = browser_submission_result(
        platform="example",
        page_url="https://careers.example.test/challenge",
        html="<html><body>Verify you are human with hcaptcha</body></html>",
    )
    assert result.status == "unknown"
    assert result.reason_code == "CHALLENGE_AFTER_SUBMIT"
