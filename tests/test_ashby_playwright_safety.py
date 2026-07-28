from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from core.submission_domain import (
    VERIFIED_ATTACHMENT_EVIDENCE_REF,
    VERIFIED_ATTACHMENT_SENTINEL,
    AnswerDecisionV1,
    AnswerDisposition,
    AnswerProvenance,
    AttemptOutcome,
    FinalSubmitPermit,
    FormPlanV1,
    PreparedFinalActionV1,
    ReasonCode,
)
from submitters.ashby_identity import parse_ashby_candidate_url
from submitters.ashby_playwright import (
    _CAPTURE_AND_RELEASE_SCRIPT,
    AshbyNetworkGuard,
    PlaywrightAshbyCandidateSession,
    _multipart_payload_commitment,
    _OutboundGate,
)
from submitters.ashby_v1 import (
    ASHBY_FINAL_CONTROL_SELECTOR,
    ASHBY_FORM_SELECTOR,
    AshbyAdapterBlockedError,
    AshbyAttachmentProof,
    AshbyBrowserSnapshot,
    AshbyBrowserV1,
    AshbyFinalActionAmbiguousError,
    AshbyFinalActionReceipt,
    AshbyFinalCommitExpectation,
    AshbyFinalRequestContract,
    _canonical_digest,
    _PreparedState,
    _typed_pre_request_block,
    ashby_v1_answer_bindings,
    ashby_v1_final_request_contract,
    ashby_v1_form_fingerprint,
    observe_ashby_v1_fields,
)
from submitters.platforms import QualificationTier, adapter_for_platform

POSTING = "4f44b0a5-5482-4be6-bc11-3d89040b9fa1"
APPLICATION_URL = f"https://jobs.ashbyhq.com/fixture-board/{POSTING}/application"
FIXTURES = Path(__file__).parent / "fixtures" / "ashby_v1"
CV_BYTES = b"%PDF-1.4\n% sanitized Ashby browser fixture\n%%EOF\n"
CV_SHA256 = hashlib.sha256(CV_BYTES).hexdigest()
NOW = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _conditional_contract():
    html = _fixture("application_conditional.html")
    fields = observe_ashby_v1_fields(html)
    identity = parse_ashby_candidate_url(APPLICATION_URL).identity
    contract = ashby_v1_final_request_contract(
        html,
        APPLICATION_URL,
        identity,
        fields,
    )
    assert contract is not None
    decisions = (
        AnswerDecisionV1(
            field_id="preferred-team",
            disposition=AnswerDisposition.RESOLVED,
            provenance=AnswerProvenance.USER_CONFIRMED,
            value="ai",
            evidence_refs=("operator_confirmation:preferred-team",),
        ),
        AnswerDecisionV1(
            field_id="conditional-detail",
            disposition=AnswerDisposition.RESOLVED,
            provenance=AnswerProvenance.USER_CONFIRMED,
            value="Sanitized reviewed answer",
            evidence_refs=("operator_confirmation:conditional-detail",),
        ),
        AnswerDecisionV1(
            field_id="resume",
            disposition=AnswerDisposition.RESOLVED,
            provenance=AnswerProvenance.VERIFIED_ATTACHMENT,
            value=VERIFIED_ATTACHMENT_SENTINEL,
            evidence_refs=(VERIFIED_ATTACHMENT_EVIDENCE_REF,),
        ),
    )
    bindings = ashby_v1_answer_bindings(
        fields,
        decisions,
        selected_cv_hash=CV_SHA256,
    )
    expectation = AshbyFinalCommitExpectation(
        identity=identity,
        form_fingerprint=ashby_v1_form_fingerprint(fields, contract.digest),
        observed_fields=fields,
        answer_bindings=bindings,
        selected_cv_id="fixture-cv",
        selected_cv_hash=CV_SHA256,
        attachment_receipt_sha256="b" * 64,
        pre_action_digest=hashlib.sha256(html.encode("utf-8")).hexdigest(),
        request_contract=contract,
    )
    return html, fields, decisions, contract, expectation


@pytest.mark.parametrize(
    ("upload_complete", "receipt_sha256"),
    [
        (True, None),
        (True, "not-a-digest"),
        (False, "b" * 64),
    ],
)
def test_attachment_proof_rejects_contradictory_receipt_state(
    upload_complete: bool,
    receipt_sha256: str | None,
) -> None:
    with pytest.raises(ValueError, match="ASHBY_ATTACHMENT_PROOF_INVALID"):
        AshbyAttachmentProof(
            field_id="resume",
            control_name="resume",
            cv_id="fixture-cv",
            cv_sha256=CV_SHA256,
            upload_complete=upload_complete,
            receipt_sha256=receipt_sha256,
        )


