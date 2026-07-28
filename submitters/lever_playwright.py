"""Private local Playwright transport for the Lever browser-v1 adapter.

The transport never reads Chrome or Edge credential stores. It launches only
an isolated, operator-bootstrapped profile. Before a live-qualified release,
the central descriptor prevents this transport from reaching its final-action
method.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
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
from core.portal_sessions import (
    PortalSessionError,
    PortalSessionLease,
    portal_session_for_url,
)
from core.submission_domain import (
    VERIFIED_ATTACHMENT_SENTINEL,
    AnswerDecisionV1,
    AnswerDisposition,
    FieldType,
    FormFieldV1,
    ReasonCode,
)
from submitters.lever_identity import (
    LeverIdentityError,
    LeverPostingIdentity,
    parse_lever_posting_identity,
)
from submitters.lever_v1 import (
    LEVER_FORM_SELECTOR,
    LeverAdapterBlockedError,
    LeverAttachmentProof,
    LeverBrowserSnapshot,
    LeverCandidateSession,
    LeverFinalActionProof,
    observe_lever_v1_fields,
)

_NAVIGATION_TIMEOUT_MS = 45_000
_ACTION_TIMEOUT_MS = 8_000
_MAX_FORM_BODY_BYTES = 24 * 1024 * 1024
_MAX_STRING_BYTES = 1024 * 1024
_MAX_ENTRIES = 256
_MAX_FIELD_NAME_BYTES = 256
_MAX_FILENAME_BYTES = 512
_MAX_MEDIA_TYPE_BYTES = 200
_MAX_ACTIONABILITY_STATE_BYTES = 64 * 1024
_FORM_DATA_COMMITMENT_VERSION = b"lever-formdata-v1;"
_SYSTEM_CONTROL_NAMES = frozenset({"authenticity_token", "csrf_token", "_csrf", "utf8"})


class LeverFinalActionAmbiguousError(RuntimeError):
    """The final request may have left the browser and must not be retried."""


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
    raise LeverAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)


def _normalize_form_string(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


def _frame(value: str) -> bytes:
    encoded = _normalize_form_string(value).encode("utf-8")
    return str(len(encoded)).encode("ascii") + b":" + encoded


def _bounded_name(value: object, maximum: int) -> str | None:
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


def canonical_multipart_payload_sha256(
    *,
    content_type: str,
    body: bytes,
    expected_cv_sha256: str,
) -> str | None:
    """Hash a bounded outgoing multipart body without returning its values."""

    if (
        not body
        or len(body) > _MAX_FORM_BODY_BYTES
        or len(content_type.encode("utf-8", errors="ignore")) > 512
        or "\r" in content_type
        or "\n" in content_type
        or re.fullmatch(r"[0-9a-f]{64}", expected_cv_sha256 or "") is None
        or content_type.split(";", 1)[0].strip().casefold() != "multipart/form-data"
    ):
        return None
    try:
        encoded_type = content_type.encode("ascii", errors="strict")
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + encoded_type + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
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
    material: list[bytes] = [_FORM_DATA_COMMITMENT_VERSION]
    entry_count = 0
    string_bytes = 0
    file_count = 0
    for part in message.iter_parts():
        entry_count += 1
        if (
            entry_count > _MAX_ENTRIES
            or part.defects
            or part.is_multipart()
            or len(part.get_all("Content-Disposition", [])) != 1
            or len(part.get_all("Content-Type", [])) > 1
            or any(
                header.casefold() not in {"content-disposition", "content-type"}
                for header in part.keys()
            )
            or part.get_content_disposition() != "form-data"
            or part.get("Content-Transfer-Encoding") is not None
        ):
            return None
        name = _bounded_name(
            part.get_param("name", header="content-disposition"),
            _MAX_FIELD_NAME_BYTES,
        )
        payload = part.get_payload(decode=True)
        if name is None or not isinstance(payload, bytes):
            return None
        filename_value = part.get_filename()
        if filename_value is None:
            try:
                value = payload.decode("utf-8", errors="strict")
            except UnicodeError:
                return None
            normalized = _normalize_form_string(value)
            string_bytes += len(normalized.encode("utf-8"))
            if string_bytes > _MAX_STRING_BYTES:
                return None
            material.append(b"S" + _frame(name) + _frame(normalized))
            continue
        filename = _bounded_name(filename_value, _MAX_FILENAME_BYTES)
        media_type = _bounded_name(
            part.get_content_type().casefold(),
            _MAX_MEDIA_TYPE_BYTES,
        )
        file_count += 1
        if (
            filename is None
            or media_type is None
            or part.get("Content-Type") is None
            or file_count > 1
            or not payload
            or len(payload) > 20 * 1024 * 1024
        ):
            return None
        file_digest = hashlib.sha256(payload).hexdigest()
        if not compare_digest(file_digest, expected_cv_sha256):
            return None
        material.append(
            b"F"
            + _frame(name)
            + _frame(filename)
            + _frame(media_type)
            + _frame(str(len(payload)))
            + _frame(file_digest)
        )
    if entry_count == 0 or file_count != 1:
        return None
    return hashlib.sha256(b"".join(material)).hexdigest()


class LeverNetworkGuard:
    """Exact Lever host, posting, public-DNS, and mutation boundary."""

    def __init__(
        self,
        initial_url: str,
        *,
        resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
    ) -> None:
        try:
            self.identity = parse_lever_posting_identity(initial_url)
        except LeverIdentityError as exc:
            raise LeverAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        self._resolver = resolver
        self._dns_verified: dict[str, frozenset[str]] = {}
        self.precommit_mutation_count = 0

    @staticmethod
    def _https_hostname(url: str) -> str:
        try:
            parsed = urlsplit((url or "").strip())
            port = parsed.port
        except ValueError as exc:
            raise LeverAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        hostname = (parsed.hostname or "").lower()
        if (
            parsed.scheme.casefold() != "https"
            or hostname not in {"jobs.lever.co", "jobs.eu.lever.co"}
            or hostname != hostname.rstrip(".")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or port not in {None, 443}
            or any(ord(character) > 127 for character in hostname)
        ):
            raise LeverAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise LeverAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        return hostname

    def require_allowed_url(self, url: str, *, main_frame: bool) -> None:
        hostname = self._https_hostname(url)
        if hostname != self.identity.hostname:
            raise LeverAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        if main_frame:
            try:
                observed = parse_lever_posting_identity(url)
            except LeverIdentityError as exc:
                raise LeverAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
            if observed != self.identity:
                raise LeverAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        try:
            answers = self._resolver(hostname, 443, 0, socket.SOCK_STREAM)
        except OSError as exc:
            raise LeverAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        addresses = {
            str(answer[4][0]).split("%", 1)[0]
            for answer in answers
            if len(answer) > 4 and answer[4]
        }
        if not addresses:
            raise LeverAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        try:
            resolved = tuple(ipaddress.ip_address(address) for address in addresses)
        except ValueError as exc:
            raise LeverAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        if any(not address.is_global for address in resolved):
            raise LeverAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        canonical_addresses = frozenset(str(address) for address in resolved)
        previous = self._dns_verified.get(hostname)
        if previous is not None and previous != canonical_addresses:
            raise LeverAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        self._dns_verified[hostname] = canonical_addresses


_FORM_PROOF_SCRIPT = r"""
async ({
    identity,
    fields,
    decisions,
    formFingerprint,
    cvSha256,
    expectedActionabilityDigest,
    expectedActionabilityState,
    release
}) => {
    const encoder = new TextEncoder();
    const toHex = value => Array.from(new Uint8Array(value))
        .map(byte => byte.toString(16).padStart(2, "0")).join("");
    const sha = async value => toHex(await crypto.subtle.digest(
        "SHA-256", encoder.encode(value)
    ));
    const secureEqual = (left, right) => {
        if (typeof left !== "string" || typeof right !== "string" || left.length !== right.length) {
            return false;
        }
        let result = 0;
        for (let index = 0; index < left.length; index += 1) {
            result |= left.charCodeAt(index) ^ right.charCodeAt(index);
        }
        return result === 0;
    };
    const visible = node => {
        let current = node;
        while (current instanceof HTMLElement) {
            const style = getComputedStyle(current);
            if (
                current.hidden
                || current.getAttribute("aria-hidden") === "true"
                || style.display === "none"
                || ["hidden", "collapse"].includes(style.visibility)
                || Number.parseFloat(style.opacity) <= 0
                || style.contentVisibility === "hidden"
            ) return false;
            current = current.parentElement;
        }
        return Boolean(node && node.isConnected && node.getClientRects().length > 0);
    };
    const actionableCapture = (element, expectedForm) => {
        if (
            !element
            || !expectedForm
            || !element.isConnected
            || !expectedForm.isConnected
            || element.ownerDocument !== document
            || expectedForm.ownerDocument !== document
            || element.form !== expectedForm
        ) return null;
        const chain = [];
        let current = element;
        while (current instanceof HTMLElement) {
            const style = getComputedStyle(current);
            const rect = current.getBoundingClientRect();
            const entry = {
                depth: chain.length,
                tag: current.tagName.toLowerCase(),
                disabledPseudo: current.matches(":disabled"),
                disabledAttribute: current.hasAttribute("disabled"),
                ariaDisabled: current.getAttribute("aria-disabled") === "true",
                inert: current.inert === true || current.hasAttribute("inert"),
                hidden: current.hidden === true || current.hasAttribute("hidden"),
                ariaHidden: current.getAttribute("aria-hidden") === "true",
                display: style.display,
                visibility: style.visibility,
                opacity: style.opacity,
                pointerEvents: style.pointerEvents,
                contentVisibility: style.contentVisibility,
                positiveRect: (
                    current.getClientRects().length > 0
                    && Number.isFinite(rect.width)
                    && Number.isFinite(rect.height)
                    && rect.width > 0
                    && rect.height > 0
                )
            };
            if (
                entry.disabledPseudo
                || entry.disabledAttribute
                || entry.ariaDisabled
                || entry.inert
                || entry.hidden
                || entry.ariaHidden
                || entry.display === "none"
                || ["hidden", "collapse"].includes(entry.visibility)
                || Number.parseFloat(entry.opacity) <= 0
                || entry.pointerEvents === "none"
                || entry.contentVisibility === "hidden"
                || !entry.positiveRect
            ) return null;
            chain.push(entry);
            if (current === document.documentElement) break;
            current = current.parentElement;
        }
        if (chain.length < 1 || current !== document.documentElement) return null;
        return chain;
    };
    const normalize = value => String(value).replace(/\r\n/g, "\n")
        .replace(/\r/g, "\n").replace(/\n/g, "\r\n");
    const frame = value => {
        const normalized = normalize(value);
        return `${encoder.encode(normalized).length}:${normalized}`;
    };
    const forms = Array.from(document.querySelectorAll(
        'form[data-qa="application-form"][data-posting-id][data-site]'
    )).filter(visible).filter(form =>
        (form.getAttribute("data-posting-id") || "").toLowerCase() === identity.postingId
        && (form.getAttribute("data-site") || "") === identity.site
    );
    if (forms.length !== 1 || !Array.isArray(fields) || !Array.isArray(decisions)) {
        return {valid: false};
    }
    const form = forms[0];
    const submits = Array.from(form.querySelectorAll(
        'button[data-qa="btn-submit"][type="submit"]'
    )).filter(visible);
    if (submits.length !== 1) return {valid: false};
    const submit = submits[0];
    const noExistingConfirmation = () => Array.from(document.querySelectorAll(
        'main[data-qa="application-confirmation"][data-application-id][data-posting-id]'
    )).filter(candidate =>
        (candidate.getAttribute("data-posting-id") || "").toLowerCase()
            === identity.postingId
    ).length === 0;
    const structureValid = () => {
        const currentForms = Array.from(document.querySelectorAll(
            'form[data-qa="application-form"][data-posting-id][data-site]'
        )).filter(visible).filter(candidate =>
            (candidate.getAttribute("data-posting-id") || "").toLowerCase()
                === identity.postingId
            && (candidate.getAttribute("data-site") || "") === identity.site
        );
        const currentSubmits = Array.from(form.querySelectorAll(
            'button[data-qa="btn-submit"][type="submit"]'
        )).filter(visible);
        return Boolean(
            currentForms.length === 1
            && currentForms[0] === form
            && currentSubmits.length === 1
            && currentSubmits[0] === submit
            && noExistingConfirmation()
            && form.isConnected
            && submit.isConnected
            && form.ownerDocument === document
            && submit.ownerDocument === document
            && submit.form === form
            && !submit.hasAttribute("form")
            && !submit.hasAttribute("formaction")
            && !submit.hasAttribute("formmethod")
            && !submit.hasAttribute("formenctype")
            && !submit.hasAttribute("name")
            && form.getAttribute("action") === identity.applyUrl
            && (form.getAttribute("method") || "").toLowerCase() === "post"
            && (form.getAttribute("enctype") || "").toLowerCase()
                === "multipart/form-data"
        );
    };
    if (!structureValid()) return {valid: false};
    const initialActionability = actionableCapture(submit, form);
    if (initialActionability === null) return {valid: false};
    const initialActionabilityState = JSON.stringify(initialActionability);
    const initialActionabilityDigest = await sha(initialActionabilityState);
    if (
        release === true
        && (
            typeof expectedActionabilityState !== "string"
            || encoder.encode(expectedActionabilityState).length > 65536
            || !/^[0-9a-f]{64}$/.test(String(expectedActionabilityDigest || ""))
            || !secureEqual(initialActionabilityDigest, expectedActionabilityDigest)
            || !secureEqual(initialActionabilityState, expectedActionabilityState)
        )
    ) return {valid: false};
    const visibleWrappers = Array.from(form.querySelectorAll(
        '[data-qa="application-field"][data-field-id]'
    )).filter(visible);
    const decisionMap = new Map(decisions.map(decision => [decision.fieldId, decision]));
    if (
        decisionMap.size !== decisions.length
        || fields.length !== decisions.length
        || visibleWrappers.length !== fields.length
        || fields.some(field => !decisionMap.has(field.fieldId))
    ) return {valid: false};
    const compactText = value => String(value || "").trim().replace(/\s+/g, " ");
    const nullableInt = value => {
        if (value === null) return null;
        if (!/^-?\d+$/.test(value)) return Number.NaN;
        return Number(value);
    };
    const nullableFloat = value => {
        if (value === null) return null;
        const number = Number(value);
        return Number.isFinite(number) ? number : Number.NaN;
    };
    const controlType = (wrapper, control) => {
        const kind = (wrapper.getAttribute("data-control-kind") || "").toLowerCase();
        if (kind === "consent" || kind === "attestation") return kind;
        if (control instanceof HTMLTextAreaElement) return "textarea";
        if (control instanceof HTMLSelectElement) {
            return control.multiple ? "multi_select" : "select";
        }
        const raw = (control.getAttribute("type") || "text").toLowerCase();
        return {
            checkbox: "checkbox",
            date: "date",
            email: "email",
            file: "file",
            number: "number",
            radio: "radio",
            tel: "phone",
            url: "url"
        }[raw] || "text";
    };
    const owners = new Map();
    let resumeControlName = "";
    for (let fieldIndex = 0; fieldIndex < fields.length; fieldIndex += 1) {
        const field = fields[fieldIndex];
        const wrapper = visibleWrappers[fieldIndex];
        if (
            (wrapper.getAttribute("data-field-id") || "") !== field.fieldId
            || field.position !== fieldIndex
            || ((wrapper.getAttribute("data-canonical-name") || null) !== field.canonicalName)
        ) return {valid: false};
        const labels = wrapper.querySelectorAll("label,legend,[data-qa='field-label']");
        if (labels.length < 1 || compactText(labels[0].textContent) !== field.label) {
            return {valid: false};
        }
        const controls = Array.from(wrapper.querySelectorAll("input,textarea,select"))
            .filter(control => (control.getAttribute("type") || "").toLowerCase() !== "hidden");
        const decision = decisionMap.get(field.fieldId);
        if (!decision || decision.disposition !== "resolved" || decision.value === null) {
            return {valid: false};
        }
        if (
            controls.length < 1
            || controls.some(control => !control.name || control.disabled || control.form !== form)
        ) return {valid: false};
        if (
            controlType(wrapper, controls[0]) !== field.fieldType
            || Boolean(controls[0].required || wrapper.getAttribute("aria-required") === "true")
                !== field.required
        ) return {valid: false};
        const control = controls[0];
        const constraints = {
            minLength: nullableInt(control.getAttribute("minlength")),
            maxLength: nullableInt(control.getAttribute("maxlength")),
            minValue: nullableFloat(control.getAttribute("min")),
            maxValue: nullableFloat(control.getAttribute("max")),
            pattern: control.getAttribute("pattern"),
            acceptedFileTypes: (control.getAttribute("accept") || "").split(",")
                .map(value => value.trim()).filter(Boolean).slice(0, 32),
            multiple: control.multiple === true
        };
        if (JSON.stringify(constraints) !== JSON.stringify(field.constraints)) {
            return {valid: false};
        }
        let observedOptions = [];
        if (["select", "multi_select"].includes(field.fieldType)) {
            observedOptions = Array.from(control.options).map((option, optionIndex) => ({
                optionId: option.getAttribute("data-option-id") || `option-${optionIndex}`,
                value: option.value.trim(),
                label: compactText(option.textContent),
                disabled: option.disabled
            })).filter(option => option.value);
        } else if (field.fieldType === "radio") {
            observedOptions = controls.map((option, optionIndex) => ({
                optionId: option.getAttribute("data-option-id") || `option-${optionIndex}`,
                value: option.value.trim(),
                label: compactText(option.getAttribute("data-option-label")),
                disabled: option.disabled
            })).filter(option => option.value);
        }
        if (JSON.stringify(observedOptions) !== JSON.stringify(field.options)) {
            return {valid: false};
        }
        const names = new Set(controls.map(control => control.name));
        if (names.size !== 1) return {valid: false};
        const name = controls[0].name;
        if (owners.has(name)) return {valid: false};
        const expectedValues = [];
        if (field.fieldType === "file") {
            if (
                controls.length !== 1
                || !(controls[0] instanceof HTMLInputElement)
                || controls[0].type !== "file"
                || decision.value !== "verified_attachment"
                || controls[0].files.length !== 1
            ) return {valid: false};
            const file = controls[0].files[0];
            const digest = toHex(await crypto.subtle.digest(
                "SHA-256", await file.arrayBuffer()
            ));
            if (!secureEqual(digest, cvSha256)) return {valid: false};
            expectedValues.push(file);
            resumeControlName = name;
        } else if (field.fieldType === "radio") {
            const checked = controls.filter(control => control.checked);
            if (checked.length !== 1 || String(decision.value) !== checked[0].value) {
                return {valid: false};
            }
            expectedValues.push(checked[0].value);
        } else if (["checkbox", "consent", "attestation"].includes(field.fieldType)) {
            if (
                controls.length !== 1
                || typeof decision.value !== "boolean"
                || controls[0].checked !== decision.value
            ) return {valid: false};
            if (controls[0].checked) expectedValues.push(controls[0].value);
        } else if (field.fieldType === "multi_select") {
            if (
                controls.length !== 1
                || !(controls[0] instanceof HTMLSelectElement)
                || !Array.isArray(decision.value)
            ) return {valid: false};
            const selected = Array.from(controls[0].selectedOptions).map(option => option.value);
            if (
                selected.length !== decision.value.length
                || selected.some((value, index) => value !== decision.value[index])
            ) return {valid: false};
            expectedValues.push(...selected);
        } else {
            if (controls.length !== 1) return {valid: false};
            const actual = controls[0].value;
            if (String(decision.value) !== actual) return {valid: false};
            expectedValues.push(actual);
        }
        owners.set(name, {expectedValues, index: 0});
    }
    if (!resumeControlName) return {valid: false};
    const allowedSystem = new Set(["authenticity_token", "csrf_token", "_csrf", "utf8"]);
    const seenSystem = new Set();
    const allNamed = Array.from(form.elements).filter(control => control.name);
    for (const control of allNamed) {
        if (owners.has(control.name)) continue;
        if (
            !allowedSystem.has(control.name)
            || seenSystem.has(control.name)
            || !(control instanceof HTMLInputElement)
            || control.type !== "hidden"
            || control.disabled
            || control.value.length < 1
            || encoder.encode(control.value).length > 4096
        ) return {valid: false};
        seenSystem.add(control.name);
    }
    let data;
    try {
        data = new FormData(form);
    } catch (_error) {
        return {valid: false};
    }
    let material = "lever-formdata-v1;";
    let entryCount = 0;
    let fileCount = 0;
    let stringBytes = 0;
    for (const [rawName, value] of data.entries()) {
        const name = String(rawName);
        entryCount += 1;
        if (
            entryCount > 256
            || !name
            || encoder.encode(name).length > 256
            || Array.from(name).some(character => character.charCodeAt(0) < 32)
        ) return {valid: false};
        const owner = owners.get(name);
        if (owner) {
            if (owner.index >= owner.expectedValues.length) return {valid: false};
            const expected = owner.expectedValues[owner.index];
            owner.index += 1;
            if (typeof expected === "string") {
                if (typeof value !== "string" || normalize(value) !== normalize(expected)) {
                    return {valid: false};
                }
            } else if (
                !(value instanceof File)
                || !(expected instanceof File)
                || value.name !== expected.name
                || value.size !== expected.size
                || (value.type || "application/octet-stream").toLowerCase()
                    !== (expected.type || "application/octet-stream").toLowerCase()
            ) return {valid: false};
        } else if (
            !seenSystem.has(name)
            || typeof value !== "string"
        ) return {valid: false};
        if (typeof value === "string") {
            const normalized = normalize(value);
            stringBytes += encoder.encode(normalized).length;
            if (stringBytes > 1048576) return {valid: false};
            material += `S${frame(name)}${frame(normalized)}`;
        } else {
            fileCount += 1;
            if (
                fileCount !== 1
                || value.size < 1
                || value.size > 20971520
                || encoder.encode(value.name).length > 512
                || encoder.encode(value.type || "application/octet-stream").length > 200
            ) return {valid: false};
            const fileDigest = toHex(await crypto.subtle.digest(
                "SHA-256", await value.arrayBuffer()
            ));
            if (!secureEqual(fileDigest, cvSha256)) return {valid: false};
            material += `F${frame(name)}${frame(value.name)}${
                frame((value.type || "application/octet-stream").toLowerCase())
            }${frame(String(value.size))}${frame(fileDigest)}`;
        }
    }
    if (
        entryCount < 1
        || fileCount !== 1
        || Array.from(owners.values()).some(owner => owner.index !== owner.expectedValues.length)
        || !form.checkValidity()
    ) return {valid: false};
    const immediateActionability = actionableCapture(submit, form);
    if (immediateActionability === null || !structureValid()) return {valid: false};
    const immediateActionabilityState = JSON.stringify(immediateActionability);
    const immediateActionabilityDigest = await sha(immediateActionabilityState);
    if (
        !secureEqual(initialActionabilityDigest, immediateActionabilityDigest)
        || (
            release === true
            && (
                !secureEqual(immediateActionabilityDigest, expectedActionabilityDigest)
                || !secureEqual(immediateActionabilityState, expectedActionabilityState)
            )
        )
    ) return {valid: false};
    const result = {
        valid: true,
        payloadDigest: await sha(material),
        identityDigest: await sha(`${identity.hostname}/${identity.site}/${identity.postingId}`),
        actionDigest: await sha(identity.applyUrl),
        submitterDigest: await sha("button:data-qa=btn-submit:type=submit"),
        actionabilityDigest: immediateActionabilityDigest,
        resumeControlDigest: await sha(resumeControlName),
        formFingerprint,
        userFieldCount: fields.length
    };
    const finalActionability = actionableCapture(submit, form);
    if (finalActionability === null) return {valid: false};
    const finalActionabilityState = JSON.stringify(finalActionability);
    if (
        !structureValid()
        || !secureEqual(finalActionabilityState, immediateActionabilityState)
        || (
            release === true
            && !secureEqual(finalActionabilityState, expectedActionabilityState)
        )
    ) return {valid: false};
    if (release === true) {
        form.requestSubmit(submit);
        return {...result, actionabilityState: finalActionabilityState, released: true};
    }
    return {...result, actionabilityState: finalActionabilityState};
}
"""


def _serialized_fields(fields: tuple[FormFieldV1, ...]) -> list[dict[str, object]]:
    return [
        {
            "fieldId": field.field_id,
            "canonicalName": field.canonical_name,
            "label": " ".join(field.label.split()),
            "fieldType": field.field_type.value,
            "required": field.required,
            "position": field.position,
            "options": [
                {
                    "optionId": option.option_id,
                    "value": option.value,
                    "label": " ".join(option.label.split()),
                    "disabled": option.disabled,
                }
                for option in field.options
            ],
            "constraints": {
                "minLength": field.constraints.min_length,
                "maxLength": field.constraints.max_length,
                "minValue": field.constraints.min_value,
                "maxValue": field.constraints.max_value,
                "pattern": field.constraints.pattern,
                "acceptedFileTypes": list(field.constraints.accepted_file_types),
                "multiple": field.constraints.multiple,
            },
        }
        for field in fields
    ]


def _serialized_decisions(decisions: tuple[AnswerDecisionV1, ...]) -> list[dict[str, object]]:
    return [
        {
            "fieldId": decision.field_id,
            "disposition": decision.disposition.value,
            "value": list(decision.value) if isinstance(decision.value, tuple) else decision.value,
        }
        for decision in decisions
    ]


class PlaywrightLeverCandidateSession:
    """One lazy, isolated Lever candidate page."""

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._lease: PortalSessionLease | None = None
        self._guard: LeverNetworkGuard | None = None
        self._identity: LeverPostingIdentity | None = None
        self._attachment: LeverAttachmentProof | None = None
        self._upload_name: str | None = None
        self._expected_proof: LeverFinalActionProof | None = None
        self._expected_js: dict[str, object] | None = None
        self._release_started = False
        self._final_request_count = 0
        self._final_request_valid = False
        self._final_violation = False
        self._clicked = False

    def _require_page(self) -> Any:
        if self._page is None:
            raise LeverAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        return self._page

    async def navigate(self, url: str) -> None:
        if self._page is not None:
            raise LeverAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        guard = LeverNetworkGuard(url)
        await asyncio.to_thread(guard.require_allowed_url, url, main_frame=True)
        self._guard = guard
        self._identity = guard.identity
        try:
            portal = portal_session_for_url(
                url,
                self._settings.portal_browser_profile_root,
            )
        except PortalSessionError as exc:
            raise LeverAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        if not portal.ready:
            raise LeverAdapterBlockedError(ReasonCode.SESSION_EXPIRED)
        lease = PortalSessionLease(
            portal,
            stale_minutes=self._settings.portal_session_lock_minutes,
        )
        try:
            lease.acquire()
        except PortalSessionError as exc:
            raise LeverAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        self._lease = lease
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            lease.release()
            self._lease = None
            raise LeverAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        try:
            self._playwright = await async_playwright().start()
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(portal.profile_dir),
                headless=self._settings.portal_browser_headless,
                viewport={"width": 1280, "height": 900},
                service_workers="block",
            )
            await self._install_routes(self._context)
            self._page = (
                self._context.pages[0] if self._context.pages else await self._context.new_page()
            )
            await self._page.goto(
                guard.identity.apply_url,
                wait_until="domcontentloaded",
                timeout=_NAVIGATION_TIMEOUT_MS,
            )
            await self._assert_current_url()
        except Exception as exc:
            await self.close()
            if isinstance(exc, LeverAdapterBlockedError):
                raise
            raise LeverAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc

    async def _install_routes(self, context: Any) -> None:
        route_web_socket = getattr(context, "route_web_socket", None)
        if not callable(route_web_socket):
            raise LeverAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        await route_web_socket("**/*", self._block_web_socket)
        await context.route("**/*", self._guard_request)

    @staticmethod
    async def _block_web_socket(web_socket: Any) -> None:
        await web_socket.close(code=1008, reason="browser transport disabled")

    async def _guard_request(self, route: Any, request: Any) -> None:
        guard = self._guard
        if guard is None:
            await route.abort("blockedbyclient")
            return
        scheme = urlsplit(request.url).scheme.casefold()
        if scheme in {"data", "blob"} and not request.is_navigation_request():
            await route.continue_()
            return
        if scheme != "https":
            await route.abort("blockedbyclient")
            return
        try:
            await asyncio.to_thread(
                guard.require_allowed_url,
                request.url,
                main_frame=request.is_navigation_request(),
            )
        except LeverAdapterBlockedError:
            if self._release_started:
                self._final_violation = True
            await route.abort("blockedbyclient")
            return
        method = str(request.method or "").upper()
        if method in {"GET", "HEAD", "OPTIONS"}:
            await route.continue_()
            return
        if not self._release_started:
            guard.precommit_mutation_count += 1
            await route.abort("blockedbyclient")
            return
        self._final_request_count += 1
        expected = self._expected_proof
        identity = self._identity
        content_type = str((request.headers or {}).get("content-type", ""))
        body = request.post_data_buffer
        payload_digest = (
            canonical_multipart_payload_sha256(
                content_type=content_type,
                body=bytes(body),
                expected_cv_sha256=expected.attached_cv_sha256,
            )
            if expected is not None and isinstance(body, (bytes, bytearray, memoryview))
            else None
        )
        valid = (
            expected is not None
            and self._final_request_count == 1
            and method == "POST"
            and request.is_navigation_request()
            and str(request.resource_type).casefold() == "document"
            and request.frame == self._require_page().main_frame
            and identity is not None
            and request.url == identity.apply_url
            and payload_digest is not None
            and compare_digest(payload_digest, expected.payload_commitment_sha256)
        )
        if not valid:
            self._final_violation = True
            await route.abort("blockedbyclient")
            return
        self._final_request_valid = True
        await route.continue_()

    async def _assert_current_url(self) -> None:
        page = self._require_page()
        guard = self._guard
        if guard is None:
            raise LeverAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        await asyncio.to_thread(guard.require_allowed_url, page.url, main_frame=True)

    async def open_candidate_form(self, identity: LeverPostingIdentity) -> None:
        page = self._require_page()
        if identity != self._identity:
            raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
        links = page.locator('a[data-qa="btn-apply"][href]')
        if await links.count() != 1 or not await links.first.is_visible():
            raise LeverAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        href = await links.first.get_attribute("href")
        if href != identity.apply_url:
            raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
        await links.first.click(timeout=_ACTION_TIMEOUT_MS)
        await page.wait_for_load_state("domcontentloaded")
        await self._assert_current_url()

    async def snapshot(self) -> LeverBrowserSnapshot:
        page = self._require_page()
        await self._assert_current_url()
        locale = await page.locator("html").get_attribute("lang") or "en"
        return LeverBrowserSnapshot(
            html=await page.content(),
            url=page.url,
            locale=locale,
        )

    async def _file_observation(self) -> dict[str, object] | None:
        page = self._require_page()
        return await page.evaluate(
            r"""
            async () => {
                const wrappers = Array.from(document.querySelectorAll(
                    '[data-qa="application-field"][data-field-id]'
                )).filter(wrapper => wrapper.getAttribute("data-canonical-name") === "resume");
                if (wrappers.length !== 1) return null;
                const inputs = wrappers[0].querySelectorAll('input[type="file"][name]');
                if (inputs.length !== 1 || inputs[0].files.length !== 1) return null;
                const input = inputs[0];
                const file = input.files[0];
                const bytes = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
                const digest = Array.from(new Uint8Array(bytes))
                    .map(byte => byte.toString(16).padStart(2, "0")).join("");
                const nameBytes = await crypto.subtle.digest(
                    "SHA-256", new TextEncoder().encode(input.name)
                );
                const nameDigest = Array.from(new Uint8Array(nameBytes))
                    .map(byte => byte.toString(16).padStart(2, "0")).join("");
                return {
                    digest,
                    filename: file.name,
                    size: file.size,
                    mediaType: file.type || "application/octet-stream",
                    controlDigest: nameDigest
                };
            }
            """
        )

    async def ensure_resume_attachment(
        self,
        *,
        resume_bytes: bytes,
        cv_id: str,
        expected_sha256: str,
    ) -> LeverAttachmentProof:
        page = self._require_page()
        if hashlib.sha256(resume_bytes).hexdigest() != expected_sha256:
            raise LeverAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        extension, media_type = _resume_payload_kind(resume_bytes)
        upload_digest = hashlib.sha256(token_bytes(32) + bytes.fromhex(expected_sha256)).hexdigest()
        upload_name = f"resume-{upload_digest[:24]}.{extension}"
        inputs = page.locator(
            f"{LEVER_FORM_SELECTOR} "
            '[data-qa="application-field"][data-canonical-name="resume"] '
            'input[type="file"][name]'
        )
        if await inputs.count() != 1:
            raise LeverAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        try:
            await inputs.first.set_input_files(
                {"name": upload_name, "mimeType": media_type, "buffer": resume_bytes},
                timeout=_ACTION_TIMEOUT_MS,
            )
            observation = await self._file_observation()
        except Exception as exc:
            raise LeverAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED) from exc
        if (
            not isinstance(observation, dict)
            or observation.get("digest") != expected_sha256
            or observation.get("filename") != upload_name
            or observation.get("size") != len(resume_bytes)
            or observation.get("mediaType") != media_type
            or re.fullmatch(
                r"[0-9a-f]{64}",
                str(observation.get("controlDigest", "")),
            )
            is None
        ):
            raise LeverAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        receipt = hashlib.sha256(
            f"{expected_sha256}|{upload_name}|{len(resume_bytes)}|{media_type}".encode()
        ).hexdigest()
        self._upload_name = upload_name
        self._attachment = LeverAttachmentProof(
            cv_id=cv_id,
            cv_sha256=expected_sha256,
            upload_complete=True,
            receipt_sha256=receipt,
            resume_control_sha256=str(observation["controlDigest"]),
        )
        return self._attachment

    async def verify_resume_attachment(
        self,
        *,
        cv_id: str,
        expected_sha256: str,
    ) -> LeverAttachmentProof:
        proof = self._attachment
        observation = await self._file_observation()
        if (
            proof is None
            or self._upload_name is None
            or observation is None
            or not proof.matches(cv_id=cv_id, cv_sha256=expected_sha256)
            or observation.get("digest") != expected_sha256
            or observation.get("filename") != self._upload_name
            or observation.get("controlDigest") != proof.resume_control_sha256
        ):
            return LeverAttachmentProof(
                cv_id=cv_id,
                cv_sha256=expected_sha256,
                upload_complete=False,
            )
        return proof

    async def fill(self, decisions: tuple[AnswerDecisionV1, ...]) -> None:
        page = self._require_page()
        identity = self._identity
        if identity is None:
            raise LeverAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        fields = {
            field.field_id: field
            for field in observe_lever_v1_fields(
                (await self.snapshot()).html,
                identity=identity,
            )
        }
        for decision in decisions:
            if decision.disposition is not AnswerDisposition.RESOLVED:
                raise LeverAdapterBlockedError(ReasonCode.REQUIRED_FIELD_UNKNOWN)
            field = fields.get(decision.field_id)
            if field is None:
                raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
            if field.field_type is FieldType.FILE:
                if decision.value != VERIFIED_ATTACHMENT_SENTINEL:
                    raise LeverAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
                continue
            wrapper = page.locator(
                f"{LEVER_FORM_SELECTOR} "
                f'[data-qa="application-field"][data-field-id="{field.field_id}"]'
            )
            if await wrapper.count() != 1:
                raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
            controls = wrapper.locator("input:not([type=hidden]), textarea, select")
            value = decision.value
            try:
                if field.field_type in {FieldType.SELECT, FieldType.MULTI_SELECT}:
                    if await controls.count() != 1:
                        raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
                    await controls.first.select_option(
                        value=list(value) if isinstance(value, tuple) else str(value)
                    )
                elif field.field_type is FieldType.RADIO:
                    matches = []
                    for index in range(await controls.count()):
                        candidate = controls.nth(index)
                        if await candidate.get_attribute("value") == str(value):
                            matches.append(candidate)
                    if len(matches) != 1:
                        raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
                    await matches[0].check()
                elif field.field_type in {
                    FieldType.CHECKBOX,
                    FieldType.CONSENT,
                    FieldType.ATTESTATION,
                }:
                    if await controls.count() != 1 or type(value) is not bool:
                        raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
                    await (controls.first.check() if value else controls.first.uncheck())
                else:
                    if await controls.count() != 1:
                        raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
                    await controls.first.fill(str(value))
            except LeverAdapterBlockedError:
                raise
            except Exception as exc:
                raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED) from exc

    async def prepare_final_action(
        self,
        *,
        identity: LeverPostingIdentity,
        fields: tuple[FormFieldV1, ...],
        decisions: tuple[AnswerDecisionV1, ...],
        form_fingerprint: str,
        attached_cv_sha256: str,
    ) -> LeverFinalActionProof:
        page = self._require_page()
        guard = self._guard
        attachment = self._attachment
        if (
            identity != self._identity
            or guard is None
            or guard.precommit_mutation_count != 0
            or attachment is None
            or not attachment.matches(
                cv_id=attachment.cv_id,
                cv_sha256=attached_cv_sha256,
            )
        ):
            raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
        expected: dict[str, object] = {
            "identity": {
                "hostname": identity.hostname,
                "site": identity.site,
                "postingId": identity.posting_id,
                "applyUrl": identity.apply_url,
            },
            "fields": _serialized_fields(fields),
            "decisions": _serialized_decisions(decisions),
            "formFingerprint": form_fingerprint,
            "cvSha256": attached_cv_sha256,
            "expectedActionabilityDigest": None,
            "expectedActionabilityState": None,
            "release": False,
        }
        try:
            result = await page.evaluate(_FORM_PROOF_SCRIPT, expected)
        except Exception as exc:
            raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED) from exc
        if not isinstance(result, dict) or result.get("valid") is not True:
            raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED)
        try:
            actionability_state = result["actionabilityState"]
            if not isinstance(actionability_state, str):
                raise ValueError("invalid actionability state")
            encoded_actionability_state = actionability_state.encode("utf-8")
            if (
                not encoded_actionability_state
                or len(encoded_actionability_state) > _MAX_ACTIONABILITY_STATE_BYTES
            ):
                raise ValueError("invalid actionability state")
            actionability_sha256 = str(result["actionabilityDigest"])
            if not compare_digest(
                hashlib.sha256(encoded_actionability_state).hexdigest(),
                actionability_sha256,
            ):
                raise ValueError("invalid actionability digest")
            proof = LeverFinalActionProof(
                identity_sha256=str(result["identityDigest"]),
                action_url_sha256=str(result["actionDigest"]),
                form_fingerprint=str(result["formFingerprint"]),
                method="POST",
                encoding="multipart/form-data",
                submitter_sha256=str(result["submitterDigest"]),
                actionability_sha256=actionability_sha256,
                resume_control_sha256=str(result["resumeControlDigest"]),
                attached_cv_sha256=attached_cv_sha256,
                payload_commitment_sha256=str(result["payloadDigest"]),
                user_field_count=int(result["userFieldCount"]),
                precommit_mutation_count=guard.precommit_mutation_count,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LeverAdapterBlockedError(ReasonCode.FORM_CHANGED) from exc
        self._expected_proof = proof
        expected["expectedActionabilityState"] = actionability_state
        self._expected_js = expected
        return proof

    async def click_final_action(self, proof: LeverFinalActionProof) -> None:
        page = self._require_page()
        if (
            self._clicked
            or proof != self._expected_proof
            or self._expected_js is None
            or self._guard is None
            or self._guard.precommit_mutation_count != 0
        ):
            raise LeverAdapterBlockedError(ReasonCode.PERMIT_REPLAYED)
        self._clicked = True
        self._release_started = True
        try:
            release_expected = dict(self._expected_js)
            release_expected["expectedActionabilityDigest"] = proof.actionability_sha256
            release_expected["release"] = True
            released = await page.evaluate(_FORM_PROOF_SCRIPT, release_expected)
            if (
                not isinstance(released, dict)
                or released.get("valid") is not True
                or released.get("released") is not True
                or released.get("payloadDigest") != proof.payload_commitment_sha256
                or released.get("actionabilityDigest") != proof.actionability_sha256
                or released.get("actionabilityState")
                != release_expected.get("expectedActionabilityState")
                or released.get("resumeControlDigest") != proof.resume_control_sha256
            ):
                raise LeverFinalActionAmbiguousError(ReasonCode.FINAL_ACTION_UNCONFIRMED.value)
            await page.wait_for_timeout(750)
        except LeverFinalActionAmbiguousError:
            raise
        except Exception as exc:
            raise LeverFinalActionAmbiguousError(ReasonCode.FINAL_ACTION_UNCONFIRMED.value) from exc
        if self._final_violation or self._final_request_count != 1 or not self._final_request_valid:
            raise LeverFinalActionAmbiguousError(ReasonCode.FINAL_ACTION_UNCONFIRMED.value)

    async def confirmation_reference(
        self,
        identity: LeverPostingIdentity,
    ) -> str | None:
        page = self._require_page()
        if identity != self._identity:
            return None
        selector = (
            'main[data-qa="application-confirmation"]'
            f'[data-posting-id="{identity.posting_id}"][data-application-id]'
        )
        locator = page.locator(selector)
        if await locator.count() != 1 or not await locator.first.is_visible():
            return None
        first = (await locator.first.get_attribute("data-application-id") or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_.:-]{6,160}", first) is None:
            return None
        await page.wait_for_timeout(250)
        locator = page.locator(selector)
        if await locator.count() != 1 or not await locator.first.is_visible():
            return None
        second = (await locator.first.get_attribute("data-application-id") or "").strip()
        return first if compare_digest(first, second) else None

    async def close(self) -> None:
        context, playwright, lease = self._context, self._playwright, self._lease
        self._context = None
        self._playwright = None
        self._page = None
        self._lease = None
        self._guard = None
        self._identity = None
        self._attachment = None
        self._upload_name = None
        self._expected_proof = None
        self._expected_js = None
        try:
            if context is not None:
                await context.close()
        finally:
            try:
                if playwright is not None:
                    await playwright.stop()
            finally:
                if lease is not None:
                    lease.release()


def playwright_lever_browser_factory(_url: str) -> LeverCandidateSession:
    """Create a lazy session; no browser starts until ``navigate``."""

    return PlaywrightLeverCandidateSession()
