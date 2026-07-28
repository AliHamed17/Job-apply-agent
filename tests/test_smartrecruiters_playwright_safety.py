"""One-shot request, payload, network, and final-call browser invariants."""

from __future__ import annotations

import hashlib
import socket
from dataclasses import replace
from pathlib import Path

import pytest

from core.submission_domain import ReasonCode
from submitters.smartrecruiters_playwright import (
    _ATOMIC_FINAL_ACTION_JS,
    _CAPTURE_FINAL_ACTION_JS,
    _DISCLOSURE_SELECTOR,
    _FIELD_WRAPPER_SELECTOR,
    _FINAL_BUTTON_SELECTOR,
    _FORM_DATA_COMMITMENT_VERSION,
    _POST_ACTION_SETTLE_TIMEOUT_MS,
    PlaywrightSmartRecruitersCandidateSession,
    SmartRecruitersNetworkGuard,
    _canonical_form_data_payload_sha256,
    _OneShotCandidatePostGate,
)
from submitters.smartrecruiters_v1 import (
    SMARTRECRUITERS_CONFIRMATION_SELECTOR,
    SMARTRECRUITERS_FORM_SELECTOR,
    SmartRecruitersAdapterBlockedError,
    SmartRecruitersFinalActionAmbiguousError,
    SmartRecruitersFinalActionProof,
)

FIXTURES = Path(__file__).parent / "fixtures" / "smartrecruiters_v1"
JOB_URL = "https://jobs.smartrecruiters.com/FixtureCo/123456789-sanitized-role"
POSTING_UUID = "11111111-2222-4333-8444-555555555555"
ACTION_URL = (
    f"https://jobs.smartrecruiters.com/candidate-experience/postings/{POSTING_UUID}/applications"
)
CV_BYTES = b"%PDF-1.4\nsanitized smartrecruiters fixture\n%%EOF\n"
CV_SHA256 = hashlib.sha256(CV_BYTES).hexdigest()
BOUNDARY = "----SmartRecruitersFixtureBoundary7MA4YWxk"


class _Resolver:
    def __init__(self, address: str) -> None:
        self.address = address
        self.calls: list[str] = []

    def __call__(self, host, _port, _family, _kind):
        self.calls.append(host)
        family = socket.AF_INET6 if ":" in self.address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (self.address, 443))]


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "192.0.2.1",
        "::1",
        "fc00::1",
        "fe80::1",
    ],
)
def test_candidate_origin_must_resolve_only_to_public_global_addresses(address) -> None:
    guard = SmartRecruitersNetworkGuard(JOB_URL, resolver=_Resolver(address))

    with pytest.raises(SmartRecruitersAdapterBlockedError) as exc_info:
        guard.require_allowed_url(JOB_URL, main_frame=True)

    assert exc_info.value.reason_code is ReasonCode.RUNTIME_NOT_READY


def test_exact_host_and_numeric_candidate_remain_bound_across_navigation() -> None:
    resolver = _Resolver("8.8.8.8")
    guard = SmartRecruitersNetworkGuard(JOB_URL, resolver=resolver)

    guard.require_allowed_url(JOB_URL, main_frame=True)
    guard.require_allowed_url(f"{JOB_URL}/apply", main_frame=True)
    guard.require_allowed_url(
        ACTION_URL,
        main_frame=True,
        allow_exact_final_action=ACTION_URL,
    )
    with pytest.raises(SmartRecruitersAdapterBlockedError):
        guard.require_allowed_url(
            f"{ACTION_URL}/other",
            main_frame=True,
            allow_exact_final_action=ACTION_URL,
        )
    with pytest.raises(SmartRecruitersAdapterBlockedError):
        guard.require_allowed_url(
            "https://jobs.smartrecruiters.com/FixtureCo/987654321-other-role",
            main_frame=True,
        )
    with pytest.raises(SmartRecruitersAdapterBlockedError):
        guard.require_allowed_url("https://cdn.smartrecruiters.com/asset.js")
    assert resolver.calls == ["jobs.smartrecruiters.com"]


def _multipart(
    *,
    first_name: str = "Fixture",
    filename: str = "resume-sanitized.pdf",
    cv_bytes: bytes = CV_BYTES,
) -> tuple[str, bytes]:
    chunks = [
        f"--{BOUNDARY}\r\n".encode(),
        b'Content-Disposition: form-data; name="candidate.firstName"\r\n\r\n',
        first_name.encode(),
        b"\r\n",
        f"--{BOUNDARY}\r\n".encode(),
        (
            f'Content-Disposition: form-data; name="candidate.resume"; filename="{filename}"\r\n'
        ).encode(),
        b"Content-Type: application/pdf\r\n\r\n",
        cv_bytes,
        b"\r\n",
        f"--{BOUNDARY}--\r\n".encode(),
    ]
    return f"multipart/form-data; boundary={BOUNDARY}", b"".join(chunks)


