"""Network and browser-safety contracts for Greenhouse browser v1."""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
from pathlib import Path

import pytest

from core.submission_domain import (
    VERIFIED_ATTACHMENT_EVIDENCE_REF,
    VERIFIED_ATTACHMENT_SENTINEL,
    AnswerDecisionV1,
    AnswerDisposition,
    AnswerProvenance,
    FieldType,
    FormFieldV1,
    ReasonCode,
    SensitiveCategory,
)
from submitters.greenhouse_identity import (
    GreenhouseCandidateRoute,
    parse_greenhouse_candidate_url,
)
from submitters.greenhouse_playwright import (
    _ACTION_IDENTITY_ENTRIES_SCRIPT,
    _ATOMIC_NATIVE_SUBMIT_SCRIPT,
    _FINAL_CONTROL_STATE_SCRIPT,
    _FORM_PAYLOAD_COMMITMENT_SCRIPT,
    GreenhouseNetworkGuard,
    PlaywrightGreenhouseCandidateSession,
    _multipart_payload_commitment,
    _OutboundPostGate,
)
from submitters.greenhouse_v1 import (
    GREENHOUSE_V1_NATIVE_TRANSPORT,
    GreenhouseAdapterBlockedError,
    GreenhouseAnswerBinding,
    GreenhouseAtomicCommitExpectation,
    GreenhouseAttachmentProof,
    GreenhouseReviewedAnswerBinding,
    GreenhouseSubmitterBinding,
    detect_greenhouse_variant,
    greenhouse_public_hostname,
    greenhouse_v1_form_fingerprint,
    greenhouse_v1_reviewed_answer_bindings,
    greenhouse_visible_confirmation_digest,
    observe_greenhouse_v1_fields,
)

FIXTURES = Path(__file__).parent / "fixtures" / "greenhouse_v1"
JOB_URL = "https://job-boards.greenhouse.io/fixture/jobs/123456"
ROUTED_CV_BYTES = b"%PDF-1.4\nsanitized routed cv\n%%EOF\n"
ROUTED_CV_SHA256 = hashlib.sha256(ROUTED_CV_BYTES).hexdigest()
MAIN_FRAME = object()


def _value_sha256(material: list[object]) -> str:
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _exact_answer_bindings(
    *,
    answer: str = "reviewed answer",
) -> tuple[
    tuple[GreenhouseAnswerBinding, ...],
    str,
    GreenhouseSubmitterBinding,
]:
    answer_name = hashlib.sha256(b"answer").hexdigest()
    resume_name = hashlib.sha256(b"resume").hexdigest()
    bindings = (
        GreenhouseAnswerBinding(
            reviewed=GreenhouseReviewedAnswerBinding(
                field_id="answer",
                field_type=FieldType.TEXT,
                value_sha256=_value_sha256(
                    ["s", answer.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")]
                ),
                successful_entry_count=1,
            ),
            control_name_sha256=answer_name,
        ),
        GreenhouseAnswerBinding(
            reviewed=GreenhouseReviewedAnswerBinding(
                field_id="resume",
                field_type=FieldType.FILE,
                value_sha256=_value_sha256(["f", ROUTED_CV_SHA256]),
                successful_entry_count=1,
            ),
            control_name_sha256=resume_name,
        ),
    )
    return (
        bindings,
        resume_name,
        GreenhouseSubmitterBinding(
            control_name_sha256=hashlib.sha256(b"commit").hexdigest(),
            value_sha256=_value_sha256(["s", "submit_application"]),
        ),
    )


def _multipart_body(
    entries: list[tuple[str, ...] | tuple[str, str, str, str, bytes]],
    *,
    boundary: str = "----GreenhouseFixtureBoundary7MA4YWxk",
) -> tuple[bytes, str]:
    chunks: list[bytes] = []
    for entry in entries:
        kind = entry[0]
        if kind == "text":
            _, name, value = entry
            chunks.extend(
                (
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    value.encode("utf-8"),
                    b"\r\n",
                )
            )
            continue
        _, name, filename, content_type, payload = entry
        assert isinstance(payload, bytes)
        chunks.extend(
            (
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                ).encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                payload,
                b"\r\n",
            )
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return (
        b"".join(chunks),
        f"multipart/form-data; boundary={boundary}",
    )


def _reviewed_payload_commitment(
    body: bytes,
    content_type: str,
    *,
    answer: str = "reviewed answer",
) -> str:
    bindings, resume_name, submitter = _exact_answer_bindings(answer=answer)
    return _multipart_payload_commitment(
        body=body,
        content_type=content_type,
        expected_cv_sha256=ROUTED_CV_SHA256,
        expected_answer_bindings=bindings,
        expected_resume_control_name_sha256=resume_name,
        expected_identity=parse_greenhouse_candidate_url(JOB_URL).identity,
        expected_submitter_binding=submitter,
    )


def _exact_native_payload(
    *,
    answer: str = "reviewed answer",
    cv_bytes: bytes = ROUTED_CV_BYTES,
    filename: str = "resume-fixture.pdf",
    action_identity: tuple[str, str] | None = None,
) -> tuple[bytes, str, str]:
    entries: list[tuple[str, ...] | tuple[str, str, str, str, bytes]] = [
        ("text", "answer", answer),
    ]
    if action_identity is not None:
        entries.append(("text", action_identity[0], action_identity[1]))
    entries.extend(
        (
            ("file", "resume", filename, "application/pdf", cv_bytes),
            ("text", "commit", "submit_application"),
        )
    )
    body, content_type = _multipart_body(entries)
    commitment = _reviewed_payload_commitment(
        body,
        content_type,
        answer=answer,
    )
    return body, content_type, commitment


class _Resolver:
    def __init__(self, addresses: dict[str, str]) -> None:
        self.addresses = addresses
        self.calls: list[str] = []

    def __call__(self, host, _port, _family, _kind):
        self.calls.append(host)
        address = self.addresses.get(host)
        if address is None:
            raise socket.gaierror("synthetic DNS miss")
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, 443))]


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.greenhouse.io/acme/jobs/123",
        "https://job-boards.greenhouse.io/acme/jobs/456",
        "https://boards.greenhouse.io/embed/job_app?for=acme&token=123",
        "https://boards.greenhouse.io/acme?gh_jid=123",
        "https://boards.greenhouse.io/acme?gh_jid=123&gh_src=fixture",
        "https://greenhouse-hosted.com/acme/jobs/123",
    ],
)
def test_initial_candidate_url_accepts_explicit_public_shapes(url: str) -> None:
    assert greenhouse_public_hostname(url) in {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "greenhouse-hosted.com",
    }


@pytest.mark.parametrize(
    ("url", "route"),
    [
        (
            "https://job-boards.greenhouse.io/acme/jobs/123",
            GreenhouseCandidateRoute.HOSTED,
        ),
        (
            "https://job-boards.greenhouse.io/embed/job_app?for=ACME&token=123",
            GreenhouseCandidateRoute.EMBEDDED,
        ),
        (
            "https://job-boards.greenhouse.io/acme?gh_jid=123",
            GreenhouseCandidateRoute.JOB_ID,
        ),
    ],
)
def test_route_variants_share_one_canonical_application_identity(
    url: str,
    route: GreenhouseCandidateRoute,
) -> None:
    candidate = parse_greenhouse_candidate_url(url)

    assert candidate.route is route
    assert candidate.application_binding == (
        "job-boards.greenhouse.io",
        "acme",
        "123",
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://boards.greenhouse.io/acme/jobs/123",
        "https://user:secret@boards.greenhouse.io/acme/jobs/123",
        "https://boards.greenhouse.io:444/acme/jobs/123",
        "https://boards.greenhouse.io/acme/jobs/123#fragment",
        "https://localhost/acme/jobs/123",
        "https://127.0.0.1/acme/jobs/123",
        "https://169.254.169.254/acme/jobs/123",
        "https://harvest.greenhouse.io/acme/jobs/123",
        "https://api.greenhouse.io/acme/jobs/123",
        "https://status.greenhouse.io/acme/jobs/123",
        "https://boards.greenhouse.io/jobs/123?gh_jid=123",
        "https://boards.greenhouse.io/admin",
        "https://boards.greenhouse.io/v1/boards/acme/jobs/123",
        "https://boards.greenhouse.io/acme/jobs/not-a-job-id",
        "https://boards.greenhouse.io/acme/jobs/123-platform-engineer",
        "https://boards.greenhouse.io/embed/job_app?for=acme&token=job_123",
        "https://boards.greenhouse.io/?gh_jid=123",
        "https://boards.greenhouse.io/acme?gh_jid=",
        "https://boards.greenhouse.io/acme/jobs/123?gh_jid=456",
        "https://boards.greenhouse.io/acme/jobs/123?gh_jid=123&gh_jid=123",
        "https://boards.greenhouse.io/acme/jobs/123?unknown=123",
        "https://boards.greenhouse.io/acme/jobs/123?gh_src=https://example.test",
        "https://boards.greenhouse.io/acme/jobs%2f123",
        "https://boards.greenhouse.io/acme/%2e%2e/jobs/123",
        "https://boards.greenhouse.io/acme/../jobs/123",
        "https://boards.greenhouse.io//acme/jobs/123",
        "https://boards.greenhouse.io/acme/jobs/123?gh_src=%2fadmin",
        "https://example.test/acme/jobs/123?gh_jid=123",
    ],
)
def test_initial_candidate_url_rejects_unsafe_or_ambiguous_shapes(url: str) -> None:
    with pytest.raises(GreenhouseAdapterBlockedError) as exc_info:
        greenhouse_public_hostname(url)

    assert exc_info.value.reason_code is ReasonCode.RUNTIME_NOT_READY


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "169.254.169.254",
        "192.0.2.10",
        "::1",
        "fc00::1",
        "fe80::1",
    ],
)
def test_dns_resolution_must_be_public_global(address: str) -> None:
    host = "job-boards.greenhouse.io"
    guard = GreenhouseNetworkGuard(JOB_URL, resolver=_Resolver({host: address}))

    with pytest.raises(GreenhouseAdapterBlockedError) as exc_info:
        guard.require_allowed_url(JOB_URL)

    assert exc_info.value.reason_code is ReasonCode.RUNTIME_NOT_READY


