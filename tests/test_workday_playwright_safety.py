"""Network-boundary tests for the private Workday Playwright transport."""

from __future__ import annotations

import hashlib
import socket

import pytest

from core.submission_domain import (
    VERIFIED_ATTACHMENT_SENTINEL,
    AnswerDecisionV1,
    AnswerDisposition,
    AnswerProvenance,
    ReasonCode,
)
from submitters.workday_playwright import (
    _ATOMIC_FINAL_SUBMIT_JS,
    _CAPTURE_FINAL_CONTROL_JS,
    PlaywrightWorkdayCandidateSession,
    WorkdayNetworkGuard,
    _canonical_form_data_payload_sha256,
)
from submitters.workday_v2 import (
    WorkdayAdapterBlockedError,
    WorkdayAttachmentProof,
    WorkdayBoundFinalRequestContract,
    WorkdayFinalActionAmbiguousError,
    WorkdayFinalCommitExpectation,
    observe_workday_v2_fields,
    workday_job_identity,
    workday_public_hostname,
    workday_v2_answer_bindings,
    workday_v2_final_action_contract,
    workday_v2_final_action_ready,
    workday_v2_final_request_matches,
    workday_v2_form_fingerprint,
    workday_v2_request_contract,
)

JOB_URL = "https://fixture.wd5.myworkdayjobs.com/en-US/jobs/job/REQ-1"
CV_BYTES = b"%PDF-1.4\nsanitized exact cv\n%%EOF\n"
CV_SHA256 = hashlib.sha256(CV_BYTES).hexdigest()
_BOUNDARY = "----WorkdayFixtureBoundary7MA4YWxk"


def _multipart_body(
    *,
    answer: str = "jobs",
    hidden_value: str | None = None,
    filename: str = "resume-sanitized.pdf",
    file_bytes: bytes = CV_BYTES,
) -> tuple[str, bytes]:
    fields = [("jobId", "REQ-1"), ("site", answer)]
    if hidden_value is not None:
        fields.append(("opaqueSuccessfulControl", hidden_value))
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.extend(
            (
                f"--{_BOUNDARY}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode(),
                b"\r\n",
            )
        )
    chunks.extend(
        (
            f"--{_BOUNDARY}\r\n".encode(),
            (f'Content-Disposition: form-data; name="resume"; filename="{filename}"\r\n').encode(),
            b"Content-Type: application/pdf\r\n\r\n",
            file_bytes,
            b"\r\n",
            f"--{_BOUNDARY}--\r\n".encode(),
        )
    )
    return f"multipart/form-data; boundary={_BOUNDARY}", b"".join(chunks)


DEFAULT_CONTENT_TYPE, DEFAULT_REQUEST_BODY = _multipart_body()


class _Frame:
    def __init__(self, page) -> None:
        self.page = page


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
        "https://workday.com/job/1",
        "https://status.workday.com/job/1",
        "https://myworkdayjobs.com/job/1",
        "https://www.myworkdayjobs.com/job/1",
        "https://api.wd5.myworkdayjobs.com/job/1",
    ],
)
def test_initial_candidate_url_rejects_unsafe_shapes(url: str) -> None:
    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
        workday_public_hostname(url)

    assert exc_info.value.reason_code is ReasonCode.RUNTIME_NOT_READY


@pytest.mark.parametrize(
    "url",
    [
        JOB_URL,
        "https://fixture.wd3.myworkday.com/en-US/jobs/job/REQ-1",
    ],
)
def test_candidate_tenant_hosts_are_explicitly_bounded(url: str) -> None:
    identity = workday_job_identity(url)

    assert identity.hostname.startswith("fixture.")
    assert identity.site == "jobs"
    assert identity.requisition == "req-1"


@pytest.mark.parametrize(
    "query",
    [
        "jobId=REQ-999",
        "requisition=REQ-999",
        "site=other-site",
        "externalCareerSiteId=other-site",
    ],
)
def test_job_selecting_query_mismatch_is_rejected(query: str) -> None:
    with pytest.raises(WorkdayAdapterBlockedError):
        workday_job_identity(f"{JOB_URL}?{query}")


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


def test_main_frame_rejects_same_tenant_other_job_redirect() -> None:
    reviewed = "fixture.wd5.myworkdayjobs.com"
    resolver = _Resolver({reviewed: "8.8.8.8"})
    guard = WorkdayNetworkGuard(JOB_URL, resolver=resolver)

    guard.require_allowed_url(JOB_URL)
    guard.require_allowed_url(f"{JOB_URL}/apply")
    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
        guard.require_allowed_url("https://fixture.wd5.myworkdayjobs.com/en-US/jobs/job/REQ-999")

    assert exc_info.value.reason_code is ReasonCode.FORM_CHANGED
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
    def __init__(self, *, continue_error: bool = False) -> None:
        self.action: str | None = None
        self.continue_error = continue_error

    async def abort(self, _reason: str) -> None:
        self.action = "abort"

    async def continue_(self) -> None:
        self.action = "continue"
        if self.continue_error:
            raise TimeoutError("synthetic route continuation ambiguity")


class _Request:
    def __init__(
        self,
        url: str,
        *,
        navigation: bool,
        method: str = "GET",
        content_type: str = "",
        body: bytes | str | None = None,
        frame=None,
        resource_type: str = "document",
    ) -> None:
        self.url = url
        self._navigation = navigation
        self.method = method
        self.resource_type = resource_type
        self.headers = {"content-type": content_type} if content_type else {}
        self.frame = frame
        if isinstance(body, str):
            self.post_data = body
            self.post_data_buffer = body.encode()
        else:
            self.post_data = None
            self.post_data_buffer = body

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


class _HtmlLocator:
    async def get_attribute(self, name: str):
        return "en" if name == "lang" else None