def test_payload_commitment_binds_every_value_and_exact_cv_bytes() -> None:
    content_type, body = _multipart()
    digest = _canonical_form_data_payload_sha256(
        content_type=content_type,
        body=body,
        expected_cv_sha256=CV_SHA256,
    )

    assert digest is not None
    assert digest == _canonical_form_data_payload_sha256(
        content_type=content_type,
        body=body,
        expected_cv_sha256=CV_SHA256,
    )
    assert digest != _canonical_form_data_payload_sha256(
        content_type=content_type,
        body=_multipart(first_name="Changed")[1],
        expected_cv_sha256=CV_SHA256,
    )
    assert (
        _canonical_form_data_payload_sha256(
            content_type=content_type,
            body=_multipart(cv_bytes=b"%PDF-1.4\nother\n%%EOF\n")[1],
            expected_cv_sha256=CV_SHA256,
        )
        is None
    )
    assert (
        _canonical_form_data_payload_sha256(
            content_type="application/json",
            body=b"{}",
            expected_cv_sha256=CV_SHA256,
        )
        is None
    )


class _Frame:
    def __init__(self, page) -> None:
        self.page = page


class _Page:
    def __init__(self) -> None:
        self.main_frame = _Frame(self)


class _CapturingGuard:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool, str | None]] = []

    def require_allowed_url(
        self,
        url: str,
        *,
        main_frame: bool,
        allow_exact_final_action: str | None = None,
    ) -> None:
        self.calls.append((url, main_frame, allow_exact_final_action))


class _Request:
    def __init__(
        self,
        page,
        *,
        url: str = ACTION_URL,
        method: str = "POST",
        navigation: bool = True,
        resource_type: str = "document",
        body: bytes | None = None,
        content_type: str = "",
    ) -> None:
        self.url = url
        self.method = method
        self.frame = page.main_frame
        self.resource_type = resource_type
        self.post_data_buffer = body
        self.headers = {"content-type": content_type} if content_type else {}
        self._navigation = navigation

    def is_navigation_request(self) -> bool:
        return self._navigation


def _gate_and_payload():
    page = _Page()
    content_type, body = _multipart()
    digest = _canonical_form_data_payload_sha256(
        content_type=content_type,
        body=body,
        expected_cv_sha256=CV_SHA256,
    )
    assert digest is not None
    gate = _OneShotCandidatePostGate(
        page=page,
        exact_url=ACTION_URL,
        expected_payload_sha256=digest,
        selected_cv_hash=CV_SHA256,
    )
    return page, gate, content_type, body


def test_gate_ignores_reads_then_releases_one_exact_main_frame_native_post() -> None:
    page, gate, content_type, body = _gate_and_payload()
    assert gate.evaluate(_Request(page, method="GET", body=None, content_type="")) is None
    assert gate.completed.is_set() is False

    assert gate.evaluate(_Request(page, body=body, content_type=content_type)) is True
    assert gate.possibly_sent is True
    assert gate.completed.is_set() is True
    assert gate.evaluate(_Request(page, body=body, content_type=content_type)) is False


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"url": f"{ACTION_URL}/other"},
        {"method": "PUT"},
        {"navigation": False},
        {"resource_type": "xhr"},
        {"body": b"malformed"},
    ],
)
def test_gate_rejects_wrong_target_shape_or_payload_before_send(request_kwargs) -> None:
    page, gate, content_type, body = _gate_and_payload()
    kwargs = {
        "body": body,
        "content_type": content_type,
        **request_kwargs,
    }

    assert gate.evaluate(_Request(page, **kwargs)) is False
    assert gate.rejected is True
    assert gate.possibly_sent is False


@pytest.mark.asyncio
async def test_current_url_allows_exact_action_only_after_verified_post() -> None:
    page, gate, content_type, body = _gate_and_payload()
    page.url = ACTION_URL
    guard = _CapturingGuard()
    session = PlaywrightSmartRecruitersCandidateSession()
    session._page = page
    session._guard = guard
    session._gate = gate

    await session._assert_current_url()
    assert (
        gate.evaluate(
            _Request(
                page,
                body=body,
                content_type=content_type,
            )
        )
        is True
    )
    await session._assert_current_url()

    assert guard.calls == [
        (ACTION_URL, True, None),
        (ACTION_URL, True, ACTION_URL),
    ]