def test_main_frame_and_subresources_stay_on_exact_reviewed_origin() -> None:
    reviewed = "job-boards.greenhouse.io"
    other = "boards.greenhouse.io"
    resolver = _Resolver({reviewed: "8.8.8.8", other: "1.1.1.1"})
    guard = GreenhouseNetworkGuard(JOB_URL, resolver=resolver)

    guard.require_allowed_url(JOB_URL)
    guard.require_allowed_url(
        f"https://{reviewed}/assets/application.js",
        main_frame=False,
    )
    guard.require_allowed_url(
        f"https://{reviewed}/embed/job_app?for=fixture&token=123456",
        main_frame=True,
    )
    guard.require_allowed_url(
        f"https://{reviewed}/fixture?gh_jid=123456",
        main_frame=True,
    )
    with pytest.raises(GreenhouseAdapterBlockedError):
        guard.require_allowed_url(
            f"https://{other}/assets/application.js",
            main_frame=False,
        )
    with pytest.raises(GreenhouseAdapterBlockedError):
        guard.require_allowed_url(
            f"https://{reviewed}/admin",
            main_frame=True,
        )
    with pytest.raises(GreenhouseAdapterBlockedError):
        guard.require_allowed_url(
            f"https://{reviewed}/fixture/jobs/999999",
            main_frame=True,
        )
    with pytest.raises(GreenhouseAdapterBlockedError):
        guard.require_allowed_url(
            f"https://{reviewed}/other/jobs/123456",
            main_frame=True,
        )

    assert resolver.calls == [reviewed]


class _Route:
    def __init__(self) -> None:
        self.action: str | None = None

    async def abort(self, _reason: str) -> None:
        self.action = "abort"

    async def continue_(self) -> None:
        self.action = "continue"


class _Request:
    def __init__(
        self,
        url: str,
        *,
        navigation: bool,
        method: str = "GET",
        frame=MAIN_FRAME,
        resource_type: str | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> None:
        self.url = url
        self._navigation = navigation
        self.method = method
        self.frame = frame
        self.resource_type = resource_type or ("document" if navigation else "xhr")
        self.post_data_buffer = body
        self.headers = {"content-type": content_type} if content_type is not None else {}

    def is_navigation_request(self) -> bool:
        return self._navigation


class _RoutePage:
    main_frame = MAIN_FRAME


def _outbound_gate(
    *,
    action_url: str = JOB_URL,
    commitment: str | None = None,
) -> _OutboundPostGate:
    candidate = parse_greenhouse_candidate_url(JOB_URL)
    answer_bindings, resume_name, submitter = _exact_answer_bindings()
    if commitment is None:
        _, _, commitment = _exact_native_payload()
    return _OutboundPostGate(
        expected_hostname=candidate.hostname,
        expected_identity=candidate.identity,
        expected_action_url=action_url,
        expected_transport=GREENHOUSE_V1_NATIVE_TRANSPORT,
        expected_payload_commitment=commitment,
        expected_answer_bindings=answer_bindings,
        expected_resume_control_name_sha256=resume_name,
        expected_submitter_binding=submitter,
        expected_cv_sha256=ROUTED_CV_SHA256,
        expected_main_frame=MAIN_FRAME,
        event=asyncio.Event(),
    )


class _RoutingContext:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.web_socket_handler = None

    async def route_web_socket(self, pattern, handler) -> None:
        assert pattern == "**/*"
        self.calls.append("websocket")
        self.web_socket_handler = handler

    async def route(self, pattern, _handler) -> None:
        assert pattern == "**/*"
        self.calls.append("http")


class _ContextWithoutWebSocketRouting:
    async def route(self, _pattern, _handler) -> None:
        raise AssertionError("HTTP routing must not install without WebSocket routing")


class _WebSocket:
    def __init__(self, url: str) -> None:
        self.url = url
        self.closed = False
        self.code = None

    async def close(self, *, code=None, reason=None) -> None:
        self.closed = True
        self.code = code
        assert reason == "browser transport disabled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "navigation", "expected"),
    [
        ("data:text/plain,safe", False, "continue"),
        ("blob:https://job-boards.greenhouse.io/id", False, "continue"),
        ("data:text/html,blocked", True, "abort"),
        ("file:///etc/passwd", False, "abort"),
        ("http://job-boards.greenhouse.io/app.js", False, "abort"),
        ("https://boards.greenhouse.io/app.js", False, "abort"),
    ],
)
async def test_non_https_and_cross_origin_routes_fail_closed(
    url: str,
    navigation: bool,
    expected: str,
) -> None:
    resolver = _Resolver({"job-boards.greenhouse.io": "8.8.8.8"})
    session = PlaywrightGreenhouseCandidateSession(settings=object())
    session._network_guard = GreenhouseNetworkGuard(JOB_URL, resolver=resolver)
    route = _Route()

    await session._guard_request(route, _Request(url, navigation=navigation))

    assert route.action == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_url", "expected_action", "request_may_have_left"),
    [
        (JOB_URL, "continue", True),
        (
            "https://job-boards.greenhouse.io/fixture/jobs/999999",
            "abort",
            False,
        ),
        ("https://example.test/collect", "abort", False),
    ],
)
async def test_atomic_outbound_gate_allows_only_the_exact_candidate_post(
    request_url: str,
    expected_action: str,
    request_may_have_left: bool,
) -> None:
    resolver = _Resolver({"job-boards.greenhouse.io": "8.8.8.8"})
    session = PlaywrightGreenhouseCandidateSession(settings=object())
    session._network_guard = GreenhouseNetworkGuard(JOB_URL, resolver=resolver)
    session._page = _RoutePage()
    body, content_type, _ = _exact_native_payload()
    gate = _outbound_gate()
    session._outbound_gate = gate
    route = _Route()

    await session._guard_request(
        route,
        _Request(
            request_url,
            navigation=True,
            method="POST",
            body=body,
            content_type=content_type,
        ),
    )

    assert route.action == expected_action
    assert gate.event.is_set() is True
    assert gate.request_may_have_left is request_may_have_left
    assert (gate.outbound_request_sha256 is not None) is request_may_have_left


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_url", "navigation", "resource_type", "frame"),
    [
        (JOB_URL, False, "fetch", MAIN_FRAME),
        (
            "https://job-boards.greenhouse.io/fixture?gh_jid=123456",
            True,
            "document",
            MAIN_FRAME,
        ),
        (JOB_URL, True, "xhr", MAIN_FRAME),
        (JOB_URL, True, "document", object()),
    ],
    ids=[
        "same-url-fetch-is-not-native-navigation",
        "same-identity-route-alias-is-not-exact-action",
        "main-frame-xhr-is-not-document",
        "different-frame-is-not-main-frame",
    ],
)
async def test_atomic_outbound_gate_rejects_non_exact_post_shapes(
    request_url: str,
    navigation: bool,
    resource_type: str,
    frame: object,
) -> None:
    session = PlaywrightGreenhouseCandidateSession(settings=object())
    session._network_guard = GreenhouseNetworkGuard(
        JOB_URL,
        resolver=_Resolver({"job-boards.greenhouse.io": "8.8.8.8"}),
    )
    session._page = _RoutePage()
    body, content_type, _ = _exact_native_payload()
    gate = _outbound_gate()
    session._outbound_gate = gate
    route = _Route()

    await session._guard_request(
        route,
        _Request(
            request_url,
            navigation=navigation,
            method="POST",
            resource_type=resource_type,
            frame=frame,
            body=body,
            content_type=content_type,
        ),
    )

    assert route.action == "abort"
    assert gate.request_may_have_left is False
    assert gate.outbound_request_sha256 is None
    assert gate.reason_code is ReasonCode.FORM_CHANGED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("changed-answer", ReasonCode.FORM_CHANGED),
        ("wrong-cv-same-filename", ReasonCode.ATTACHMENT_UNVERIFIED),
        ("routed-cv-under-cover-letter", ReasonCode.ATTACHMENT_UNVERIFIED),
        ("unknown-hidden-successful-control", ReasonCode.FORM_CHANGED),
        ("duplicate-control", ReasonCode.FORM_CHANGED),
    ],
)
async def test_atomic_outbound_gate_recomputes_exact_native_payload(
    mutation: str,
    expected_reason: ReasonCode,
) -> None:
    _, _, expected_commitment = _exact_native_payload()
    if mutation == "changed-answer":
        body, content_type, _ = _exact_native_payload(answer="changed after review")
    elif mutation == "wrong-cv-same-filename":
        body, content_type = _multipart_body(
            [
                ("text", "answer", "reviewed answer"),
                (
                    "file",
                    "resume",
                    "resume-fixture.pdf",
                    "application/pdf",
                    b"%PDF-1.4\nwrong bytes with same name\n%%EOF\n",
                ),
                ("text", "commit", "submit_application"),
            ]
        )
    elif mutation == "routed-cv-under-cover-letter":
        body, content_type = _multipart_body(
            [
                ("text", "answer", "reviewed answer"),
                (
                    "file",
                    "cover_letter",
                    "resume-fixture.pdf",
                    "application/pdf",
                    ROUTED_CV_BYTES,
                ),
                ("text", "commit", "submit_application"),
            ]
        )
    elif mutation == "unknown-hidden-successful-control":
        body, content_type = _multipart_body(
            [
                ("text", "answer", "reviewed answer"),
                ("text", "unreviewed_hidden_default", "surprise"),
                (
                    "file",
                    "resume",
                    "resume-fixture.pdf",
                    "application/pdf",
                    ROUTED_CV_BYTES,
                ),
                ("text", "commit", "submit_application"),
            ]
        )
    else:
        body, content_type = _multipart_body(
            [
                ("text", "answer", "reviewed answer"),
                ("text", "answer", "reviewed answer"),
                (
                    "file",
                    "resume",
                    "resume-fixture.pdf",
                    "application/pdf",
                    ROUTED_CV_BYTES,
                ),
                ("text", "commit", "submit_application"),
            ]
        )
    session = PlaywrightGreenhouseCandidateSession(settings=object())
    session._network_guard = GreenhouseNetworkGuard(
        JOB_URL,
        resolver=_Resolver({"job-boards.greenhouse.io": "8.8.8.8"}),
    )
    session._page = _RoutePage()
    gate = _outbound_gate(commitment=expected_commitment)
    session._outbound_gate = gate
    route = _Route()

    await session._guard_request(
        route,
        _Request(
            JOB_URL,
            navigation=True,
            method="POST",
            body=body,
            content_type=content_type,
        ),
    )

    assert route.action == "abort"
    assert gate.request_may_have_left is False
    assert gate.outbound_request_sha256 is None
    assert gate.reason_code is expected_reason