class _ExactSubmitHandle:
    def __init__(
        self,
        *,
        request_url: str = f"{JOB_URL}/apply",
        request_body: bytes = DEFAULT_REQUEST_BODY,
        request_content_type: str = DEFAULT_CONTENT_TYPE,
        captured_body: bytes = DEFAULT_REQUEST_BODY,
        atomic_body: bytes | None = None,
        request_method: str = "POST",
        request_navigation: bool = True,
        request_resource_type: str = "document",
        use_iframe: bool = False,
        background_get: bool = False,
        post_send_error: bool = False,
        post_send_blocked_error: bool = False,
        mutate_before_atomic_submit: bool = False,
        captured_disabled: bool = False,
        captured_aria_disabled: bool = False,
        captured_inert: bool = False,
        atomic_disabled: bool = False,
        atomic_aria_disabled: bool = False,
        atomic_inert: bool = False,
    ) -> None:
        self.session = None
        self.request_url = request_url
        self.request_body = request_body
        self.request_content_type = request_content_type
        self.captured_body = captured_body
        self.atomic_body = atomic_body
        self.request_method = request_method
        self.request_navigation = request_navigation
        self.request_resource_type = request_resource_type
        self.use_iframe = use_iframe
        self.background_get = background_get
        self.post_send_error = post_send_error
        self.post_send_blocked_error = post_send_blocked_error
        self.mutate_before_atomic_submit = mutate_before_atomic_submit
        self.captured_disabled = captured_disabled
        self.captured_aria_disabled = captured_aria_disabled
        self.captured_inert = captured_inert
        self.atomic_disabled = atomic_disabled
        self.atomic_aria_disabled = atomic_aria_disabled
        self.atomic_inert = atomic_inert
        self.clicks = 0
        self.route: _Route | None = None
        self.background_route: _Route | None = None

    async def evaluate(self, _script: str, arg=None):
        if "stateDigest" not in (arg or {}):
            payload_digest = _canonical_form_data_payload_sha256(
                content_type=self.request_content_type,
                body=self.captured_body,
                expected_cv_sha256=str(arg["cvSha256"]),
            )
            assert payload_digest is not None
            coverage_valid = (
                self.session is not None
                and "data-unreviewed-successful-control" not in self.session._page.html
            )
            return {
                "connected": True,
                "inReview": True,
                "reviewCount": 1,
                "type": "submit",
                "explicitForm": False,
                "explicitFormAction": False,
                "explicitFormMethod": False,
                "disabled": self.captured_disabled,
                "ariaDisabled": self.captured_aria_disabled,
                "inert": self.captured_inert,
                "actionable": not (
                    self.captured_disabled or self.captured_aria_disabled or self.captured_inert
                ),
                "action": f"{JOB_URL}/apply",
                "method": "post",
                "encoding": "multipart/form-data",
                "stateStable": True,
                "stateDigest": "c" * 64,
                "payloadValid": coverage_valid,
                "payloadDigest": payload_digest if coverage_valid else None,
            }
        if (
            self.mutate_before_atomic_submit
            or self.atomic_disabled
            or self.atomic_aria_disabled
            or self.atomic_inert
        ):
            return {"released": False}
        atomic_digest = _canonical_form_data_payload_sha256(
            content_type=self.request_content_type,
            body=self.atomic_body or self.captured_body,
            expected_cv_sha256=str(arg["cvSha256"]),
        )
        if atomic_digest is None or atomic_digest != arg["payloadDigest"]:
            return {"released": False}
        self.clicks += 1
        assert self.session is not None
        page = self.session._page
        if self.background_get:
            self.background_route = _Route()
            await self.session._guard_request(
                self.background_route,
                _Request(
                    f"{JOB_URL}/assets/status",
                    navigation=False,
                    method="GET",
                    frame=page.main_frame,
                ),
            )
        self.route = _Route()
        frame = _Frame(page) if self.use_iframe else page.main_frame
        await self.session._guard_request(
            self.route,
            _Request(
                self.request_url,
                navigation=self.request_navigation,
                method=self.request_method,
                content_type=self.request_content_type,
                body=self.request_body,
                frame=frame,
                resource_type=self.request_resource_type,
            ),
        )
        if self.post_send_blocked_error:
            raise WorkdayAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        if self.post_send_error:
            raise TimeoutError("synthetic post-send ambiguity")
        return {"released": True}

    async def click(self, **_kwargs) -> None:
        raise AssertionError("final action must use the retained atomic submit primitive")


class _ScopedSubmitButton:
    def __init__(
        self,
        handle: _ExactSubmitHandle,
        *,
        retarget: _ExactSubmitHandle | None = None,
    ) -> None:
        self.handle = handle
        self.retarget = retarget

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return True

    async def is_enabled(self) -> bool:
        return True

    async def element_handle(self):
        retained = self.handle
        if self.retarget is not None:
            self.handle = self.retarget
        return retained


class _ScopedReview:
    def __init__(self, submit: _ScopedSubmitButton) -> None:
        self.submit = submit

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return True

    def locator(self, selector: str):
        assert selector == 'button[data-automation-id="submitApplication"]'
        return self.submit


class _DecoySubmitPage:
    url = JOB_URL

    def __init__(
        self,
        handle: _ExactSubmitHandle | None = None,
        *,
        extra_form_html: str = "",
    ) -> None:
        self.main_frame = _Frame(self)
        self.real_handle = handle or _ExactSubmitHandle()
        self.decoy_handle = _ExactSubmitHandle()
        self.real_submit = _ScopedSubmitButton(
            self.real_handle,
            retarget=self.decoy_handle,
        )
        self.review = _ScopedReview(self.real_submit)
        self.global_submit_queries = 0
        self.html = f"""
        <button data-automation-id="submitApplication" id="decoy">Submit</button>
        <form data-automation-id="reviewPage" action="{JOB_URL}/apply" method="post"
              enctype="multipart/form-data">
          <button data-automation-id="submitApplication" id="real"
                  type="submit">Submit</button>
          {extra_form_html}
        </form>
        """

    def locator(self, selector: str):
        if selector == "html":
            return _HtmlLocator()
        if selector == '[data-automation-id="reviewPage"]':
            return self.review
        if selector == 'button[data-automation-id="submitApplication"]':
            self.global_submit_queries += 1
            raise AssertionError("final action must not use a page-global submit locator")
        raise AssertionError(f"unexpected selector: {selector}")

    async def content(self) -> str:
        return self.html