@pytest.mark.asyncio
async def test_real_guard_allows_only_exact_action_after_verified_post() -> None:
    page, gate, content_type, body = _gate_and_payload()
    page.url = ACTION_URL
    session = PlaywrightSmartRecruitersCandidateSession()
    session._page = page
    session._guard = SmartRecruitersNetworkGuard(
        JOB_URL,
        resolver=_Resolver("8.8.8.8"),
    )
    session._gate = gate

    with pytest.raises(SmartRecruitersAdapterBlockedError) as before_send:
        await session._assert_current_url()
    assert before_send.value.reason_code is ReasonCode.FORM_CHANGED

    assert (
        gate.evaluate(
            _Request(
                page,
                body=body,
                content_type=content_type,
            )
        )
        is True
    )
    await session._assert_current_url()

    for rejected_url in (
        f"{ACTION_URL}/other",
        "https://jobs.smartrecruiters.com/FixtureCo/987654321-other-role",
    ):
        page.url = rejected_url
        with pytest.raises(SmartRecruitersAdapterBlockedError) as after_send:
            await session._assert_current_url()
        assert after_send.value.reason_code is ReasonCode.FORM_CHANGED


def test_final_call_source_has_no_async_gap_and_checks_all_ancestors() -> None:
    normalized = " ".join(_ATOMIC_FINAL_ACTION_JS.split())

    assert "await " not in normalized
    assert ".requestSubmit(button)" in normalized
    assert ".click(" not in normalized
    assert "current = current.parentElement" in normalized
    assert 'style.pointerEvents === "none"' in normalized
    assert 'style.contentVisibility === "hidden"' in normalized
    assert "retained.observerState.mutations === 0" in normalized
    assert "document.querySelectorAll(expected.confirmationSelector).length === 0" in normalized
    assert "crypto.subtle" in _CAPTURE_FINAL_ACTION_JS
    assert _FORM_DATA_COMMITMENT_VERSION in "smartrecruiters-formdata-v1"


class _FinalHandle:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls = 0

    async def evaluate(self, _script, _expected):
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class _ObservedPostThenFalseHandle:
    def __init__(
        self,
        *,
        session: PlaywrightSmartRecruitersCandidateSession,
        page: _Page,
        body: bytes,
        content_type: str,
    ) -> None:
        self.session = session
        self.page = page
        self.body = body
        self.content_type = content_type

    async def evaluate(self, _script, _expected):
        gate = self.session._gate
        assert gate is not None
        assert (
            gate.evaluate(
                _Request(
                    self.page,
                    body=self.body,
                    content_type=self.content_type,
                )
            )
            is True
        )
        return {"released": False}


class _ObservedPostThenTrueHandle(_ObservedPostThenFalseHandle):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.calls = 0

    async def evaluate(self, _script, _expected):
        self.calls += 1
        gate = self.session._gate
        assert gate is not None
        assert (
            gate.evaluate(
                _Request(
                    self.page,
                    body=self.body,
                    content_type=self.content_type,
                )
            )
            is True
        )
        return {"released": True}


class _SettlementLocator:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def wait_for(self, *, state: str, timeout: int):
        self.calls.append((state, timeout))
        if self.error is not None:
            raise self.error


class _SettlementPage(_Page):
    def __init__(self, error: BaseException | None = None) -> None:
        super().__init__()
        self.settlement = _SettlementLocator(error)
        self.locator_selectors: list[str] = []

    def locator(self, selector: str):
        self.locator_selectors.append(selector)
        return self.settlement


class _RejectedPostThenFalseHandle:
    def __init__(
        self,
        *,
        session: PlaywrightSmartRecruitersCandidateSession,
        page: _Page,
    ) -> None:
        self.session = session
        self.page = page

    async def evaluate(self, _script, _expected):
        gate = self.session._gate
        assert gate is not None
        assert (
            gate.evaluate(
                _Request(
                    self.page,
                    url=f"{ACTION_URL}/wrong",
                    body=b"blocked",
                    content_type="multipart/form-data; boundary=blocked",
                )
            )
            is False
        )
        return {"released": False}


def _transport_proof() -> SmartRecruitersFinalActionProof:
    return SmartRecruitersFinalActionProof(
        identity_sha256="1" * 64,
        action_url_sha256="2" * 64,
        form_fingerprint="3" * 64,
        method="POST",
        encoding="multipart/form-data",
        submitter_sha256="4" * 64,
        actionability_sha256="5" * 64,
        disclosures_sha256="6" * 64,
        resume_control_sha256="7" * 64,
        attached_cv_sha256=CV_SHA256,
        payload_commitment_sha256="8" * 64,
        user_field_count=1,
        disclosure_count=0,
        precommit_mutation_count=0,
    )


