from profile.models import (
    Personal,
    Preferences,
    ProfileEvidence,
    Resume,
    UserProfile,
)
from profile.readiness import (
    profile_discovery_readiness_issues,
    profile_readiness_issues,
    profile_submission_readiness_issues,
)


def test_placeholder_profile_is_blocked():
    profile = UserProfile(
        personal=Personal(
            name="Jane Doe",
            email="jane.doe@example.com",
            location="Israel",
        ),
        resume=Resume(text="Experienced engineer. " * 20),
        preferences=Preferences(roles=["Software Engineer"], locations=["Israel"]),
    )

    assert profile_readiness_issues(profile) == [
        "PROFILE_NAME_PLACEHOLDER",
        "PROFILE_EMAIL_PLACEHOLDER",
    ]


def test_real_minimum_profile_is_ready():
    profile = UserProfile(
        personal=Personal(
            name="Candidate Name",
            email="candidate@domain.test",
            location="Israel",
        ),
        resume=Resume(text="Experienced engineer. " * 20),
        preferences=Preferences(roles=["Software Engineer"], locations=["Israel"]),
    )

    assert profile_readiness_issues(profile) == []


def test_placeholder_identity_does_not_block_discovery():
    profile = UserProfile(
        personal=Personal(name="Jane Doe", email="jane.doe@example.com"),
        preferences=Preferences(
            roles=["Machine Learning Engineer"],
            locations=["Israel", "Worldwide Remote"],
        ),
    )

    assert profile_discovery_readiness_issues(profile) == []


def test_discovery_requires_real_roles_locations_and_workplace_preference():
    profile = UserProfile(
        preferences=Preferences(
            roles=[""],
            locations=["Your preferred location"],
            remote_ok=False,
            hybrid_ok=False,
            onsite_ok=False,
        )
    )

    assert profile_discovery_readiness_issues(profile) == [
        "PROFILE_TARGET_ROLES_MISSING",
        "PROFILE_SEARCH_LOCATIONS_PLACEHOLDER",
        "PROFILE_WORKPLACE_PREFERENCE_MISSING",
    ]


def test_discovery_rejects_placeholder_mixed_with_real_location():
    profile = UserProfile(
        preferences=Preferences(
            roles=["Software Engineer"],
            locations=["Your preferred location", "Israel"],
        )
    )

    assert profile_discovery_readiness_issues(profile) == ["PROFILE_SEARCH_LOCATIONS_PLACEHOLDER"]


def test_submission_requires_operator_confirmed_identity_and_legal_facts():
    profile = UserProfile(
        personal=Personal(
            name="Candidate Name",
            email="candidate@domain.test",
            phone="+972 50 000 0000",
            location="Israel",
        ),
        preferences=Preferences(
            roles=["Software Engineer"],
            locations=["Israel"],
        ),
        evidence=ProfileEvidence(
            user_confirmed={
                "work_authorization": "Confirmed by operator",
                "visa_sponsorship": "Confirmed by operator",
                "nationality": "Confirmed by operator",
            }
        ),
    )

    assert profile_submission_readiness_issues(profile) == []


def test_placeholder_current_location_blocks_preparation_and_submission():
    profile = UserProfile(
        personal=Personal(
            name="Candidate Name",
            email="candidate@domain.test",
            phone="+972 50 000 0000",
            location="City, Country",
        ),
        preferences=Preferences(
            roles=["Software Engineer"],
            locations=["Israel"],
        ),
        evidence=ProfileEvidence(
            user_confirmed={
                "work_authorization": "Confirmed",
                "visa_sponsorship": "Confirmed",
                "nationality": "Confirmed",
            }
        ),
    )

    assert "PROFILE_CURRENT_LOCATION_PLACEHOLDER" in profile_readiness_issues(profile)
    assert "PROFILE_CURRENT_LOCATION_PLACEHOLDER" in (profile_submission_readiness_issues(profile))


def test_shipped_placeholder_phone_blocks_submission():
    profile = UserProfile(
        personal=Personal(
            name="Candidate Name",
            email="candidate@domain.test",
            phone="+10000000000",
            location="Israel",
        ),
        preferences=Preferences(
            roles=["Software Engineer"],
            locations=["Israel"],
        ),
        evidence=ProfileEvidence(
            user_confirmed={
                "work_authorization": "Confirmed",
                "visa_sponsorship": "Confirmed",
                "nationality": "Confirmed",
            }
        ),
    )

    assert "PROFILE_PHONE_PLACEHOLDER" in profile_submission_readiness_issues(profile)
