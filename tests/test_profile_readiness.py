from profile.models import Personal, Preferences, Resume, UserProfile
from profile.readiness import profile_readiness_issues


def test_placeholder_profile_is_blocked():
    profile = UserProfile(
        personal=Personal(name="Jane Doe", email="jane.doe@example.com"),
        resume=Resume(text="Experienced engineer. " * 20),
        preferences=Preferences(roles=["Software Engineer"]),
    )

    assert profile_readiness_issues(profile) == [
        "PROFILE_NAME_PLACEHOLDER",
        "PROFILE_EMAIL_PLACEHOLDER",
    ]


def test_real_minimum_profile_is_ready():
    profile = UserProfile(
        personal=Personal(name="Candidate Name", email="candidate@domain.test"),
        resume=Resume(text="Experienced engineer. " * 20),
        preferences=Preferences(roles=["Software Engineer"]),
    )

    assert profile_readiness_issues(profile) == []