@pytest.mark.asyncio
async def test_native_primitive_invoked_without_observed_request_is_ambiguous(
    monkeypatch,
) -> None:
    import submitters.smartrecruiters_playwright as transport

    monkeypatch.setattr(transport, "_ACTION_TIMEOUT_MS", 1)
    page = _Page()
    handle = _FinalHandle({"released": True})
    proof = _transport_proof()
    session = PlaywrightSmartRecruitersCandidateSession()
    session._page = page
    session._guard = object()
    session._prepared_handle = handle
    session._prepared_proof = proof
    session._prepared_expected = {"action": ACTION_URL}

    with pytest.raises(SmartRecruitersFinalActionAmbiguousError):
        await session.click_final_action(proof)

    assert handle.calls == 1
    assert session._gate is not None
    assert session._gate.possibly_sent is False


@pytest.mark.asyncio
async def test_atomic_recheck_rejection_before_primitive_is_definitive() -> None:
    page = _Page()
    handle = _FinalHandle({"released": False})
    proof = _transport_proof()
    session = PlaywrightSmartRecruitersCandidateSession()
    session._page = page
    session._guard = object()
    session._prepared_handle = handle
    session._prepared_proof = proof
    session._prepared_expected = {"action": ACTION_URL}

    with pytest.raises(SmartRecruitersAdapterBlockedError) as exc_info:
        await session.click_final_action(proof)

    assert exc_info.value.reason_code is ReasonCode.FORM_CHANGED
    assert session._gate is not None
    assert session._gate.possibly_sent is False


@pytest.mark.asyncio
async def test_script_false_is_ambiguous_if_exact_post_was_already_observed() -> None:
    page, _unused_gate, content_type, body = _gate_and_payload()
    payload_digest = _canonical_form_data_payload_sha256(
        content_type=content_type,
        body=body,
        expected_cv_sha256=CV_SHA256,
    )
    assert payload_digest is not None
    proof = replace(
        _transport_proof(),
        payload_commitment_sha256=payload_digest,
    )
    session = PlaywrightSmartRecruitersCandidateSession()
    session._page = page
    session._guard = object()
    session._prepared_handle = _ObservedPostThenFalseHandle(
        session=session,
        page=page,
        body=body,
        content_type=content_type,
    )
    session._prepared_proof = proof
    session._prepared_expected = {"action": ACTION_URL}

    with pytest.raises(SmartRecruitersFinalActionAmbiguousError):
        await session.click_final_action(proof)

    assert session._gate is not None
    assert session._gate.possibly_sent is True


@pytest.mark.asyncio
async def test_exact_post_waits_for_visible_confirmation_before_returning() -> None:
    page = _SettlementPage()
    content_type, body = _multipart()
    payload_digest = _canonical_form_data_payload_sha256(
        content_type=content_type,
        body=body,
        expected_cv_sha256=CV_SHA256,
    )
    assert payload_digest is not None
    proof = replace(
        _transport_proof(),
        payload_commitment_sha256=payload_digest,
    )
    session = PlaywrightSmartRecruitersCandidateSession()
    session._page = page
    session._guard = object()
    handle = _ObservedPostThenTrueHandle(
        session=session,
        page=page,
        body=body,
        content_type=content_type,
    )
    session._prepared_handle = handle
    session._prepared_proof = proof
    session._prepared_expected = {"action": ACTION_URL}

    await session.click_final_action(proof)

    assert handle.calls == 1
    assert session._gate is not None
    assert session._gate.possibly_sent is True
    assert page.locator_selectors == [SMARTRECRUITERS_CONFIRMATION_SELECTOR]
    assert page.settlement.calls == [("visible", _POST_ACTION_SETTLE_TIMEOUT_MS)]


@pytest.mark.asyncio
async def test_confirmation_settlement_timeout_is_ambiguous_after_one_exact_post() -> None:
    page = _SettlementPage(TimeoutError("confirmation did not settle"))
    content_type, body = _multipart()
    payload_digest = _canonical_form_data_payload_sha256(
        content_type=content_type,
        body=body,
        expected_cv_sha256=CV_SHA256,
    )
    assert payload_digest is not None
    proof = replace(
        _transport_proof(),
        payload_commitment_sha256=payload_digest,
    )
    session = PlaywrightSmartRecruitersCandidateSession()
    session._page = page
    session._guard = object()
    handle = _ObservedPostThenTrueHandle(
        session=session,
        page=page,
        body=body,
        content_type=content_type,
    )
    session._prepared_handle = handle
    session._prepared_proof = proof
    session._prepared_expected = {"action": ACTION_URL}

    with pytest.raises(SmartRecruitersFinalActionAmbiguousError):
        await session.click_final_action(proof)

    assert handle.calls == 1
    assert session._gate is not None
    assert session._gate.possibly_sent is True
    assert page.settlement.calls == [("visible", _POST_ACTION_SETTLE_TIMEOUT_MS)]