def test_commit_expectation_binds_request_manifest_to_observed_fields() -> None:
    _html, fields, _decisions, contract, expectation = _conditional_contract()
    altered_manifest = (
        ("unreviewed-field", contract.field_controls[0][1], contract.field_controls[0][2]),
        *contract.field_controls[1:],
    )
    payload = {
        "identity": {
            "board": contract.identity.board_token,
            "posting": contract.identity.posting_id,
        },
        "target_url": contract.target_url,
        "method": contract.method,
        "enctype": contract.enctype,
        "field_controls": [
            {
                "field_id": field_id,
                "field_type": field_type.value,
                "names": names,
            }
            for field_id, field_type, names in altered_manifest
        ],
        "system_controls": contract.system_controls,
        "submit_control": contract.submit_control,
    }
    altered_contract = AshbyFinalRequestContract(
        identity=contract.identity,
        target_url=contract.target_url,
        method=contract.method,
        enctype=contract.enctype,
        field_controls=altered_manifest,
        system_controls=contract.system_controls,
        submit_control=contract.submit_control,
        digest=_canonical_digest(payload),
    )

    with pytest.raises(ValueError, match="ASHBY_FINAL_COMMIT_EXPECTATION_INVALID"):
        replace(
            expectation,
            request_contract=altered_contract,
            form_fingerprint=ashby_v1_form_fingerprint(fields, altered_contract.digest),
        )


def _multipart_part(
    boundary: str,
    name: str,
    value: bytes,
    *,
    filename: str | None = None,
    media_type: str | None = None,
) -> bytes:
    disposition = f'Content-Disposition: form-data; name="{name}"'
    if filename is not None:
        disposition += f'; filename="{filename}"'
    headers = [disposition]
    if media_type is not None:
        headers.append(f"Content-Type: {media_type}")
    return (
        f"--{boundary}\r\n".encode() + "\r\n".join(headers).encode() + b"\r\n\r\n" + value + b"\r\n"
    )


def _conditional_multipart_body(
    expectation: AshbyFinalCommitExpectation,
) -> tuple[bytes, str, str]:
    boundary = "----AshbyFixtureBoundary7MA4YWxk"
    body = b"".join(
        (
            _multipart_part(boundary, "postingId", b"redacted"),
            _multipart_part(boundary, "preferredTeam", b"ai"),
            _multipart_part(
                boundary,
                "conditionalDetail",
                b"Sanitized reviewed answer",
            ),
            _multipart_part(
                boundary,
                "resume",
                CV_BYTES,
                filename="resume-fixture.pdf",
                media_type="application/pdf",
            ),
            _multipart_part(boundary, "action", b"submit"),
            f"--{boundary}--\r\n".encode(),
        )
    )
    content_type = f"multipart/form-data; boundary={boundary}"
    payload_sha256 = _multipart_payload_commitment(
        body=body,
        content_type=content_type,
        expectation=expectation,
    )
    return body, content_type, payload_sha256


def test_exact_multipart_parser_binds_reviewed_answers_and_cv_bytes() -> None:
    _html, _fields, _decisions, _contract, expectation = _conditional_contract()
    body, content_type, commitment = _conditional_multipart_body(expectation)
    assert len(commitment) == 64

    tampered = body.replace(CV_BYTES, b"%PDF-1.4\nwrong CV\n%%EOF\n")
    with pytest.raises(Exception, match="ATTACHMENT_UNVERIFIED"):
        _multipart_payload_commitment(
            body=tampered,
            content_type=content_type,
            expectation=expectation,
        )


@pytest.mark.parametrize(
    "mutation",
    ["duplicate-name", "transfer-encoding", "truncated-closing-boundary"],
)
def test_multipart_parser_rejects_ambiguous_mime_shapes(mutation: str) -> None:
    _html, _fields, _decisions, _contract, expectation = _conditional_contract()
    body, content_type, _commitment = _conditional_multipart_body(expectation)
    if mutation == "duplicate-name":
        body = body.replace(
            b'Content-Disposition: form-data; name="postingId"',
            b'Content-Disposition: form-data; name="postingId"; name="other"',
            1,
        )
    elif mutation == "transfer-encoding":
        body = body.replace(
            b'Content-Disposition: form-data; name="postingId"\r\n',
            b'Content-Disposition: form-data; name="postingId"\r\n'
            b"Content-Transfer-Encoding: base64\r\n",
            1,
        )
    else:
        body = body.rsplit(b"--", 1)[0]

    with pytest.raises(AshbyAdapterBlockedError) as raised:
        _multipart_payload_commitment(
            body=body,
            content_type=content_type,
            expectation=expectation,
        )

    assert raised.value.reason_code is ReasonCode.FORM_CHANGED


