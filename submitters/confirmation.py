"""Authoritative browser confirmation checks shared by ATS adapters."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from submitters.base import SubmissionResult

_CONFIRMATION_PHRASES = (
    "application submitted",
    "application successfully submitted",
    "application has been submitted",
    "application received",
    "we have received your application",
    "thanks for applying",
    "thank you for applying",
    "thank you for your application",
)
_CONFIRMATION_PATH_MARKERS = (
    "application-confirmation",
    "application_confirmation",
    "thank-you",
    "thank_you",
    "confirmation",
)
_CONFIRMATION_ATTRIBUTES = (
    "confirmation",
    "application-submitted",
    "application-success",
    "thank-you",
)


@dataclass(frozen=True)
class ConfirmationEvidence:
    confirmed: bool
    evidence: str | None = None


def detect_submission_confirmation(url: str, html: str) -> ConfirmationEvidence:
    """Require explicit application-specific confirmation, not generic words."""
    soup = BeautifulSoup(html or "", "html.parser")
    text = " ".join(soup.stripped_strings).casefold()
    if any(phrase in text for phrase in _CONFIRMATION_PHRASES):
        return ConfirmationEvidence(True, "confirmation_phrase")

    for element in soup.find_all(True):
        structural = " ".join(
            str(element.get(attribute, ""))
            for attribute in ("id", "class", "data-testid", "data-qa")
        ).casefold()
        if any(marker in structural for marker in _CONFIRMATION_ATTRIBUTES):
            if any(word in text for word in ("application", "candidate", "apply")):
                return ConfirmationEvidence(True, "confirmation_structure")

    path = urlparse(url or "").path.casefold()
    if any(marker in path for marker in _CONFIRMATION_PATH_MARKERS) and any(
        phrase in text
        for phrase in (
            "application",
            "thanks for applying",
            "thank you",
        )
    ):
        return ConfirmationEvidence(True, "confirmation_redirect")
    return ConfirmationEvidence(False)


def browser_submission_result(
    *,
    platform: str,
    page_url: str,
    html: str,
) -> SubmissionResult:
    """Convert a post-click page into success or an indeterminate outcome."""
    confirmation = detect_submission_confirmation(page_url, html)
    if confirmation.confirmed:
        return SubmissionResult(
            success=True,
            platform=platform,
            status="submitted",
            reason_code="SUBMITTED",
            diagnostic_details={
                "terminal_reason": "SUBMITTED",
            },
        )

    low = (html or "").casefold()
    if any(
        marker in low
        for marker in (
            "captcha",
            "recaptcha",
            "hcaptcha",
            "verify you are human",
            "security challenge",
        )
    ):
        return SubmissionResult(
            success=False,
            platform=platform,
            status="unknown",
            error="SUBMIT_OUTCOME_UNKNOWN",
            reason_code="CHALLENGE_AFTER_SUBMIT",
        )
    return SubmissionResult(
        success=False,
        platform=platform,
        status="unknown",
        error="Submit clicked but no success confirmation appeared",
        reason_code="SUBMIT_UNCONFIRMED",
    )