@pytest.mark.asyncio
async def test_script_false_is_ambiguous_after_any_mutation_request_was_seen() -> None:
    page = _Page()
    proof = _transport_proof()
    session = PlaywrightSmartRecruitersCandidateSession()
    session._page = page
    session._guard = object()
    session._prepared_handle = _RejectedPostThenFalseHandle(
        session=session,
        page=page,
    )
    session._prepared_proof = proof
    session._prepared_expected = {"action": ACTION_URL}

    with pytest.raises(SmartRecruitersFinalActionAmbiguousError):
        await session.click_final_action(proof)

    assert session._gate is not None
    assert session._gate.rejected is True
    assert session._gate.possibly_sent is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        RuntimeError("synthetic context loss"),
        None,
        {},
        {"released": "true"},
        {"released": True, "unexpected": True},
    ],
)
async def test_armed_final_evaluate_exception_or_malformed_result_is_ambiguous(
    result,
) -> None:
    page = _Page()
    handle = _FinalHandle(result)
    proof = _transport_proof()
    session = PlaywrightSmartRecruitersCandidateSession()
    session._page = page
    session._guard = object()
    session._prepared_handle = handle
    session._prepared_proof = proof
    session._prepared_expected = {"action": ACTION_URL}

    with pytest.raises(SmartRecruitersFinalActionAmbiguousError):
        await session.click_final_action(proof)

    assert handle.calls == 1
    assert session._gate is not None
    assert session._gate.possibly_sent is False


@pytest.mark.asyncio
async def test_has_proxy_ancestor_css_makes_final_control_non_actionable() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("Playwright package is not installed")
    html = (FIXTURES / "outer_has_proxy_guard.html").read_text(encoding="utf-8")
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch()
        except Exception:
            pytest.skip("Playwright browser executable is not installed")
        page = await browser.new_page()
        await page.set_content(html)
        await page.locator('input[type="file"]').set_input_files(
            {
                "name": "resume-sanitized.pdf",
                "mimeType": "application/pdf",
                "buffer": CV_BYTES,
            }
        )
        await page.locator("form").evaluate(
            """(form, values) => {
                const marker = document.createElement("div");
                marker.dataset.qa = "resume-upload-complete";
                marker.dataset.uploadId = "upload-fixture";
                marker.dataset.fileName = "resume-sanitized.pdf";
                marker.dataset.fileSha256 = values.cvSha256;
                form.append(marker);
                const button = form.querySelector(values.buttonSelector);
                globalThis.__jobAgentSmartRecruitersFinal = {
                    button,
                    form,
                    observer: {disconnect() {}},
                    observerState: {mutations: 0},
                    payloadDigest: "fixture-payload",
                    disclosureMaterial: "[]"
                };
            }""",
            {"cvSha256": CV_SHA256, "buttonSelector": _FINAL_BUTTON_SELECTOR},
        )
        expected = {
            "formSelector": SMARTRECRUITERS_FORM_SELECTOR,
            "buttonSelector": _FINAL_BUTTON_SELECTOR,
            "confirmationSelector": SMARTRECRUITERS_CONFIRMATION_SELECTOR,
            "wrapperSelector": _FIELD_WRAPPER_SELECTOR,
            "disclosureSelector": _DISCLOSURE_SELECTOR,
            "uploadMarkerSelector": (
                '[data-qa="resume-upload-complete"][data-upload-id]'
                "[data-file-name][data-file-sha256]"
            ),
            "action": ACTION_URL,
            "fields": [
                {
                    "fieldId": "resume",
                    "fieldType": "file",
                    "answer": {"kind": "file", "sha256": CV_SHA256},
                }
            ],
            "uploadId": "upload-fixture",
            "uploadName": "resume-sanitized.pdf",
            "cvSha256": CV_SHA256,
            "payloadDigest": "fixture-payload",
        }
        result = await page.locator(_FINAL_BUTTON_SELECTOR).evaluate(
            _ATOMIC_FINAL_ACTION_JS,
            expected,
        )
        await browser.close()

    assert result == {"released": False}