class _UploadChild:
    def __init__(self, *, filename: str = "", digest: str = "") -> None:
        self.filename = filename
        self.digest = digest

    async def is_visible(self) -> bool:
        return True

    async def inner_text(self) -> str:
        return self.filename

    async def get_attribute(self, name: str):
        return self.digest if name == "data-file-sha256" else None


class _UploadNode:
    def __init__(
        self,
        *,
        automation_id: str,
        marker_id: str,
        filename: str,
        digest: str = "",
        child_filename: str = "",
        child_digest: str = "",
    ) -> None:
        self.automation_id = automation_id
        self.attrs = {
            "data-upload-id": marker_id,
            "data-file-name": filename,
            "data-file-sha256": digest,
        }
        self.child_filename = child_filename
        self.child_digest = child_digest

    async def is_visible(self) -> bool:
        return True

    async def get_attribute(self, name: str):
        return self.attrs.get(name)

    def locator(self, selector: str):
        if selector == '[data-automation-id="uploadedFileName"]' and self.child_filename:
            return _NodeList([_UploadChild(filename=self.child_filename)])
        if selector == "[data-file-sha256]" and self.child_digest:
            return _NodeList([_UploadChild(digest=self.child_digest)])
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
        if not self.page.omit_digest and not self.page.conflicting_digest:
            self.page.fresh.attrs["data-file-sha256"] = hashlib.sha256(
                payload["buffer"]
            ).hexdigest()
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

    def __init__(
        self,
        *,
        fresh_marker: bool,
        conflicting_digest: str = "",
        omit_digest: bool = False,
        conflicting_child_filename: str = "",
        conflicting_child_digest: str = "",
    ) -> None:
        self.upload_started = False
        self.selected_name = ""
        self.uploaded_buffer = b""
        self.fresh_marker = fresh_marker
        self.conflicting_digest = conflicting_digest
        self.omit_digest = omit_digest
        self.stale = _UploadNode(
            automation_id="uploadCompleted",
            marker_id="pre-existing",
            filename="fixture-cv.pdf",
        )
        self.fresh = _UploadNode(
            automation_id="uploadCompleted",
            marker_id="fresh-after-input",
            filename="fixture-cv.pdf",
            digest=conflicting_digest,
            child_filename=conflicting_child_filename,
            child_digest=conflicting_child_digest,
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
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_unarmed_mutation_request_is_aborted_before_network_send(method: str) -> None:
    resolver = _Resolver({"fixture.wd5.myworkdayjobs.com": "8.8.8.8"})
    session = PlaywrightWorkdayCandidateSession(settings=object())
    session._network_guard = WorkdayNetworkGuard(JOB_URL, resolver=resolver)
    route = _Route()

    await session._guard_request(
        route,
        _Request(
            f"{JOB_URL}/apply",
            navigation=True,
            method=method,
            content_type="application/x-www-form-urlencoded",
            body="jobId=REQ-1&site=jobs",
        ),
    )

    assert route.action == "abort"
    assert session._final_request_gate is None


@pytest.mark.asyncio
async def test_unarmed_reversible_get_remains_allowed() -> None:
    resolver = _Resolver({"fixture.wd5.myworkdayjobs.com": "8.8.8.8"})
    session = PlaywrightWorkdayCandidateSession(settings=object())
    session._network_guard = WorkdayNetworkGuard(JOB_URL, resolver=resolver)
    route = _Route()

    await session._guard_request(
        route,
        _Request(JOB_URL, navigation=True, method="GET"),
    )

    assert route.action == "continue"
    assert session._final_request_gate is None


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
async def test_explicit_conflicting_upload_digest_cannot_be_overridden_by_filename() -> None:
    payload = b"%PDF-1.4\nsanitized\n%%EOF\n"
    page = _UploadPage(fresh_marker=True, conflicting_digest="f" * 64)
    session = PlaywrightWorkdayCandidateSession(settings=object())
    session._page = page

    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
        await session.ensure_resume_attachment(
            resume_bytes=payload,
            cv_id="fixture-cv",
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )

    assert exc_info.value.reason_code is ReasonCode.ATTACHMENT_UNVERIFIED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "page",
    [
        pytest.param(
            _UploadPage(
                fresh_marker=True,
                conflicting_child_filename="different-visible-name.pdf",
            ),
            id="conflicting-visible-child-name",
        ),
        pytest.param(
            _UploadPage(
                fresh_marker=True,
                conflicting_child_digest="f" * 64,
            ),
            id="conflicting-visible-child-digest",
        ),
    ],
)
async def test_every_exposed_upload_name_and_digest_must_agree(page) -> None:
    payload = b"%PDF-1.4\nsanitized\n%%EOF\n"
    session = PlaywrightWorkdayCandidateSession(settings=object())
    session._page = page

    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
        await session.ensure_resume_attachment(
            resume_bytes=payload,
            cv_id="fixture-cv",
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )

    assert exc_info.value.reason_code is ReasonCode.ATTACHMENT_UNVERIFIED


@pytest.mark.asyncio
async def test_filename_only_upload_marker_cannot_prove_selected_cv() -> None:
    payload = b"%PDF-1.4\nsanitized\n%%EOF\n"
    page = _UploadPage(fresh_marker=True, omit_digest=True)
    session = PlaywrightWorkdayCandidateSession(settings=object())
    session._page = page

    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
        await session.ensure_resume_attachment(
            resume_bytes=payload,
            cv_id="fixture-cv",
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )

    assert exc_info.value.reason_code is ReasonCode.ATTACHMENT_UNVERIFIED


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


