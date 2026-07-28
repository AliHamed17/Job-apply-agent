from __future__ import annotations

import pytest

from jobs.models import JobData
from submitters.ashby import AshbySubmitter
from submitters.ashby_identity import (
    ASHBY_CANDIDATE_HOST,
    AshbyApplicationIdentity,
    AshbyCandidateRoute,
    AshbyIdentityError,
    canonical_ashby_application_url,
    parse_ashby_candidate_url,
)
from submitters.platforms import (
    TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    QualificationTier,
    adapter_for_platform,
    detect_platform,
)
from submitters.registry import get_two_phase_registry

POSTING = "4f44b0a5-5482-4be6-bc11-3d89040b9fa1"
JOB_URL = f"https://jobs.ashbyhq.com/example/{POSTING}"
APPLICATION_URL = f"{JOB_URL}/application"


@pytest.mark.parametrize(
    ("board_token", "posting_id"),
    [
        ("bad/board", POSTING),
        ("example", POSTING.upper()),
        ("example", "not-a-uuid"),
    ],
)
def test_identity_object_cannot_bypass_canonical_parser_rules(
    board_token: str,
    posting_id: str,
) -> None:
    with pytest.raises(AshbyIdentityError):
        AshbyApplicationIdentity(board_token=board_token, posting_id=posting_id)


@pytest.mark.parametrize(
    ("url", "route"),
    [
        (JOB_URL, AshbyCandidateRoute.JOB),
        (APPLICATION_URL, AshbyCandidateRoute.APPLICATION),
        (
            f"{APPLICATION_URL}?utm_source=fixture&utm_campaign=qualification",
            AshbyCandidateRoute.APPLICATION,
        ),
    ],
)
def test_exact_candidate_routes_share_one_identity(
    url: str,
    route: AshbyCandidateRoute,
) -> None:
    parsed = parse_ashby_candidate_url(url)

    assert parsed.hostname == ASHBY_CANDIDATE_HOST
    assert parsed.identity.board_token == "example"
    assert parsed.identity.posting_id == POSTING
    assert parsed.route is route
    assert canonical_ashby_application_url(url) == APPLICATION_URL
    assert detect_platform(url) == "ashby"


@pytest.mark.parametrize(
    "url",
    [
        f"http://jobs.ashbyhq.com/example/{POSTING}",
        f"https://evil.example/example/{POSTING}",
        f"https://jobs.ashbyhq.com.evil.example/example/{POSTING}",
        f"https://jobs.ashbyhq.com./example/{POSTING}",
        f"https://user@jobs.ashbyhq.com/example/{POSTING}",
        f"https://jobs.ashbyhq.com:444/example/{POSTING}",
        f"https://jobs.ashbyhq.com/example/{POSTING}/apply",
        f"https://jobs.ashbyhq.com/example/{POSTING}/application/extra",
        "https://jobs.ashbyhq.com/example/not-a-uuid",
        f"https://jobs.ashbyhq.com/example/{POSTING.upper()}",
        f"https://jobs.ashbyhq.com/example/{POSTING}?jobPostingId={POSTING}",
        f"https://jobs.ashbyhq.com/example/{POSTING}?utm_source=",
        f"https://jobs.ashbyhq.com/example/{POSTING}?utm_source=a&utm_source=b",
        f"https://jobs.ashbyhq.com/example/{POSTING}?utm_source=%00fixture",
        f"https://jobs.ashbyhq.com/example/{POSTING}?utm_source=fixture%5Cescape",
        f"https://example.ashbyhq.com/jobs/{POSTING}",
        f"https://api.ashbyhq.com/applicationForm.submit?posting={POSTING}",
    ],
)
def test_near_match_urls_are_not_candidate_authority(url: str) -> None:
    with pytest.raises(AshbyIdentityError):
        parse_ashby_candidate_url(url)

    assert detect_platform(url) != "ashby"
    assert (
        AshbySubmitter().can_submit(JobData(title="Role", company="Example", apply_url=url))
        is False
    )


def test_descriptor_is_fixture_only_with_empty_live_scope() -> None:
    descriptor = adapter_for_platform("ashby")

    assert descriptor is not None
    assert descriptor.adapter_version == "1.0.0"
    assert descriptor.selector_version == "ashby-candidate-v1"
    assert descriptor.transport == "browser"
    assert descriptor.authentication_mode == "public_candidate_flow"
    assert descriptor.qualification is QualificationTier.FIXTURE_QUALIFIED
    assert descriptor.qualified_form_scope == ()
    assert descriptor.execution_contract_version == TWO_PHASE_EXECUTION_CONTRACT_VERSION
    assert descriptor.allows_live_submission is False
    assert descriptor.allows_final_execution is False


def test_fixture_qualified_adapter_is_not_an_ordinary_employer_inspector() -> None:
    registry = get_two_phase_registry()
    job = JobData(
        title="Role",
        company="Example",
        apply_url=APPLICATION_URL,
    )

    assert registry.get_inspector(job) is None
    assert (
        registry.get_final_executor(
            job,
            adapter_version="1.0.0",
            selector_version="ashby-candidate-v1",
            execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
            form_fingerprint="f" * 64,
        )
        is None
    )


@pytest.mark.asyncio
async def test_legacy_submitter_is_always_draft_only() -> None:
    submitter = AshbySubmitter()
    result = await submitter.submit(
        JobData(title="Role", company="Example", apply_url=JOB_URL),
        application=object(),  # type: ignore[arg-type]
        user_profile={},
        resume_path=None,
    )

    assert result.success is False
    assert result.status == "draft_only"
    assert result.reason_code == "ADAPTER_NOT_QUALIFIED"
    assert result.confirmation_id is None
