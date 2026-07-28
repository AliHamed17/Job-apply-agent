"""Strict public numeric ID and read-only UUID resolver coverage."""

from __future__ import annotations

from pathlib import Path

import pytest

from submitters.platforms import detect_platform
from submitters.smartrecruiters_identity import (
    SmartRecruitersIdentityError,
    parse_smartrecruiters_candidate_identity,
    resolve_smartrecruiters_posting_identity,
    same_smartrecruiters_candidate,
)

FIXTURES = Path(__file__).parent / "fixtures" / "smartrecruiters_v1"
JOB_URL = "https://jobs.smartrecruiters.com/FixtureCo/123456789-sanitized-role"
POSTING_UUID = "11111111-2222-4333-8444-555555555555"


def test_candidate_identity_keeps_numeric_id_separate_from_uuid() -> None:
    identity = parse_smartrecruiters_candidate_identity(JOB_URL)

    assert detect_platform(JOB_URL) == "smartrecruiters"
    assert identity.company == "FixtureCo"
    assert identity.public_id == "123456789"
    assert identity.slug == "sanitized-role"
    assert identity.apply_url == f"{JOB_URL}/apply"
    assert POSTING_UUID not in identity.stable_key


@pytest.mark.parametrize(
    "url",
    [
        "http://jobs.smartrecruiters.com/FixtureCo/123456789-role",
        "https://user:secret@jobs.smartrecruiters.com/FixtureCo/123456789-role",
        "https://jobs.smartrecruiters.com:444/FixtureCo/123456789-role",
        "https://jobs.smartrecruiters.com./FixtureCo/123456789-role",
        "https://www.jobs.smartrecruiters.com/FixtureCo/123456789-role",
        "https://smartrecruiters.com/FixtureCo/123456789-role",
        "https://jobs.smartrecruiters.com.evil.test/FixtureCo/123456789-role",
        "https://jobs.smartrecruiters.com/FixtureCo/not-numeric-role",
        "https://jobs.smartrecruiters.com/FixtureCo/12345-role",
        "https://jobs.smartrecruiters.com/FixtureCo/000000-role",
        "https://jobs.smartrecruiters.com/FixtureCo/123456789-role/unknown",
        "https://jobs.smartrecruiters.com/FixtureCo/123456789-role?uuid=111",
        "https://jobs.smartrecruiters.com/FixtureCo/123456789-role#apply",
        "https://jobs.smartrecruiters.com/FixtureCo%2FInjected/123456789-role",
        "https://jobs.smartrecruiters.com/FixtureCo//123456789-role",
        "https://jobs.smartrecruiters.com/FixtureCo/123456789-role/",
    ],
)
def test_candidate_identity_rejects_non_exact_routes(url: str) -> None:
    with pytest.raises(SmartRecruitersIdentityError):
        parse_smartrecruiters_candidate_identity(url)
    assert detect_platform(url) != "smartrecruiters"


def test_slug_change_does_not_change_exact_numeric_candidate_identity() -> None:
    assert same_smartrecruiters_candidate(
        JOB_URL,
        "https://jobs.smartrecruiters.com/FixtureCo/123456789-renamed-role/apply",
    )
    assert not same_smartrecruiters_candidate(
        JOB_URL,
        "https://jobs.smartrecruiters.com/FixtureCo/987654321-sanitized-role",
    )


def test_read_only_resolver_requires_one_cross_checked_canonical_uuid() -> None:
    candidate = parse_smartrecruiters_candidate_identity(JOB_URL)
    html = (FIXTURES / "candidate_job.html").read_text(encoding="utf-8")

    resolved = resolve_smartrecruiters_posting_identity(html, candidate)

    assert resolved.candidate == candidate
    assert resolved.posting_uuid == POSTING_UUID
    assert resolved.resolver_source == "candidate_page_metadata"
    assert len(resolved.resolver_evidence_sha256) == 64


@pytest.mark.parametrize(
    "mutation",
    [
        lambda html: html.replace(POSTING_UUID, "123456789"),
        lambda html: html.replace(
            POSTING_UUID,
            "00000000-0000-0000-0000-000000000000",
        ),
        lambda html: html.replace('data-public-id="123456789"', 'data-public-id="987654321"'),
        lambda html: html.replace("FixtureCo", "OtherCo", 1),
        lambda html: html.replace(
            "</main>",
            (
                f'<i data-qa="posting-identity" data-company="FixtureCo" '
                f'data-public-id="123456789" data-posting-uuid="{POSTING_UUID}" '
                f'data-candidate-url="{JOB_URL}"></i></main>'
            ),
        ),
        lambda html: html.replace('data-qa="posting-identity"', 'data-qa="unknown-identity"'),
    ],
)
def test_resolver_never_guesses_or_derives_from_public_route(mutation) -> None:
    candidate = parse_smartrecruiters_candidate_identity(JOB_URL)
    html = (FIXTURES / "candidate_job.html").read_text(encoding="utf-8")

    with pytest.raises(SmartRecruitersIdentityError):
        resolve_smartrecruiters_posting_identity(mutation(html), candidate)