def test_final_action_contract_binds_form_action_to_exact_job() -> None:
    expected = workday_job_identity(JOB_URL)
    same_job = f"""
    <form data-automation-id="reviewPage" action="{JOB_URL}/apply" method="post"
          enctype="multipart/form-data">
      <button data-automation-id="submitApplication" type="submit">Submit</button>
    </form>
    """
    other_job = same_job.replace("REQ-1/apply", "REQ-999/apply")
    other_job_query = same_job.replace(
        'action="https://fixture.wd5.myworkdayjobs.com/en-US/jobs/job/REQ-1/apply"',
        'action="https://fixture.wd5.myworkdayjobs.com/en-US/jobs/job/REQ-1/apply?jobId=REQ-999"',
    )
    submitter_override = same_job.replace(
        'type="submit"',
        f'type="submit" formaction="{JOB_URL}/apply?source=override"',
    )
    wrong_encoding = same_job.replace(
        "multipart/form-data",
        "application/x-www-form-urlencoded",
    )
    actionless = """
    <main data-automation-id="reviewPage">
      <button data-automation-id="submitApplication" type="button">Submit</button>
    </main>
    """
    decoy_only = """
    <button data-automation-id="submitApplication">Decoy</button>
    <main data-automation-id="reviewPage"><h1>Review</h1></main>
    """

    assert workday_v2_final_action_ready(same_job, JOB_URL, expected) is True
    assert workday_v2_final_action_ready(other_job, JOB_URL, expected) is False
    assert workday_v2_final_action_ready(other_job_query, JOB_URL, expected) is False
    assert workday_v2_final_action_ready(submitter_override, JOB_URL, expected) is False
    assert workday_v2_final_action_ready(wrong_encoding, JOB_URL, expected) is False
    assert workday_v2_final_action_ready(actionless, JOB_URL, expected) is False
    assert workday_v2_final_action_ready(decoy_only, JOB_URL, expected) is False


@pytest.mark.parametrize(
    "non_actionable_attribute",
    [
        pytest.param("disabled", id="disabled"),
        pytest.param('aria-disabled="true"', id="aria-disabled"),
        pytest.param("inert", id="inert"),
        pytest.param('style="pointer-events: none"', id="pointer-events"),
    ],
)
def test_final_action_contract_rejects_factually_non_actionable_submit(
    non_actionable_attribute: str,
) -> None:
    expected = workday_job_identity(JOB_URL)
    html = f"""
    <form data-automation-id="reviewPage" action="{JOB_URL}/apply" method="post"
          enctype="multipart/form-data">
      <button data-automation-id="submitApplication" type="submit"
              {non_actionable_attribute}>Submit</button>
    </form>
    """

    assert workday_v2_final_action_contract(html, JOB_URL, expected) is None


@pytest.mark.parametrize(
    ("payload_sha256", "expected"),
    [
        ("b" * 64, True),
        ("c" * 64, False),
        ("not-a-digest", False),
    ],
)
def test_final_request_contract_requires_exact_target_and_payload_commitment(
    payload_sha256: str,
    expected: bool,
) -> None:
    identity = workday_job_identity(JOB_URL)
    target = workday_v2_request_contract(f"{JOB_URL}/apply", "POST", identity)
    assert target is not None
    contract = WorkdayBoundFinalRequestContract.bind(target, "b" * 64)

    assert (
        workday_v2_final_request_matches(
            contract,
            method="POST",
            url=f"{JOB_URL}/apply",
            payload_sha256=payload_sha256,
        )
        is expected
    )
    assert "private" not in repr(contract)


def test_final_request_contract_hashes_private_query_without_retaining_it() -> None:
    identity = workday_job_identity(JOB_URL)
    private_value = "private-answer-sentinel"
    action_url = f"{JOB_URL}/apply?opaque={private_value}"
    target = workday_v2_request_contract(action_url, "POST", identity)
    assert target is not None
    contract = WorkdayBoundFinalRequestContract.bind(target, "b" * 64)

    assert private_value not in repr(contract)
    assert not hasattr(contract.target_contract, "canonical_target")
    assert workday_v2_final_request_matches(
        contract,
        method="POST",
        url=action_url,
        payload_sha256="b" * 64,
    )
    assert not workday_v2_final_request_matches(
        contract,
        method="POST",
        url=f"{JOB_URL}/apply?opaque=changed",
        payload_sha256="b" * 64,
    )


def test_canonical_form_data_commitment_binds_order_values_hidden_controls_and_file() -> None:
    expected = _canonical_form_data_payload_sha256(
        content_type=DEFAULT_CONTENT_TYPE,
        body=DEFAULT_REQUEST_BODY,
        expected_cv_sha256=CV_SHA256,
    )
    assert expected is not None

    _content_type, answer_changed = _multipart_body(answer="changed")
    _content_type, hidden_changed = _multipart_body(hidden_value="changed")
    _content_type, renamed_file = _multipart_body(filename="other-name.pdf")
    _, wrong_file_same_name = _multipart_body(
        filename="resume-sanitized.pdf",
        file_bytes=b"%PDF-1.4\nwrong bytes\n%%EOF\n",
    )
    trailing_bytes = DEFAULT_REQUEST_BODY + b"smuggled"
    duplicate_disposition = DEFAULT_REQUEST_BODY.replace(
        b'Content-Disposition: form-data; name="jobId"\r\n',
        (
            b'Content-Disposition: form-data; name="jobId"\r\n'
            b'Content-Disposition: form-data; name="jobId"\r\n'
        ),
        1,
    )

    assert (
        _canonical_form_data_payload_sha256(
            content_type=DEFAULT_CONTENT_TYPE,
            body=answer_changed,
            expected_cv_sha256=CV_SHA256,
        )
        != expected
    )
    assert (
        _canonical_form_data_payload_sha256(
            content_type=DEFAULT_CONTENT_TYPE,
            body=hidden_changed,
            expected_cv_sha256=CV_SHA256,
        )
        != expected
    )
    assert (
        _canonical_form_data_payload_sha256(
            content_type=DEFAULT_CONTENT_TYPE,
            body=renamed_file,
            expected_cv_sha256=CV_SHA256,
        )
        != expected
    )
    assert (
        _canonical_form_data_payload_sha256(
            content_type=DEFAULT_CONTENT_TYPE,
            body=wrong_file_same_name,
            expected_cv_sha256=CV_SHA256,
        )
        is None
    )
    assert (
        _canonical_form_data_payload_sha256(
            content_type=DEFAULT_CONTENT_TYPE,
            body=trailing_bytes,
            expected_cv_sha256=CV_SHA256,
        )
        is None
    )
    assert (
        _canonical_form_data_payload_sha256(
            content_type=DEFAULT_CONTENT_TYPE,
            body=duplicate_disposition,
            expected_cv_sha256=CV_SHA256,
        )
        is None
    )