def test_native_multipart_commitment_is_boundary_independent_but_ordered() -> None:
    entries: list[tuple[str, ...] | tuple[str, str, str, str, bytes]] = [
        ("text", "answer", "line one\nline two"),
        (
            "file",
            "resume",
            "resume-fixture.pdf",
            "application/pdf",
            ROUTED_CV_BYTES,
        ),
        ("text", "commit", "submit_application"),
    ]
    first_body, first_type = _multipart_body(entries, boundary="GreenhouseBoundaryOne")
    second_body, second_type = _multipart_body(entries, boundary="GreenhouseBoundaryTwo")
    reordered_body, reordered_type = _multipart_body(
        [entries[2], entries[0], entries[1]],
        boundary="GreenhouseBoundaryThree",
    )

    first = _reviewed_payload_commitment(
        first_body,
        first_type,
        answer="line one\nline two",
    )
    second = _reviewed_payload_commitment(
        second_body,
        second_type,
        answer="line one\nline two",
    )
    reordered = _reviewed_payload_commitment(
        reordered_body,
        reordered_type,
        answer="line one\nline two",
    )

    assert first == second
    assert reordered != first


def test_native_multipart_commitment_rejects_unreviewed_empty_file_controls() -> None:
    base_entries: list[tuple[str, ...] | tuple[str, str, str, str, bytes]] = [
        ("text", "answer", "reviewed answer"),
        (
            "file",
            "resume",
            "resume-fixture.pdf",
            "application/pdf",
            ROUTED_CV_BYTES,
        ),
        ("text", "commit", "submit_application"),
    ]
    base_body, base_type = _multipart_body(base_entries)
    optional_body, optional_type = _multipart_body(
        [
            base_entries[0],
            (
                "file",
                "cover_letter",
                "",
                "application/octet-stream",
                b"",
            ),
            base_entries[1],
            base_entries[2],
        ],
        boundary="GreenhouseOptionalFileBoundary",
    )

    assert _reviewed_payload_commitment(base_body, base_type)
    with pytest.raises(GreenhouseAdapterBlockedError):
        _reviewed_payload_commitment(optional_body, optional_type)


def test_native_multipart_commitment_requires_routed_cv_hash_exactly_once() -> None:
    body, content_type = _multipart_body(
        [
            (
                "file",
                "resume",
                "resume-fixture.pdf",
                "application/pdf",
                ROUTED_CV_BYTES,
            ),
            (
                "file",
                "cover_letter",
                "duplicate-routed-bytes.pdf",
                "application/pdf",
                ROUTED_CV_BYTES,
            ),
        ]
    )

    with pytest.raises(GreenhouseAdapterBlockedError) as exc_info:
        _reviewed_payload_commitment(body, content_type)

    assert exc_info.value.reason_code is ReasonCode.ATTACHMENT_UNVERIFIED


def test_native_multipart_commitment_rejects_duplicate_resume_control() -> None:
    body, content_type = _multipart_body(
        [
            ("text", "answer", "reviewed answer"),
            (
                "file",
                "resume",
                "resume-fixture.pdf",
                "application/pdf",
                ROUTED_CV_BYTES,
            ),
            (
                "file",
                "resume",
                "duplicate-resume.pdf",
                "application/pdf",
                ROUTED_CV_BYTES,
            ),
            ("text", "commit", "submit_application"),
        ]
    )

    with pytest.raises(GreenhouseAdapterBlockedError) as exc_info:
        _reviewed_payload_commitment(body, content_type)

    assert exc_info.value.reason_code is ReasonCode.ATTACHMENT_UNVERIFIED


def test_reviewed_bindings_include_exact_blank_and_unchecked_states() -> None:
    observed_fields = observe_greenhouse_v1_fields(
        (FIXTURES / "embedded_form.html").read_text(encoding="utf-8")
    )
    unchecked = FormFieldV1(
        field_id="updates",
        canonical_name="updates",
        label="Receive optional updates",
        field_type=FieldType.CHECKBOX,
        required=False,
        position=len(observed_fields),
    )
    fields = (*observed_fields, unchecked)
    decisions = (
        AnswerDecisionV1(
            field_id="first_name",
            disposition=AnswerDisposition.RESOLVED,
            provenance=AnswerProvenance.DETERMINISTIC_IDENTITY,
            value="Reviewed Candidate",
        ),
        AnswerDecisionV1(
            field_id="resume",
            disposition=AnswerDisposition.RESOLVED,
            provenance=AnswerProvenance.VERIFIED_ATTACHMENT,
            value=VERIFIED_ATTACHMENT_SENTINEL,
            evidence_refs=(VERIFIED_ATTACHMENT_EVIDENCE_REF,),
        ),
        AnswerDecisionV1(
            field_id="question_note",
            disposition=AnswerDisposition.ABSTAINED,
            provenance=AnswerProvenance.ABSTAINED,
            reason_code=ReasonCode.REQUIRED_FIELD_UNKNOWN,
        ),
        AnswerDecisionV1(
            field_id="updates",
            disposition=AnswerDisposition.RESOLVED,
            provenance=AnswerProvenance.USER_CONFIRMED,
            value=False,
            evidence_refs=("operator_confirmation:updates:false",),
        ),
    )

    bindings = greenhouse_v1_reviewed_answer_bindings(
        fields,
        decisions,
        selected_cv_hash=ROUTED_CV_SHA256,
    )
    by_field = {binding.field_id: binding for binding in bindings}

    assert by_field["question_note"].value_sha256 == _value_sha256(["s", ""])
    assert by_field["question_note"].successful_entry_count == 1
    assert by_field["updates"].value_sha256 == _value_sha256(["b", False])
    assert by_field["updates"].successful_entry_count == 0
    assert "Reviewed Candidate" not in repr(bindings)


@pytest.mark.parametrize(
    "decision",
    [
        AnswerDecisionV1(
            field_id="nationality",
            disposition=AnswerDisposition.ABSTAINED,
            provenance=AnswerProvenance.ABSTAINED,
            reason_code=ReasonCode.REQUIRED_FIELD_UNKNOWN,
        ),
        AnswerDecisionV1(
            field_id="optional_note",
            disposition=AnswerDisposition.OPERATOR_REQUIRED,
            provenance=AnswerProvenance.ABSTAINED,
            reason_code=ReasonCode.REQUIRED_FIELD_UNKNOWN,
        ),
    ],
    ids=["optional-sensitive-default", "operator-required-default"],
)
def test_reviewed_bindings_reject_unconfirmed_optional_defaults(
    decision: AnswerDecisionV1,
) -> None:
    resume = FormFieldV1(
        field_id="resume",
        canonical_name="resume",
        label="Resume",
        field_type=FieldType.FILE,
        required=True,
        position=0,
    )
    field = FormFieldV1(
        field_id=decision.field_id,
        canonical_name=decision.field_id,
        label=(
            "What is your nationality?" if decision.field_id == "nationality" else "Optional note"
        ),
        field_type=FieldType.TEXT,
        required=False,
        position=1,
        sensitive_category=(
            SensitiveCategory.NATIONALITY if decision.field_id == "nationality" else None
        ),
    )
    attachment = AnswerDecisionV1(
        field_id="resume",
        disposition=AnswerDisposition.RESOLVED,
        provenance=AnswerProvenance.VERIFIED_ATTACHMENT,
        value=VERIFIED_ATTACHMENT_SENTINEL,
        evidence_refs=(VERIFIED_ATTACHMENT_EVIDENCE_REF,),
    )

    with pytest.raises(ValueError, match="GREENHOUSE_REVIEWED_ANSWER_BINDING_INVALID"):
        greenhouse_v1_reviewed_answer_bindings(
            (resume, field),
            (attachment, decision),
            selected_cv_hash=ROUTED_CV_SHA256,
        )


