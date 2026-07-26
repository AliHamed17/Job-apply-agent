"""Network-boundary tests for the private Workday Playwright transport."""

from __future__ import annotations

import hashlib
import socket

import pytest

from core.submission_domain import ReasonCode
from submitters.workday_playwright import (
    PlaywrightWorkdayCandidateSession,
    WorkdayNetworkGuard,
)
from submitters.workday_v2 import WorkdayAdapterBlockedError, workday_public_hostname

JOB_URL = "https://fixture.wd5.myworkdayjobs.com/en-US/jobs/job/REQ-1"


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
        "http://fixture.wd5.myworkdayjobs.com/job/1",
        "https://user:secret@fixture.wd5.myworkdayjobs.com/job/1",
        "https://fixture.wd5.myworkdayjobs.com:444/job/1",
        "https://fixture.wd5.myworkdayjobs.com/job/1#fragment",
        "https://localhost/job/1",
        "https://127.0.0.1/job/1",
        "https://169.254.169.254/job/1",
        "https://example.test/job/1",
    ],
)
def test_initial_candidate_url_rejects_unsafe_shapes(url: str) -> None:
    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
        workday_public_hostname(url)

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
    host = "fixture.wd5.myworkdayjobs.com"
    guard = WorkdayNetworkGuard(JOB_URL, resolver=_Resolver({host: address}))

    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
        guard.require_allowed_url(JOB_URL)

    assert exc_info.value.reason_code is ReasonCode.RUNTIME_NOT_READY


def test_main_frame_is_bound_to_exact_reviewed_origin() -> None:
    reviewed = "fixture.wd5.myworkdayjobs.com"
    other_tenant = "other.wd5.myworkdayjobs.com"
    resolver = _Resolver({reviewed: "8.8.8.8", other_tenant: "1.1.1.1"})
    guard = WorkdayNetworkGuard(JOB_URL, resolver=resolver)

    guard.require_allowed_url(JOB_URL)
    with pytest.raises(WorkdayAdapterBlockedError):
        guard.require_allowed_url(f"https://{other_tenant}/job/REQ-2")

    assert resolver.calls == [reviewed]


def test_static_assets_use_bounded_cdn_allowlist_and_per_host_dns() -> None:
    reviewed = "fixture.wd5.myworkdayjobs.com"
    cdn = "wd5.myworkdaycdn.com"
    resolver = _Resolver({reviewed: "8.8.8.8", cdn: "1.1.1.1"})
    guard = WorkdayNetworkGuard(JOB_URL, resolver=resolver)

    guard.require_allowed_url(JOB_URL)
    guard.require_allowed_url(f"https://{cdn}/assets/app.js", main_frame=False)
    guard.require_allowed_url(f"https://{cdn}/assets/app.css", main_frame=False)
    with pytest.raises(WorkdayAdapterBlockedError):
        guard.require_allowed_url(
            "https://other.wd5.myworkdayjobs.com/assets/app.js",
            main_frame=False,
        )

    assert resolver.calls == [reviewed, cdn]


def test_private_cdn_resolution_is_not_bypassed_by_verified_initial_host() -> None:
    reviewed = "fixture.wd5.myworkdayjobs.com"
    cdn = "wd5.myworkdaycdn.com"
    resolver = _Resolver({reviewed: "8.8.8.8", cdn: "127.0.0.1"})
    guard = WorkdayNetworkGuard(JOB_URL, resolver=resolver)

    guard.require_allowed_url(JOB_URL)
    with pytest.raises(WorkdayAdapterBlockedError):
        guard.require_allowed_url(f"https://{cdn}/assets/app.js", main_frame=False)

    assert resolver.calls == [reviewed, cdn]


class _Route:
    def __init__(self) -> None:
        self.action: str | None = None

    async def abort(self, _reason: str) -> None:
        self.action = "abort"

    async def continue_(self) -> None:
        self.action = "continue"


class _Request:
    def __init__(self, url: str, *, navigation: bool) -> None:
        self.url = url
        self._navigation = navigation

    def is_navigation_request(self) -> bool:
        return self._navigation


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
        raise AssertionError("HTTP routing must not install after missing WebSocket routing")


class _WebSocket:
    def __init__(self, url: str) -> None:
        self.url = url
        self.closed = False
        self.code = None

    async def close(self, *, code=None, reason=None) -> None:
        self.closed = True
        self.code = code
        assert reason == "browser transport disabled"


class _EmptyLocator:
    async def count(self) -> int:
        return 0

    async def is_visible(self) -> bool:
        return False


class _UploadNode:
    def __init__(
        self,
        *,
        automation_id: str,
        marker_id: str,
        filename: str,
    ) -> None:
        self.automation_id = automation_id
        self.attrs = {
            "data-upload-id": marker_id,
            "data-file-name": filename,
        }

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
        self.page = page
        self.first = _FileInput(page)

    async def count(self) -> int:
        return 1