def test_answer_binding_is_value_exact_without_retaining_private_answer() -> None:
    fields = observe_workday_v2_fields(
        """
        <div data-automation-id="formField" data-field-id="cover_note"
             data-canonical-name="cover_note" aria-required="true">
          <label for="cover-note">Cover note</label>
          <textarea id="cover-note" required></textarea>
        </div>
        """
    )
    private_answer = "private-answer-sentinel"
    first = workday_v2_answer_bindings(
        fields,
        (
            AnswerDecisionV1(
                field_id="cover_note",
                disposition=AnswerDisposition.RESOLVED,
                provenance=AnswerProvenance.USER_CONFIRMED,
                value=private_answer,
            ),
        ),
        selected_cv_hash=CV_SHA256,
    )
    second = workday_v2_answer_bindings(
        fields,
        (
            AnswerDecisionV1(
                field_id="cover_note",
                disposition=AnswerDisposition.RESOLVED,
                provenance=AnswerProvenance.USER_CONFIRMED,
                value=f"{private_answer}-changed",
            ),
        ),
        selected_cv_hash=CV_SHA256,
    )

    assert private_answer not in repr(first)
    assert first[0].value_sha256 != second[0].value_sha256


def test_file_answer_binding_is_content_bound_to_selected_cv() -> None:
    fields = observe_workday_v2_fields(
        """
        <div data-automation-id="formField" data-field-id="resume"
             data-canonical-name="resume" aria-required="true">
          <label for="resume">Resume</label>
          <input id="resume" type="file" required>
        </div>
        """
    )
    decisions = (
        AnswerDecisionV1(
            field_id="resume",
            disposition=AnswerDisposition.RESOLVED,
            provenance=AnswerProvenance.VERIFIED_ATTACHMENT,
            value=VERIFIED_ATTACHMENT_SENTINEL,
        ),
    )

    first = workday_v2_answer_bindings(
        fields,
        decisions,
        selected_cv_hash=CV_SHA256,
    )
    second = workday_v2_answer_bindings(
        fields,
        decisions,
        selected_cv_hash="f" * 64,
    )

    assert first[0].value_sha256 != second[0].value_sha256
    assert VERIFIED_ATTACHMENT_SENTINEL not in repr(first)


def test_hidden_answer_control_cannot_be_observed_as_reviewed_text() -> None:
    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
        observe_workday_v2_fields(
            """
            <div data-automation-id="formField" data-field-id="unreviewed"
                 data-canonical-name="unreviewed">
              <label for="unreviewed">Unreviewed</label>
              <input id="unreviewed" name="unreviewed" type="hidden" value="default">
            </div>
            """
        )

    assert exc_info.value.reason_code is ReasonCode.SELECTOR_DRIFT


def test_browser_commitment_has_strict_answer_coverage_and_tiny_system_allowlist() -> None:
    for script in (_CAPTURE_FINAL_CONTROL_JS, _ATOMIC_FINAL_SUBMIT_JS):
        assert "const answerControlOwners" in script
        assert "const systemEntryValid" in script
        assert '["jobId", "jobPostingId", "requisitionId"]' in script
        assert '["site", "careerSite", "externalCareerSiteId"]' in script
        assert '["_csrf", "csrfToken", "xsrfToken"]' in script
        assert "replace(/[^a-z0-9]/g" not in script
        assert 'normalized.split("_").pop()' not in script
        assert "owners.get(name)" in script
        assert 'controls[0].type !== "hidden"' in script
        assert "owner.index !== owner.expectedValues.length" in script
        assert 'element.matches(":disabled")' in script
        assert 'getAttribute("aria-disabled")' in script
        assert 'hasAttribute("inert")' in script
        assert "style.pointerEvents" in script
        assert "element.getClientRects()" in script


def _commit_expectation(html: str) -> tuple[WorkdayFinalCommitExpectation, WorkdayAttachmentProof]:
    identity = workday_job_identity(JOB_URL)
    request_contract = workday_v2_final_action_contract(html, JOB_URL, identity)
    assert request_contract is not None
    fields = observe_workday_v2_fields(
        """
        <div data-automation-id="formField" data-field-id="resume"
             data-canonical-name="resume" aria-required="true">
          <label for="resume">Resume</label>
          <input id="resume" type="file" required>
        </div>
        """
    )
    form_fingerprint = workday_v2_form_fingerprint(
        fields,
        (len(fields),),
        request_contract.digest,
    )
    decisions = (
        AnswerDecisionV1(
            field_id="resume",
            disposition=AnswerDisposition.RESOLVED,
            provenance=AnswerProvenance.VERIFIED_ATTACHMENT,
            value=VERIFIED_ATTACHMENT_SENTINEL,
        ),
    )
    proof = WorkdayAttachmentProof(
        cv_id="fixture-cv",
        cv_sha256=CV_SHA256,
        upload_complete=True,
        receipt_sha256="b" * 64,
    )
    return (
        WorkdayFinalCommitExpectation(
            job_identity=identity,
            pre_action_digest=hashlib.sha256(html.encode("utf-8")).hexdigest(),
            form_fingerprint=form_fingerprint,
            final_action_binding=request_contract.digest,
            observed_fields=fields,
            step_field_counts=(len(fields),),
            answer_bindings=workday_v2_answer_bindings(
                fields,
                decisions,
                selected_cv_hash=proof.cv_sha256,
            ),
            selected_cv_id=proof.cv_id,
            selected_cv_hash=proof.cv_sha256,
            attachment_receipt_sha256=proof.receipt_sha256 or "",
            request_contract=request_contract,
        ),
        proof,
    )