class _Route:
    def __init__(self) -> None:
        self.aborted = False
        self.continued = False

    async def abort(self, _reason: str) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        self.continued = True


class _Request:
    def __init__(
        self,
        *,
        url: str,
        body: bytes,
        content_type: str,
        frame: object,
        navigation: bool = True,
        resource_type: str = "document",
        method: str = "POST",
    ) -> None:
        self.url = url
        self.post_data_buffer = body
        self.headers = {"content-type": content_type}
        self.frame = frame
        self.resource_type = resource_type
        self.method = method
        self._navigation = navigation

    def is_navigation_request(self) -> bool:
        return self._navigation

    async def all_headers(self) -> dict[str, str]:
        return self.headers


@pytest.mark.asyncio
async def test_gate_allows_one_exact_main_frame_document_payload() -> None:
    _html, _fields, _decisions, _contract, expectation = _conditional_contract()
    body, content_type, payload_sha256 = _conditional_multipart_body(expectation)
    main_frame = object()
    gate = _OutboundGate(
        expectation=expectation,
        expected_main_frame=main_frame,
        event=asyncio.Event(),
        expected_payload_sha256=payload_sha256,
    )
    session = PlaywrightAshbyCandidateSession(settings=object())  # type: ignore[arg-type]
    guard = AshbyNetworkGuard(APPLICATION_URL)
    guard._dns_verified = True
    session._guard = guard
    route = _Route()
    request = _Request(
        url=APPLICATION_URL,
        body=body,
        content_type=content_type,
        frame=main_frame,
    )

    await session._guard_outbound(
        route,
        request,
        method="POST",
        gate=gate,
    )

    assert route.continued is True
    assert route.aborted is False
    assert gate.request_may_have_left is True
    assert gate.receipt is not None
    assert gate.receipt.request_contract_digest == expectation.request_contract.digest
    assert gate.receipt.payload_sha256 == payload_sha256
    assert gate.event.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("navigation", [True, False])
async def test_gate_armed_non_post_request_is_aborted_as_contradictory(
    navigation: bool,
) -> None:
    _html, _fields, _decisions, _contract, expectation = _conditional_contract()
    body, content_type, payload_sha256 = _conditional_multipart_body(expectation)
    main_frame = object()
    gate = _OutboundGate(
        expectation=expectation,
        expected_main_frame=main_frame,
        event=asyncio.Event(),
        expected_payload_sha256=payload_sha256,
    )
    session = PlaywrightAshbyCandidateSession(settings=object())  # type: ignore[arg-type]
    guard = AshbyNetworkGuard(APPLICATION_URL)
    guard._dns_verified = True
    session._guard = guard
    session._gate = gate
    route = _Route()
    request = _Request(
        url=APPLICATION_URL,
        body=body,
        content_type=content_type,
        frame=main_frame,
        method="GET",
        navigation=navigation,
    )

    await session._guard_request(route, request)

    assert route.aborted is True
    assert route.continued is False
    assert gate.request_may_have_left is False
    assert gate.reason_code is ReasonCode.FORM_CHANGED
    assert gate.closed is True
    assert gate.event.is_set()


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("wrong-url", ReasonCode.FORM_CHANGED),
        ("xhr", ReasonCode.FORM_CHANGED),
        ("iframe", ReasonCode.FORM_CHANGED),
        ("not-navigation", ReasonCode.FORM_CHANGED),
        ("extra-field", ReasonCode.FORM_CHANGED),
        ("wrong-cv", ReasonCode.ATTACHMENT_UNVERIFIED),
    ],
)
@pytest.mark.asyncio
async def test_gate_aborts_every_non_exact_request_before_bytes_leave(
    mutation: str,
    expected_reason: ReasonCode,
) -> None:
    _html, _fields, _decisions, _contract, expectation = _conditional_contract()
    body, content_type, payload_sha256 = _conditional_multipart_body(expectation)
    main_frame = object()
    request_frame = main_frame
    request_url = APPLICATION_URL
    navigation = True
    resource_type = "document"
    if mutation == "wrong-url":
        request_url = f"https://jobs.ashbyhq.com/other-board/{POSTING}/application"
    elif mutation == "xhr":
        resource_type = "xhr"
    elif mutation == "iframe":
        request_frame = object()
    elif mutation == "not-navigation":
        navigation = False
    elif mutation == "extra-field":
        boundary = content_type.rsplit("=", 1)[1]
        body = body.replace(
            f"--{boundary}--\r\n".encode(),
            _multipart_part(boundary, "unreviewed", b"secret") + f"--{boundary}--\r\n".encode(),
        )
    elif mutation == "wrong-cv":
        body = body.replace(CV_BYTES, b"%PDF-1.4\nwrong CV\n%%EOF\n")

    gate = _OutboundGate(
        expectation=expectation,
        expected_main_frame=main_frame,
        event=asyncio.Event(),
        expected_payload_sha256=payload_sha256,
    )
    session = PlaywrightAshbyCandidateSession(settings=object())  # type: ignore[arg-type]
    guard = AshbyNetworkGuard(APPLICATION_URL)
    guard._dns_verified = True
    session._guard = guard
    route = _Route()
    request = _Request(
        url=request_url,
        body=body,
        content_type=content_type,
        frame=request_frame,
        navigation=navigation,
        resource_type=resource_type,
    )

    await session._guard_outbound(
        route,
        request,
        method="POST",
        gate=gate,
    )

    assert route.aborted is True
    assert route.continued is False
    assert gate.request_may_have_left is False
    assert gate.receipt is None
    assert gate.reason_code is expected_reason
    assert gate.closed is True
    assert gate.event.is_set()


