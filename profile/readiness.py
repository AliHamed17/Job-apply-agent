"""Stage-specific, fail-closed candidate-profile readiness checks.

Discovery must not needlessly depend on private identity.  Preparation and
submission do, however, remain blocked until the operator has supplied the
facts required by those stages.  All public results are stable reason codes;
no candidate values are returned or logged here.
"""

from __future__ import annotations

from profile.models import UserProfile

_PLACEHOLDER_NAMES = {"jane doe", "john doe", "your name", "example user"}
_PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net"}
_PLACEHOLDER_LOCATIONS = {
    "city, country",
    "your city",
    "your current location",
    "your location",
    "your preferred location",
    "location",
}


def _identity_issues(profile: UserProfile) -> list[str]:
    issues: list[str] = []
    name = profile.personal.name.strip().casefold()
    email = profile.personal.email.strip().casefold()
    if not name or name in _PLACEHOLDER_NAMES:
        issues.append("PROFILE_NAME_PLACEHOLDER")
    if (
        not email
        or "@" not in email
        or any(email.endswith(f"@{domain}") for domain in _PLACEHOLDER_DOMAINS)
    ):
        issues.append("PROFILE_EMAIL_PLACEHOLDER")
    return issues


def profile_discovery_readiness_issues(profile: UserProfile) -> list[str]:
    """Return only the non-private blockers for scheduled job discovery."""

    issues: list[str] = []
    if not any(role.strip() for role in profile.preferences.roles):
        issues.append("PROFILE_TARGET_ROLES_MISSING")
    locations = {
        location.strip().casefold()
        for location in profile.preferences.locations
        if location.strip()
    }
    if not locations:
        issues.append("PROFILE_SEARCH_LOCATIONS_MISSING")
    elif locations <= _PLACEHOLDER_LOCATIONS:
        issues.append("PROFILE_SEARCH_LOCATIONS_PLACEHOLDER")
    if not (
        profile.preferences.remote_ok
        or profile.preferences.hybrid_ok
        or profile.preferences.onsite_ok
    ):
        issues.append("PROFILE_WORKPLACE_PREFERENCE_MISSING")
    return issues


def profile_preparation_readiness_issues(profile: UserProfile) -> list[str]:
    """Return profile blockers for CV routing and material preparation."""

    issues = [
        *profile_discovery_readiness_issues(profile),
        *_identity_issues(profile),
    ]
    current_location = profile.personal.location.strip().casefold()
    if not current_location:
        issues.append("PROFILE_CURRENT_LOCATION_MISSING")
    elif current_location in _PLACEHOLDER_LOCATIONS:
        issues.append("PROFILE_CURRENT_LOCATION_PLACEHOLDER")
    return issues


def profile_submission_readiness_issues(profile: UserProfile) -> list[str]:
    """Return profile blockers for any employer-facing external action."""

    issues = profile_preparation_readiness_issues(profile)
    if not profile.personal.phone.strip():
        issues.append("PROFILE_PHONE_MISSING")
    confirmed = profile.evidence.user_confirmed
    if not str(confirmed.get("work_authorization") or "").strip():
        issues.append("PROFILE_WORK_AUTHORIZATION_UNCONFIRMED")
    if not str(confirmed.get("visa_sponsorship") or "").strip():
        issues.append("PROFILE_SPONSORSHIP_UNCONFIRMED")
    if not any(str(confirmed.get(key) or "").strip() for key in ("citizenship", "nationality")):
        issues.append("PROFILE_CITIZENSHIP_OR_NATIONALITY_UNCONFIRMED")
    return issues


def profile_readiness_issues(profile: UserProfile) -> list[str]:
    """Backward-compatible preparation check for legacy automation callers.

    The legacy function retains its resume-text requirement.  New discovery
    and submission paths must call their explicit stage function instead.
    """

    issues = profile_preparation_readiness_issues(profile)
    if len(profile.resume.text.strip()) < 100:
        issues.append("PROFILE_RESUME_MISSING")
    return issues
