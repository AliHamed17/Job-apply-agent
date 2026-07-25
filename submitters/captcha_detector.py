"""CAPTCHA and security challenge detector for browser automation submitters."""

from __future__ import annotations

from dataclasses import dataclass
import structlog

logger = structlog.get_logger(__name__)


@dataclass
class SecurityChallengeReport:
    captcha_detected: bool
    challenge_type: str | None = None
    action_required: str = "none"


def detect_security_challenges(page_html: str, url: str) -> SecurityChallengeReport:
    """Analyze page content for CAPTCHA & bot protection challenges."""
    html_lower = (page_html or "").lower()

    if "cf-turnstile" in html_lower or "challenges.cloudflare.com" in html_lower or "just a moment..." in html_lower:
        return SecurityChallengeReport(
            captcha_detected=True,
            challenge_type="cloudflare",
            action_required="human_intervention",
        )

    if "g-recaptcha" in html_lower or "recaptcha/api.js" in html_lower or "google.com/recaptcha" in html_lower:
        return SecurityChallengeReport(
            captcha_detected=True,
            challenge_type="recaptcha",
            action_required="human_intervention",
        )

    if "hcaptcha.com" in html_lower or "h-captcha" in html_lower:
        return SecurityChallengeReport(
            captcha_detected=True,
            challenge_type="hcaptcha",
            action_required="human_intervention",
        )

    if "datadome" in html_lower or "incapsula" in html_lower:
        return SecurityChallengeReport(
            captcha_detected=True,
            challenge_type="datadome",
            action_required="human_intervention",
        )

    return SecurityChallengeReport(
        captcha_detected=False,
        challenge_type=None,
        action_required="none",
    )