@pytest.mark.parametrize("mode", ["missing", "duplicate"])
def test_reviewed_bindings_require_one_decision_for_every_exact_field(mode: str) -> None:
    fields = observe_greenhouse_v1_fields(
        (FIXTURES / "embedded_form.html").read_text(encoding="utf-8")
    )
    first_name = AnswerDecisionV1(
        field_id="first_name",
        disposition=AnswerDisposition.RESOLVED,
        provenance=AnswerProvenance.DETERMINISTIC_IDENTITY,
        value="Reviewed Candidate",
    )
    resume = AnswerDecisionV1(
        field_id="resume",
        disposition=AnswerDisposition.RESOLVED,
        provenance=AnswerProvenance.VERIFIED_ATTACHMENT,
        value=VERIFIED_ATTACHMENT_SENTINEL,
        evidence_refs=(VERIFIED_ATTACHMENT_EVIDENCE_REF,),
    )
    decisions = (first_name, resume) if mode == "missing" else (first_name, resume, first_name)

    with pytest.raises(ValueError, match="GREENHOUSE_REVIEWED_ANSWER_BINDING_INVALID"):
        greenhouse_v1_reviewed_answer_bindings(
            fields,
            decisions,
            selected_cv_hash=ROUTED_CV_SHA256,
        )


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b"", "multipart/form-data; boundary=fixture"),
        (b"--fixture--\r\n", "text/plain"),
        (b"--fixture\r\nnot-a-part\r\n--fixture--\r\n", "multipart/form-data; boundary=fixture"),
    ],
)
def test_native_multipart_commitment_rejects_malformed_payloads(
    body: bytes,
    content_type: str,
) -> None:
    with pytest.raises(GreenhouseAdapterBlockedError):
        _reviewed_payload_commitment(body, content_type)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_unarmed_mutations_are_blocked_including_exact_candidate_post(
    method: str,
) -> None:
    resolver = _Resolver({"job-boards.greenhouse.io": "8.8.8.8"})
    session = PlaywrightGreenhouseCandidateSession(settings=object())
    session._network_guard = GreenhouseNetworkGuard(JOB_URL, resolver=resolver)
    route = _Route()

    await session._guard_request(
        route,
        _Request(JOB_URL, navigation=True, method=method),
    )

    assert route.action == "abort"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "path",
    [
        "/api/candidate/profile",
        "/application_uploads/resume",
        "/resume/upload",
        "/attachments/document",
        "/documents/file",
        "/files/resume.pdf",
    ],
)
async def test_every_precommit_profile_and_upload_mutation_is_blocked(
    method: str,
    path: str,
) -> None:
    session = PlaywrightGreenhouseCandidateSession(settings=object())
    session._network_guard = GreenhouseNetworkGuard(
        JOB_URL,
        resolver=_Resolver({"job-boards.greenhouse.io": "8.8.8.8"}),
    )
    route = _Route()

    await session._guard_request(
        route,
        _Request(
            f"https://job-boards.greenhouse.io{path}",
            navigation=False,
            method=method,
        ),
    )

    assert route.action == "abort"


@pytest.mark.asyncio
async def test_atomic_outbound_gate_is_single_use() -> None:
    resolver = _Resolver({"job-boards.greenhouse.io": "8.8.8.8"})
    session = PlaywrightGreenhouseCandidateSession(settings=object())
    session._network_guard = GreenhouseNetworkGuard(JOB_URL, resolver=resolver)
    session._page = _RoutePage()
    body, content_type, _ = _exact_native_payload()
    gate = _outbound_gate()
    session._outbound_gate = gate
    first_route = _Route()
    second_route = _Route()

    await session._guard_request(
        first_route,
        _Request(
            JOB_URL,
            navigation=True,
            method="POST",
            body=body,
            content_type=content_type,
        ),
    )
    await session._guard_request(
        second_route,
        _Request(
            JOB_URL,
            navigation=True,
            method="POST",
            body=body,
            content_type=content_type,
        ),
    )

    assert first_route.action == "continue"
    assert second_route.action == "abort"
    assert gate.request_may_have_left is True
    assert gate.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
async def test_atomic_gate_blocks_pre_send_safe_method_navigation(method: str) -> None:
    resolver = _Resolver({"job-boards.greenhouse.io": "8.8.8.8"})
    session = PlaywrightGreenhouseCandidateSession(settings=object())
    session._network_guard = GreenhouseNetworkGuard(JOB_URL, resolver=resolver)
    session._page = _RoutePage()
    gate = _outbound_gate()
    session._outbound_gate = gate
    route = _Route()

    await session._guard_request(
        route,
        _Request(JOB_URL, navigation=True, method=method),
    )

    assert route.action == "abort"
    assert gate.closed is True
    assert gate.event.is_set() is True
    assert gate.request_may_have_left is False
    assert gate.reason_code is ReasonCode.FORM_CHANGED


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
async def test_atomic_gate_allows_exact_origin_resource_loads_around_post(
    method: str,
) -> None:
    resolver = _Resolver({"job-boards.greenhouse.io": "8.8.8.8"})
    session = PlaywrightGreenhouseCandidateSession(settings=object())
    session._network_guard = GreenhouseNetworkGuard(JOB_URL, resolver=resolver)
    session._page = _RoutePage()
    body, content_type, _ = _exact_native_payload()
    gate = _outbound_gate()
    session._outbound_gate = gate
    before_route = _Route()
    post_route = _Route()
    after_route = _Route()
    resource_url = "https://job-boards.greenhouse.io/assets/confirmation.css"

    await session._guard_request(
        before_route,
        _Request(resource_url, navigation=False, method=method),
    )
    await session._guard_request(
        post_route,
        _Request(
            JOB_URL,
            navigation=True,
            method="POST",
            body=body,
            content_type=content_type,
        ),
    )
    await session._guard_request(
        after_route,
        _Request(resource_url, navigation=False, method=method),
    )

    assert before_route.action == "continue"
    assert post_route.action == "continue"
    assert after_route.action == "continue"
    assert gate.request_may_have_left is True
    assert gate.reason_code is None


@pytest.mark.asyncio
async def test_websocket_routing_capability_is_required_fail_closed() -> None:
    session = PlaywrightGreenhouseCandidateSession(settings=object())

    with pytest.raises(GreenhouseAdapterBlockedError) as exc_info:
        await session._install_network_routes(_ContextWithoutWebSocketRouting())

    assert exc_info.value.reason_code is ReasonCode.RUNTIME_NOT_READY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "ws://127.0.0.1:9000/private",
        "wss://127.0.0.1/private",
        "ws://arbitrary.example.test/socket",
        "wss://arbitrary.example.test/socket",
    ],
)
async def test_all_websockets_are_blocked_before_http_routes(url: str) -> None:
    context = _RoutingContext()
    session = PlaywrightGreenhouseCandidateSession(settings=object())

    await session._install_network_routes(context)

    assert context.calls == ["websocket", "http"]
    assert context.web_socket_handler is not None
    web_socket = _WebSocket(url)
    await context.web_socket_handler(web_socket)
    assert web_socket.closed is True
    assert web_socket.code == 1008


class _EmptyLocator:
    async def count(self) -> int:
        return 0

    async def is_visible(self) -> bool:
        return False


class _UploadNode:
    def __init__(
        self,
        *,
        marker_id: str,
        filename: str,
        digest: str | None = None,
    ) -> None:
        self.attrs = {
            "data-upload-id": marker_id,
            "data-file-name": filename,
        }
        if digest is not None:
            self.attrs["data-file-sha256"] = digest

    async def is_visible(self) -> bool:
        return True

    async def get_attribute(self, name: str):
        return self.attrs.get(name)

    def locator(self, _selector: str):
        return _EmptyLocator()


class _NodeList:
    def __init__(self, nodes) -> None:
        self.nodes = list(nodes)

    async def count(self) -> int:
        return len(self.nodes)

    def nth(self, index: int):
        return self.nodes[index]


class _FileInput:
    def __init__(self, page) -> None:
        self.page = page

    async def set_input_files(self, payload, **_kwargs) -> None:
        self.page.selected_name = payload["name"]
        self.page.uploaded_buffer = payload["buffer"]
        self.page.fresh.attrs["data-file-name"] = payload["name"]
        self.page.upload_started = True

    async def input_value(self) -> str:
        return f"C:\\fakepath\\{self.page.selected_name}"


class _FileInputList:
    def __init__(self, page) -> None:
        self.first = _FileInput(page)

    async def count(self) -> int:
        return 1


class _UploadPage:
    url = JOB_URL

    def __init__(
        self,
        *,
        fresh_marker: bool,
        fresh_digest: str | None = None,
    ) -> None:
        self.upload_started = False
        self.selected_name = ""
        self.uploaded_buffer = b""
        self.fresh_marker = fresh_marker
        self.stale = _UploadNode(
            marker_id="pre-existing",
            filename="pre-existing.pdf",
        )
        self.fresh = _UploadNode(
            marker_id="fresh-after-input",
            filename="pending.pdf",
            digest=fresh_digest,
        )

    def locator(self, selector: str):
        if 'input[type="file"]' in selector:
            return _FileInputList(self)
        nodes = []
        if 'data-qa="resume-upload-complete"' in selector:
            if not self.upload_started or not self.fresh_marker:
                nodes.append(self.stale)
            else:
                nodes.append(self.fresh)
        return _NodeList(nodes)

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("upload_state", ["pending", "failed"])
async def test_upload_without_fresh_completion_cannot_prove_current_cv(
    upload_state: str,
) -> None:
    payload = b"%PDF-1.4\nsanitized\n%%EOF\n"
    session = PlaywrightGreenhouseCandidateSession(settings=object())
    session._page = _UploadPage(fresh_marker=False)
    session._network_guard = GreenhouseNetworkGuard(
        JOB_URL,
        resolver=_Resolver({"job-boards.greenhouse.io": "8.8.8.8"}),
    )

    with pytest.raises(GreenhouseAdapterBlockedError) as exc_info:
        await session.ensure_resume_attachment(
            resume_bytes=payload,
            cv_id=f"fixture-cv-{upload_state}",
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )

    assert exc_info.value.reason_code is ReasonCode.ATTACHMENT_UNVERIFIED


@pytest.mark.asyncio
async def test_fresh_upload_marker_is_receipt_bound_and_rechecked() -> None:
    payload = b"%PDF-1.4\nsanitized\n%%EOF\n"
    digest = hashlib.sha256(payload).hexdigest()
    page = _UploadPage(fresh_marker=True)
    session = PlaywrightGreenhouseCandidateSession(settings=object())
    session._page = page
    session._network_guard = GreenhouseNetworkGuard(
        JOB_URL,
        resolver=_Resolver({"job-boards.greenhouse.io": "8.8.8.8"}),
    )

    proof = await session.ensure_resume_attachment(
        resume_bytes=payload,
        cv_id="fixture-cv",
        expected_sha256=digest,
    )
    verified = await session.verify_resume_attachment(
        cv_id="fixture-cv",
        expected_sha256=digest,
    )
    page.fresh_marker = False
    missing = await session.verify_resume_attachment(
        cv_id="fixture-cv",
        expected_sha256=digest,
    )

    assert proof.matches(cv_id="fixture-cv", cv_sha256=digest) is True
    assert verified == proof
    assert missing.matches(cv_id="fixture-cv", cv_sha256=digest) is False
    assert page.uploaded_buffer == payload
    assert page.selected_name.startswith("resume-")
    assert "fixture-cv" not in page.selected_name


