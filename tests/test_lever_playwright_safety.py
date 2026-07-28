"""Transport-level safety tests for Lever browser v1."""

from __future__ import annotations

import inspect

import pytest

import submitters.lever_playwright as lever_transport
from core.submission_domain import ReasonCode
from submitters.lever_playwright import (
    LeverFinalActionAmbiguousError,
    LeverNetworkGuard,
    PlaywrightLeverCandidateSession,
    canonical_multipart_payload_sha256,
)
from submitters.lever_v1 import LeverAdapterBlockedError, LeverFinalActionProof

POSTING = "11111111-2222-4333-8444-555555555555"
APPLY_URL = f"https://jobs.lever.co/sample-company/{POSTING}/apply"
CV_BYTES = b"%PDF-1.4\nsanitized fixture resume\n%%EOF\n"


def _resolver(_host, _port, _family, _socket_type):
    return [(2, 1, 6, "", ("8.8.8.8", 443))]


def _multipart(
    *,
    resume: bytes = CV_BYTES,
    extra_file: bool = False,
    candidate_name: str = "Fixture Candidate",
) -> tuple[str, bytes]:
    boundary = "----lever-fixture-boundary"
    parts = [
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="authenticity_token"\r\n\r\n'
            "fixture-token\r\n"
        ).encode(),
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="name"\r\n\r\n'
            f"{candidate_name}\r\n"
        ).encode(),
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="resume"; filename="resume-fixture.pdf"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode()
        + resume
        + b"\r\n",
    ]
    if extra_file:
        parts.append(
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="cover"; filename="cover.txt"\r\n'
                "Content-Type: text/plain\r\n\r\nfixture\r\n"
            ).encode()
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


def test_network_guard_requires_exact_host_posting_and_public_dns() -> None:
    guard = LeverNetworkGuard(APPLY_URL, resolver=_resolver)

    guard.require_allowed_url(APPLY_URL, main_frame=True)
    guard.require_allowed_url(
        "https://jobs.lever.co/assets/application.css",
        main_frame=False,
    )

    with pytest.raises(LeverAdapterBlockedError):
        guard.require_allowed_url(
            f"https://jobs.eu.lever.co/sample-company/{POSTING}/apply",
            main_frame=True,
        )
    with pytest.raises(LeverAdapterBlockedError):
        guard.require_allowed_url(
            f"https://jobs.lever.co/other-company/{POSTING}/apply",
            main_frame=True,
        )
    with pytest.raises(LeverAdapterBlockedError):
        guard.require_allowed_url(
            "https://jobs.lever.co.evil.test/asset.js",
            main_frame=False,
        )


def test_network_guard_rejects_private_or_mixed_dns_answers() -> None:
    def private_resolver(_host, _port, _family, _socket_type):
        return [
            (2, 1, 6, "", ("8.8.8.8", 443)),
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ]

    guard = LeverNetworkGuard(APPLY_URL, resolver=private_resolver)

    with pytest.raises(LeverAdapterBlockedError) as exc_info:
        guard.require_allowed_url(APPLY_URL, main_frame=True)
    assert exc_info.value.reason_code is ReasonCode.RUNTIME_NOT_READY


def test_multipart_commitment_binds_order_values_and_exact_cv_bytes() -> None:
    content_type, body = _multipart()
    cv_hash = __import__("hashlib").sha256(CV_BYTES).hexdigest()
    digest = canonical_multipart_payload_sha256(
        content_type=content_type,
        body=body,
        expected_cv_sha256=cv_hash,
    )
    changed_type, changed_body = _multipart(candidate_name="Changed Candidate")

    assert digest is not None
    assert (
        canonical_multipart_payload_sha256(
            content_type=changed_type,
            body=changed_body,
            expected_cv_sha256=cv_hash,
        )
        != digest
    )
    assert (
        canonical_multipart_payload_sha256(
            content_type=content_type,
            body=body,
            expected_cv_sha256="0" * 64,
        )
        is None
    )


@pytest.mark.parametrize(
    "body_factory",
    [
        lambda: _multipart(resume=b"different"),
        lambda: _multipart(extra_file=True),
    ],
)
def test_multipart_commitment_rejects_wrong_or_multiple_files(body_factory) -> None:
    content_type, body = body_factory()
    cv_hash = __import__("hashlib").sha256(CV_BYTES).hexdigest()

    assert (
        canonical_multipart_payload_sha256(
            content_type=content_type,
            body=body,
            expected_cv_sha256=cv_hash,
        )
        is None
    )


class _Route:
    def __init__(self) -> None:
        self.continued = 0
        self.aborted = 0

    async def continue_(self) -> None:
        self.continued += 1

    async def abort(self, _reason: str) -> None:
        self.aborted += 1


class _Request:
    def __init__(
        self,
        *,
        method: str,
        content_type: str = "",
        body: bytes = b"",
        navigation: bool = False,
        resource_type: str = "fetch",
        frame=None,
        url: str = APPLY_URL,
    ) -> None:
        self.method = method
        self.url = url
        self.headers = {"content-type": content_type}
        self.post_data_buffer = body
        self.resource_type = resource_type
        self.frame = frame
        self._navigation = navigation

    def is_navigation_request(self) -> bool:
        return self._navigation


class _Page:
    def __init__(self) -> None:
        self.main_frame = object()


