"""Exact Lever candidate URL identity tests."""

from __future__ import annotations

import pytest

from jobs.models import JobData
from submitters.lever import LeverSubmitter
from submitters.lever_identity import (
    LeverIdentityError,
    canonical_lever_apply_url,
    canonical_lever_job_url,
    canonical_lever_listing_url,
    is_lever_public_url,
    parse_lever_posting_identity,
    same_lever_posting,
)
from submitters.platforms import adapter_for_url, detect_platform

POSTING = "11111111-2222-4333-8444-555555555555"


@pytest.mark.parametrize("hostname", ["jobs.lever.co", "jobs.eu.lever.co"])
@pytest.mark.parametrize("suffix", ["", "/", "/apply", "/apply/"])
def test_exact_candidate_hosts_and_routes_are_canonical(hostname: str, suffix: str) -> None:
    identity = parse_lever_posting_identity(f"https://{hostname}/sample-company/{POSTING}{suffix}")

    assert identity.hostname == hostname
    assert identity.site == "sample-company"
    assert identity.posting_id == POSTING
    assert identity.job_url == f"https://{hostname}/sample-company/{POSTING}"
    assert identity.apply_url == f"https://{hostname}/sample-company/{POSTING}/apply"
    assert canonical_lever_job_url(identity.apply_url) == identity.job_url
    assert canonical_lever_apply_url(identity.job_url) == identity.apply_url


@pytest.mark.parametrize(
    "url",
    [
        f"http://jobs.lever.co/sample-company/{POSTING}",
        f"https://lever.co/sample-company/{POSTING}",
        f"https://api.lever.co/v0/postings/sample-company/{POSTING}",
        f"https://jobs.lever.co.evil.test/sample-company/{POSTING}",
        f"https://evil.jobs.lever.co/sample-company/{POSTING}",
        f"https://user@jobs.lever.co/sample-company/{POSTING}",
        f"https://jobs.lever.co:444/sample-company/{POSTING}",
        f"https://jobs.lever.co/sample-company/{POSTING}?source=test",
        f"https://jobs.lever.co/sample-company/{POSTING}#apply",
        "https://jobs.lever.co/sample-company/not-a-uuid",
        f"https://jobs.lever.co/sample-company/{POSTING}/thanks",
        f"https://jobs.lever.co/sample-company/{POSTING}/apply/extra",
        "https://jobs.lever.co/",
        "https://jobs.lever.co/sample-company",
    ],
)
def test_posting_identity_rejects_noncanonical_or_ambiguous_urls(url: str) -> None:
    with pytest.raises(LeverIdentityError):
        parse_lever_posting_identity(url)


def test_listing_identity_is_exact_and_cannot_be_a_posting() -> None:
    listing = "https://jobs.lever.co/sample-company/"

    assert canonical_lever_listing_url(listing) == "https://jobs.lever.co/sample-company"
    assert is_lever_public_url(listing) is True
    with pytest.raises(LeverIdentityError):
        canonical_lever_listing_url(f"https://jobs.lever.co/sample-company/{POSTING}")


def test_detection_uses_exact_hosts_without_subdomain_or_suffix_laundering() -> None:
    valid = f"https://jobs.lever.co/sample-company/{POSTING}"
    evil = f"https://jobs.lever.co.evil.test/sample-company/{POSTING}"
    subdomain = f"https://evil.jobs.lever.co/sample-company/{POSTING}"

    assert detect_platform(valid) == "lever"
    assert adapter_for_url(valid).platform == "lever"
    assert detect_platform(evil) == "generic_portal"
    assert detect_platform(subdomain) == "generic_portal"


def test_identity_comparison_includes_region_host_site_and_posting() -> None:
    base = f"https://jobs.lever.co/sample-company/{POSTING}"

    assert same_lever_posting(base, f"{base}/apply") is True
    assert (
        same_lever_posting(
            base,
            f"https://jobs.eu.lever.co/sample-company/{POSTING}",
        )
        is False
    )


def test_legacy_shim_only_recognizes_exact_posting_and_cannot_submit() -> None:
    shim = LeverSubmitter(api_key="must-not-be-retained")
    valid = JobData(
        title="Fixture",
        apply_url=f"https://jobs.lever.co/sample-company/{POSTING}",
    )
    invalid = JobData(
        title="Fixture",
        apply_url=f"https://jobs.lever.co.evil.test/sample-company/{POSTING}",
    )

    assert shim.can_submit(valid) is True
    assert shim.can_submit(invalid) is False
    assert shim._extract_posting_id(valid.apply_url) == POSTING
    assert shim._extract_company(valid.apply_url) == "sample-company"