@pytest.mark.asyncio
async def test_explicit_wrong_upload_digest_cannot_fall_back_to_matching_filename() -> None:
    payload = b"%PDF-1.4\nsanitized\n%%EOF\n"
    digest = hashlib.sha256(payload).hexdigest()
    page = _UploadPage(
        fresh_marker=True,
        fresh_digest="0" * 64,
    )
    session = PlaywrightGreenhouseCandidateSession(settings=object())
    session._page = page
    session._network_guard = GreenhouseNetworkGuard(
        JOB_URL,
        resolver=_Resolver({"job-boards.greenhouse.io": "8.8.8.8"}),
    )

    with pytest.raises(GreenhouseAdapterBlockedError) as exc_info:
        await session.ensure_resume_attachment(
            resume_bytes=payload,
            cv_id="fixture-cv",
            expected_sha256=digest,
        )

    assert exc_info.value.reason_code is ReasonCode.ATTACHMENT_UNVERIFIED
    assert "routedCvCount !== 1" in _ATOMIC_NATIVE_SUBMIT_SCRIPT
    assert "HTMLFormElement.prototype.submit.call(form)" in _ATOMIC_NATIVE_SUBMIT_SCRIPT
    assert "button.click()" not in _ATOMIC_NATIVE_SUBMIT_SCRIPT
    assert "requestSubmit" not in _ATOMIC_NATIVE_SUBMIT_SCRIPT


class _LanguageLocator:
    async def get_attribute(self, _name: str):
        return "en"


class _FormLocator:
    def __init__(
        self,
        *,
        browser_valid: bool,
        action: str | None,
        method: str = "post",
        visible: bool = True,
        validation_visible: bool = False,
        submit_visible: bool = True,
        submit_enabled: bool = True,
        submit_belongs: bool = True,
        atomic_status: object = "FORM_CHANGED",
        action_entries: list[list[str]] | None = None,
        payload_commitment: str = "c" * 64,
        enctype: str = "multipart/form-data",
        target: str = "",
        control_state: dict[str, bool] | None = None,
        atomic_actionability_drift: str | None = None,
    ) -> None:
        self.browser_valid = browser_valid
        self.action = action
        self.method = method
        self.visible = visible
        self.first = self
        self.validation = _ValidationNode(visible=validation_visible)
        self.action_entries = action_entries or []
        self.payload_commitment = payload_commitment
        self.enctype = enctype
        self.target = target
        self.submit = _SubmitLocator(
            form=self,
            visible=submit_visible,
            enabled=submit_enabled,
            belongs=submit_belongs,
            atomic_status=atomic_status,
            control_state=control_state,
            atomic_actionability_drift=atomic_actionability_drift,
        )

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return self.visible

    async def evaluate(self, script: str, *args):
        if script == "element => element.checkValidity()":
            return self.browser_valid
        if script == "form => form.action":
            return self.action or JOB_URL
        if script == "form => form.outerHTML":
            return (FIXTURES / "upload_complete.html").read_text(encoding="utf-8")
        if "__greenhouseAtomicCommitMarker" in script:
            return bool(args)
        raise AssertionError(f"unexpected form script: {script}")

    async def get_attribute(self, name: str):
        return {
            "action": self.action,
            "method": self.method,
            "enctype": self.enctype,
            "target": self.target,
        }.get(name)

    async def element_handle(self):
        return self

    def locator(self, selector: str):
        if selector.startswith('[data-qa="validation-error"]'):
            return _NodeList([self.validation])
        if selector.startswith('button#submit_app[type="submit"]'):
            return self.submit
        raise AssertionError(f"unexpected scoped selector: {selector}")


class _ValidationNode:
    def __init__(self, *, visible: bool) -> None:
        self.visible = visible

    async def is_visible(self) -> bool:
        return self.visible

    async def inner_text(self) -> str:
        raise AssertionError("validation text must never be read or persisted")


class _SubmitLocator:
    def __init__(
        self,
        *,
        form=None,
        visible: bool = True,
        enabled: bool = True,
        belongs: bool = True,
        atomic_status: object = "FORM_CHANGED",
        control_state: dict[str, bool] | None = None,
        atomic_actionability_drift: str | None = None,
    ) -> None:
        self.form = form
        self.visible = visible
        self.enabled = enabled
        self.belongs = belongs
        self.atomic_status = atomic_status
        self.control_state = {
            "connected": True,
            "exactForm": True,
            "disabled": False,
            "ariaDisabled": False,
            "inert": False,
            "hidden": False,
            "cssActionable": True,
            "hasRect": True,
            "actionable": True,
            **(control_state or {}),
        }
        self.atomic_actionability_drift = atomic_actionability_drift
        self.clicked = False
        self.session: PlaywrightGreenhouseCandidateSession | None = None

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return self.visible

    async def is_enabled(self) -> bool:
        return self.enabled

    async def evaluate(self, script: str, argument=None):
        if script == _FINAL_CONTROL_STATE_SCRIPT:
            state = dict(self.control_state)
            state["exactForm"] = state["exactForm"] and self.belongs and argument is self.form
            state["actionable"] = state["actionable"] and all(
                (
                    state["connected"],
                    state["exactForm"],
                    not state["disabled"],
                    not state["ariaDisabled"],
                    not state["inert"],
                    not state["hidden"],
                    state["cssActionable"],
                    state["hasRect"],
                )
            )
            return state
        if script == _ACTION_IDENTITY_ENTRIES_SCRIPT:
            return self.form.action_entries
        if script == _FORM_PAYLOAD_COMMITMENT_SCRIPT:
            assert isinstance(argument, dict)
            reviewed = argument["reviewedAnswers"]
            bound = [
                {
                    **item,
                    "controlNameSha256": hashlib.sha256(
                        item["fieldId"].encode("utf-8")
                    ).hexdigest(),
                }
                for item in reviewed
            ]
            resume = next(item for item in bound if item["fieldType"] == FieldType.FILE.value)
            return {
                "payloadCommitment": self.form.payload_commitment,
                "answerBindings": bound,
                "resumeControlNameSha256": resume["controlNameSha256"],
                "submitterBinding": {
                    "controlNameSha256": hashlib.sha256(b"commit").hexdigest(),
                    "valueSha256": _value_sha256(["s", "submit_application"]),
                },
            }
        if script == _ATOMIC_NATIVE_SUBMIT_SCRIPT:
            if self.atomic_actionability_drift is not None:
                return "FORM_CHANGED"
            if self.atomic_status == "EVALUATION_CONTEXT_LOST_NO_REQUEST":
                raise RuntimeError("synthetic evaluation context loss without request")
            if self.atomic_status == "CONTRADICTORY_GATE_FORM_CHANGED":
                assert self.session is not None
                gate = self.session._outbound_gate
                assert gate is not None
                gate.request_may_have_left = True
                gate.outbound_request_sha256 = hashlib.sha256(JOB_URL.encode()).hexdigest()
                gate.event.set()
                return "FORM_CHANGED"
            if self.atomic_status == "NAVIGATION_CONTEXT_DESTROYED":
                assert self.session is not None
                gate = self.session._outbound_gate
                assert gate is not None
                gate.request_may_have_left = True
                gate.outbound_request_sha256 = hashlib.sha256(JOB_URL.encode()).hexdigest()
                gate.event.set()
                raise RuntimeError("synthetic navigation destroyed context")
            return self.atomic_status
        raise AssertionError(f"unexpected button script: {script}")

    async def element_handle(self):
        return self

    async def get_attribute(self, name: str):
        return {
            "id": "submit_app",
            "data-qa": "submit-application",
            "name": "commit",
            "value": "submit_application",
            "type": "submit",
        }.get(name)

    async def click(self, **_kwargs) -> None:
        self.clicked = True