def test_release_script_has_no_submitter_proxy_and_adjacent_final_action() -> None:
    assert "data-ashby-submit-proxy" in _CAPTURE_AND_RELEASE_SCRIPT
    assert 'document.querySelector("[data-ashby-submit-proxy]") !== null' in (
        _CAPTURE_AND_RELEASE_SCRIPT
    )
    assert "document.createElement" not in _CAPTURE_AND_RELEASE_SCRIPT
    assert ".appendChild(" not in _CAPTURE_AND_RELEASE_SCRIPT
    assert ".insertBefore(" not in _CAPTURE_AND_RELEASE_SCRIPT
    assert 'button.getAttribute("type")' in _CAPTURE_AND_RELEASE_SCRIPT
    assert (
        '["formaction", "formmethod", "formenctype", "formtarget", "formnovalidate"]'
        in _CAPTURE_AND_RELEASE_SCRIPT
    )

    final_probe = _CAPTURE_AND_RELEASE_SCRIPT.rindex("if (actionable())")
    primitive = _CAPTURE_AND_RELEASE_SCRIPT.rindex(
        "HTMLFormElement.prototype.requestSubmit.call(form, button)"
    )
    adjacent = _CAPTURE_AND_RELEASE_SCRIPT[final_probe:primitive]
    assert "await " not in adjacent
    assert "MutationObserver" not in adjacent
    assert "createElement" not in adjacent
    assert ".append(" not in adjacent
    assert ".before(" not in adjacent


