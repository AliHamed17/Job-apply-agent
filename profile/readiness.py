"""Fail-closed checks for profiles used by automatic processing."""

from __future__ import annotations

from profile.models import UserProfile

_PLACEHOLDER_NAMES = {"jane doe", "john doe", "your name", "example user"}
_PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net"}


def profile_readiness_issues(profile: UserProfile) -> list[str]:
    """Return stable, non-sensitive reasons automation must not use a profile."""
    issues: list[str] = []
    name = profile.personal.name.strip().casefold()
    email = profile.personal.email.strip().casefold()
    resume_text = profile.resume.text.strip()

    if not name or name in _PLACEHOLDER_NAMES:
        issues.append("PROFILE_NAME_PLACEHOLDER")
    if not email or any(email.endswith(f"@{domain}") for domain in _PLACEHOLDER_DOMAINS):
        issues.append("PROFILE_EMAIL_PLACEHOLDER")
    if len(resume_text) < 100:
        issues.append("PROFILE_RESUME_MISSING")
    if not profile.preferences.roles:
        issues.append("PROFILE_TARGET_ROLES_MISSING")
    return issues