class _ReadinessPage:
    url = JOB_URL
    main_frame = MAIN_FRAME

    def __init__(
        self,
        *,
        browser_valid: bool = True,
        validation_visible: bool = False,
        action: str | None = None,
        method: str = "post",
        submit_visible: bool = True,
        submit_enabled: bool = True,
        submit_belongs: bool = True,
        atomic_status: object = "FORM_CHANGED",
        decoy_submit: bool = False,
        action_entries: list[list[str]] | None = None,
        payload_commitment: str = "c" * 64,
        enctype: str = "multipart/form-data",
        target: str = "",
        control_state: dict[str, bool] | None = None,
        atomic_actionability_drift: str | None = None,
    ) -> None:
        self.form = _FormLocator(
            browser_valid=browser_valid,
            action=action,
            method=method,
            validation_visible=validation_visible,
            visible=True,
            submit_visible=submit_visible,
            submit_enabled=submit_enabled,
            submit_belongs=submit_belongs,
            atomic_status=atomic_status,
            action_entries=action_entries,
            payload_commitment=payload_commitment,
            enctype=enctype,
            target=target,
            control_state=control_state,
            atomic_actionability_drift=atomic_actionability_drift,
        )
        self.decoy = _SubmitLocator() if decoy_submit else None

    async def content(self) -> str:
        return (FIXTURES / "upload_complete.html").read_text(encoding="utf-8")

    def locator(self, selector: str):
        if selector == "html":
            return _LanguageLocator()
        if selector.startswith("form#application_form"):
            return self.form
        if selector.startswith('button#submit_app[type="submit"]'):
            if self.decoy is None:
                return _NodeList([])
            return self.decoy
        raise AssertionError(f"unexpected synthetic selector: {selector}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "browser_valid",
        "validation_visible",
        "action",
        "submit_visible",
        "submit_enabled",
        "expected",
    ),
    [
        (True, False, None, True, True, True),
        (False, False, None, True, True, False),
        (True, True, None, True, True, False),
        (True, False, "https://example.test/collect", True, True, False),
        (
            True,
            False,
            "https://job-boards.greenhouse.io/fixture/jobs/999999",
            True,
            True,
            False,
        ),
        (True, False, None, False, True, False),
        (True, False, None, True, False, False),
    ],
    ids=[
        "native-valid",
        "native-invalid",
        "visible-validation-error",
        "external-form-action",
        "same-origin-wrong-job-action",
        "hidden-submit",
        "disabled-submit",
    ],
)
async def test_final_action_requires_native_validity_and_clean_visible_state(
    browser_valid: bool,
    validation_visible: bool,
    action: str | None,
    submit_visible: bool,
    submit_enabled: bool,
    expected: bool,
) -> None:
    resolver = _Resolver({"job-boards.greenhouse.io": "8.8.8.8"})
    session = PlaywrightGreenhouseCandidateSession(settings=object())
    session._page = _ReadinessPage(
        browser_valid=browser_valid,
        validation_visible=validation_visible,
        action=action,
        submit_visible=submit_visible,
        submit_enabled=submit_enabled,
    )
    session._network_guard = GreenhouseNetworkGuard(JOB_URL, resolver=resolver)

    assert await session.final_action_ready() is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "control_state",
    [
        pytest.param({"connected": False}, id="detached-control"),
        pytest.param({"exactForm": False}, id="wrong-retained-form"),
        pytest.param({"disabled": True}, id="disabled-pseudo-class"),
        pytest.param({"ariaDisabled": True}, id="aria-disabled-ancestor"),
        pytest.param({"inert": True}, id="inert-ancestor-outside-form"),
        pytest.param({"hidden": True}, id="hidden-ancestor"),
        pytest.param({"cssActionable": False}, id="non-actionable-computed-css"),
        pytest.param({"hasRect": False}, id="zero-area-control"),
        pytest.param({"actionable": False}, id="malformed-actionable-summary"),
    ],
)
async def test_final_action_capture_python_boundary_rejects_non_actionable_control(
    control_state: dict[str, bool],
) -> None:
    session = PlaywrightGreenhouseCandidateSession(settings=object())
    session._page = _ReadinessPage(control_state=control_state)
    session._network_guard = GreenhouseNetworkGuard(
        JOB_URL,
        resolver=_Resolver({"job-boards.greenhouse.io": "8.8.8.8"}),
    )

    assert await session.final_action_ready() is False


def test_final_control_scripts_require_exact_actionability_through_outer_ancestors() -> None:
    for script in (_FINAL_CONTROL_STATE_SCRIPT, _ATOMIC_NATIVE_SUBMIT_SCRIPT):
        assert 'button.matches(":disabled")' in script
        assert 'getAttribute("aria-disabled")' in script
        assert 'hasAttribute("inert")' in script
        assert "current = current.parentElement" in script
        assert "style.pointerEvents" in script
        assert "style.contentVisibility" in script
        assert "style.visibility" in script
        assert "style.opacity" in script
        assert "button.getClientRects()" in script
        assert "form.contains(button)" in script
    assert "button.form === form" in _FINAL_CONTROL_STATE_SCRIPT
    assert "button.form !== form" in _ATOMIC_NATIVE_SUBMIT_SCRIPT
    assert _ATOMIC_NATIVE_SUBMIT_SCRIPT.count("finalControlActionable()") >= 2


def test_named_submitter_proxy_precedes_the_adjacent_final_release_boundary() -> None:
    proxy_insert = _ATOMIC_NATIVE_SUBMIT_SCRIPT.index("button.before(submitProxy)")
    final_release = _ATOMIC_NATIVE_SUBMIT_SCRIPT.index("const finalReleaseFailure")
    final_actionability = _ATOMIC_NATIVE_SUBMIT_SCRIPT.rindex("if (!finalControlActionable())")
    native_submit = _ATOMIC_NATIVE_SUBMIT_SCRIPT.rindex(
        "HTMLFormElement.prototype.submit.call(form)"
    )

    assert proxy_insert < final_release < final_actionability < native_submit
    assert "new FormData(form)" in _ATOMIC_NATIVE_SUBMIT_SCRIPT
    assert "currentValue !== expectedValue" in _ATOMIC_NATIVE_SUBMIT_SCRIPT
    assert "data-greenhouse-atomic-submitter-proxy" in _ATOMIC_NATIVE_SUBMIT_SCRIPT
    assert 'return rejectAfterProxy("FORM_CHANGED");' in _ATOMIC_NATIVE_SUBMIT_SCRIPT

    release_slice = _ATOMIC_NATIVE_SUBMIT_SCRIPT[final_actionability:native_submit]
    for forbidden in (
        "await ",
        ".before(",
        ".append(",
        ".appendChild(",
        ".insertBefore(",
        ".remove(",
        ".removeChild(",
        ".setAttribute(",
        "new FormData",
    ):
        assert forbidden not in release_slice
    assert (
        """
    if (!finalControlActionable()) {
      return rejectAfterProxy("FORM_CHANGED");
    }
  } catch {
    return rejectAfterProxy("FORM_CHANGED");
  }
  try {
    HTMLFormElement.prototype.submit.call(form);"""
        in _ATOMIC_NATIVE_SUBMIT_SCRIPT
    )
    post_invocation = _ATOMIC_NATIVE_SUBMIT_SCRIPT[native_submit:]
    assert 'rejectAfterProxy("FORM_CHANGED")' not in post_invocation
    assert post_invocation.count('return "NATIVE_SUBMIT_INVOKED";') == 2