def _prime_transport_attachment(session: PlaywrightWorkdayCandidateSession) -> None:
    session._attachment_marker_id = "sanitized-upload-receipt"
    session._attachment_upload_name = "resume-sanitized.pdf"


async def _configured_transport(
    handle: _ExactSubmitHandle,
    monkeypatch,
    *,
    extra_form_html: str = "",
):
    resolver = _Resolver({"fixture.wd5.myworkdayjobs.com": "8.8.8.8"})
    page = _DecoySubmitPage(handle, extra_form_html=extra_form_html)
    session = PlaywrightWorkdayCandidateSession(settings=object())
    session._page = page
    session._network_guard = WorkdayNetworkGuard(JOB_URL, resolver=resolver)
    handle.session = session
    expectation, proof = _commit_expectation(page.html)
    _prime_transport_attachment(session)

    async def _verified_attachment(**_kwargs):
        return proof

    monkeypatch.setattr(session, "verify_resume_attachment", _verified_attachment)
    return session, expectation, page


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra_form_html",
    [
        pytest.param(
            '<input data-unreviewed-successful-control type="hidden" '
            'name="unknownDefault" value="page-default">',
            id="unknown-hidden-successful-control",
        ),
        pytest.param(
            '<input data-unreviewed-successful-control type="text" '
            'name="unknownAnswer" value="page-default">',
            id="unreviewed-text-page-default",
        ),
    ],
)
async def test_unreviewed_successful_control_blocks_before_gate(
    monkeypatch,
    extra_form_html: str,
) -> None:
    handle = _ExactSubmitHandle()
    session, expectation, _page = await _configured_transport(
        handle,
        monkeypatch,
        extra_form_html=extra_form_html,
    )

    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
        await session.commit_final_action(expectation)

    assert exc_info.value.reason_code is ReasonCode.FORM_CHANGED
    assert session._final_request_gate is None
    assert handle.route is None


@pytest.mark.asyncio
async def test_atomic_commit_retains_exact_element_and_ignores_locator_retarget(
    monkeypatch,
) -> None:
    resolver = _Resolver({"fixture.wd5.myworkdayjobs.com": "8.8.8.8"})
    page = _DecoySubmitPage()
    session = PlaywrightWorkdayCandidateSession(settings=object())
    session._page = page
    session._network_guard = WorkdayNetworkGuard(JOB_URL, resolver=resolver)
    page.real_handle.session = session
    page.decoy_handle.session = session
    expectation, proof = _commit_expectation(page.html)
    _prime_transport_attachment(session)

    async def _verified_attachment(**_kwargs):
        return proof

    monkeypatch.setattr(session, "verify_resume_attachment", _verified_attachment)

    receipt = await session.commit_final_action(expectation)

    assert receipt.target_digest == expectation.request_contract.digest
    assert receipt.payload_sha256 == _canonical_form_data_payload_sha256(
        content_type=DEFAULT_CONTENT_TYPE,
        body=DEFAULT_REQUEST_BODY,
        expected_cv_sha256=CV_SHA256,
    )
    assert receipt.request_digest != expectation.request_contract.digest
    assert page.real_handle.clicks == 1
    assert page.decoy_handle.clicks == 0
    assert page.real_submit.handle is page.decoy_handle
    assert page.real_handle.route is not None
    assert page.real_handle.route.action == "continue"
    assert page.global_submit_queries == 0

    duplicate_route = _Route()
    await session._guard_request(
        duplicate_route,
        _Request(
            f"{JOB_URL}/apply",
            navigation=True,
            method="POST",
            content_type=DEFAULT_CONTENT_TYPE,
            body=DEFAULT_REQUEST_BODY,
            frame=page.main_frame,
        ),
    )
    assert duplicate_route.action == "abort"

    confirmation_get = _Route()
    await session._guard_request(
        confirmation_get,
        _Request(JOB_URL, navigation=True, method="GET", frame=page.main_frame),
    )
    assert confirmation_get.action == "continue"


