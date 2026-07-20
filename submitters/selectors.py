"""Centralized LinkedIn Easy Apply selector fallback chains."""

from __future__ import annotations

EASY_APPLY_BUTTON = [
    "button.jobs-apply-button",
    'button[aria-label*="Easy Apply"]',
    'button[data-control-name="jobdetails_topcard_inapply"]',
]
SUBMIT_BUTTON = [
    'button[aria-label*="Submit application"]',
    'button[data-control-name="submit_unify"]',
]
NEXT_BUTTON = [
    'button[aria-label*="Continue to next step"]',
    'button[data-control-name="continue_unify"]',
]
REVIEW_BUTTON = ['button[aria-label*="Review your application"]']
DISCARD_BUTTON = ['button[aria-label*="Dismiss"]', 'button[aria-label*="Discard"]']
DISCARD_CONFIRM_BUTTON = ['button[data-control-name="discard_application_confirm_btn"]',
                          'button[aria-label*="Discard"]']
SUCCESS_DIALOG = ['div.artdeco-modal:has-text("Application sent")',
                  'h2:has-text("Your application was sent")',
                  'div:has-text("Application sent")']
FORM_FIELD_CONTAINER = ['.jobs-easy-apply-form-section__grouping',
                        '.fb-dash-form-element',
                        '.jobs-easy-apply-form-element']


def join(selectors: list[str]) -> str:
    return ", ".join(selectors)