@pytest.mark.asyncio
async def test_real_chromium_named_submitter_proxy_failures_restore_original_form() -> None:
    playwright = pytest.importorskip(
        "playwright.async_api",
        reason="real Chromium regression requires the browser test extra",
    )
    response_body = b"<!doctype html><html><body>fixture origin</body></html>"

    async def serve_fixture(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/html; charset=utf-8\r\n"
                + f"Content-Length: {len(response_body)}\r\n".encode()
                + b"Connection: close\r\n\r\n"
                + response_body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve_fixture, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    origin = f"http://127.0.0.1:{port}/"
    try:
        async with playwright.async_playwright() as manager:
            try:
                browser = await manager.chromium.launch(headless=True)
            except playwright.Error as exc:
                pytest.skip(f"real Chromium executable unavailable: {exc}")
            try:
                for (
                    css_rule,
                    throw_on_final_probe,
                    throw_during_submit,
                    expected_status,
                    expected_calls,
                    expected_proxies,
                ) in (
                    ("", False, False, "NATIVE_SUBMIT_INVOKED", 1, 1),
                    (
                        (
                            "form:has(input[type='hidden'][name='commit']) "
                            "#submit_app { pointer-events: none !important; }"
                        ),
                        False,
                        False,
                        "FORM_CHANGED",
                        0,
                        0,
                    ),
                    ("", True, False, "FORM_CHANGED", 0, 0),
                    ("", False, True, "NATIVE_SUBMIT_INVOKED", 1, 1),
                ):
                    page = await browser.new_page()
                    try:
                        await page.goto(origin)
                        await page.set_content(
                            f"""
                            <!doctype html>
                            <html>
                              <head><style>{css_rule}</style></head>
                              <body>
                                <form id="application_form" method="post"
                                      action="/apply" enctype="multipart/form-data"
                                      target="_self">
                                  <div data-gh-field data-field-id="resume">
                                    <input name="resume" type="file" required>
                                    <span data-qa="resume-upload-complete"
                                          data-upload-id="fixture-upload"
                                          data-file-name="resume-fixture.pdf"></span>
                                  </div>
                                  <button id="submit_app" type="submit" name="commit"
                                          value="submit_application">
                                    Submit application
                                  </button>
                                </form>
                              </body>
                            </html>
                            """
                        )
                        button = page.locator("#submit_app")
                        form = page.locator("#application_form")
                        await page.locator("input[type=file]").set_input_files(
                            {
                                "name": "resume-fixture.pdf",
                                "mimeType": "application/pdf",
                                "buffer": ROUTED_CV_BYTES,
                            }
                        )
                        if css_rule:
                            assert (
                                await button.evaluate(
                                    """
                                    button => {
                                      const proxy = document.createElement("input");
                                      proxy.type = "hidden";
                                      proxy.name = "commit";
                                      button.before(proxy);
                                      const pointerEvents =
                                        getComputedStyle(button).pointerEvents;
                                      proxy.remove();
                                      return pointerEvents;
                                    }
                                    """
                                )
                                == "none"
                            )

                        reviewed_answers = [
                            {
                                "fieldId": "resume",
                                "fieldType": FieldType.FILE.value,
                                "valueSha256": _value_sha256(["f", ROUTED_CV_SHA256]),
                                "successfulEntryCount": 1,
                            }
                        ]
                        payload_binding = await button.evaluate(
                            _FORM_PAYLOAD_COMMITMENT_SCRIPT,
                            {
                                "cvSha256": ROUTED_CV_SHA256,
                                "reviewedAnswers": reviewed_answers,
                                "actionIdentityEntries": [],
                            },
                        )
                        assert isinstance(payload_binding, dict)
                        assert payload_binding["submitterBinding"] is not None
                        form_outer_html = await form.evaluate("form => form.outerHTML")
                        form_marker = "f" * 64
                        assert (
                            await form.evaluate(
                                """
                                (form, marker) => {
                                  Object.defineProperty(
                                    form,
                                    "__greenhouseAtomicCommitMarker",
                                    {
                                      configurable: true,
                                      enumerable: false,
                                      value: marker,
                                      writable: false,
                                    }
                                  );
                                  return form.__greenhouseAtomicCommitMarker === marker;
                                }
                                """,
                                form_marker,
                            )
                            is True
                        )
                        await page.evaluate(
                            """
                            throwDuringSubmit => {
                              window.__nativeSubmitCalls = 0;
                              window.__nativeSubmitEntries = [];
                              HTMLFormElement.prototype.submit = function () {
                                window.__nativeSubmitCalls += 1;
                                window.__nativeSubmitEntries =
                                  Array.from(new FormData(this).entries())
                                    .map(([name, value]) => [
                                      name,
                                      typeof value === "string" ? value : value.name,
                                    ]);
                                if (throwDuringSubmit) {
                                  throw new Error("synthetic submit ambiguity");
                                }
                              };
                            }
                            """,
                            throw_during_submit,
                        )
                        if throw_on_final_probe:
                            await button.evaluate(
                                """
                                button => {
                                  const nativeGetClientRects =
                                    button.getClientRects.bind(button);
                                  window.__rectProbeCount = 0;
                                  Object.defineProperty(button, "getClientRects", {
                                    configurable: true,
                                    value: () => {
                                      window.__rectProbeCount += 1;
                                      if (window.__rectProbeCount === 3) {
                                        throw new Error(
                                          "synthetic final actionability failure"
                                        );
                                      }
                                      return nativeGetClientRects();
                                    },
                                  });
                                }
                                """
                            )
                        resolved_action = await form.evaluate("form => form.action")
                        status = await button.evaluate(
                            _ATOMIC_NATIVE_SUBMIT_SCRIPT,
                            {
                                "formMarker": form_marker,
                                "resolvedAction": resolved_action,
                                "formOuterHtml": form_outer_html,
                                "actionIdentityEntries": [],
                                "reviewedAnswers": payload_binding["answerBindings"],
                                "resumeControlNameSha256": payload_binding[
                                    "resumeControlNameSha256"
                                ],
                                "submitterBinding": payload_binding["submitterBinding"],
                                "nativeTransport": ("native-multipart-form-post-v1"),
                                "payloadCommitment": payload_binding["payloadCommitment"],
                                "uploadMarkerSelector": ('[data-qa="resume-upload-complete"]'),
                                "uploadMarkerId": "fixture-upload",
                                "uploadName": "resume-fixture.pdf",
                                "cvSha256": ROUTED_CV_SHA256,
                                "resumeInputSelector": ('input[type="file"][name="resume"]'),
                            },
                        )
                        observation = await page.evaluate(
                            """
                            () => ({
                              calls: window.__nativeSubmitCalls,
                              entries: window.__nativeSubmitEntries,
                              proxies: document.querySelectorAll(
                                "input[data-greenhouse-atomic-submitter-proxy]"
                              ).length,
                              currentEntries:
                                Array.from(
                                  new FormData(
                                    document.querySelector("#application_form")
                                  ).entries()
                                ).map(([name, value]) => [
                                  name,
                                  typeof value === "string" ? value : value.name,
                                ]),
                              rectProbeCount: window.__rectProbeCount || 0,
                            })
                            """
                        )

                        assert status == expected_status
                        assert observation["calls"] == expected_calls
                        assert observation["proxies"] == expected_proxies
                        if expected_calls:
                            assert observation["entries"] == [
                                ["resume", "resume-fixture.pdf"],
                                ["commit", "submit_application"],
                            ]
                            assert observation["currentEntries"] == observation["entries"]
                        else:
                            assert observation["entries"] == []
                            assert observation["currentEntries"] == [
                                ["resume", "resume-fixture.pdf"]
                            ]
                        if throw_on_final_probe:
                            assert observation["rectProbeCount"] == 3
                    finally:
                        await page.close()
            finally:
                await browser.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_final_action_rejects_non_post_or_wrong_form_association() -> None:
    resolver = _Resolver({"job-boards.greenhouse.io": "8.8.8.8"})
    for page in (
        _ReadinessPage(method="get"),
        _ReadinessPage(enctype="application/x-www-form-urlencoded"),
        _ReadinessPage(target="_blank"),
        _ReadinessPage(submit_belongs=False),
    ):
        session = PlaywrightGreenhouseCandidateSession(settings=object())
        session._page = page
        session._network_guard = GreenhouseNetworkGuard(JOB_URL, resolver=resolver)

        assert await session.final_action_ready() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action_entries", "expected"),
    [
        ([], True),
        ([["board", "board_token", "fixture"]], True),
        ([["job", "job_id", "123456"]], True),
        (
            [
                ["board", "greenhouse_board_token", "fixture"],
                ["job", "greenhouse_job_id", "123456"],
            ],
            False,
        ),
        ([["board", "board_token", "other"]], False),
        ([["job", "job_id", "999999"]], False),
        (
            [
                ["job", "job_id", "123456"],
                ["job", "gh_jid", "999999"],
            ],
            False,
        ),
        (
            [
                ["job", "job_id", "123456"],
                ["job", "job_id", "123456"],
            ],
            False,
        ),
    ],
    ids=[
        "no-hidden-identity",
        "matching-board",
        "matching-job",
        "unqualified-suffixed-controls",
        "wrong-board",
        "css-hidden-wrong-job-is-still-successful-and-blocks",
        "conflicting-successful-job-controls",
        "duplicate-job-control",
    ],
)
async def test_final_action_binds_all_successful_action_identity_controls(
    action_entries: list[list[str]],
    expected: bool,
) -> None:
    resolver = _Resolver({"job-boards.greenhouse.io": "8.8.8.8"})
    session = PlaywrightGreenhouseCandidateSession(settings=object())
    session._page = _ReadinessPage(action_entries=action_entries)
    session._network_guard = GreenhouseNetworkGuard(JOB_URL, resolver=resolver)

    assert await session.final_action_ready() is expected
    assert "new FormData(form, button)" in _ACTION_IDENTITY_ENTRIES_SCRIPT
    assert "querySelectorAll('input[type=\"hidden\"]')" not in _ACTION_IDENTITY_ENTRIES_SCRIPT
    assert "getComputedStyle" not in _ACTION_IDENTITY_ENTRIES_SCRIPT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("submitter_entries", "expected"),
    [
        ([["job", "job_id", "123456"]], True),
        ([["job", "job_id", "999999"]], False),
    ],
    ids=["matching-submit-button-identity", "wrong-submit-button-identity"],
)
async def test_submit_button_identity_is_part_of_successful_control_binding(
    submitter_entries: list[list[str]],
    expected: bool,
) -> None:
    session = PlaywrightGreenhouseCandidateSession(settings=object())
    session._page = _ReadinessPage(action_entries=submitter_entries)
    session._network_guard = GreenhouseNetworkGuard(
        JOB_URL,
        resolver=_Resolver({"job-boards.greenhouse.io": "8.8.8.8"}),
    )

    assert await session.final_action_ready() is expected


@pytest.mark.asyncio
async def test_validated_action_retains_exact_form_and_button_handles() -> None:
    resolver = _Resolver({"job-boards.greenhouse.io": "8.8.8.8"})
    page = _ReadinessPage(decoy_submit=True)
    session = PlaywrightGreenhouseCandidateSession(settings=object())
    session._page = page
    session._network_guard = GreenhouseNetworkGuard(JOB_URL, resolver=resolver)

    validated = await session._validated_form_action(require_ready=True)

    assert validated is not None
    assert validated.form_handle is page.form
    assert validated.button_handle is page.form.submit
    assert page.form.submit.clicked is False
    assert page.decoy is not None
    assert page.decoy.clicked is False


class _AtomicReadinessSession(PlaywrightGreenhouseCandidateSession):
    async def verify_resume_attachment(
        self,
        *,
        cv_id: str,
        expected_sha256: str,
    ) -> GreenhouseAttachmentProof:
        proof = self._attachment
        if proof is not None and proof.matches(
            cv_id=cv_id,
            cv_sha256=expected_sha256,
        ):
            return proof
        return GreenhouseAttachmentProof(
            cv_id=cv_id,
            cv_sha256=expected_sha256,
            upload_complete=False,
        )