@pytest.mark.asyncio
async def test_atomic_commit_rejects_mutation_inside_retained_handle_before_send(
    monkeypatch,
) -> None:
    resolver = _Resolver({"fixture.wd5.myworkdayjobs.com": "8.8.8.8"})
    handle = _ExactSubmitHandle(mutate_before_atomic_submit=True)
    page = _DecoySubmitPage(handle)
    session = PlaywrightWorkdayCandidateSession(settings=object())
    session._page = page
    session._network_guard = WorkdayNetworkGuard(JOB_URL, resolver=resolver)
    handle.session = session
    expectation, proof = _commit_expectation(page.html)
    _prime_transport_attachment(session)

    async def _verified_attachment(**_kwargs):
        return proof

    monkeypatch.setattr(session, "verify_resume_attachment", _verified_attachment)

    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
        await session.commit_final_action(expectation)

    assert exc_info.value.reason_code is ReasonCode.FORM_CHANGED
    assert handle.clicks == 0
    assert handle.route is None
    assert session._final_request_gate is not None
    assert session._final_request_gate.rejected is True
    assert session._final_request_gate.possibly_sent is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handle_kwargs",
    [
        pytest.param({"captured_disabled": True}, id="disabled-property"),
        pytest.param({"captured_aria_disabled": True}, id="aria-disabled-property"),
        pytest.param({"captured_inert": True}, id="inert-ancestor"),
    ],
)
async def test_python_boundary_rejects_non_actionable_captured_final_control(
    monkeypatch,
    handle_kwargs,
) -> None:
    handle = _ExactSubmitHandle(**handle_kwargs)
    session, expectation, _page = await _configured_transport(handle, monkeypatch)

    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
        await session.commit_final_action(expectation)

    assert exc_info.value.reason_code is ReasonCode.FORM_CHANGED
    assert handle.clicks == 0
    assert handle.route is None
    assert session._final_request_gate is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handle_kwargs",
    [
        pytest.param({"atomic_disabled": True}, id="disabled-before-native-submit"),
        pytest.param(
            {"atomic_aria_disabled": True},
            id="aria-disabled-before-native-submit",
        ),
        pytest.param({"atomic_inert": True}, id="inert-before-native-submit"),
    ],
)
async def test_atomic_gate_rejects_non_actionable_retained_final_control(
    monkeypatch,
    handle_kwargs,
) -> None:
    handle = _ExactSubmitHandle(**handle_kwargs)
    session, expectation, _page = await _configured_transport(handle, monkeypatch)

    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
        await session.commit_final_action(expectation)

    assert exc_info.value.reason_code is ReasonCode.FORM_CHANGED
    assert handle.clicks == 0
    assert handle.route is None
    assert session._final_request_gate is not None
    assert session._final_request_gate.rejected is True
    assert session._final_request_gate.possibly_sent is False


@pytest.mark.asyncio
async def test_atomic_commit_rejects_changed_answer_before_native_submit(
    monkeypatch,
) -> None:
    _content_type, changed = _multipart_body(answer="changed")
    handle = _ExactSubmitHandle(atomic_body=changed)
    session, expectation, _page = await _configured_transport(handle, monkeypatch)

    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
        await session.commit_final_action(expectation)

    assert exc_info.value.reason_code is ReasonCode.FORM_CHANGED
    assert handle.route is None
    assert session._final_request_gate is not None
    assert session._final_request_gate.possibly_sent is False


@pytest.mark.asyncio
async def test_atomic_commit_rejects_changed_hidden_successful_control(
    monkeypatch,
) -> None:
    _content_type, changed = _multipart_body(hidden_value="changed")
    handle = _ExactSubmitHandle(atomic_body=changed)
    session, expectation, _page = await _configured_transport(handle, monkeypatch)

    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
        await session.commit_final_action(expectation)

    assert exc_info.value.reason_code is ReasonCode.FORM_CHANGED
    assert handle.route is None
    assert session._final_request_gate is not None
    assert session._final_request_gate.possibly_sent is False


@pytest.mark.asyncio
async def test_atomic_commit_rejects_wrong_file_bytes_with_same_filename(
    monkeypatch,
) -> None:
    _content_type, changed = _multipart_body(
        filename="resume-sanitized.pdf",
        file_bytes=b"%PDF-1.4\nwrong bytes same filename\n%%EOF\n",
    )
    handle = _ExactSubmitHandle(atomic_body=changed)
    session, expectation, _page = await _configured_transport(handle, monkeypatch)

    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
        await session.commit_final_action(expectation)

    assert exc_info.value.reason_code is ReasonCode.FORM_CHANGED
    assert handle.route is None
    assert session._final_request_gate is not None
    assert session._final_request_gate.possibly_sent is False


@pytest.mark.asyncio
async def test_atomic_commit_rejects_dom_drift_inside_transport_before_click(
    monkeypatch,
) -> None:
    resolver = _Resolver({"fixture.wd5.myworkdayjobs.com": "8.8.8.8"})
    page = _DecoySubmitPage()
    session = PlaywrightWorkdayCandidateSession(settings=object())
    session._page = page
    session._network_guard = WorkdayNetworkGuard(JOB_URL, resolver=resolver)
    page.real_handle.session = session
    expectation, proof = _commit_expectation(page.html)
    _prime_transport_attachment(session)
    page.html = page.html.replace("Submit</button>", "Changed</button>", 1)

    async def _verified_attachment(**_kwargs):
        return proof

    monkeypatch.setattr(session, "verify_resume_attachment", _verified_attachment)

    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
        await session.commit_final_action(expectation)

    assert exc_info.value.reason_code is ReasonCode.FORM_CHANGED
    assert page.real_handle.clicks == 0
    assert session._final_request_gate is None


