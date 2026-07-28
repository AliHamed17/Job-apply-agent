"""Private, fail-closed Playwright transport for SmartRecruiters candidate v1.

The transport owns one isolated browser-profile lease. It never reads a
desktop browser password store. Before the irreversible action it binds the
exact candidate POST, reviewed controls, disclosures, and selected CV bytes.
The final browser call synchronously rechecks those bindings and invokes the
native form submission without an intervening await or DOM mutation.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
import zipfile
from collections.abc import Callable
from email import policy
from email.parser import BytesParser
from hmac import compare_digest
from io import BytesIO
from secrets import token_bytes
from typing import Any
from urllib.parse import urlsplit

from core.config import Settings, get_settings
from core.portal_sessions import PortalSessionError, PortalSessionLease, portal_session_for_url
from core.submission_domain import (
    VERIFIED_ATTACHMENT_SENTINEL,
    AnswerDecisionV1,
    AnswerDisposition,
    FieldType,
    FormDisclosureV1,
    FormFieldV1,
    ReasonCode,
)
from submitters.smartrecruiters_identity import (
    SmartRecruitersCandidateIdentity,
    SmartRecruitersIdentityError,
    SmartRecruitersResolvedIdentity,
    parse_smartrecruiters_candidate_identity,
    resolve_smartrecruiters_posting_identity,
)
from submitters.smartrecruiters_v1 import (
    SMARTRECRUITERS_CONFIRMATION_SELECTOR,
    SMARTRECRUITERS_FINAL_SUBMIT_SELECTOR,
    SMARTRECRUITERS_FORM_SELECTOR,
    SmartRecruitersAdapterBlockedError,
    SmartRecruitersAttachmentProof,
    SmartRecruitersBrowserSnapshot,
    SmartRecruitersCandidateSession,
    SmartRecruitersFinalActionAmbiguousError,
    SmartRecruitersFinalActionProof,
    smartrecruiters_disclosures_digest,
    smartrecruiters_v1_disclosure_runtime_material,
    smartrecruiters_v1_final_action_binding,
    smartrecruiters_v1_form_fingerprint,
)

_NAVIGATION_TIMEOUT_MS = 45_000
_ACTION_TIMEOUT_MS = 8_000
_POST_ACTION_SETTLE_TIMEOUT_MS = 12_000
_MAX_FORM_DATA_BODY_BYTES = 24 * 1024 * 1024
_MAX_FORM_DATA_ENTRIES = 256
_MAX_FORM_DATA_STRING_BYTES = 1024 * 1024
_MAX_FORM_FIELD_NAME_BYTES = 256
_MAX_FORM_FILENAME_BYTES = 512
_MAX_FORM_MEDIA_TYPE_BYTES = 200
_MAX_RESUME_FORM_BYTES = 20 * 1024 * 1024
_FORM_DATA_COMMITMENT_VERSION = "smartrecruiters-formdata-v1"
_CANDIDATE_HOST = "jobs.smartrecruiters.com"
_UPLOAD_MARKER_SELECTOR = (
    '[data-qa="resume-upload-complete"][data-upload-id][data-file-name][data-file-sha256]'
)
_FINAL_BUTTON_SELECTOR = SMARTRECRUITERS_FINAL_SUBMIT_SELECTOR
_FIELD_WRAPPER_SELECTOR = '[data-qa="application-field"][data-field-id]'
_DISCLOSURE_SELECTOR = (
    '[data-qa="form-disclosure"][data-disclosure-id][data-disclosure-kind][data-disclosure-source]'
)
_CAPTURE_DOM_SNAPSHOT_JS = r"""
async () => {
    const html = document.documentElement.outerHTML;
    const bytes = new TextEncoder().encode(html);
    const digest = Array.from(
        new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))
    ).map(byte => byte.toString(16).padStart(2, "0")).join("");
    return {
        html,
        digest,
        locale: document.documentElement.lang || "en"
    };
}
"""


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_form_string(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


def _length_frame(value: str) -> bytes:
    encoded = _normalize_form_string(value).encode("utf-8")
    return str(len(encoded)).encode("ascii") + b":" + encoded


def _bounded_form_name(value: object, maximum: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        return None
    if len(encoded) > maximum or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        return None
    return value


def _canonical_form_data_payload_sha256(
    *,
    content_type: str,
    body: bytes,
    expected_cv_sha256: str,
) -> str | None:
    """Return a bounded redacted commitment for one exact multipart body."""

    if (
        not body
        or len(body) > _MAX_FORM_DATA_BODY_BYTES
        or len(content_type.encode("utf-8", errors="ignore")) > 512
        or "\r" in content_type
        or "\n" in content_type
        or len(expected_cv_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_cv_sha256)
    ):
        return None
    if content_type.split(";", 1)[0].strip().casefold() != "multipart/form-data":
        return None
    try:
        header = content_type.encode("ascii", errors="strict")
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + header + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
        )
    except (UnicodeError, ValueError):
        return None
    boundary = message.get_boundary()
    try:
        encoded_boundary = boundary.encode("ascii", errors="strict") if boundary else b""
    except UnicodeError:
        return None
    if (
        message.defects
        or not message.is_multipart()
        or not encoded_boundary
        or len(encoded_boundary) > 70
        or message.preamble not in {None, ""}
        or message.epilogue not in {None, ""}
        or message.get_content_type().casefold() != "multipart/form-data"
    ):
        return None

    material: list[bytes] = [_FORM_DATA_COMMITMENT_VERSION.encode("ascii")]
    entry_count = 0
    string_bytes = 0
    file_count = 0
    for part in message.iter_parts():
        entry_count += 1
        if (
            entry_count > _MAX_FORM_DATA_ENTRIES
            or part.defects
            or part.is_multipart()
            or len(part.get_all("Content-Disposition", [])) != 1
            or len(part.get_all("Content-Type", [])) > 1
            or any(
                item.casefold() not in {"content-disposition", "content-type"}
                for item in part.keys()
            )
            or part.get_content_disposition() != "form-data"
            or part.get("Content-Transfer-Encoding") is not None
        ):
            return None
        name = _bounded_form_name(
            part.get_param("name", header="content-disposition"),
            _MAX_FORM_FIELD_NAME_BYTES,
        )
        if name is None:
            return None
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            return None
        filename_value = part.get_filename()
        if filename_value is None:
            try:
                value = payload.decode("utf-8", errors="strict")
            except UnicodeError:
                return None
            normalized = _normalize_form_string(value)
            string_bytes += len(normalized.encode("utf-8"))
            if string_bytes > _MAX_FORM_DATA_STRING_BYTES:
                return None
            material.append(b"S" + _length_frame(name) + _length_frame(normalized))
            continue

        filename = _bounded_form_name(filename_value, _MAX_FORM_FILENAME_BYTES)
        media_type = _bounded_form_name(
            part.get_content_type().casefold(),
            _MAX_FORM_MEDIA_TYPE_BYTES,
        )
        file_count += 1
        if (
            filename is None
            or media_type is None
            or part.get("Content-Type") is None
            or file_count > 1
            or not payload
            or len(payload) > _MAX_RESUME_FORM_BYTES
        ):
            return None
        file_sha256 = hashlib.sha256(payload).hexdigest()
        if not compare_digest(file_sha256, expected_cv_sha256):
            return None
        material.append(
            b"F"
            + _length_frame(name)
            + _length_frame(filename)
            + _length_frame(media_type)
            + _length_frame(str(len(payload)))
            + _length_frame(file_sha256)
        )
    if entry_count == 0 or file_count != 1:
        return None
    return hashlib.sha256(b"".join(material)).hexdigest()


def _request_body_bytes(request: Any) -> bytes | None:
    try:
        buffered = getattr(request, "post_data_buffer", None)
    except Exception:
        return None
    if isinstance(buffered, bytes):
        return buffered if len(buffered) <= _MAX_FORM_DATA_BODY_BYTES else None
    if isinstance(buffered, (bytearray, memoryview)):
        payload = bytes(buffered)
        return payload if len(payload) <= _MAX_FORM_DATA_BODY_BYTES else None
    return None


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
    raise SmartRecruitersAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)


def _answer_material(
    field: FormFieldV1,
    decision: AnswerDecisionV1,
    *,
    selected_cv_hash: str,
) -> object:
    if decision.disposition is not AnswerDisposition.RESOLVED or decision.value is None:
        raise SmartRecruitersAdapterBlockedError(ReasonCode.REQUIRED_FIELD_UNKNOWN)
    if field.field_type is FieldType.FILE:
        if decision.value != VERIFIED_ATTACHMENT_SENTINEL:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        return {"kind": "file", "sha256": selected_cv_hash}
    if field.field_type is FieldType.MULTI_SELECT:
        if not isinstance(decision.value, tuple):
            raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
        return {"kind": "multi", "value": sorted(decision.value)}
    if field.field_type in {
        FieldType.CHECKBOX,
        FieldType.CONSENT,
        FieldType.ATTESTATION,
    }:
        if type(decision.value) is not bool:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
        return {"kind": "bool", "value": decision.value}
    if isinstance(decision.value, bool) or not isinstance(decision.value, (str, int, float)):
        raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
    return {"kind": "scalar", "value": str(decision.value)}


def _field_bindings(
    fields: tuple[FormFieldV1, ...],
    decisions: tuple[AnswerDecisionV1, ...],
    *,
    selected_cv_hash: str,
) -> list[dict[str, object]]:
    by_id = {decision.field_id: decision for decision in decisions}
    if len(by_id) != len(decisions) or set(by_id) != {field.field_id for field in fields}:
        raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
    return [
        {
            "fieldId": field.field_id,
            "fieldType": field.field_type.value,
            "answer": _answer_material(
                field,
                by_id[field.field_id],
                selected_cv_hash=selected_cv_hash,
            ),
        }
        for field in fields
    ]


class SmartRecruitersNetworkGuard:
    """Exact candidate origin plus public-DNS policy."""

    def __init__(
        self,
        initial_url: str,
        *,
        resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
    ) -> None:
        try:
            identity = parse_smartrecruiters_candidate_identity(initial_url)
        except SmartRecruitersIdentityError as exc:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        self.identity = identity
        self.expected_hostname = identity.hostname
        self._resolver = resolver
        self._dns_verified = False

    @staticmethod
    def _https_hostname(url: str) -> str:
        try:
            parsed = urlsplit((url or "").strip())
            port = parsed.port
        except ValueError as exc:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        hostname = (parsed.hostname or "").casefold()
        if (
            parsed.scheme.casefold() != "https"
            or hostname != _CANDIDATE_HOST
            or hostname != hostname.rstrip(".")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or port not in {None, 443}
            or any(ord(character) > 127 for character in hostname)
        ):
            raise SmartRecruitersAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        return hostname

    def _verify_public_dns(self) -> None:
        if self._dns_verified:
            return
        try:
            answers = self._resolver(
                self.expected_hostname,
                443,
                0,
                socket.SOCK_STREAM,
            )
            addresses = {
                str(answer[4][0]).split("%", 1)[0]
                for answer in answers
                if len(answer) > 4 and answer[4]
            }
            parsed = tuple(ipaddress.ip_address(address) for address in addresses)
        except (OSError, ValueError) as exc:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        if not parsed or any(not address.is_global for address in parsed):
            raise SmartRecruitersAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        self._dns_verified = True

    def require_allowed_url(
        self,
        url: str,
        *,
        main_frame: bool = False,
        allow_exact_final_action: str | None = None,
    ) -> None:
        self._https_hostname(url)
        if main_frame:
            if allow_exact_final_action is not None and url == allow_exact_final_action:
                pass
            else:
                try:
                    observed = parse_smartrecruiters_candidate_identity(url)
                except SmartRecruitersIdentityError as exc:
                    raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED) from exc
                if (
                    observed.hostname,
                    observed.company,
                    observed.public_id,
                ) != (
                    self.identity.hostname,
                    self.identity.company,
                    self.identity.public_id,
                ):
                    raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
        self._verify_public_dns()


class _OneShotCandidatePostGate:
    _SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

    def __init__(
        self,
        *,
        page: Any,
        exact_url: str,
        expected_payload_sha256: str,
        selected_cv_hash: str,
    ) -> None:
        self.page = page
        self.main_frame = page.main_frame
        if self.main_frame is None:
            raise ValueError("SMARTRECRUITERS_FINAL_FRAME_INVALID")
        self.exact_url = exact_url
        self.expected_payload_sha256 = expected_payload_sha256
        self.selected_cv_hash = selected_cv_hash
        self.completed = asyncio.Event()
        self.rejected = False
        self.possibly_sent = False

    def reject(self) -> None:
        if not self.completed.is_set():
            self.rejected = True
            self.completed.set()

    def evaluate(self, request: Any) -> bool | None:
        try:
            method = str(getattr(request, "method", "")).strip().upper()
        except Exception:
            method = ""
        if self.completed.is_set():
            return None if self.possibly_sent and method in self._SAFE_METHODS else False
        if method in self._SAFE_METHODS:
            return None
        if method != "POST":
            self.reject()
            return False
        try:
            is_navigation = request.is_navigation_request() is True
            frame = request.frame
            resource_type = str(request.resource_type).strip().casefold()
            headers = getattr(request, "headers", {}) or {}
            content_type = str(headers.get("content-type", ""))[:512]
            request_url = str(request.url)
        except Exception:
            self.reject()
            return False
        if (
            not is_navigation
            or resource_type != "document"
            or frame is not self.main_frame
            or frame.page is not self.page
            or request_url != self.exact_url
        ):
            self.reject()
            return False
        body = _request_body_bytes(request)
        payload_digest = (
            _canonical_form_data_payload_sha256(
                content_type=content_type,
                body=body,
                expected_cv_sha256=self.selected_cv_hash,
            )
            if body is not None
            else None
        )
        if payload_digest is None or not compare_digest(
            payload_digest,
            self.expected_payload_sha256,
        ):
            self.reject()
            return False
        self.possibly_sent = True
        self.completed.set()
        return True


_CAPTURE_FINAL_ACTION_JS = r"""
async (button, expected) => {
    const encoder = new TextEncoder();
    const toHex = buffer => Array.from(new Uint8Array(buffer))
        .map(byte => byte.toString(16).padStart(2, "0")).join("");
    const normalize = value => String(value).replace(/\r\n|\r|\n/g, "\r\n");
    const frame = value => {
        const normalized = normalize(value);
        return `${encoder.encode(normalized).length}:${normalized}`;
    };
    const visible = element => {
        for (
            let current = element;
            current instanceof HTMLElement;
            current = current.parentElement
        ) {
            const style = getComputedStyle(current);
            if (
                current.hidden
                || current.getAttribute("aria-hidden") === "true"
                || current.inert
                || current.getAttribute("aria-disabled") === "true"
                || style.display === "none"
                || ["hidden", "collapse"].includes(style.visibility)
                || Number(style.opacity) <= 0
                || style.pointerEvents === "none"
                || style.contentVisibility === "hidden"
            ) {
                return false;
            }
        }
        return element.getClientRects().length > 0
            && Array.from(element.getClientRects()).some(rect => rect.width > 0 && rect.height > 0);
    };
    const exactForm = () => {
        const forms = Array.from(document.querySelectorAll(expected.formSelector))
            .filter(form => (
                form instanceof HTMLFormElement
                && form.dataset.company === expected.company
                && form.dataset.publicId === expected.publicId
                && form.dataset.postingUuid === expected.postingUuid
            ));
        return forms.length === 1 ? forms[0] : null;
    };
    const form = exactForm();
    if (
        !(button instanceof HTMLButtonElement)
        || !form
        || button.form !== form
        || !button.isConnected
        || button.type !== "submit"
        || button.hasAttribute("form")
        || button.hasAttribute("formaction")
        || button.hasAttribute("formmethod")
        || button.hasAttribute("formenctype")
        || button.hasAttribute("name")
        || !visible(button)
        || button.disabled
        || form.action !== expected.action
        || form.method.toUpperCase() !== "POST"
        || form.enctype.toLowerCase() !== "multipart/form-data"
        || document.querySelectorAll(expected.buttonSelector).length !== 1
        || document.querySelectorAll(expected.confirmationSelector).length !== 0
    ) {
        return {valid: false};
    }
    const wrappers = Array.from(form.querySelectorAll(expected.wrapperSelector));
    if (wrappers.length !== expected.fields.length) {
        return {valid: false};
    }
    for (let index = 0; index < expected.fields.length; index += 1) {
        const binding = expected.fields[index];
        const wrapper = wrappers[index];
        if (
            wrapper.dataset.fieldId !== binding.fieldId
            || !visible(wrapper)
        ) {
            return {valid: false};
        }
        const controls = Array.from(wrapper.querySelectorAll("input,textarea,select"))
            .filter(control => control.type !== "hidden");
        let matches = false;
        if (binding.fieldType === "radio") {
            const checked = controls.filter(control => (
                control instanceof HTMLInputElement
                && control.type === "radio"
                && control.checked
            ));
            matches = checked.length === 1
                && binding.answer.kind === "scalar"
                && checked[0].value === binding.answer.value;
        } else if (binding.fieldType === "multi_select") {
            matches = controls.length === 1
                && controls[0] instanceof HTMLSelectElement
                && controls[0].multiple
                && binding.answer.kind === "multi"
                && JSON.stringify(Array.from(controls[0].selectedOptions)
                    .map(option => option.value).sort()) === JSON.stringify(binding.answer.value);
        } else if (binding.fieldType === "select") {
            matches = controls.length === 1
                && controls[0] instanceof HTMLSelectElement
                && !controls[0].multiple
                && binding.answer.kind === "scalar"
                && controls[0].value === binding.answer.value;
        } else if (["checkbox", "consent", "attestation"].includes(binding.fieldType)) {
            matches = controls.length === 1
                && controls[0] instanceof HTMLInputElement
                && controls[0].type === "checkbox"
                && binding.answer.kind === "bool"
                && controls[0].checked === binding.answer.value;
        } else if (binding.fieldType === "file") {
            matches = controls.length === 1
                && controls[0] instanceof HTMLInputElement
                && controls[0].type === "file"
                && controls[0].files.length === 1
                && binding.answer.kind === "file";
        } else {
            matches = controls.length === 1
                && ["INPUT", "TEXTAREA"].includes(controls[0].tagName)
                && binding.answer.kind === "scalar"
                && controls[0].value === binding.answer.value;
        }
        if (
            !matches
            || controls.some(control => control.disabled || !control.name)
        ) {
            return {valid: false};
        }
    }
    const markers = Array.from(document.querySelectorAll(expected.uploadMarkerSelector))
        .filter(marker => visible(marker))
        .filter(marker => (
            marker.dataset.uploadId === expected.uploadId
            && marker.dataset.fileName === expected.uploadName
            && marker.dataset.fileSha256 === expected.cvSha256
        ));
    if (markers.length !== 1 || !form.checkValidity()) {
        return {valid: false};
    }
    const disclosureMaterial = Array.from(document.querySelectorAll(expected.disclosureSelector))
        .filter(node => visible(node))
        .map(node => {
            const summaries = Array.from(node.querySelectorAll('[data-qa="disclosure-summary"]'))
                .filter(summary => visible(summary));
            const links = Array.from(node.querySelectorAll('a[data-qa="disclosure-link"][href]'))
                .filter(link => visible(link));
            return {
                id: node.dataset.disclosureId || "",
                kind: node.dataset.disclosureKind || "",
                source: node.dataset.disclosureSource || "",
                acknowledgement: node.dataset.acknowledgementFieldId || "",
                summary: summaries.length === 1
                    ? (summaries[0].textContent || "").trim().replace(/\s+/g, " ")
                    : "",
                href: links.length === 1 ? links[0].href : ""
            };
        });
    if (JSON.stringify(disclosureMaterial) !== expected.disclosureMaterial) {
        return {valid: false};
    }
    const material = [expected.commitmentVersion];
    let fileCount = 0;
    let entryCount = 0;
    for (const [name, entry] of new FormData(form).entries()) {
        entryCount += 1;
        if (!name || entryCount > expected.maxEntries) {
            return {valid: false};
        }
        if (typeof entry === "string") {
            material.push(`S${frame(name)}${frame(entry)}`);
            continue;
        }
        fileCount += 1;
        if (
            fileCount > 1
            || entry.size <= 0
            || entry.size > expected.maxResumeBytes
        ) {
            return {valid: false};
        }
        const digest = toHex(await crypto.subtle.digest("SHA-256", await entry.arrayBuffer()));
        if (digest !== expected.cvSha256) {
            return {valid: false};
        }
        material.push(
            `F${frame(name)}${frame(entry.name)}${frame(entry.type.toLowerCase())}`
            + `${frame(String(entry.size))}${frame(digest)}`
        );
    }
    if (entryCount === 0 || fileCount !== 1) {
        return {valid: false};
    }
    const payloadDigest = toHex(
        await crypto.subtle.digest("SHA-256", encoder.encode(material.join("")))
    );
    const finalHtml = document.documentElement.outerHTML;
    const finalDomDigest = toHex(
        await crypto.subtle.digest("SHA-256", encoder.encode(finalHtml))
    );
    if (finalDomDigest !== expected.domDigest) {
        return {valid: false};
    }
    const actionability = [];
    for (
        let current = button;
        current instanceof HTMLElement;
        current = current.parentElement
    ) {
        const style = getComputedStyle(current);
        actionability.push({
            tag: current.tagName.toLowerCase(),
            disabled: current.hasAttribute("disabled"),
            ariaDisabled: current.getAttribute("aria-disabled") === "true",
            inert: current.inert || current.hasAttribute("inert"),
            hidden: current.hidden,
            ariaHidden: current.getAttribute("aria-hidden") === "true",
            display: style.display,
            visibility: style.visibility,
            opacity: style.opacity,
            pointerEvents: style.pointerEvents,
            contentVisibility: style.contentVisibility,
            hasArea: current.getClientRects().length > 0
                && Array.from(current.getClientRects())
                    .some(rect => rect.width > 0 && rect.height > 0)
        });
    }
    const observerState = {mutations: 0};
    const observer = new MutationObserver(records => {
        observerState.mutations += records.length;
    });
    observer.observe(document.documentElement, {
        attributes: true,
        childList: true,
        characterData: true,
        subtree: true
    });
    globalThis.__jobAgentSmartRecruitersFinal = {
        button,
        form,
        observer,
        observerState,
        disclosureMaterial: expected.disclosureMaterial,
        fields: expected.fields,
        uploadId: expected.uploadId,
        uploadName: expected.uploadName,
        payloadDigest
    };
    return {
        valid: true,
        action: form.action,
        actionability: JSON.stringify(actionability),
        disclosureMaterial: expected.disclosureMaterial,
        payloadDigest,
        mutationCount: 0
    };
}
"""


_ATOMIC_FINAL_ACTION_JS = r"""
(button, expected) => {
    const retained = globalThis.__jobAgentSmartRecruitersFinal;
    const visible = element => {
        for (
            let current = element;
            current instanceof HTMLElement;
            current = current.parentElement
        ) {
            const style = getComputedStyle(current);
            if (
                current.hidden
                || current.getAttribute("aria-hidden") === "true"
                || current.inert
                || current.getAttribute("aria-disabled") === "true"
                || style.display === "none"
                || ["hidden", "collapse"].includes(style.visibility)
                || Number(style.opacity) <= 0
                || style.pointerEvents === "none"
                || style.contentVisibility === "hidden"
            ) {
                return false;
            }
        }
        return element.getClientRects().length > 0
            && Array.from(element.getClientRects()).some(rect => rect.width > 0 && rect.height > 0);
    };
    const currentDisclosures = () => JSON.stringify(
        Array.from(document.querySelectorAll(expected.disclosureSelector))
            .filter(node => visible(node))
            .map(node => {
                const summaries = Array.from(
                    node.querySelectorAll('[data-qa="disclosure-summary"]')
                ).filter(summary => visible(summary));
                const links = Array.from(
                    node.querySelectorAll('a[data-qa="disclosure-link"][href]')
                ).filter(link => visible(link));
                return {
                    id: node.dataset.disclosureId || "",
                    kind: node.dataset.disclosureKind || "",
                    source: node.dataset.disclosureSource || "",
                    acknowledgement: node.dataset.acknowledgementFieldId || "",
                    summary: summaries.length === 1
                        ? (summaries[0].textContent || "").trim().replace(/\s+/g, " ")
                        : "",
                    href: links.length === 1 ? links[0].href : ""
                };
            })
    );
    const answersMatch = () => {
        const wrappers = Array.from(
            retained.form.querySelectorAll(expected.wrapperSelector)
        );
        if (wrappers.length !== expected.fields.length) {
            return false;
        }
        return expected.fields.every((binding, index) => {
            const wrapper = wrappers[index];
            if (wrapper.dataset.fieldId !== binding.fieldId || !visible(wrapper)) {
                return false;
            }
            const controls = Array.from(wrapper.querySelectorAll("input,textarea,select"))
                .filter(control => control.type !== "hidden");
            if (controls.some(control => control.disabled || !control.name)) {
                return false;
            }
            if (binding.fieldType === "radio") {
                const checked = controls.filter(control => (
                    control instanceof HTMLInputElement
                    && control.type === "radio"
                    && control.checked
                ));
                return checked.length === 1
                    && binding.answer.kind === "scalar"
                    && checked[0].value === binding.answer.value;
            }
            if (binding.fieldType === "multi_select") {
                return controls.length === 1
                    && controls[0] instanceof HTMLSelectElement
                    && controls[0].multiple
                    && binding.answer.kind === "multi"
                    && JSON.stringify(Array.from(controls[0].selectedOptions)
                        .map(option => option.value).sort())
                        === JSON.stringify(binding.answer.value);
            }
            if (binding.fieldType === "select") {
                return controls.length === 1
                    && controls[0] instanceof HTMLSelectElement
                    && !controls[0].multiple
                    && binding.answer.kind === "scalar"
                    && controls[0].value === binding.answer.value;
            }
            if (["checkbox", "consent", "attestation"].includes(binding.fieldType)) {
                return controls.length === 1
                    && controls[0] instanceof HTMLInputElement
                    && controls[0].type === "checkbox"
                    && binding.answer.kind === "bool"
                    && controls[0].checked === binding.answer.value;
            }
            if (binding.fieldType === "file") {
                return controls.length === 1
                    && controls[0] instanceof HTMLInputElement
                    && controls[0].type === "file"
                    && controls[0].files.length === 1
                    && binding.answer.kind === "file";
            }
            return controls.length === 1
                && ["INPUT", "TEXTAREA"].includes(controls[0].tagName)
                && binding.answer.kind === "scalar"
                && controls[0].value === binding.answer.value;
        });
    };
    const markers = Array.from(document.querySelectorAll(expected.uploadMarkerSelector))
        .filter(marker => visible(marker))
        .filter(marker => (
            marker.dataset.uploadId === expected.uploadId
            && marker.dataset.fileName === expected.uploadName
            && marker.dataset.fileSha256 === expected.cvSha256
        ));
    const valid = Boolean(
        retained
        && retained.button === button
        && retained.form === button.form
        && retained.observerState.mutations === 0
        && retained.payloadDigest === expected.payloadDigest
        && button instanceof HTMLButtonElement
        && button.isConnected
        && button.type === "submit"
        && !button.hasAttribute("form")
        && !button.hasAttribute("formaction")
        && !button.hasAttribute("formmethod")
        && !button.hasAttribute("formenctype")
        && !button.hasAttribute("name")
        && !button.disabled
        && visible(button)
        && button.form.action === expected.action
        && button.form.method.toUpperCase() === "POST"
        && button.form.enctype.toLowerCase() === "multipart/form-data"
        && button.form.checkValidity()
        && document.querySelectorAll(expected.formSelector).length === 1
        && document.querySelectorAll(expected.buttonSelector).length === 1
        && document.querySelectorAll(expected.confirmationSelector).length === 0
        && markers.length === 1
        && currentDisclosures() === retained.disclosureMaterial
        && answersMatch()
    );
    retained.observer.disconnect();
    if (!valid) {
        return {released: false};
    }
    retained.form.requestSubmit(button);
    return {released: true};
}
"""


class PlaywrightSmartRecruitersCandidateSession:
    """One candidate page backed by one dedicated local browser profile."""

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._lease: PortalSessionLease | None = None
        self._guard: SmartRecruitersNetworkGuard | None = None
        self._gate: _OneShotCandidatePostGate | None = None
        self._attachment: SmartRecruitersAttachmentProof | None = None
        self._upload_id: str | None = None
        self._upload_name: str | None = None
        self._prepared_proof: SmartRecruitersFinalActionProof | None = None
        self._prepared_handle: Any = None
        self._prepared_expected: dict[str, object] | None = None
        self._clicked = False

    def _require_page(self) -> Any:
        if self._page is None:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        return self._page

    async def navigate(self, url: str) -> None:
        if self._page is not None:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        guard = SmartRecruitersNetworkGuard(url)
        await asyncio.to_thread(guard.require_allowed_url, url, main_frame=True)
        self._guard = guard
        try:
            portal = portal_session_for_url(
                url,
                self._settings.portal_browser_profile_root,
            )
        except PortalSessionError as exc:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        if not portal.ready:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.SESSION_EXPIRED)
        lease = PortalSessionLease(
            portal,
            stale_minutes=self._settings.portal_session_lock_minutes,
        )
        try:
            lease.acquire()
            from playwright.async_api import async_playwright
        except (ImportError, PortalSessionError) as exc:
            lease.release()
            raise SmartRecruitersAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        self._lease = lease
        try:
            self._playwright = await async_playwright().start()
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(portal.profile_dir),
                headless=self._settings.portal_browser_headless,
                viewport={"width": 1280, "height": 900},
                service_workers="block",
            )
            route_web_socket = getattr(self._context, "route_web_socket", None)
            if not callable(route_web_socket):
                raise SmartRecruitersAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
            await route_web_socket("**/*", self._block_web_socket)
            await self._context.route("**/*", self._guard_request)
            self._page = (
                self._context.pages[0] if self._context.pages else await self._context.new_page()
            )
            await self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=_NAVIGATION_TIMEOUT_MS,
            )
            await self._assert_current_url()
        except Exception as exc:
            await self.close()
            if isinstance(exc, SmartRecruitersAdapterBlockedError):
                raise
            raise SmartRecruitersAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc

    @staticmethod
    async def _block_web_socket(web_socket: Any) -> None:
        await web_socket.close(code=1008, reason="browser transport disabled")

    def _allowed_final_action_url(self) -> str | None:
        gate = self._gate
        return gate.exact_url if gate is not None and gate.possibly_sent else None

    async def _guard_request(self, route: Any, request: Any) -> None:
        guard = self._guard
        gate = self._gate
        if guard is None:
            if gate is not None:
                gate.reject()
            await route.abort("blockedbyclient")
            return
        try:
            method = str(request.method).strip().upper()
            is_navigation = request.is_navigation_request() is True
            scheme = urlsplit(request.url).scheme.casefold()
        except Exception:
            method = ""
            is_navigation = False
            scheme = ""
        if scheme in {"data", "blob"} and not is_navigation:
            await route.continue_()
            return
        try:
            await asyncio.to_thread(
                guard.require_allowed_url,
                request.url,
                main_frame=is_navigation and method in _OneShotCandidatePostGate._SAFE_METHODS,
                allow_exact_final_action=self._allowed_final_action_url(),
            )
        except SmartRecruitersAdapterBlockedError:
            if gate is not None:
                gate.reject()
            await route.abort("blockedbyclient")
            return
        if gate is None and method not in _OneShotCandidatePostGate._SAFE_METHODS:
            await route.abort("blockedbyclient")
            return
        if gate is not None:
            decision = gate.evaluate(request)
            if decision is False:
                await route.abort("blockedbyclient")
                return
        await route.continue_()

    async def _assert_current_url(self) -> None:
        page = self._require_page()
        guard = self._guard
        if guard is None:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        await asyncio.to_thread(
            guard.require_allowed_url,
            page.url,
            main_frame=True,
            allow_exact_final_action=self._allowed_final_action_url(),
        )

    async def open_candidate_form(
        self,
        identity: SmartRecruitersCandidateIdentity,
    ) -> None:
        page = self._require_page()
        links = page.locator('a[data-qa="apply-link"][href]')
        visible: list[Any] = []
        for index in range(await links.count()):
            link = links.nth(index)
            if await link.is_visible():
                visible.append(link)
        if len(visible) != 1:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        href = await visible[0].get_attribute("href")
        if href != identity.apply_url:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
        await visible[0].click(timeout=_ACTION_TIMEOUT_MS)
        await page.wait_for_load_state("domcontentloaded")
        await self._assert_current_url()

    async def snapshot(self) -> SmartRecruitersBrowserSnapshot:
        page = self._require_page()
        await self._assert_current_url()
        return SmartRecruitersBrowserSnapshot(
            html=await page.content(),
            url=page.url,
            locale=await page.locator("html").get_attribute("lang") or "en",
        )

    async def _matching_upload_marker(
        self,
        *,
        upload_name: str,
        expected_sha256: str,
    ) -> str | None:
        page = self._require_page()
        markers = page.locator(_UPLOAD_MARKER_SELECTOR)
        matches: list[str] = []
        for index in range(await markers.count()):
            marker = markers.nth(index)
            if not await marker.is_visible():
                continue
            marker_id = (await marker.get_attribute("data-upload-id") or "").strip()
            name = (await marker.get_attribute("data-file-name") or "").strip()
            digest = (await marker.get_attribute("data-file-sha256") or "").strip().casefold()
            if (
                marker_id
                and name.casefold() == upload_name.casefold()
                and compare_digest(digest, expected_sha256)
            ):
                matches.append(marker_id)
        unique = tuple(dict.fromkeys(matches))
        return unique[0] if len(unique) == 1 else None

    async def ensure_resume_attachment(
        self,
        *,
        resume_bytes: bytes,
        cv_id: str,
        expected_sha256: str,
    ) -> SmartRecruitersAttachmentProof:
        page = self._require_page()
        if not compare_digest(hashlib.sha256(resume_bytes).hexdigest(), expected_sha256):
            raise SmartRecruitersAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        extension, media_type = _resume_payload_kind(resume_bytes)
        upload_name_digest = hashlib.sha256(
            token_bytes(32) + bytes.fromhex(expected_sha256)
        ).hexdigest()
        upload_name = f"resume-{upload_name_digest[:24]}.{extension}"
        inputs = page.locator(
            f'{SMARTRECRUITERS_FORM_SELECTOR} input[type="file"][data-qa="resume-upload"]'
        )
        if await inputs.count() != 1:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        try:
            await inputs.first.set_input_files(
                {
                    "name": upload_name,
                    "mimeType": media_type,
                    "buffer": resume_bytes,
                },
                timeout=_ACTION_TIMEOUT_MS,
            )
        except Exception as exc:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED) from exc
        marker_id: str | None = None
        for _poll in range(20):
            marker_id = await self._matching_upload_marker(
                upload_name=upload_name,
                expected_sha256=expected_sha256,
            )
            if marker_id is not None:
                break
            await page.wait_for_timeout(100)
        if marker_id is None:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        control_sha256 = _sha(f"{marker_id}|{upload_name}|{expected_sha256}")
        proof = SmartRecruitersAttachmentProof(
            cv_id=cv_id,
            cv_sha256=expected_sha256,
            upload_complete=True,
            receipt_sha256=_sha(f"smartrecruiters-upload-v1|{control_sha256}"),
            resume_control_sha256=control_sha256,
        )
        self._attachment = proof
        self._upload_id = marker_id
        self._upload_name = upload_name
        return proof

    async def verify_resume_attachment(
        self,
        *,
        cv_id: str,
        expected_sha256: str,
    ) -> SmartRecruitersAttachmentProof:
        proof = self._attachment
        upload_id = self._upload_id
        upload_name = self._upload_name
        if (
            proof is not None
            and upload_id is not None
            and upload_name is not None
            and proof.matches(cv_id=cv_id, cv_sha256=expected_sha256)
            and await self._matching_upload_marker(
                upload_name=upload_name,
                expected_sha256=expected_sha256,
            )
            == upload_id
        ):
            return proof
        return SmartRecruitersAttachmentProof(
            cv_id=cv_id,
            cv_sha256=expected_sha256,
            upload_complete=False,
        )

    async def _exact_wrapper(self, field_id: str) -> Any:
        page = self._require_page()
        wrappers = page.locator(_FIELD_WRAPPER_SELECTOR)
        matches = []
        for index in range(await wrappers.count()):
            wrapper = wrappers.nth(index)
            if await wrapper.get_attribute("data-field-id") == field_id:
                matches.append(wrapper)
        if len(matches) != 1 or not await matches[0].is_visible():
            raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
        return matches[0]

    async def fill(self, decisions: tuple[AnswerDecisionV1, ...]) -> None:
        for decision in decisions:
            if decision.disposition is not AnswerDisposition.RESOLVED:
                raise SmartRecruitersAdapterBlockedError(ReasonCode.REQUIRED_FIELD_UNKNOWN)
            wrapper = await self._exact_wrapper(decision.field_id)
            controls = wrapper.locator("input:not([type=hidden]),textarea,select")
            value = decision.value
            control_type = (
                (await wrapper.get_attribute("data-control-kind") or "").strip().casefold()
            )
            try:
                if value == VERIFIED_ATTACHMENT_SENTINEL:
                    continue
                if await controls.count() > 1:
                    radios = wrapper.locator('input[type="radio"]')
                    matches = []
                    for index in range(await radios.count()):
                        radio = radios.nth(index)
                        if await radio.get_attribute("value") == str(value):
                            matches.append(radio)
                    if len(matches) != 1:
                        raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
                    await matches[0].check()
                else:
                    control = controls.first
                    tag = await control.evaluate("(node) => node.tagName.toLowerCase()")
                    input_type = (await control.get_attribute("type") or "").casefold()
                    if tag == "select":
                        await control.select_option(
                            value=list(value) if isinstance(value, tuple) else str(value)
                        )
                    elif input_type == "checkbox" or control_type in {
                        "consent",
                        "attestation",
                    }:
                        await (control.check() if value is True else control.uncheck())
                    else:
                        await control.fill(str(value))
            except SmartRecruitersAdapterBlockedError:
                raise
            except Exception as exc:
                raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED) from exc

    async def prepare_final_action(
        self,
        *,
        identity: SmartRecruitersResolvedIdentity,
        fields: tuple[FormFieldV1, ...],
        disclosures: tuple[FormDisclosureV1, ...],
        decisions: tuple[AnswerDecisionV1, ...],
        form_fingerprint: str,
        attached_cv_sha256: str,
    ) -> SmartRecruitersFinalActionProof:
        page = self._require_page()
        attachment = self._attachment
        upload_id = self._upload_id
        upload_name = self._upload_name
        if (
            attachment is None
            or upload_id is None
            or upload_name is None
            or not attachment.matches(
                cv_id=attachment.cv_id,
                cv_sha256=attached_cv_sha256,
            )
            or attachment.resume_control_sha256 is None
        ):
            raise SmartRecruitersAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        await self._assert_current_url()
        try:
            captured_dom = await page.evaluate(_CAPTURE_DOM_SNAPSHOT_JS)
        except Exception as exc:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.SELECTOR_DRIFT) from exc
        if (
            not isinstance(captured_dom, dict)
            or not isinstance(captured_dom.get("html"), str)
            or not isinstance(captured_dom.get("digest"), str)
            or len(captured_dom["digest"]) != 64
            or not isinstance(captured_dom.get("locale"), str)
        ):
            raise SmartRecruitersAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        snapshot = SmartRecruitersBrowserSnapshot(
            html=captured_dom["html"],
            url=page.url,
            locale=captured_dom["locale"] or "en",
        )
        try:
            observed_identity = resolve_smartrecruiters_posting_identity(
                snapshot.html,
                identity.candidate,
            )
        except SmartRecruitersIdentityError as exc:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED) from exc
        if observed_identity != identity:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
        binding = smartrecruiters_v1_final_action_binding(
            snapshot.html,
            identity=identity,
            fields=fields,
            disclosures=disclosures,
        )
        if not compare_digest(
            smartrecruiters_v1_form_fingerprint(
                identity,
                fields,
                disclosures,
                binding,
            ),
            form_fingerprint,
        ):
            raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
        buttons = page.locator(_FINAL_BUTTON_SELECTOR)
        if await buttons.count() != 1 or not await buttons.first.is_visible():
            raise SmartRecruitersAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        handle = await buttons.first.element_handle()
        if handle is None:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        action = (
            f"https://{identity.candidate.hostname}/candidate-experience/postings/"
            f"{identity.posting_uuid}/applications"
        )
        field_bindings = _field_bindings(
            fields,
            decisions,
            selected_cv_hash=attached_cv_sha256,
        )
        disclosure_material = smartrecruiters_v1_disclosure_runtime_material(
            snapshot.html,
            identity=identity,
            disclosures=disclosures,
        )
        expected: dict[str, object] = {
            "formSelector": SMARTRECRUITERS_FORM_SELECTOR,
            "buttonSelector": _FINAL_BUTTON_SELECTOR,
            "confirmationSelector": SMARTRECRUITERS_CONFIRMATION_SELECTOR,
            "wrapperSelector": _FIELD_WRAPPER_SELECTOR,
            "disclosureSelector": _DISCLOSURE_SELECTOR,
            "uploadMarkerSelector": _UPLOAD_MARKER_SELECTOR,
            "company": identity.candidate.company,
            "publicId": identity.candidate.public_id,
            "postingUuid": identity.posting_uuid,
            "action": action,
            "fields": field_bindings,
            "disclosureMaterial": disclosure_material,
            "uploadId": upload_id,
            "uploadName": upload_name,
            "cvSha256": attached_cv_sha256,
            "commitmentVersion": _FORM_DATA_COMMITMENT_VERSION,
            "maxEntries": _MAX_FORM_DATA_ENTRIES,
            "maxResumeBytes": _MAX_RESUME_FORM_BYTES,
            "domDigest": captured_dom["digest"],
        }
        try:
            capture = await handle.evaluate(_CAPTURE_FINAL_ACTION_JS, expected)
        except Exception as exc:
            raise SmartRecruitersAdapterBlockedError(ReasonCode.SELECTOR_DRIFT) from exc
        if (
            not isinstance(capture, dict)
            or capture.get("valid") is not True
            or capture.get("action") != action
            or not isinstance(capture.get("actionability"), str)
            or capture.get("disclosureMaterial") != disclosure_material
            or not isinstance(capture.get("payloadDigest"), str)
            or len(capture["payloadDigest"]) != 64
            or capture.get("mutationCount") != 0
        ):
            raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
        proof = SmartRecruitersFinalActionProof(
            identity_sha256=_sha(identity.stable_key),
            action_url_sha256=_sha(action),
            form_fingerprint=form_fingerprint,
            method="POST",
            encoding="multipart/form-data",
            submitter_sha256=_sha(_FINAL_BUTTON_SELECTOR),
            actionability_sha256=_sha(capture["actionability"]),
            disclosures_sha256=smartrecruiters_disclosures_digest(disclosures),
            resume_control_sha256=attachment.resume_control_sha256,
            attached_cv_sha256=attached_cv_sha256,
            payload_commitment_sha256=capture["payloadDigest"],
            user_field_count=len(fields),
            disclosure_count=len(disclosures),
            precommit_mutation_count=0,
        )
        self._prepared_proof = proof
        self._prepared_handle = handle
        self._prepared_expected = expected
        return proof

    async def click_final_action(
        self,
        proof: SmartRecruitersFinalActionProof,
    ) -> None:
        page = self._require_page()
        handle = self._prepared_handle
        expected = self._prepared_expected
        if (
            self._clicked
            or handle is None
            or expected is None
            or proof != self._prepared_proof
            or self._guard is None
        ):
            raise SmartRecruitersAdapterBlockedError(ReasonCode.PERMIT_REPLAYED)
        action = str(expected["action"])
        gate = _OneShotCandidatePostGate(
            page=page,
            exact_url=action,
            expected_payload_sha256=proof.payload_commitment_sha256,
            selected_cv_hash=proof.attached_cv_sha256,
        )
        self._gate = gate
        self._clicked = True
        expected["payloadDigest"] = proof.payload_commitment_sha256
        try:
            # This is the final browser call. The JavaScript rechecks every
            # binding synchronously and immediately invokes requestSubmit;
            # there is no DOM mutation or await between those two operations.
            result = await handle.evaluate(_ATOMIC_FINAL_ACTION_JS, expected)
        except Exception as exc:
            # The gate is armed and the retained final browser task may have
            # reached requestSubmit before its context/result was lost.
            raise SmartRecruitersFinalActionAmbiguousError(
                ReasonCode.FINAL_ACTION_UNCONFIRMED.value
            ) from exc
        if result == {"released": False}:
            if gate.completed.is_set():
                raise SmartRecruitersFinalActionAmbiguousError(
                    ReasonCode.FINAL_ACTION_UNCONFIRMED.value
                )
            gate.reject()
            raise SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED)
        if result != {"released": True}:
            raise SmartRecruitersFinalActionAmbiguousError(
                ReasonCode.FINAL_ACTION_UNCONFIRMED.value
            )
        # The native irreversible primitive has now been invoked. Even if the
        # route observer sees no matching request, it is no longer safe to
        # classify the outcome as pre-send or retryable.
        try:
            await asyncio.wait_for(
                gate.completed.wait(),
                timeout=_ACTION_TIMEOUT_MS / 1000,
            )
        except TimeoutError as exc:
            raise SmartRecruitersFinalActionAmbiguousError(
                ReasonCode.FINAL_ACTION_UNCONFIRMED.value
            ) from exc
        if gate.rejected or not gate.possibly_sent:
            raise SmartRecruitersFinalActionAmbiguousError(
                ReasonCode.FINAL_ACTION_UNCONFIRMED.value
            )
        try:
            # The request observer fires before the response and destination
            # document necessarily settle. Wait for bounded, visible
            # confirmation markup before the adapter snapshots evidence.
            await page.locator(SMARTRECRUITERS_CONFIRMATION_SELECTOR).wait_for(
                state="visible",
                timeout=_POST_ACTION_SETTLE_TIMEOUT_MS,
            )
        except Exception as exc:
            raise SmartRecruitersFinalActionAmbiguousError(
                ReasonCode.FINAL_ACTION_UNCONFIRMED.value
            ) from exc

    async def confirmation_reference(
        self,
        identity: SmartRecruitersResolvedIdentity,
    ) -> str | None:
        page = self._require_page()
        nodes = page.locator(SMARTRECRUITERS_CONFIRMATION_SELECTOR)
        matches: list[str] = []
        for index in range(await nodes.count()):
            node = nodes.nth(index)
            if (
                await node.is_visible()
                and (await node.get_attribute("data-posting-uuid") or "").strip().casefold()
                == identity.posting_uuid
            ):
                reference = (await node.get_attribute("data-application-id") or "").strip()
                if 6 <= len(reference) <= 160 and all(
                    character.isalnum() or character in "_.:-" for character in reference
                ):
                    matches.append(reference)
        return matches[0] if len(matches) == 1 else None

    async def close(self) -> None:
        retained = self._page
        if retained is not None:
            try:
                await retained.evaluate(
                    """() => {
                        const retained = globalThis.__jobAgentSmartRecruitersFinal;
                        if (retained?.observer) retained.observer.disconnect();
                        delete globalThis.__jobAgentSmartRecruitersFinal;
                    }"""
                )
            except Exception:
                pass
        self._page = None
        try:
            if self._context is not None:
                await self._context.close()
        finally:
            self._context = None
            try:
                if self._playwright is not None:
                    await self._playwright.stop()
            finally:
                self._playwright = None
                if self._lease is not None:
                    self._lease.release()
                    self._lease = None


def playwright_smartrecruiters_browser_factory(
    _candidate_url: str,
) -> SmartRecruitersCandidateSession:
    """Return a lazy local candidate session; no browser is launched here."""

    return PlaywrightSmartRecruitersCandidateSession()
