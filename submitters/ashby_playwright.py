"""Fail-closed local Playwright transport for Ashby candidate forms.

This fixture-qualified transport permits only safe reads until one exact
main-frame candidate POST gate is armed. It never calls undocumented Ashby
React endpoints and never reads Chrome or Edge credential stores.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import ipaddress
import json
import re
import socket
import zipfile
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from hmac import compare_digest
from io import BytesIO
from secrets import token_bytes
from typing import Any
from urllib.parse import urljoin, urlsplit

from core.config import Settings, get_settings
from core.submission_domain import (
    VERIFIED_ATTACHMENT_SENTINEL,
    AnswerDecisionV1,
    AnswerDisposition,
    FieldType,
    ReasonCode,
)
from submitters.ashby_identity import (
    ASHBY_CANDIDATE_HOST,
    AshbyIdentityError,
    canonical_ashby_application_url,
    parse_ashby_candidate_url,
)
from submitters.ashby_v1 import (
    ASHBY_CONFIRMATION_SELECTOR,
    ASHBY_FIELD_SELECTOR,
    ASHBY_FINAL_CONTROL_SELECTOR,
    ASHBY_FORM_SELECTOR,
    AshbyAdapterBlockedError,
    AshbyAttachmentProof,
    AshbyBrowserSnapshot,
    AshbyCandidateSession,
    AshbyFinalActionAmbiguousError,
    AshbyFinalActionReceipt,
    AshbyFinalCommitExpectation,
    ashby_v1_final_request_contract,
    observe_ashby_v1_fields,
)

_NAVIGATION_TIMEOUT_MS = 45_000
_ACTION_TIMEOUT_MS = 8_000
_MAX_NATIVE_BODY_BYTES = 24 * 1024 * 1024
_MAX_MULTIPART_PARTS = 256
_MAX_TEXT_BYTES = 1024 * 1024
_MULTIPART_CONTENT_TYPE = re.compile(
    r'^multipart/form-data;\s*boundary=(?:"([^"]{1,200})"|([^;\s]{1,200}))$',
    re.IGNORECASE,
)
_MULTIPART_BOUNDARY = re.compile(r"^[0-9A-Za-z'()+_,./:=?-]{1,200}$")
_UPLOAD_RECEIPT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_UPLOAD_COMPLETE_SELECTOR = (
    "[data-ashby-upload-state='complete'][data-upload-receipt][data-file-sha256][data-file-name]"
)
_PRE_REQUEST_RELEASE_REASONS = {
    "FORM_CHANGED": ReasonCode.FORM_CHANGED,
    "ATTACHMENT_UNVERIFIED": ReasonCode.ATTACHMENT_UNVERIFIED,
}


_CAPTURE_AND_RELEASE_SCRIPT = r"""
async (button, expected) => {
  const form = button?.form;
  const encoder = new TextEncoder();
  const normalizeText = value => String(value).replace(/\r\n|\r|\n/g, "\r\n");
  const base64Utf8 = value => {
    const bytes = encoder.encode(String(value));
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary);
  };
  const sha256 = async value => {
    const bytes = value instanceof ArrayBuffer ? value : encoder.encode(String(value));
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest))
      .map(byte => byte.toString(16).padStart(2, "0"))
      .join("");
  };
  const digestMaterial = value => sha256(JSON.stringify(value));
  const actionable = () => {
    if (
      !(button instanceof HTMLButtonElement)
      || !(form instanceof HTMLFormElement)
      || !button.isConnected
      || !form.isConnected
      || button.form !== form
      || !form.contains(button)
      || button.hasAttribute("form")
      || button.disabled
      || button.matches(":disabled")
    ) return false;
    for (
      let current = button;
      current instanceof HTMLElement;
      current = current.parentElement
    ) {
      if (
        current.hidden
        || current.inert === true
        || current.hasAttribute("inert")
        || String(current.getAttribute("aria-hidden") || "").trim().toLowerCase()
          === "true"
        || String(current.getAttribute("aria-disabled") || "").trim().toLowerCase()
          === "true"
      ) return false;
      const style = getComputedStyle(current);
      if (
        style.display === "none"
        || ["hidden", "collapse"].includes(style.visibility)
        || style.opacity === "0"
        || style.pointerEvents === "none"
        || style.contentVisibility === "hidden"
      ) return false;
    }
    return Array.from(button.getClientRects()).some(
      rectangle => rectangle.width > 0 && rectangle.height > 0
    );
  };
  if (
    !(form instanceof HTMLFormElement)
    || !actionable()
    || String(form.getAttribute("method") || "").trim().toUpperCase() !== "POST"
    || String(form.getAttribute("enctype") || "").trim().toLowerCase()
      !== "multipart/form-data"
    || !["", "_self"].includes(
      String(form.getAttribute("target") || "").trim().toLowerCase()
    )
    || new URL(form.getAttribute("action") || location.href, location.href).href
      !== expected.targetUrl
    || String(button.getAttribute("type") || "").trim().toLowerCase() !== "submit"
    || ["formaction", "formmethod", "formenctype", "formtarget", "formnovalidate"]
      .some(name => button.hasAttribute(name))
    || String(button.getAttribute("name") || "") !== expected.submitName
    || button.value !== expected.submitValue
    || !form.checkValidity()
    || document.querySelector("[data-ashby-submit-proxy]") !== null
  ) return "FORM_CHANGED";

  const observer = new MutationObserver(() => {});
  observer.observe(form, {
    attributes: true,
    childList: true,
    characterData: true,
    subtree: true,
  });
  let data;
  try {
    data = new FormData(form, button);
  } catch {
    observer.disconnect();
    return "FORM_CHANGED";
  }
  const expectedFields = new Map(expected.fields.map(field => [field.fieldId, field]));
  const allowedNames = new Set(expected.systemNames);
  allowedNames.add(expected.submitName);
  for (const field of expected.fields) {
    if (!Array.isArray(field.names) || field.names.length !== 1) {
      observer.disconnect();
      return "FORM_CHANGED";
    }
    allowedNames.add(field.names[0]);
  }
  const valuesByName = new Map();
  const canonical = [];
  let totalTextBytes = 0;
  for (const [rawName, rawValue] of data.entries()) {
    if (canonical.length >= 256) {
      observer.disconnect();
      return "FORM_CHANGED";
    }
    const name = String(rawName);
    if (!allowedNames.has(name) || encoder.encode(name).length > 256) {
      observer.disconnect();
      return "FORM_CHANGED";
    }
    const values = valuesByName.get(name) || [];
    values.push(rawValue);
    valuesByName.set(name, values);
    if (typeof rawValue === "string") {
      const value = normalizeText(rawValue);
      const valueBytes = encoder.encode(value);
      totalTextBytes += valueBytes.length;
      if (valueBytes.length > 262144 || totalTextBytes > 1048576) {
        observer.disconnect();
        return "FORM_CHANGED";
      }
      canonical.push(["t", base64Utf8(name), base64Utf8(value)]);
      continue;
    }
    if (!(rawValue instanceof File) || rawValue.size > 20971520) {
      observer.disconnect();
      return "ATTACHMENT_UNVERIFIED";
    }
    const digest = await sha256(await rawValue.arrayBuffer());
    const filename = String(rawValue.name || "");
    const mediaType = String(rawValue.type || "").trim().toLowerCase();
    if (
      digest !== expected.cvSha256
      || !filename
      || filename.includes("/")
      || filename.includes("\\")
      || !mediaType
    ) {
      observer.disconnect();
      return "ATTACHMENT_UNVERIFIED";
    }
    canonical.push([
      "f",
      base64Utf8(name),
      base64Utf8(filename),
      base64Utf8(mediaType),
      String(rawValue.size),
      digest,
    ]);
  }
  for (const field of expected.fields) {
    const values = valuesByName.get(field.names[0]) || [];
    let material;
    if (field.fieldType === "file") {
      if (
        values.length !== 1
        || !(values[0] instanceof File)
        || await sha256(await values[0].arrayBuffer()) !== expected.cvSha256
      ) {
        observer.disconnect();
        return "ATTACHMENT_UNVERIFIED";
      }
      material = `file:${expected.cvSha256}`;
    } else if (field.fieldType === "multi_select") {
      if (values.some(value => typeof value !== "string")) {
        observer.disconnect();
        return "FORM_CHANGED";
      }
      material = `multi:${JSON.stringify(values.map(normalizeText).sort())}`;
    } else if (["checkbox", "consent", "attestation"].includes(field.fieldType)) {
      if (values.some(value => typeof value !== "string") || values.length > 1) {
        observer.disconnect();
        return "FORM_CHANGED";
      }
      material = `bool:${values.length === 1 ? 1 : 0}`;
    } else {
      if (values.length !== 1 || typeof values[0] !== "string") {
        observer.disconnect();
        return "FORM_CHANGED";
      }
      material = `value:${normalizeText(values[0])}`;
    }
    if (await sha256(material) !== field.valueSha256) {
      observer.disconnect();
      return "FORM_CHANGED";
    }
  }
  const submitValues = valuesByName.get(expected.submitName) || [];
  if (
    submitValues.length !== 1
    || submitValues[0] !== expected.submitValue
    || expected.systemNames.some(name => {
      const values = valuesByName.get(name) || [];
      return values.length !== 1 || typeof values[0] !== "string" || !values[0];
    })
  ) {
    observer.disconnect();
    return "FORM_CHANGED";
  }
  const payloadSha256 = await sha256(JSON.stringify(canonical));
  const armed = await globalThis[expected.captureBinding](payloadSha256);
  const mutations = observer.takeRecords();
  observer.disconnect();
  if (armed !== true || mutations.length !== 0) return "FORM_CHANGED";

  // No proxy or any other DOM mutation is needed. This final computed-style
  // check is immediately adjacent to requestSubmit with no intervening await.
  if (actionable()) {
    try {
      HTMLFormElement.prototype.requestSubmit.call(form, button);
      return "REQUEST_SUBMITTED";
    } catch {
      // Invocation is the ambiguity boundary. A page-owned submit handler may
      // have started the exact request before throwing, so only the route gate
      // and fresh employer evidence may resolve what happened.
      return "REQUEST_SUBMITTED";
    }
  }
  return "FORM_CHANGED";
}
"""


def _b64_utf8(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _payload_digest(canonical: list[list[str]]) -> str:
    return hashlib.sha256(
        json.dumps(canonical, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _answer_material(field_type: FieldType, values: list[tuple[str, object]], cv_hash: str) -> str:
    if field_type is FieldType.FILE:
        if len(values) != 1 or values[0] != ("file", cv_hash):
            raise AshbyAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        return f"file:{cv_hash}"
    if field_type is FieldType.MULTI_SELECT:
        if any(kind != "text" or not isinstance(value, str) for kind, value in values):
            raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
        return "multi:" + json.dumps(
            sorted(str(value) for _, value in values),
            ensure_ascii=True,
            separators=(",", ":"),
        )
    if field_type in {FieldType.CHECKBOX, FieldType.CONSENT, FieldType.ATTESTATION}:
        if any(kind != "text" for kind, _ in values) or len(values) > 1:
            raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
        return f"bool:{int(len(values) == 1)}"
    if len(values) != 1 or values[0][0] != "text" or not isinstance(values[0][1], str):
        raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
    return f"value:{values[0][1]}"


def _multipart_payload_commitment(
    *,
    body: bytes,
    content_type: str,
    expectation: AshbyFinalCommitExpectation,
) -> str:
    """Parse and verify the exact browser-captured multipart form payload."""

    if (
        not body
        or len(body) > _MAX_NATIVE_BODY_BYTES
        or "\r" in content_type
        or "\n" in content_type
    ):
        raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
    match = _MULTIPART_CONTENT_TYPE.fullmatch(content_type.strip())
    boundary = next((value for value in match.groups() if value), "") if match else ""
    if not boundary or _MULTIPART_BOUNDARY.fullmatch(boundary) is None:
        raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
    delimiter = b"--" + boundary.encode("ascii")
    if not body.startswith(delimiter + b"\r\n") or not (
        body.endswith(b"\r\n" + delimiter + b"--\r\n") or body.endswith(b"\r\n" + delimiter + b"--")
    ):
        raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
    envelope = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + body
    try:
        message = BytesParser(policy=policy.default).parsebytes(envelope)
    except Exception as exc:
        raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED) from exc
    if message.defects or not message.is_multipart():
        raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
    parts = list(message.iter_parts())
    if not parts or len(parts) > _MAX_MULTIPART_PARTS:
        raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)

    contract = expectation.request_contract
    field_by_name = {
        names[0]: (field_id, field_type)
        for field_id, field_type, names in contract.field_controls
        if len(names) == 1
    }
    if len(field_by_name) != len(contract.field_controls):
        raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
    binding_by_id = {binding.field_id: binding for binding in expectation.answer_bindings}
    observed: dict[str, list[tuple[str, object]]] = {
        field_id: [] for field_id, _, _ in contract.field_controls
    }
    canonical: list[list[str]] = []
    system_seen: set[str] = set()
    submit_seen = 0
    total_text_bytes = 0
    for part in parts:
        if part.defects or part.is_multipart():
            raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
        raw_headers = tuple((name.casefold(), value) for name, value in part.raw_items())
        if (
            not raw_headers
            or len(raw_headers) > 2
            or any(name not in {"content-disposition", "content-type"} for name, _ in raw_headers)
            or sum(name == "content-disposition" for name, _ in raw_headers) != 1
        ):
            raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
        raw_disposition = next(
            value for name, value in raw_headers if name == "content-disposition"
        )
        if (
            len(raw_disposition.encode("utf-8")) > 1024
            or "\r" in raw_disposition
            or "\n" in raw_disposition
            or len(re.findall(r"(?:^|;)\s*name\s*=", raw_disposition, re.IGNORECASE)) != 1
            or len(re.findall(r"(?:^|;)\s*filename\s*=", raw_disposition, re.IGNORECASE)) > 1
        ):
            raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
        disposition = part.get("Content-Disposition", "")
        if len(disposition.encode("utf-8")) > 1024 or part.get_content_disposition() != "form-data":
            raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
        parameters = part.get_params(header="content-disposition", unquote=True) or []
        names = [value for key, value in parameters[1:] if str(key).casefold() == "name"]
        filenames = [value for key, value in parameters[1:] if str(key).casefold() == "filename"]
        if (
            len(names) != 1
            or len(filenames) > 1
            or not isinstance(names[0], str)
            or not names[0]
            or len(names[0].encode("utf-8")) > 256
            or "\r" in disposition
            or "\n" in disposition
        ):
            raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
        name = names[0]
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
        if not filenames:
            if any(header == "content-type" for header, _ in raw_headers):
                if (
                    part.get_content_type().casefold() != "text/plain"
                    or (part.get_content_charset() or "utf-8").casefold() != "utf-8"
                ):
                    raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
            try:
                value = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED) from exc
            value = re.sub(r"\r\n|\r|\n", "\r\n", value)
            total_text_bytes += len(value.encode("utf-8"))
            if len(payload) > 262_144 or total_text_bytes > _MAX_TEXT_BYTES:
                raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
            canonical.append(["t", _b64_utf8(name), _b64_utf8(value)])
            if name in field_by_name:
                field_id, _ = field_by_name[name]
                observed[field_id].append(("text", value))
            elif name in contract.system_controls:
                if not value or name in system_seen:
                    raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
                system_seen.add(name)
            elif name == contract.submit_control[0]:
                if value != contract.submit_control[1]:
                    raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
                submit_seen += 1
            else:
                raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
            continue

        filename = filenames[0]
        digest = hashlib.sha256(payload).hexdigest()
        media_type = part.get_content_type().strip().casefold()
        if (
            name not in field_by_name
            or field_by_name[name][1] is not FieldType.FILE
            or not isinstance(filename, str)
            or not filename
            or "/" in filename
            or "\\" in filename
            or len(filename.encode("utf-8")) > 255
            or not media_type
            or len(media_type.encode("utf-8")) > 160
            or len(payload) > 20 * 1024 * 1024
            or not payload
            or digest != expectation.selected_cv_hash
        ):
            raise AshbyAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        field_id, _ = field_by_name[name]
        observed[field_id].append(("file", digest))
        canonical.append(
            [
                "f",
                _b64_utf8(name),
                _b64_utf8(filename),
                _b64_utf8(media_type),
                str(len(payload)),
                digest,
            ]
        )

    if system_seen != set(contract.system_controls) or submit_seen != 1:
        raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
    for field_id, field_type, _ in contract.field_controls:
        binding = binding_by_id.get(field_id)
        if binding is None or binding.field_type is not field_type:
            raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
        material = _answer_material(
            field_type,
            observed[field_id],
            expectation.selected_cv_hash,
        )
        if not compare_digest(
            hashlib.sha256(material.encode("utf-8")).hexdigest(),
            binding.value_sha256,
        ):
            raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
    return _payload_digest(canonical)


class AshbyNetworkGuard:
    """Exact candidate origin, identity, and public-DNS admission policy."""

    def __init__(
        self,
        initial_url: str,
        *,
        resolver: Any = socket.getaddrinfo,
    ) -> None:
        try:
            parsed = parse_ashby_candidate_url(initial_url)
        except AshbyIdentityError as exc:
            raise AshbyAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        self.expected_identity = parsed.identity
        self._resolver = resolver
        self._dns_verified = False

    def require_allowed_url(self, url: str, *, main_frame: bool) -> None:
        try:
            parsed = urlsplit((url or "").strip())
            port = parsed.port
        except ValueError as exc:
            raise AshbyAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        hostname = (parsed.hostname or "").casefold()
        if (
            parsed.scheme.casefold() != "https"
            or hostname != ASHBY_CANDIDATE_HOST
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or parsed.fragment
        ):
            raise AshbyAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        if main_frame:
            try:
                parse_ashby_candidate_url(
                    url,
                    expected_identity=self.expected_identity,
                )
            except AshbyIdentityError as exc:
                raise AshbyAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        if self._dns_verified:
            return
        try:
            answers = self._resolver(hostname, 443, 0, socket.SOCK_STREAM)
            addresses = {
                str(answer[4][0]).split("%", 1)[0]
                for answer in answers
                if len(answer) > 4 and answer[4]
            }
            parsed_addresses = tuple(ipaddress.ip_address(value) for value in addresses)
        except (OSError, ValueError) as exc:
            raise AshbyAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        if not parsed_addresses or any(not address.is_global for address in parsed_addresses):
            raise AshbyAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        self._dns_verified = True


@dataclass(slots=True, repr=False)
class _OutboundGate:
    expectation: AshbyFinalCommitExpectation
    expected_main_frame: Any
    event: asyncio.Event
    expected_payload_sha256: str | None = None
    request_may_have_left: bool = False
    receipt: AshbyFinalActionReceipt | None = None
    reason_code: ReasonCode | None = None
    closed: bool = False


class PlaywrightAshbyCandidateSession:
    """One lazily launched public Ashby candidate browser session."""

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._guard: AshbyNetworkGuard | None = None
        self._attachment: AshbyAttachmentProof | None = None
        self._attachment_upload_name: str | None = None
        self._attachment_receipt_id: str | None = None
        self._filled_ids: set[str] = set()
        self._gate: _OutboundGate | None = None
        self._commit_claimed = False
        self._clicked = False

    def _require_page(self) -> Any:
        if self._page is None:
            raise AshbyAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        return self._page

    async def navigate(self, url: str) -> None:
        if self._page is not None:
            raise AshbyAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        guard = AshbyNetworkGuard(url)
        await asyncio.to_thread(guard.require_allowed_url, url, main_frame=True)
        self._guard = guard
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise AshbyAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self._settings.portal_browser_headless,
            )
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 900},
                service_workers="block",
                accept_downloads=False,
            )
            route_web_socket = getattr(self._context, "route_web_socket", None)
            if not callable(route_web_socket):
                raise AshbyAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
            await route_web_socket("**/*", self._block_web_socket)
            await self._context.route("**/*", self._guard_request)
            self._page = await self._context.new_page()
            await self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=_NAVIGATION_TIMEOUT_MS,
            )
            await self._assert_current_url()
        except Exception as exc:
            await self.close()
            if isinstance(exc, AshbyAdapterBlockedError):
                raise
            raise AshbyAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc

    @staticmethod
    async def _block_web_socket(web_socket: Any) -> None:
        await web_socket.close(code=1008, reason="browser transport disabled")

    async def _guard_request(self, route: Any, request: Any) -> None:
        guard = self._guard
        if guard is None:
            await route.abort("blockedbyclient")
            return
        method = str(getattr(request, "method", "GET") or "GET").strip().upper()
        gate = self._gate
        if gate is not None and method not in {"GET", "HEAD", "OPTIONS"}:
            await self._guard_outbound(route, request, method=method, gate=gate)
            return
        if gate is None and method not in {"GET", "HEAD", "OPTIONS"}:
            await route.abort("blockedbyclient")
            return
        if gate is not None and not gate.request_may_have_left:
            # Once the one-use release gate is armed, the exact candidate POST
            # is the only request admitted. This also prevents a submit handler
            # from leaking reviewed values through a same-origin GET.
            gate.reason_code = ReasonCode.FORM_CHANGED
            gate.closed = True
            gate.event.set()
            await route.abort("blockedbyclient")
            return
        scheme = urlsplit(request.url).scheme.casefold()
        if scheme in {"data", "blob"} and not request.is_navigation_request():
            await route.continue_()
            return
        try:
            await asyncio.to_thread(
                guard.require_allowed_url,
                request.url,
                main_frame=request.is_navigation_request(),
            )
        except AshbyAdapterBlockedError:
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def _guard_outbound(
        self,
        route: Any,
        request: Any,
        *,
        method: str,
        gate: _OutboundGate,
    ) -> None:
        expectation = gate.expectation
        if gate.closed or gate.request_may_have_left:
            gate.reason_code = ReasonCode.FINAL_ACTION_UNCONFIRMED
            gate.event.set()
            await route.abort("blockedbyclient")
            return
        try:
            exact = (
                method == "POST"
                and request.url == expectation.request_contract.target_url
                and request.is_navigation_request() is True
                and str(request.resource_type).casefold() == "document"
                and request.frame == gate.expected_main_frame
            )
        except Exception:
            exact = False
        if not exact or gate.expected_payload_sha256 is None:
            gate.reason_code = ReasonCode.FORM_CHANGED
            gate.closed = True
            gate.event.set()
            await route.abort("blockedbyclient")
            return
        try:
            guard = self._guard
            if guard is None:
                raise AshbyAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
            await asyncio.to_thread(
                guard.require_allowed_url,
                request.url,
                main_frame=True,
            )
            headers_value = (
                request.all_headers()
                if callable(getattr(request, "all_headers", None))
                else request.headers
            )
            if inspect.isawaitable(headers_value):
                headers_value = await headers_value
            body_value = request.post_data_buffer
            if callable(body_value):
                body_value = body_value()
            if inspect.isawaitable(body_value):
                body_value = await body_value
            content_types = [
                str(value)
                for key, value in dict(headers_value).items()
                if str(key).casefold() == "content-type"
            ]
            if len(content_types) != 1 or not isinstance(
                body_value,
                (bytes, bytearray, memoryview),
            ):
                raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
            payload_sha256 = _multipart_payload_commitment(
                body=bytes(body_value),
                content_type=content_types[0],
                expectation=expectation,
            )
            if not compare_digest(payload_sha256, gate.expected_payload_sha256):
                raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
        except AshbyAdapterBlockedError as exc:
            gate.reason_code = exc.reason_code
            gate.closed = True
            gate.event.set()
            await route.abort("blockedbyclient")
            return
        except Exception:
            gate.reason_code = ReasonCode.FORM_CHANGED
            gate.closed = True
            gate.event.set()
            await route.abort("blockedbyclient")
            return

        gate.request_may_have_left = True
        gate.receipt = AshbyFinalActionReceipt(
            request_contract_digest=expectation.request_contract.digest,
            payload_sha256=gate.expected_payload_sha256,
        )
        gate.event.set()
        try:
            await route.continue_()
        except Exception as exc:
            gate.reason_code = ReasonCode.FINAL_ACTION_UNCONFIRMED
            raise AshbyFinalActionAmbiguousError from exc

    async def _assert_current_url(self) -> None:
        page = self._require_page()
        guard = self._guard
        if guard is None:
            raise AshbyAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        await asyncio.to_thread(guard.require_allowed_url, page.url, main_frame=True)

    async def open_application_form(self) -> None:
        page = self._require_page()
        target = canonical_ashby_application_url(page.url)
        link = page.locator(
            f'a[data-ashby-open-application][href="{target}"], '
            f'a[data-ashby-open-application][href$="/application"]'
        )
        if await link.count() != 1 or not await link.is_visible():
            raise AshbyAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        observed = await link.get_attribute("href")
        if (
            observed is None
            or canonical_ashby_application_url(urljoin(page.url, observed)) != target
        ):
            raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
        await link.click(timeout=_ACTION_TIMEOUT_MS)
        await page.wait_for_load_state("domcontentloaded")
        await self._assert_current_url()

    async def snapshot(self) -> AshbyBrowserSnapshot:
        page = self._require_page()
        await self._assert_current_url()
        locale = await page.locator("html").get_attribute("lang") or "en"
        return AshbyBrowserSnapshot(
            html=await page.content(),
            url=page.url,
            locale=locale,
        )

    @staticmethod
    def _resume_payload_kind(payload: bytes) -> tuple[str, str]:
        if payload.startswith(b"%PDF-"):
            return "pdf", "application/pdf"
        try:
            with zipfile.ZipFile(BytesIO(payload)) as archive:
                names = set(archive.namelist())
        except (OSError, zipfile.BadZipFile):
            names = set()
        if {"[Content_Types].xml", "word/document.xml"}.issubset(names):
            return (
                "docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        raise AshbyAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)

    async def ensure_resume_attachment(
        self,
        *,
        field_id: str,
        control_name: str,
        resume_bytes: bytes,
        cv_id: str,
        expected_sha256: str,
    ) -> AshbyAttachmentProof:
        page = self._require_page()
        if not compare_digest(hashlib.sha256(resume_bytes).hexdigest(), expected_sha256):
            raise AshbyAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        extension, media_type = self._resume_payload_kind(resume_bytes)
        upload_name = f"resume-{hashlib.sha256(token_bytes(32)).hexdigest()[:24]}.{extension}"
        wrapper = page.locator(f'{ASHBY_FIELD_SELECTOR}[data-field-id="{field_id}"]')
        if await wrapper.count() != 1 or not await wrapper.is_visible():
            raise AshbyAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        control = wrapper.locator(f'input[type="file"][name="{control_name}"]')
        if await control.count() != 1:
            raise AshbyAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        prior_receipts = set(
            await wrapper.locator(_UPLOAD_COMPLETE_SELECTOR).evaluate_all(
                "nodes => nodes.map(node => node.getAttribute('data-upload-receipt'))"
            )
        )
        await control.set_input_files(
            {"name": upload_name, "mimeType": media_type, "buffer": resume_bytes},
            timeout=_ACTION_TIMEOUT_MS,
        )
        receipt_id: str | None = None
        for _poll in range(30):
            markers = wrapper.locator(_UPLOAD_COMPLETE_SELECTOR)
            matches: list[str] = []
            for index in range(await markers.count()):
                marker = markers.nth(index)
                if not await marker.is_visible():
                    continue
                receipt = (await marker.get_attribute("data-upload-receipt") or "").strip()
                digest = (await marker.get_attribute("data-file-sha256") or "").strip()
                name = (await marker.get_attribute("data-file-name") or "").strip()
                if (
                    _UPLOAD_RECEIPT_ID.fullmatch(receipt) is not None
                    and receipt not in prior_receipts
                    and compare_digest(digest, expected_sha256)
                    and name == upload_name
                ):
                    matches.append(receipt)
            if len(matches) == 1:
                receipt_id = matches[0]
                break
            if len(matches) > 1:
                raise AshbyAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
            await page.wait_for_timeout(100)
        if receipt_id is None:
            raise AshbyAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        receipt_sha256 = hashlib.sha256(
            f"{field_id}|{control_name}|{expected_sha256}|{receipt_id}".encode()
        ).hexdigest()
        proof = AshbyAttachmentProof(
            field_id=field_id,
            control_name=control_name,
            cv_id=cv_id,
            cv_sha256=expected_sha256,
            upload_complete=True,
            receipt_sha256=receipt_sha256,
        )
        self._attachment = proof
        self._attachment_upload_name = upload_name
        self._attachment_receipt_id = receipt_id
        return proof

    async def verify_resume_attachment(
        self,
        *,
        field_id: str,
        control_name: str,
        cv_id: str,
        expected_sha256: str,
    ) -> AshbyAttachmentProof:
        page = self._require_page()
        proof = self._attachment
        if (
            proof is None
            or not proof.matches(
                field_id=field_id,
                control_name=control_name,
                cv_id=cv_id,
                cv_sha256=expected_sha256,
            )
            or self._attachment_upload_name is None
            or self._attachment_receipt_id is None
        ):
            return AshbyAttachmentProof(
                field_id=field_id,
                control_name=control_name,
                cv_id=cv_id,
                cv_sha256=expected_sha256,
                upload_complete=False,
            )
        wrapper = page.locator(f'{ASHBY_FIELD_SELECTOR}[data-field-id="{field_id}"]')
        marker = wrapper.locator(
            f'{_UPLOAD_COMPLETE_SELECTOR}[data-upload-receipt="{self._attachment_receipt_id}"]'
        )
        control = wrapper.locator(f'input[type="file"][name="{control_name}"]')
        if (
            await marker.count() != 1
            or not await marker.is_visible()
            or (await marker.get_attribute("data-file-sha256") or "") != expected_sha256
            or (await marker.get_attribute("data-file-name") or "") != self._attachment_upload_name
            or await control.count() != 1
            or not (await control.input_value())
            .replace("\\", "/")
            .endswith(f"/{self._attachment_upload_name}")
        ):
            return AshbyAttachmentProof(
                field_id=field_id,
                control_name=control_name,
                cv_id=cv_id,
                cv_sha256=expected_sha256,
                upload_complete=False,
            )
        return proof

    async def fill_once(self, decisions: tuple[AnswerDecisionV1, ...]) -> None:
        page = self._require_page()
        fields = {
            field.field_id: field for field in observe_ashby_v1_fields((await self.snapshot()).html)
        }
        decision_ids = {decision.field_id for decision in decisions}
        if len(decision_ids) != len(decisions) or decision_ids.intersection(self._filled_ids):
            raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
        for decision in decisions:
            field = fields.get(decision.field_id)
            if (
                field is None
                or decision.disposition is not AnswerDisposition.RESOLVED
                or decision.value is None
            ):
                raise AshbyAdapterBlockedError(ReasonCode.REQUIRED_FIELD_UNKNOWN)
            if field.field_type is FieldType.FILE:
                if decision.value != VERIFIED_ATTACHMENT_SENTINEL:
                    raise AshbyAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
                continue
            wrapper = page.locator(f'{ASHBY_FIELD_SELECTOR}[data-field-id="{field.field_id}"]')
            if await wrapper.count() != 1 or not await wrapper.is_visible():
                raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
            controls = wrapper.locator("input:not([type=hidden]), textarea, select")
            try:
                if field.field_type in {FieldType.SELECT, FieldType.MULTI_SELECT}:
                    await controls.first.select_option(
                        list(decision.value)
                        if isinstance(decision.value, tuple)
                        else str(decision.value)
                    )
                elif field.field_type is FieldType.RADIO:
                    radios = wrapper.locator('input[type="radio"]')
                    radio_count = await radios.count()
                    if radio_count > 128:
                        raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
                    matches = [
                        radios.nth(index)
                        for index in range(radio_count)
                        if await radios.nth(index).input_value() == str(decision.value)
                    ]
                    if len(matches) != 1:
                        raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
                    await matches[0].check()
                elif field.field_type in {
                    FieldType.CHECKBOX,
                    FieldType.CONSENT,
                    FieldType.ATTESTATION,
                }:
                    await (
                        controls.first.check()
                        if decision.value is True
                        else controls.first.uncheck()
                    )
                else:
                    await controls.first.fill(str(decision.value))
            except AshbyAdapterBlockedError:
                raise
            except Exception as exc:
                raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED) from exc
        self._filled_ids.update(decision_ids)

    async def settle_react(self) -> None:
        page = self._require_page()
        await page.wait_for_timeout(150)
        pending = page.locator("[data-ashby-react-pending='true']")
        if await pending.count() != 0:
            raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)

    async def commit_final_action(
        self,
        expectation: AshbyFinalCommitExpectation,
    ) -> AshbyFinalActionReceipt:
        page = self._require_page()
        if self._clicked or self._commit_claimed:
            raise AshbyAdapterBlockedError(ReasonCode.PERMIT_REPLAYED)
        self._commit_claimed = True
        snapshot = await self.snapshot()
        parsed = parse_ashby_candidate_url(
            snapshot.url,
            expected_identity=expectation.identity,
        )
        fields = observe_ashby_v1_fields(snapshot.html)
        contract = ashby_v1_final_request_contract(
            snapshot.html,
            snapshot.url,
            parsed.identity,
            fields,
        )
        if (
            fields != expectation.observed_fields
            or contract is None
            or contract != expectation.request_contract
            or not compare_digest(
                hashlib.sha256(snapshot.html.encode("utf-8")).hexdigest(),
                expectation.pre_action_digest,
            )
            or tuple(
                (binding.field_id, binding.field_type) for binding in expectation.answer_bindings
            )
            != tuple((field.field_id, field.field_type) for field in fields)
        ):
            raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)

        # Exact answer values remain redacted in the expectation. The browser
        # script and route independently re-hash each live value before release.
        attachment = self._attachment
        if attachment is None:
            raise AshbyAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        verified = await self.verify_resume_attachment(
            field_id=attachment.field_id,
            control_name=attachment.control_name,
            cv_id=expectation.selected_cv_id,
            expected_sha256=expectation.selected_cv_hash,
        )
        if (
            not verified.matches(
                field_id=attachment.field_id,
                control_name=attachment.control_name,
                cv_id=expectation.selected_cv_id,
                cv_sha256=expectation.selected_cv_hash,
            )
            or verified.receipt_sha256 != expectation.attachment_receipt_sha256
        ):
            raise AshbyAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        button = page.locator(ASHBY_FINAL_CONTROL_SELECTOR)
        form = page.locator(ASHBY_FORM_SELECTOR)
        if await button.count() != 1 or not await button.is_visible() or await form.count() != 1:
            raise AshbyAdapterBlockedError(ReasonCode.FORM_CHANGED)
        main_frame = getattr(page, "main_frame", None)
        if main_frame is None:
            raise AshbyAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        gate = _OutboundGate(
            expectation=expectation,
            expected_main_frame=main_frame,
            event=asyncio.Event(),
        )
        self._gate = gate
        self._clicked = True
        binding_name = f"__ashbyCapture_{hashlib.sha256(token_bytes(32)).hexdigest()}"

        async def capture(_source: Any, payload_sha256: object) -> bool:
            if (
                gate.closed
                or gate.expected_payload_sha256 is not None
                or not isinstance(payload_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", payload_sha256) is None
            ):
                return False
            gate.expected_payload_sha256 = payload_sha256
            return True

        try:
            await page.expose_binding(binding_name, capture)
        except Exception:
            gate.closed = True
            raise AshbyFinalActionAmbiguousError from None
        status: object = None
        try:
            status = await button.evaluate(
                _CAPTURE_AND_RELEASE_SCRIPT,
                {
                    "targetUrl": contract.target_url,
                    "submitName": contract.submit_control[0],
                    "submitValue": contract.submit_control[1],
                    "systemNames": list(contract.system_controls),
                    "cvSha256": expectation.selected_cv_hash,
                    "captureBinding": binding_name,
                    "fields": [
                        {
                            "fieldId": binding.field_id,
                            "fieldType": binding.field_type.value,
                            "valueSha256": binding.value_sha256,
                            "names": list(
                                next(
                                    names
                                    for field_id, _, names in contract.field_controls
                                    if field_id == binding.field_id
                                )
                            ),
                        }
                        for binding in expectation.answer_bindings
                    ],
                },
            )
        except Exception:
            gate.closed = True
            # Evaluation context loss or an exception after admission cannot
            # prove that requestSubmit was not invoked. Never make it retryable.
            raise AshbyFinalActionAmbiguousError from None

        pre_request_reason = (
            _PRE_REQUEST_RELEASE_REASONS.get(status) if isinstance(status, str) else None
        )
        if pre_request_reason is not None:
            contradictory_gate_signal = (
                gate.event.is_set()
                or gate.request_may_have_left
                or gate.receipt is not None
                or gate.reason_code is not None
            )
            gate.closed = True
            if contradictory_gate_signal:
                raise AshbyFinalActionAmbiguousError
            raise AshbyAdapterBlockedError(pre_request_reason)
        if status != "REQUEST_SUBMITTED":
            # None, malformed objects, and unrecognized strings arrive after
            # the gate and one-use action are armed. They cannot prove that the
            # intrinsic requestSubmit boundary was not crossed.
            gate.closed = True
            raise AshbyFinalActionAmbiguousError
        if not gate.event.is_set():
            try:
                await asyncio.wait_for(gate.event.wait(), timeout=2.0)
            except TimeoutError:
                gate.closed = True
                raise AshbyFinalActionAmbiguousError from None
        gate.closed = True
        if gate.request_may_have_left and gate.receipt is not None and gate.reason_code is None:
            return gate.receipt
        # requestSubmit was invoked but the exact request was rejected or was
        # not observed. That is non-retryable indeterminate state, even when a
        # route guard can name the drift that prevented continuation.
        raise AshbyFinalActionAmbiguousError

    async def confirmation_reference(self) -> str | None:
        page = self._require_page()
        await self._assert_current_url()
        locator = page.locator(ASHBY_CONFIRMATION_SELECTOR)
        if await locator.count() != 1 or not await locator.is_visible():
            return None
        first = (await locator.get_attribute("data-submission-id") or "").strip()
        if not first:
            return None
        await page.wait_for_timeout(250)
        await self._assert_current_url()
        locator = page.locator(ASHBY_CONFIRMATION_SELECTOR)
        if await locator.count() != 1 or not await locator.is_visible():
            return None
        second = (await locator.get_attribute("data-submission-id") or "").strip()
        return first if first == second and second else None

    async def close(self) -> None:
        context, browser, playwright = self._context, self._browser, self._playwright
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._guard = None
        self._attachment = None
        self._attachment_upload_name = None
        self._attachment_receipt_id = None
        self._gate = None
        self._commit_claimed = False
        try:
            if context is not None:
                await context.close()
        finally:
            try:
                if browser is not None:
                    await browser.close()
            finally:
                if playwright is not None:
                    await playwright.stop()


def playwright_ashby_browser_factory(_url: str) -> AshbyCandidateSession:
    """Create a lazy fixture-qualified local browser session."""

    return PlaywrightAshbyCandidateSession()