@pytest.mark.asyncio
async def test_atomic_commit_rechecks_exact_cv_receipt_after_retaining_handle(
    monkeypatch,
) -> None:
    resolver = _Resolver({"fixture.wd5.myworkdayjobs.com": "8.8.8.8"})
    page = _DecoySubmitPage()
    session = PlaywrightWorkdayCandidateSession(settings=object())
    session._page = page
    session._network_guard = WorkdayNetworkGuard(JOB_URL, resolver=resolver)
    page.real_handle.session = session
    expectation, proof = _commit_expectation(page.html)
    _prime_transport_attachment(session)
    calls = 0

    async def _changing_attachment(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return proof
        return WorkdayAttachmentProof(
            cv_id=proof.cv_id,
            cv_sha256=proof.cv_sha256,
            upload_complete=False,
        )

    monkeypatch.setattr(session, "verify_resume_attachment", _changing_attachment)

    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
        await session.commit_final_action(expectation)

    assert exc_info.value.reason_code is ReasonCode.ATTACHMENT_UNVERIFIED
    assert page.real_handle.clicks == 0
    assert session._final_request_gate is None


@pytest.mark.asyncio
async def test_same_url_wrong_payload_is_aborted_before_network_send(monkeypatch) -> None:
    resolver = _Resolver({"fixture.wd5.myworkdayjobs.com": "8.8.8.8"})
    _content_type, wrong_body = _multipart_body(answer="other-site")
    handle = _ExactSubmitHandle(request_body=wrong_body)
    page = _DecoySubmitPage(handle)
    session = PlaywrightWorkdayCandidateSession(settings=object())
    session._page = page
    session._network_guard = WorkdayNetworkGuard(JOB_URL, resolver=resolver)
    handle.session = session
    expectation, proof = _commit_expectation(page.html)
    _prime_transport_attachment(session)

    async def _verified_attachment(**_kwargs):
        return proof

    monkeypatch.setattr(session, "verify_resume_attachment", _verified_attachment)

    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
        await session.commit_final_action(expectation)

    assert exc_info.value.reason_code is ReasonCode.FORM_CHANGED
    assert handle.route is not None
    assert handle.route.action == "abort"
    assert session._final_request_gate is not None
    assert session._final_request_gate.possibly_sent is False


@pytest.mark.asyncio
async def test_non_navigation_fetch_post_cannot_consume_final_gate(monkeypatch) -> None:
    handle = _ExactSubmitHandle(
        request_navigation=False,
        request_resource_type="fetch",
    )
    session, expectation, _page = await _configured_transport(handle, monkeypatch)

    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
        await session.commit_final_action(expectation)

    assert exc_info.value.reason_code is ReasonCode.FORM_CHANGED
    assert handle.route is not None
    assert handle.route.action == "abort"
    assert session._final_request_gate is not None
    assert session._final_request_gate.possibly_sent is False


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_type", ["fetch", "xhr", "script"])
async def test_non_document_main_frame_post_cannot_consume_final_gate(
    monkeypatch,
    resource_type: str,
) -> None:
    handle = _ExactSubmitHandle(
        request_navigation=True,
        request_resource_type=resource_type,
    )
    session, expectation, _page = await _configured_transport(handle, monkeypatch)

    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
        await session.commit_final_action(expectation)

    assert exc_info.value.reason_code is ReasonCode.FORM_CHANGED
    assert handle.route is not None
    assert handle.route.action == "abort"
    assert session._final_request_gate is not None
    assert session._final_request_gate.rejected is True
    assert session._final_request_gate.possibly_sent is False


@pytest.mark.asyncio
async def test_iframe_navigation_post_cannot_consume_final_gate(monkeypatch) -> None:
    handle = _ExactSubmitHandle(use_iframe=True)
    session, expectation, _page = await _configured_transport(handle, monkeypatch)

    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
        await session.commit_final_action(expectation)

    assert exc_info.value.reason_code is ReasonCode.FORM_CHANGED
    assert handle.route is not None
    assert handle.route.action == "abort"
    assert session._final_request_gate is not None
    assert session._final_request_gate.possibly_sent is False


@pytest.mark.asyncio
async def test_background_get_does_not_consume_exact_post_gate(monkeypatch) -> None:
    resolver = _Resolver({"fixture.wd5.myworkdayjobs.com": "8.8.8.8"})
    handle = _ExactSubmitHandle(background_get=True)
    page = _DecoySubmitPage(handle)
    session = PlaywrightWorkdayCandidateSession(settings=object())
    session._page = page
    session._network_guard = WorkdayNetworkGuard(JOB_URL, resolver=resolver)
    handle.session = session
    expectation, proof = _commit_expectation(page.html)
    _prime_transport_attachment(session)

    async def _verified_attachment(**_kwargs):
        return proof

    monkeypatch.setattr(session, "verify_resume_attachment", _verified_attachment)

    receipt = await session.commit_final_action(expectation)

    assert receipt.target_digest == expectation.request_contract.digest
    assert handle.background_route is not None
    assert handle.background_route.action == "continue"
    assert handle.route is not None
    assert handle.route.action == "continue"
    assert session._final_request_gate is not None
    assert session._final_request_gate.rejected is False
    assert session._final_request_gate.possibly_sent is True


@pytest.mark.asyncio
async def test_error_after_validated_request_is_explicitly_ambiguous(monkeypatch) -> None:
    resolver = _Resolver({"fixture.wd5.myworkdayjobs.com": "8.8.8.8"})
    handle = _ExactSubmitHandle(post_send_error=True)
    page = _DecoySubmitPage(handle)
    session = PlaywrightWorkdayCandidateSession(settings=object())
    session._page = page
    session._network_guard = WorkdayNetworkGuard(JOB_URL, resolver=resolver)
    handle.session = session
    expectation, proof = _commit_expectation(page.html)
    _prime_transport_attachment(session)

    async def _verified_attachment(**_kwargs):
        return proof

    monkeypatch.setattr(session, "verify_resume_attachment", _verified_attachment)

    with pytest.raises(WorkdayFinalActionAmbiguousError):
        await session.commit_final_action(expectation)

    assert handle.route is not None
    assert handle.route.action == "continue"
    assert session._final_request_gate is not None
    assert session._final_request_gate.possibly_sent is True


@pytest.mark.asyncio
async def test_blocked_error_after_validated_request_is_explicitly_ambiguous(
    monkeypatch,
) -> None:
    resolver = _Resolver({"fixture.wd5.myworkdayjobs.com": "8.8.8.8"})
    handle = _ExactSubmitHandle(post_send_blocked_error=True)
    page = _DecoySubmitPage(handle)
    session = PlaywrightWorkdayCandidateSession(settings=object())
    session._page = page
    session._network_guard = WorkdayNetworkGuard(JOB_URL, resolver=resolver)
    handle.session = session
    expectation, proof = _commit_expectation(page.html)
    _prime_transport_attachment(session)

    async def _verified_attachment(**_kwargs):
        return proof

    monkeypatch.setattr(session, "verify_resume_attachment", _verified_attachment)

    with pytest.raises(WorkdayFinalActionAmbiguousError):
        await session.commit_final_action(expectation)

    assert handle.route is not None
    assert handle.route.action == "continue"
    assert session._final_request_gate is not None
    assert session._final_request_gate.possibly_sent is True