class _ActionabilityDriftPage(_Page):
    def __init__(self, proof: LeverFinalActionProof) -> None:
        super().__init__()
        self.proof = proof
        self.evaluate_calls = 0

    async def evaluate(self, _script, payload):
        self.evaluate_calls += 1
        assert payload["release"] is True
        return {
            "valid": True,
            "released": True,
            "payloadDigest": self.proof.payload_commitment_sha256,
            "actionabilityDigest": "f" * 64,
            "resumeControlDigest": self.proof.resume_control_sha256,
        }

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        raise AssertionError("drift must stop before confirmation wait")


@pytest.mark.asyncio
async def test_precommit_mutation_is_aborted_and_counted() -> None:
    session = PlaywrightLeverCandidateSession()
    session._guard = LeverNetworkGuard(APPLY_URL, resolver=_resolver)
    session._page = _Page()
    route = _Route()

    await session._guard_request(route, _Request(method="POST"))

    assert route.aborted == 1
    assert route.continued == 0
    assert session._guard.precommit_mutation_count == 1


@pytest.mark.asyncio
async def test_only_exact_main_frame_document_post_can_cross_final_boundary() -> None:
    import hashlib

    content_type, body = _multipart()
    cv_hash = hashlib.sha256(CV_BYTES).hexdigest()
    payload_digest = canonical_multipart_payload_sha256(
        content_type=content_type,
        body=body,
        expected_cv_sha256=cv_hash,
    )
    assert payload_digest is not None
    page = _Page()
    proof = LeverFinalActionProof(
        identity_sha256="1" * 64,
        action_url_sha256="2" * 64,
        form_fingerprint="3" * 64,
        method="POST",
        encoding="multipart/form-data",
        submitter_sha256="4" * 64,
        actionability_sha256="6" * 64,
        resume_control_sha256="5" * 64,
        attached_cv_sha256=cv_hash,
        payload_commitment_sha256=payload_digest,
        user_field_count=3,
        precommit_mutation_count=0,
    )
    session = PlaywrightLeverCandidateSession()
    session._guard = LeverNetworkGuard(APPLY_URL, resolver=_resolver)
    session._identity = session._guard.identity
    session._page = page
    session._expected_proof = proof
    session._release_started = True
    accepted = _Route()

    await session._guard_request(
        accepted,
        _Request(
            method="POST",
            content_type=content_type,
            body=body,
            navigation=True,
            resource_type="document",
            frame=page.main_frame,
        ),
    )

    assert accepted.continued == 1
    assert accepted.aborted == 0
    assert session._final_request_count == 1
    assert session._final_request_valid is True

    duplicate = _Route()
    await session._guard_request(
        duplicate,
        _Request(
            method="POST",
            content_type=content_type,
            body=body,
            navigation=True,
            resource_type="document",
            frame=page.main_frame,
        ),
    )
    assert duplicate.aborted == 1
    assert session._final_violation is True


@pytest.mark.asyncio
async def test_capture_to_submit_actionability_drift_is_ambiguous_and_never_released_twice() -> (
    None
):
    cv_hash = __import__("hashlib").sha256(CV_BYTES).hexdigest()
    proof = LeverFinalActionProof(
        identity_sha256="1" * 64,
        action_url_sha256="2" * 64,
        form_fingerprint="3" * 64,
        method="POST",
        encoding="multipart/form-data",
        submitter_sha256="4" * 64,
        actionability_sha256="6" * 64,
        resume_control_sha256="5" * 64,
        attached_cv_sha256=cv_hash,
        payload_commitment_sha256="7" * 64,
        user_field_count=3,
        precommit_mutation_count=0,
    )
    page = _ActionabilityDriftPage(proof)
    session = PlaywrightLeverCandidateSession()
    session._guard = LeverNetworkGuard(APPLY_URL, resolver=_resolver)
    session._identity = session._guard.identity
    session._page = page
    session._expected_proof = proof
    session._expected_js = {"release": False}

    with pytest.raises(LeverFinalActionAmbiguousError):
        await session.click_final_action(proof)

    assert page.evaluate_calls == 1
    assert session._clicked is True
    assert session._release_started is True


def test_transport_source_has_no_password_store_or_api_fallback() -> None:
    source = inspect.getsource(lever_transport).casefold()

    assert "login data" not in source
    assert "chrome password" not in source
    assert "edge password" not in source
    assert "api.lever.co" not in source
    assert "proxy" not in source
    assert "stealth" not in source
    assert "requestsubmit" in source
    assert "expectedactionabilitydigest" in source
    assert "getboundingclientrect" in source
    assert "pointerevents" in source
    assert "contentvisibility" in source
    assert "current = current.parentelement" in source


def test_release_has_no_await_mutation_or_request_after_final_actionability_recheck() -> None:
    script = lever_transport._FORM_PROOF_SCRIPT
    start = script.index("const finalActionability =")
    submit = script.index("form.requestSubmit(submit);", start)
    critical_section = script[start:submit]

    assert "finalActionabilityState" in critical_section
    assert "structureValid()" in critical_section
    assert "expectedActionabilityState" in critical_section
    assert "noExistingConfirmation()" in script
    assert "await " not in critical_section
    for forbidden in (
        "new FormData",
        "fetch(",
        "XMLHttpRequest",
        "createElement",
        "append(",
        "appendChild",
        "insertAdjacent",
        "insertBefore",
        "replaceChild",
        "setAttribute",
        "dispatchEvent",
        ".click(",
    ):
        assert forbidden not in critical_section