async def _atomic_expectation(
    session: _AtomicReadinessSession,
) -> GreenhouseAtomicCommitExpectation:
    snapshot = await session.snapshot()
    fields = observe_greenhouse_v1_fields(snapshot.html)
    variant = detect_greenhouse_variant(snapshot.html, snapshot.url)
    action_binding = await session.final_action_binding()
    action_url = await session.final_action_url()
    dom_commitment = await session.commit_dom_commitment()
    reviewed_answers = greenhouse_v1_reviewed_answer_bindings(
        fields,
        (
            AnswerDecisionV1(
                field_id="resume",
                disposition=AnswerDisposition.RESOLVED,
                provenance=AnswerProvenance.VERIFIED_ATTACHMENT,
                value=VERIFIED_ATTACHMENT_SENTINEL,
                evidence_refs=(VERIFIED_ATTACHMENT_EVIDENCE_REF,),
            ),
        ),
        selected_cv_hash="a" * 64,
    )
    payload_binding = await session.commit_payload_binding(
        reviewed_answers=reviewed_answers,
        expected_cv_sha256="a" * 64,
    )
    proof = session._attachment
    guard = session._network_guard
    assert action_binding is not None
    assert action_url is not None
    assert dom_commitment is not None
    assert payload_binding is not None
    assert proof is not None
    assert proof.receipt_sha256 is not None
    assert guard is not None
    return GreenhouseAtomicCommitExpectation(
        expected_hostname=guard.expected_hostname,
        expected_identity=guard.expected_identity,
        fields=fields,
        variant=variant,
        form_fingerprint=greenhouse_v1_form_fingerprint(
            fields,
            variant,
            action_binding,
        ),
        action_binding=action_binding,
        dom_commitment=dom_commitment,
        resolved_action_url=action_url,
        native_transport=GREENHOUSE_V1_NATIVE_TRANSPORT,
        payload_commitment=payload_binding.payload_commitment,
        answer_bindings=payload_binding.answer_bindings,
        resume_control_name_sha256=payload_binding.resume_control_name_sha256,
        submitter_binding=payload_binding.submitter_binding,
        cv_id=proof.cv_id,
        cv_sha256=proof.cv_sha256,
        cv_receipt_sha256=proof.receipt_sha256,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("atomic_status", "expected_reason"),
    [
        ("ATTACHMENT_UNVERIFIED", ReasonCode.ATTACHMENT_UNVERIFIED),
        ("FORM_CHANGED", ReasonCode.FORM_CHANGED),
    ],
    ids=["attachment-invalidated-at-click", "action-retargeted-at-click"],
)
async def test_atomic_primitive_reports_last_instant_drift_before_request(
    atomic_status: str,
    expected_reason: ReasonCode,
) -> None:
    resolver = _Resolver({"job-boards.greenhouse.io": "8.8.8.8"})
    page = _ReadinessPage(atomic_status=atomic_status)
    session = _AtomicReadinessSession(settings=object())
    session._page = page
    session._network_guard = GreenhouseNetworkGuard(JOB_URL, resolver=resolver)
    session._attachment_marker_id = "fixture-upload"
    session._attachment_upload_name = "resume-fixture.pdf"
    session._attachment = GreenhouseAttachmentProof(
        cv_id="fixture-cv",
        cv_sha256="a" * 64,
        upload_complete=True,
        receipt_sha256="b" * 64,
    )
    expectation = await _atomic_expectation(session)

    observation = await session.atomic_commit(expectation)

    assert observation.binds(expectation) is True
    assert observation.final_action_invoked is False
    assert observation.request_may_have_left is False
    assert observation.outbound_request_sha256 is None
    assert observation.reason_code is expected_reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "atomic_status",
    [
        pytest.param(None, id="none"),
        pytest.param({"status": "NATIVE_SUBMIT_INVOKED"}, id="malformed-object"),
        pytest.param("UNRECOGNIZED_STATUS", id="unrecognized-string"),
        pytest.param(
            "EVALUATION_CONTEXT_LOST_NO_REQUEST",
            id="context-loss-without-gate-event",
        ),
    ],
)
async def test_non_contract_atomic_result_after_gate_is_ambiguous(
    atomic_status: object,
) -> None:
    page = _ReadinessPage(atomic_status=atomic_status)
    session = _AtomicReadinessSession(settings=object())
    page.form.submit.session = session
    session._page = page
    session._network_guard = GreenhouseNetworkGuard(
        JOB_URL,
        resolver=_Resolver({"job-boards.greenhouse.io": "8.8.8.8"}),
    )
    session._attachment_marker_id = "fixture-upload"
    session._attachment_upload_name = "resume-fixture.pdf"
    session._attachment = GreenhouseAttachmentProof(
        cv_id="fixture-cv",
        cv_sha256="a" * 64,
        upload_complete=True,
        receipt_sha256="b" * 64,
    )
    expectation = await _atomic_expectation(session)

    observation = await session.atomic_commit(expectation)
    gate = session._outbound_gate

    assert observation.binds(expectation) is True
    assert observation.final_action_invoked is True
    assert observation.request_may_have_left is False
    assert observation.outbound_request_sha256 is None
    assert observation.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED
    assert gate is not None
    assert gate.closed is True


@pytest.mark.asyncio
async def test_gate_request_overrides_contradictory_pre_request_status() -> None:
    page = _ReadinessPage(atomic_status="CONTRADICTORY_GATE_FORM_CHANGED")
    session = _AtomicReadinessSession(settings=object())
    page.form.submit.session = session
    session._page = page
    session._network_guard = GreenhouseNetworkGuard(
        JOB_URL,
        resolver=_Resolver({"job-boards.greenhouse.io": "8.8.8.8"}),
    )
    session._attachment_marker_id = "fixture-upload"
    session._attachment_upload_name = "resume-fixture.pdf"
    session._attachment = GreenhouseAttachmentProof(
        cv_id="fixture-cv",
        cv_sha256="a" * 64,
        upload_complete=True,
        receipt_sha256="b" * 64,
    )
    expectation = await _atomic_expectation(session)

    observation = await session.atomic_commit(expectation)

    assert observation.binds(expectation) is True
    assert observation.final_action_invoked is True
    assert observation.request_may_have_left is True
    assert observation.outbound_request_sha256 == hashlib.sha256(JOB_URL.encode()).hexdigest()
    assert observation.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "drift",
    [
        "disabled",
        "aria-disabled",
        "inert-ancestor-outside-form",
        "hidden-ancestor",
        "pointer-events-none",
        "content-visibility-hidden",
        "visibility-collapse",
        "opacity-zero",
        "detached-control",
        "zero-area-control",
        "wrong-retained-form",
    ],
)
async def test_capture_to_native_submit_actionability_drift_is_blocked_before_request(
    drift: str,
) -> None:
    session = _AtomicReadinessSession(settings=object())
    session._page = _ReadinessPage(atomic_actionability_drift=drift)
    session._network_guard = GreenhouseNetworkGuard(
        JOB_URL,
        resolver=_Resolver({"job-boards.greenhouse.io": "8.8.8.8"}),
    )
    session._attachment_marker_id = "fixture-upload"
    session._attachment_upload_name = "resume-fixture.pdf"
    session._attachment = GreenhouseAttachmentProof(
        cv_id="fixture-cv",
        cv_sha256="a" * 64,
        upload_complete=True,
        receipt_sha256="b" * 64,
    )
    expectation = await _atomic_expectation(session)

    observation = await session.atomic_commit(expectation)

    assert observation.binds(expectation) is True
    assert observation.final_action_invoked is False
    assert observation.request_may_have_left is False
    assert observation.outbound_request_sha256 is None
    assert observation.reason_code is ReasonCode.FORM_CHANGED


@pytest.mark.asyncio
async def test_atomic_primitive_returns_typed_pre_request_attachment_failure() -> None:
    resolver = _Resolver({"job-boards.greenhouse.io": "8.8.8.8"})
    session = _AtomicReadinessSession(settings=object())
    session._page = _ReadinessPage()
    session._network_guard = GreenhouseNetworkGuard(JOB_URL, resolver=resolver)
    session._attachment_marker_id = "fixture-upload"
    session._attachment_upload_name = "resume-fixture.pdf"
    session._attachment = GreenhouseAttachmentProof(
        cv_id="fixture-cv",
        cv_sha256="a" * 64,
        upload_complete=True,
        receipt_sha256="b" * 64,
    )
    expectation = await _atomic_expectation(session)
    session._attachment = GreenhouseAttachmentProof(
        cv_id="fixture-cv",
        cv_sha256="a" * 64,
        upload_complete=False,
    )

    observation = await session.atomic_commit(expectation)

    assert observation.binds(expectation) is True
    assert observation.final_action_invoked is False
    assert observation.request_may_have_left is False
    assert observation.reason_code is ReasonCode.ATTACHMENT_UNVERIFIED


@pytest.mark.asyncio
async def test_navigation_context_destruction_after_gate_is_ambiguous() -> None:
    page = _ReadinessPage(atomic_status="NAVIGATION_CONTEXT_DESTROYED")
    session = _AtomicReadinessSession(settings=object())
    page.form.submit.session = session
    session._page = page
    session._network_guard = GreenhouseNetworkGuard(
        JOB_URL,
        resolver=_Resolver({"job-boards.greenhouse.io": "8.8.8.8"}),
    )
    session._attachment_marker_id = "fixture-upload"
    session._attachment_upload_name = "resume-fixture.pdf"
    session._attachment = GreenhouseAttachmentProof(
        cv_id="fixture-cv",
        cv_sha256="a" * 64,
        upload_complete=True,
        receipt_sha256="b" * 64,
    )
    expectation = await _atomic_expectation(session)

    observation = await session.atomic_commit(expectation)

    assert observation.binds(expectation) is True
    assert observation.final_action_invoked is True
    assert observation.request_may_have_left is True
    assert observation.outbound_request_sha256 == hashlib.sha256(JOB_URL.encode()).hexdigest()
    assert observation.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED


class _ConfirmationLocator:
    def __init__(self, *, count: int, visible: bool, outer_html: str) -> None:
        self._count = count
        self.visible = visible
        self.outer_html = outer_html

    async def count(self) -> int:
        return self._count

    async def is_visible(self) -> bool:
        return self.visible

    async def evaluate(self, script: str):
        assert script == "element => element.outerHTML"
        return self.outer_html


class _ConfirmationPage:
    url = JOB_URL

    def __init__(
        self,
        states: tuple[tuple[int, bool, str], ...],
    ) -> None:
        self.states = list(states)

    def locator(self, selector: str):
        assert selector == '[data-qa="application-confirmation"], #application_confirmation'
        count, visible, outer_html = self.states.pop(0)
        return _ConfirmationLocator(
            count=count,
            visible=visible,
            outer_html=outer_html,
        )

    async def content(self) -> str:
        if not self.states:
            return ""
        return self.states[0][2]

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("states", "expected"),
    [
        (((0, False, ""),), None),
        (((2, True, "<div>duplicate</div>"),), None),
        (((1, False, "<div>hidden</div>"),), None),
        (((1, True, ""),), None),
        (
            (
                (1, True, "<div data-qa='application-confirmation'>one</div>"),
                (1, True, "<div data-qa='application-confirmation'>two</div>"),
            ),
            None,
        ),
        (
            (
                (1, True, "<div data-qa='application-confirmation'>stable</div>"),
                (1, True, "<div data-qa='application-confirmation'>stable</div>"),
            ),
            greenhouse_visible_confirmation_digest(
                "<div data-qa='application-confirmation'>stable</div>",
            ),
        ),
    ],
    ids=["missing", "duplicate", "hidden", "blank", "unstable", "stable"],
)
async def test_confirmation_reference_requires_one_visible_stable_node(
    states: tuple[tuple[int, bool, str], ...],
    expected: str | None,
) -> None:
    resolver = _Resolver({"job-boards.greenhouse.io": "8.8.8.8"})
    session = PlaywrightGreenhouseCandidateSession(settings=object())
    session._page = _ConfirmationPage(states)
    session._network_guard = GreenhouseNetworkGuard(JOB_URL, resolver=resolver)

    assert await session.confirmation_reference() == expected