class _UploadPage:
    url = JOB_URL

    def __init__(self, *, fresh_marker: bool) -> None:
        self.upload_started = False
        self.selected_name = ""
        self.uploaded_buffer = b""
        self.fresh_marker = fresh_marker
        self.stale = _UploadNode(
            automation_id="uploadCompleted",
            marker_id="pre-existing",
            filename="fixture-cv.pdf",
        )
        self.fresh = _UploadNode(
            automation_id="uploadCompleted",
            marker_id="fresh-after-input",
            filename="fixture-cv.pdf",
        )

    def locator(self, selector: str):
        if 'input[type="file"]' in selector:
            return _FileInputList(self)
        nodes = []
        if 'data-automation-id="uploadCompleted"' in selector:
            if not self.upload_started or not self.fresh_marker:
                nodes.append(self.stale)
            else:
                nodes.append(self.fresh)
        return _NodeList(nodes)

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        return None


class _ConfirmationLocator:
    def __init__(self, *, visible: bool, reference: str | None) -> None:
        self.visible = visible
        self.reference = reference

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return self.visible

    async def get_attribute(self, _name: str):
        return self.reference


class _ConfirmationPage:
    url = JOB_URL

    def __init__(self, states: tuple[tuple[bool, str | None], ...]) -> None:
        self.states = list(states)

    def locator(self, _selector: str):
        visible, reference = self.states.pop(0)
        return _ConfirmationLocator(visible=visible, reference=reference)

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "navigation", "expected"),
    [
        ("data:text/plain,safe", False, "continue"),
        ("blob:https://fixture.wd5.myworkdayjobs.com/id", False, "continue"),
        ("data:text/html,blocked", True, "abort"),
        ("file:///etc/passwd", False, "abort"),
        ("http://fixture.wd5.myworkdayjobs.com/app.js", False, "abort"),
    ],
)
async def test_non_https_routes_are_limited_to_non_navigation_data(
    url: str,
    navigation: bool,
    expected: str,
) -> None:
    resolver = _Resolver({"fixture.wd5.myworkdayjobs.com": "8.8.8.8"})
    session = PlaywrightWorkdayCandidateSession(settings=object())
    session._network_guard = WorkdayNetworkGuard(JOB_URL, resolver=resolver)
    route = _Route()

    await session._guard_request(route, _Request(url, navigation=navigation))

    assert route.action == expected


@pytest.mark.asyncio
async def test_websocket_routing_capability_is_required_fail_closed() -> None:
    session = PlaywrightWorkdayCandidateSession(settings=object())

    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
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
    session = PlaywrightWorkdayCandidateSession(settings=object())

    await session._install_network_routes(context)

    assert context.calls == ["websocket", "http"]
    assert context.web_socket_handler is not None
    web_socket = _WebSocket(url)
    await context.web_socket_handler(web_socket)
    assert web_socket.closed is True
    assert web_socket.code == 1008


@pytest.mark.asyncio
async def test_preexisting_generic_upload_marker_cannot_prove_current_cv() -> None:
    payload = b"%PDF-1.4\nsanitized\n%%EOF\n"
    session = PlaywrightWorkdayCandidateSession(settings=object())
    session._page = _UploadPage(fresh_marker=False)

    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
        await session.ensure_resume_attachment(
            resume_bytes=payload,
            cv_id="fixture-cv",
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )

    assert exc_info.value.reason_code is ReasonCode.ATTACHMENT_UNVERIFIED


@pytest.mark.asyncio
async def test_fresh_upload_marker_is_receipt_bound_and_rechecked() -> None:
    payload = b"%PDF-1.4\nsanitized\n%%EOF\n"
    digest = hashlib.sha256(payload).hexdigest()
    page = _UploadPage(fresh_marker=True)
    session = PlaywrightWorkdayCandidateSession(settings=object())
    session._page = page

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
@pytest.mark.parametrize(
    ("states", "expected"),
    [
        (((False, "APP-1"),), None),
        (((True, ""),), None),
        (((True, "APP-1"), (True, "APP-2")), None),
        (((True, "APP-1"), (False, "APP-1")), None),
        (((True, "APP-1"), (True, "APP-1")), "APP-1"),
    ],
)
async def test_confirmation_reference_requires_nonblank_visible_stable_locator(
    states,
    expected,
) -> None:
    resolver = _Resolver({"fixture.wd5.myworkdayjobs.com": "8.8.8.8"})
    session = PlaywrightWorkdayCandidateSession(settings=object())
    session._page = _ConfirmationPage(states)
    session._network_guard = WorkdayNetworkGuard(JOB_URL, resolver=resolver)

    assert await session.confirmation_reference() == expected