@pytest.mark.asyncio
async def test_real_chromium_has_proxy_css_cannot_change_actionability() -> None:
    playwright = pytest.importorskip(
        "playwright.async_api",
        reason="real Chromium regression requires browser extra",
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
    try:
        async with playwright.async_playwright() as manager:
            try:
                browser = await manager.chromium.launch(headless=True)
            except playwright.Error as exc:
                pytest.skip(f"real Chromium executable unavailable: {exc}")
            try:
                page = await browser.new_page()
                await page.goto(f"http://127.0.0.1:{port}/")
                await page.set_content(
                    f"""
                    <!doctype html><html><head><style>
                    button:has([data-ashby-submit-proxy]) {{
                      pointer-events: none !important;
                    }}
                    </style></head><body>
                    <form data-ashby-application-form method="post"
                          enctype="multipart/form-data"
                          action="{APPLICATION_URL}">
                      <input type="hidden" name="postingId" value="redacted">
                      <div data-ashby-field data-field-id="resume"
                           data-ashby-rendered="true">
                        <label>Resume</label>
                        <input type="file" name="resume" required>
                      </div>
                      <button type="submit" name="action" value="submit"
                              data-ashby-submit-application>Submit</button>
                    </form></body></html>
                    """
                )
                await page.locator('input[type="file"]').set_input_files(
                    {
                        "name": "resume-fixture.pdf",
                        "mimeType": "application/pdf",
                        "buffer": CV_BYTES,
                    }
                )
                capture_name = "__ashby_fixture_capture"
                observed_payload: list[str] = []

                async def capture(_source, payload_sha256):
                    observed_payload.append(payload_sha256)
                    return True

                await page.expose_binding(capture_name, capture)
                await page.evaluate(
                    """
                    () => {
                      window.__requestSubmitCalls = 0;
                      window.__requestSubmitEntries = [];
                      HTMLFormElement.prototype.requestSubmit = function (button) {
                        window.__requestSubmitCalls += 1;
                        window.__requestSubmitEntries =
                          Array.from(new FormData(this, button).entries())
                            .map(([name, value]) => [
                              name,
                              typeof value === "string" ? value : value.name,
                            ]);
                      };
                    }
                    """
                )
                status = await page.locator("button[data-ashby-submit-application]").evaluate(
                    _CAPTURE_AND_RELEASE_SCRIPT,
                    {
                        "targetUrl": APPLICATION_URL,
                        "submitName": "action",
                        "submitValue": "submit",
                        "systemNames": ["postingId"],
                        "cvSha256": CV_SHA256,
                        "captureBinding": capture_name,
                        "fields": [
                            {
                                "fieldId": "resume",
                                "fieldType": "file",
                                "valueSha256": hashlib.sha256(
                                    f"file:{CV_SHA256}".encode()
                                ).hexdigest(),
                                "names": ["resume"],
                            }
                        ],
                    },
                )
                observed = await page.evaluate(
                    """
                    () => ({
                      calls: window.__requestSubmitCalls,
                      entries: window.__requestSubmitEntries,
                      proxies: document.querySelectorAll(
                        "[data-ashby-submit-proxy]"
                      ).length,
                      pointer: getComputedStyle(
                        document.querySelector(
                          "button[data-ashby-submit-application]"
                        )
                      ).pointerEvents,
                    })
                    """
                )
                assert status == "REQUEST_SUBMITTED"
                assert observed_payload and len(observed_payload[0]) == 64
                assert observed == {
                    "calls": 1,
                    "entries": [
                        ["postingId", "redacted"],
                        ["resume", "resume-fixture.pdf"],
                        ["action", "submit"],
                    ],
                    "proxies": 0,
                    "pointer": "auto",
                }
                await page.close()
            finally:
                await browser.close()
    finally:
        server.close()
        await server.wait_closed()


class _CountLocator:
    def __init__(self, count: int = 1) -> None:
        self._count = count

    async def count(self) -> int:
        return self._count


class _FinalReleaseButton(_CountLocator):
    def __init__(
        self,
        *,
        status: object,
        gate_signal: str | None = None,
        evaluate_error: bool = False,
    ) -> None:
        super().__init__()
        self.status = status
        self.gate_signal = gate_signal
        self.evaluate_error = evaluate_error
        self.page: _FinalReleasePage | None = None
        self.evaluate_calls = 0

    async def is_visible(self) -> bool:
        return True

    async def evaluate(self, script: str, expected: dict[str, object]) -> object:
        assert script == _CAPTURE_AND_RELEASE_SCRIPT
        assert self.page is not None
        assert self.page.session is not None
        self.evaluate_calls += 1
        capture = self.page.bindings[str(expected["captureBinding"])]
        assert await capture(None, "e" * 64) is True
        gate = self.page.session._gate
        assert gate is not None
        if self.gate_signal == "exact":
            gate.request_may_have_left = True
            gate.receipt = AshbyFinalActionReceipt(
                request_contract_digest=gate.expectation.request_contract.digest,
                payload_sha256="e" * 64,
            )
            gate.event.set()
        elif self.gate_signal == "blocked":
            gate.reason_code = ReasonCode.FORM_CHANGED
            gate.event.set()
        if self.evaluate_error:
            raise RuntimeError("synthetic evaluation context loss after invocation")
        return self.status


class _FinalReleasePage:
    def __init__(self, button: _FinalReleaseButton) -> None:
        self.button = button
        self.button.page = self
        self.form = _CountLocator()
        self.main_frame = object()
        self.bindings: dict[str, object] = {}
        self.session: _FinalReleaseSession | None = None

    def locator(self, selector: str):
        if selector == ASHBY_FINAL_CONTROL_SELECTOR:
            return self.button
        if selector == ASHBY_FORM_SELECTOR:
            return self.form
        raise AssertionError(f"unexpected selector: {selector}")

    async def expose_binding(self, name: str, callback) -> None:
        self.bindings[name] = callback


class _FinalReleaseSession(PlaywrightAshbyCandidateSession):
    def __init__(
        self,
        *,
        snapshot: AshbyBrowserSnapshot,
        proof: AshbyAttachmentProof,
        page: _FinalReleasePage,
    ) -> None:
        super().__init__(settings=object())  # type: ignore[arg-type]
        self.snapshot_value = snapshot
        self.proof = proof
        self._page = page
        self._attachment = proof
        page.session = self

    async def snapshot(self) -> AshbyBrowserSnapshot:
        return self.snapshot_value

    async def verify_resume_attachment(self, **_kwargs) -> AshbyAttachmentProof:
        return self.proof


def _final_release_session(
    *,
    status: object,
    gate_signal: str | None = None,
    evaluate_error: bool = False,
) -> tuple[_FinalReleaseSession, AshbyFinalCommitExpectation]:
    html, _fields, _decisions, _contract, expectation = _conditional_contract()
    proof = AshbyAttachmentProof(
        field_id="resume",
        control_name="resume",
        cv_id=expectation.selected_cv_id,
        cv_sha256=expectation.selected_cv_hash,
        upload_complete=True,
        receipt_sha256=expectation.attachment_receipt_sha256,
    )
    page = _FinalReleasePage(
        _FinalReleaseButton(
            status=status,
            gate_signal=gate_signal,
            evaluate_error=evaluate_error,
        )
    )
    return (
        _FinalReleaseSession(
            snapshot=AshbyBrowserSnapshot(html=html, url=APPLICATION_URL),
            proof=proof,
            page=page,
        ),
        expectation,
    )


@pytest.mark.parametrize(
    "status",
    [
        pytest.param(None, id="none"),
        pytest.param({"status": "REQUEST_SUBMITTED"}, id="malformed-object"),
        pytest.param("UNRECOGNIZED_STATUS", id="unrecognized-string"),
    ],
)
@pytest.mark.asyncio
async def test_non_contract_release_status_after_gate_is_ambiguous(status: object) -> None:
    session, expectation = _final_release_session(status=status)

    with pytest.raises(AshbyFinalActionAmbiguousError):
        await session.commit_final_action(expectation)

    assert session._clicked is True
    assert session._gate is not None
    assert session._gate.closed is True
    assert session._page.button.evaluate_calls == 1


@pytest.mark.parametrize("gate_signal", [None, "exact"])
@pytest.mark.asyncio
async def test_evaluation_exception_after_gate_is_always_ambiguous(
    gate_signal: str | None,
) -> None:
    session, expectation = _final_release_session(
        status="REQUEST_SUBMITTED",
        gate_signal=gate_signal,
        evaluate_error=True,
    )

    with pytest.raises(AshbyFinalActionAmbiguousError):
        await session.commit_final_action(expectation)

    assert session._clicked is True
    assert session._gate is not None
    assert session._gate.closed is True
    assert session._page.button.evaluate_calls == 1


@pytest.mark.asyncio
async def test_binding_setup_failure_after_one_use_claim_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, expectation = _final_release_session(status="REQUEST_SUBMITTED")

    async def fail_binding(_name: str, _callback) -> None:
        raise RuntimeError("synthetic binding setup failure")

    monkeypatch.setattr(session._page, "expose_binding", fail_binding)

    with pytest.raises(AshbyFinalActionAmbiguousError):
        await session.commit_final_action(expectation)

    assert session._clicked is True
    assert session._commit_claimed is True
    assert session._gate is not None
    assert session._gate.closed is True
    assert session._page.button.evaluate_calls == 0


@pytest.mark.parametrize(
    ("status", "expected_reason"),
    [
        ("FORM_CHANGED", ReasonCode.FORM_CHANGED),
        ("ATTACHMENT_UNVERIFIED", ReasonCode.ATTACHMENT_UNVERIFIED),
    ],
)
@pytest.mark.asyncio
async def test_only_exact_pre_request_statuses_remain_reviewable(
    status: str,
    expected_reason: ReasonCode,
) -> None:
    session, expectation = _final_release_session(status=status)

    with pytest.raises(AshbyAdapterBlockedError) as raised:
        await session.commit_final_action(expectation)

    assert raised.value.reason_code is expected_reason
    assert session._clicked is True
    assert session._gate is not None
    assert session._gate.closed is True


@pytest.mark.asyncio
async def test_session_concurrent_commit_callers_invoke_release_once() -> None:
    session, expectation = _final_release_session(status="FORM_CHANGED")

    outcomes = await asyncio.gather(
        session.commit_final_action(expectation),
        session.commit_final_action(expectation),
        return_exceptions=True,
    )

    blocked = [outcome for outcome in outcomes if isinstance(outcome, AshbyAdapterBlockedError)]
    assert len(blocked) == 2
    assert {outcome.reason_code for outcome in blocked} == {
        ReasonCode.FORM_CHANGED,
        ReasonCode.PERMIT_REPLAYED,
    }
    assert session._page.button.evaluate_calls == 1
    assert session._commit_claimed is True


@pytest.mark.parametrize("status", ["FORM_CHANGED", "ATTACHMENT_UNVERIFIED"])
@pytest.mark.asyncio
async def test_gate_request_overrides_contradictory_pre_request_status(
    status: str,
) -> None:
    session, expectation = _final_release_session(
        status=status,
        gate_signal="exact",
    )

    with pytest.raises(AshbyFinalActionAmbiguousError):
        await session.commit_final_action(expectation)

    assert session._gate is not None
    assert session._gate.request_may_have_left is True
    assert session._gate.closed is True


@pytest.mark.asyncio
async def test_exact_request_receipt_can_continue_to_fresh_evidence() -> None:
    session, expectation = _final_release_session(
        status="REQUEST_SUBMITTED",
        gate_signal="exact",
    )

    receipt = await session.commit_final_action(expectation)

    assert receipt.request_contract_digest == expectation.request_contract.digest
    assert receipt.payload_sha256 == "e" * 64
    assert session._gate is not None
    assert session._gate.closed is True


@pytest.mark.asyncio
async def test_invoked_primitive_with_rejected_gate_signal_is_ambiguous() -> None:
    session, expectation = _final_release_session(
        status="REQUEST_SUBMITTED",
        gate_signal="blocked",
    )

    with pytest.raises(AshbyFinalActionAmbiguousError):
        await session.commit_final_action(expectation)

    assert session._gate is not None
    assert session._gate.request_may_have_left is False
    assert session._gate.reason_code is ReasonCode.FORM_CHANGED
    assert session._gate.closed is True


class _AmbiguousSession:
    def __init__(
        self,
        snapshot: AshbyBrowserSnapshot,
        proof: AshbyAttachmentProof,
        blocked_reason: ReasonCode | None = None,
        receipt: AshbyFinalActionReceipt | None = None,
        post_snapshot: AshbyBrowserSnapshot | None = None,
    ) -> None:
        self.snapshot_value = snapshot
        self.proof = proof
        self.closed = False
        self.primitive_calls = 0
        self.blocked_reason = blocked_reason
        self.receipt = receipt
        self.post_snapshot = post_snapshot

    async def verify_resume_attachment(self, **_kwargs):
        return self.proof

    async def snapshot(self):
        return self.snapshot_value

    async def commit_final_action(self, _expectation):
        self.primitive_calls += 1
        if self.blocked_reason is not None:
            raise AshbyAdapterBlockedError(self.blocked_reason)
        if self.receipt is not None:
            if self.post_snapshot is not None:
                self.snapshot_value = self.post_snapshot
            return self.receipt
        raise AshbyFinalActionAmbiguousError

    async def confirmation_reference(self):
        return None

    async def close(self):
        self.closed = True


def _prepared_ambiguous_adapter(
    blocked_reason: ReasonCode | None = None,
    *,
    post_fixture: str | None = None,
):
    html, fields, decisions, contract, _expectation = _conditional_contract()
    fingerprint = ashby_v1_form_fingerprint(fields, contract.digest)
    descriptor = adapter_for_platform("ashby")
    assert descriptor is not None
    live_descriptor = replace(
        descriptor,
        qualification=QualificationTier.LIVE_CANARY_QUALIFIED,
        qualified_form_scope=(fingerprint,),
    )
    proof = AshbyAttachmentProof(
        field_id="resume",
        control_name="resume",
        cv_id="fixture-cv",
        cv_sha256=CV_SHA256,
        upload_complete=True,
        receipt_sha256="b" * 64,
    )
    plan = FormPlanV1(
        plan_id=uuid4(),
        application_id=7,
        application_revision=3,
        adapter_name="ashby",
        adapter_version="1.0.0",
        selector_version="ashby-candidate-v1",
        form_fingerprint=fingerprint,
        selected_cv_id="fixture-cv",
        selected_cv_hash=CV_SHA256,
        attached_cv_id="fixture-cv",
        attached_cv_hash=CV_SHA256,
        attachment_verified=True,
        profile_version=4,
        session_verified_at=NOW,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
        fields=fields,
        decisions=decisions,
    )
    permit = FinalSubmitPermit(
        attempt_id=19,
        job_url_hash="c" * 64,
        application_revision=plan.application_revision,
        adapter_name=plan.adapter_name,
        adapter_version=plan.adapter_version,
        selector_version=plan.selector_version,
        form_fingerprint=plan.form_fingerprint,
        cv_hash=plan.selected_cv_hash,
        expires_at=NOW + timedelta(minutes=5),
        nonce="fixture-permit",
    )
    action = PreparedFinalActionV1(
        attempt_id=permit.attempt_id,
        adapter_name=plan.adapter_name,
        adapter_version=plan.adapter_version,
        selector_version=plan.selector_version,
        form_fingerprint=plan.form_fingerprint,
        attached_cv_hash=plan.attached_cv_hash,
        prepared_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
        action_nonce="d" * 64,
    )
    session = _AmbiguousSession(
        AshbyBrowserSnapshot(html=html, url=APPLICATION_URL),
        proof,
        blocked_reason,
        (
            AshbyFinalActionReceipt(
                request_contract_digest=contract.digest,
                payload_sha256="e" * 64,
            )
            if post_fixture is not None
            else None
        ),
        (
            AshbyBrowserSnapshot(
                html=_fixture(post_fixture),
                url=APPLICATION_URL,
            )
            if post_fixture is not None
            else None
        ),
    )
    adapter = AshbyBrowserV1(
        browser_factory=lambda _url: session,  # type: ignore[arg-type]
        descriptor=live_descriptor,
        clock=lambda: NOW + timedelta(seconds=1),
    )
    adapter._prepared[action.action_nonce] = _PreparedState(
        session=session,  # type: ignore[arg-type]
        plan=plan,
        permit=permit,
        identity=parse_ashby_candidate_url(APPLICATION_URL).identity,
        fields=fields,
        attachment=proof,
        request_contract=contract,
        pre_action_html=html,
        pre_action_digest=hashlib.sha256(html.encode()).hexdigest(),
    )
    return adapter, action, permit, session


@pytest.mark.asyncio
async def test_final_primitive_without_outbound_is_unknown_and_not_retryable() -> None:
    adapter, action, permit, session = _prepared_ambiguous_adapter()

    outcome = await adapter.commit(action=action, permit=permit)
    replay = await adapter.commit(action=action, permit=permit)
    await adapter.cleanup_prepared_action(action=action)

    assert outcome.kind is AttemptOutcome.UNKNOWN
    assert outcome.reason_code.value == "FINAL_ACTION_UNCONFIRMED"
    assert replay.kind is AttemptOutcome.FAILED_BEFORE_COMMIT
    assert replay.reason_code.value == "PERMIT_REPLAYED"
    assert session.primitive_calls == 1
    assert session.closed is True


@pytest.mark.asyncio
async def test_concurrent_commit_callers_share_one_final_action_claim() -> None:
    adapter, action, permit, session = _prepared_ambiguous_adapter()

    outcomes = await asyncio.gather(
        adapter.commit(action=action, permit=permit),
        adapter.commit(action=action, permit=permit),
    )
    await adapter.cleanup_prepared_action(action=action)

    assert sum(outcome.kind is AttemptOutcome.UNKNOWN for outcome in outcomes) == 1
    replays = [
        outcome for outcome in outcomes if outcome.kind is AttemptOutcome.FAILED_BEFORE_COMMIT
    ]
    assert len(replays) == 1
    assert replays[0].reason_code is ReasonCode.PERMIT_REPLAYED
    assert session.primitive_calls == 1
    assert session.closed is True


def test_pre_request_reason_mapping_is_total_and_domain_valid() -> None:
    for reason_code in ReasonCode:
        outcome = _typed_pre_request_block(reason_code)

        assert outcome.kind in {
            AttemptOutcome.NEEDS_REVIEW,
            AttemptOutcome.FAILED_BEFORE_COMMIT,
        }


@pytest.mark.asyncio
async def test_invalid_review_reason_after_release_is_unknown_without_validation_error() -> None:
    adapter, action, permit, session = _prepared_ambiguous_adapter(ReasonCode.PERMIT_REPLAYED)

    outcome = await adapter.commit(action=action, permit=permit)
    replay = await adapter.commit(action=action, permit=permit)
    await adapter.cleanup_prepared_action(action=action)

    assert outcome.kind is AttemptOutcome.UNKNOWN
    assert outcome.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED
    assert replay.kind is AttemptOutcome.FAILED_BEFORE_COMMIT
    assert replay.reason_code is ReasonCode.PERMIT_REPLAYED
    assert session.primitive_calls == 1
    assert session.closed is True


@pytest.mark.asyncio
async def test_post_request_already_applied_is_not_newly_confirmed() -> None:
    adapter, action, permit, session = _prepared_ambiguous_adapter(
        post_fixture="already_applied.html"
    )

    outcome = await adapter.commit(action=action, permit=permit)
    await adapter.cleanup_prepared_action(action=action)

    assert outcome.kind is AttemptOutcome.ALREADY_APPLIED
    assert outcome.reason_code is ReasonCode.ALREADY_APPLIED
    assert session.primitive_calls == 1
    assert session.closed is True
