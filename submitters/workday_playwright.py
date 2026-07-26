"""Private local Playwright transport for the Workday v2 adapter.

The transport reuses only an operator-bootstrapped, tenant-isolated Playwright
profile.  It never reads Chrome or Edge password stores.  Every page and
profile lease is owned by one async lifecycle and closed on that same event
loop.
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
from urllib.parse import parse_qsl, urljoin, urlsplit

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
    ReasonCode,
)
from submitters.workday_v2 import (
    WorkdayAdapterBlockedError,
    WorkdayAttachmentProof,
    WorkdayBoundFinalCommitExpectation,
    WorkdayBoundFinalRequestContract,
    WorkdayBrowserSnapshot,
    WorkdayCandidateSession,
    WorkdayFinalActionAmbiguousError,
    WorkdayFinalActionReceipt,
    WorkdayFinalCommitExpectation,
    WorkdayPageState,
    assess_workday_v2_snapshot,
    observe_workday_v2_fields,
    workday_job_identity,
    workday_v2_final_action_contract,
    workday_v2_final_request_matches,
    workday_v2_request_contract,
)

_NAVIGATION_TIMEOUT_MS = 45_000
_ACTION_TIMEOUT_MS = 8_000
_MAX_FORM_DATA_BODY_BYTES = 24 * 1024 * 1024
_MAX_FORM_DATA_ENTRIES = 256
_MAX_FORM_DATA_STRING_BYTES = 1024 * 1024
_MAX_FORM_FIELD_NAME_BYTES = 256
_MAX_FORM_FILENAME_BYTES = 512
_MAX_FORM_MEDIA_TYPE_BYTES = 200
_MAX_RESUME_FORM_BYTES = 20 * 1024 * 1024
_FORM_DATA_COMMITMENT_VERSION = "workday-formdata-v1;"
_UPLOAD_COMPLETE_SELECTORS = (
    '[data-automation-id="uploadCompleted"][data-upload-id]',
    '[data-automation-id="file-upload-success"][data-upload-id]',
    '[data-automation-id="attachmentStatus"][data-upload-id]',
)
_WORKDAY_STATIC_ASSET_DOMAINS = ("myworkdaycdn.com", "workdaycdn.com")
_CAPTURE_FINAL_CONTROL_JS = """
async (element, expected) => {
    const form = element.form;
    const encoder = new TextEncoder();
    const secureEqual = (left, right) => {
        const a = String(left);
        const b = String(right);
        const length = Math.max(a.length, b.length);
        let difference = a.length ^ b.length;
        for (let index = 0; index < length; index += 1) {
            difference |= (a.charCodeAt(index) || 0) ^ (b.charCodeAt(index) || 0);
        }
        return difference === 0;
    };
    const toHex = buffer => Array.from(new Uint8Array(buffer))
        .map(byte => byte.toString(16).padStart(2, "0"))
        .join("");
    const normalizeString = value => String(value).replace(/\\r\\n|\\r|\\n/g, "\\r\\n");
    const frame = value => {
        const normalized = normalizeString(value);
        return `${encoder.encode(normalized).length}:${normalized}`;
    };
    const boundedName = (value, maximum) => {
        const text = String(value);
        const size = encoder.encode(text).length;
        return text.length > 0
            && size <= maximum
            && !/[\\u0000-\\u001f\\u007f]/.test(text);
    };
    const finalControlState = () => {
        let ariaDisabled = false;
        let inert = false;
        let cssActionable = true;
        for (
            let current = element;
            current instanceof HTMLElement;
            current = current.parentElement
        ) {
            ariaDisabled = ariaDisabled
                || (current.getAttribute("aria-disabled") || "").trim().toLowerCase()
                    === "true";
            inert = inert || current.inert === true || current.hasAttribute("inert");
            const style = getComputedStyle(current);
            cssActionable = cssActionable
                && style.display !== "none"
                && !["hidden", "collapse"].includes(style.visibility)
                && style.opacity !== "0"
                && style.pointerEvents !== "none"
                && style.contentVisibility !== "hidden";
        }
        const disabled = !(element instanceof HTMLButtonElement)
            || element.disabled
            || element.matches(":disabled");
        const rectangles = element instanceof HTMLElement
            ? Array.from(element.getClientRects())
            : [];
        return {
            disabled,
            ariaDisabled,
            inert,
            actionable: Boolean(
                element instanceof HTMLButtonElement
                && element.form === form
                && element.isConnected
                && !disabled
                && !ariaDisabled
                && !inert
                && cssActionable
                && !element.hidden
                && rectangles.some(rectangle => (
                    rectangle.width > 0 && rectangle.height > 0
                ))
            )
        };
    };
    const answerControlOwners = () => {
        if (!Array.isArray(expected.answerBindings) || typeof CSS.escape !== "function") {
            return null;
        }
        const owners = new Map();
        for (const binding of expected.answerBindings) {
            const wrappers = Array.from(document.querySelectorAll(
                `[data-automation-id="formField"][data-field-id="${
                    CSS.escape(binding.fieldId)
                }"]`
            ));
            if (wrappers.length !== 1) {
                return null;
            }
            const allControls = Array.from(
                wrappers[0].querySelectorAll("input,textarea,select")
            ).filter(control => control.form === form);
            let controls = [];
            if (binding.fieldType === "file") {
                controls = allControls.filter(
                    control => control instanceof HTMLInputElement
                        && control.type === "file"
                );
            } else if (binding.fieldType === "multi_select") {
                controls = allControls.filter(
                    control => control instanceof HTMLSelectElement && control.multiple
                );
            } else if (binding.fieldType === "select") {
                controls = allControls.filter(
                    control => control instanceof HTMLSelectElement && !control.multiple
                );
            } else if (binding.fieldType === "radio") {
                controls = allControls.filter(
                    control => control instanceof HTMLInputElement
                        && control.type === "radio"
                );
            } else if (["checkbox", "consent", "attestation"].includes(
                binding.fieldType
            )) {
                controls = allControls.filter(
                    control => control instanceof HTMLInputElement
                        && control.type === "checkbox"
                );
            } else if (binding.fieldType === "textarea") {
                controls = allControls.filter(
                    control => control instanceof HTMLTextAreaElement
                );
            } else {
                const expectedTypes = {
                    text: ["text", "search"],
                    email: ["email"],
                    phone: ["tel"],
                    url: ["url"],
                    number: ["number"],
                    date: ["date"]
                }[binding.fieldType] || [];
                controls = allControls.filter(
                    control => control instanceof HTMLInputElement
                        && expectedTypes.includes(control.type)
                );
            }
            const expectedControlCount = binding.fieldType === "radio"
                ? controls.length
                : 1;
            if (
                controls.length !== expectedControlCount
                || controls.length === 0
                || allControls.length !== controls.length
                || controls.some(control => (
                    control.disabled || !boundedName(control.name, 256)
                ))
            ) {
                return null;
            }
            for (const control of controls) {
                let expectedValues = [String(control.value)];
                if (control instanceof HTMLSelectElement && control.multiple) {
                    expectedValues = Array.from(control.selectedOptions)
                        .filter(option => !option.disabled)
                        .map(option => String(option.value));
                } else if (
                    control instanceof HTMLInputElement
                    && ["checkbox", "radio"].includes(control.type)
                ) {
                    expectedValues = control.checked ? [String(control.value)] : [];
                } else if (
                    control instanceof HTMLInputElement
                    && control.type === "file"
                ) {
                    expectedValues = Array.from(control.files || []);
                }
                const prior = owners.get(control.name);
                if (prior && prior.fieldId !== binding.fieldId) {
                    return null;
                }
                const owner = prior || {
                    fieldId: binding.fieldId,
                    expectedValues: [],
                    index: 0
                };
                owner.expectedValues.push(...expectedValues);
                owners.set(control.name, owner);
            }
        }
        return owners;
    };
    const systemEntryValid = (name, value, seen) => {
        if (typeof value !== "string" || seen.has(name)) {
            return false;
        }
        const controls = Array.from(form.elements).filter(
            control => control.name === name && !control.disabled
        );
        if (
            controls.length !== 1
            || !(controls[0] instanceof HTMLInputElement)
            || controls[0].type !== "hidden"
        ) {
            return false;
        }
        seen.add(name);
        const normalized = String(value).trim().toLowerCase();
        if (["jobId", "jobPostingId", "requisitionId"].includes(name)) {
            return secureEqual(normalized, expected.jobRequisition);
        }
        if (["site", "careerSite", "externalCareerSiteId"].includes(name)) {
            return secureEqual(normalized, expected.careerSite);
        }
        return ["_csrf", "csrfToken", "xsrfToken"].includes(name)
            && value.length > 0
            && encoder.encode(value).length <= 4096;
    };
    const formDataCommitment = async () => {
        if (
            !form
            || !/^[0-9a-f]{64}$/.test(String(expected.cvSha256 || ""))
        ) {
            return {valid: false};
        }
        let data;
        try {
            data = new FormData(form);
        } catch (_error) {
            return {valid: false};
        }
        const owners = answerControlOwners();
        if (owners === null) {
            return {valid: false};
        }
        const systemNames = new Set();
        let material = "workday-formdata-v1;";
        let entryCount = 0;
        let stringBytes = 0;
        let fileCount = 0;
        let estimatedBytes = encoder.encode(material).length;
        for (const [rawName, value] of data.entries()) {
            entryCount += 1;
            const name = String(rawName);
            if (
                entryCount > 256
                || !boundedName(name, 256)
            ) {
                return {valid: false};
            }
            const owner = owners.get(name);
            if (owner) {
                if (owner.index >= owner.expectedValues.length) {
                    return {valid: false};
                }
                const expectedValue = owner.expectedValues[owner.index];
                owner.index += 1;
                if (typeof expectedValue === "string") {
                    if (
                        typeof value !== "string"
                        || !secureEqual(
                            normalizeString(value),
                            normalizeString(expectedValue)
                        )
                    ) {
                        return {valid: false};
                    }
                } else if (
                    !(value instanceof File)
                    || !(expectedValue instanceof File)
                    || value.name !== expectedValue.name
                    || value.size !== expectedValue.size
                    || String(value.type || "application/octet-stream").toLowerCase()
                        !== String(
                            expectedValue.type || "application/octet-stream"
                        ).toLowerCase()
                ) {
                    return {valid: false};
                }
            } else if (!systemEntryValid(name, value, systemNames)) {
                return {valid: false};
            }
            if (typeof value === "string") {
                const normalized = normalizeString(value);
                const valueBytes = encoder.encode(normalized).length;
                stringBytes += valueBytes;
                if (stringBytes > 1048576) {
                    return {valid: false};
                }
                const entry = `S${frame(name)}${frame(normalized)}`;
                estimatedBytes += encoder.encode(entry).length;
                material += entry;
                continue;
            }
            if (!(value instanceof File)) {
                return {valid: false};
            }
            fileCount += 1;
            const filename = String(value.name);
            const mediaType = String(value.type || "application/octet-stream").toLowerCase();
            if (
                fileCount > 1
                || value.size <= 0
                || value.size > 20971520
                || !boundedName(filename, 512)
                || !boundedName(mediaType, 200)
            ) {
                return {valid: false};
            }
            let fileDigest;
            try {
                fileDigest = toHex(await crypto.subtle.digest(
                    "SHA-256",
                    await value.arrayBuffer()
                ));
            } catch (_error) {
                return {valid: false};
            }
            if (!secureEqual(fileDigest, expected.cvSha256)) {
                return {valid: false};
            }
            const entry = `F${frame(name)}${frame(filename)}${frame(mediaType)}${
                frame(String(value.size))
            }${frame(fileDigest)}`;
            estimatedBytes += encoder.encode(entry).length + value.size;
            material += entry;
        }
        if (
            entryCount === 0
            || fileCount !== 1
            || estimatedBytes > 25165824
            || Array.from(owners.values()).some(
                owner => owner.index !== owner.expectedValues.length
            )
        ) {
            return {valid: false};
        }
        return {
            valid: true,
            digest: toHex(await crypto.subtle.digest("SHA-256", encoder.encode(material)))
        };
    };
    const snapshot = () => JSON.stringify({
        outerHTML: form ? form.outerHTML : "",
        controls: form ? Array.from(form.elements).map((control, index) => ({
            index,
            tag: control.tagName.toLowerCase(),
            type: (control.getAttribute("type") || "").toLowerCase(),
            id: control.id || "",
            name: control.getAttribute("name") || "",
            disabled: Boolean(control.disabled),
            required: Boolean(control.required),
            checked: "checked" in control ? Boolean(control.checked) : null,
            value: control instanceof HTMLInputElement && control.type === "file"
                ? Array.from(control.files || []).map(file => ({
                    name: file.name,
                    size: file.size,
                    type: file.type,
                    lastModified: file.lastModified
                }))
                : ("value" in control ? String(control.value) : ""),
            selected: control instanceof HTMLSelectElement
                ? Array.from(control.selectedOptions).map(option => ({
                    value: option.value,
                    index: option.index
                }))
                : []
        })) : []
    });
    const before = snapshot();
    const stateDigest = toHex(await crypto.subtle.digest("SHA-256", encoder.encode(before)));
    const payload = await formDataCommitment();
    const finalState = finalControlState();
    return {
        connected: element.isConnected,
        inReview: element.closest(
            '[data-automation-id="reviewPage"]'
        ) !== null,
        reviewCount: document.querySelectorAll(
            '[data-automation-id="reviewPage"]'
        ).length,
        type: (element.getAttribute("type") || "submit").toLowerCase(),
        explicitForm: element.hasAttribute("form"),
        explicitFormAction: element.hasAttribute("formaction"),
        explicitFormMethod: element.hasAttribute("formmethod"),
        disabled: finalState.disabled,
        ariaDisabled: finalState.ariaDisabled,
        inert: finalState.inert,
        actionable: finalState.actionable,
        action: form ? (form.getAttribute("action") || "") : "",
        method: form ? (form.getAttribute("method") || "") : "",
        encoding: form ? String(form.enctype || "").toLowerCase() : "",
        stateStable: before === snapshot(),
        stateDigest,
        payloadValid: payload.valid === true,
        payloadDigest: payload.valid === true ? payload.digest : null
    };
}
"""
_ATOMIC_FINAL_SUBMIT_JS = """
async (element, expected) => {
    const form = element.form;
    const encoder = new TextEncoder();
    const snapshot = () => JSON.stringify({
        outerHTML: form ? form.outerHTML : "",
        controls: form ? Array.from(form.elements).map((control, index) => ({
            index,
            tag: control.tagName.toLowerCase(),
            type: (control.getAttribute("type") || "").toLowerCase(),
            id: control.id || "",
            name: control.getAttribute("name") || "",
            disabled: Boolean(control.disabled),
            required: Boolean(control.required),
            checked: "checked" in control ? Boolean(control.checked) : null,
            value: control instanceof HTMLInputElement && control.type === "file"
                ? Array.from(control.files || []).map(file => ({
                    name: file.name,
                    size: file.size,
                    type: file.type,
                    lastModified: file.lastModified
                }))
                : ("value" in control ? String(control.value) : ""),
            selected: control instanceof HTMLSelectElement
                ? Array.from(control.selectedOptions).map(option => ({
                    value: option.value,
                    index: option.index
                }))
                : []
        })) : []
    });
    const secureEqual = (left, right) => {
        const a = String(left);
        const b = String(right);
        const length = Math.max(a.length, b.length);
        let difference = a.length ^ b.length;
        for (let index = 0; index < length; index += 1) {
            difference |= (a.charCodeAt(index) || 0) ^ (b.charCodeAt(index) || 0);
        }
        return difference === 0;
    };
    const toHex = buffer => Array.from(new Uint8Array(buffer))
        .map(byte => byte.toString(16).padStart(2, "0"))
        .join("");
    const normalizeString = value => String(value).replace(/\\r\\n|\\r|\\n/g, "\\r\\n");
    const frame = value => {
        const normalized = normalizeString(value);
        return `${encoder.encode(normalized).length}:${normalized}`;
    };
    const boundedName = (value, maximum) => {
        const text = String(value);
        const size = encoder.encode(text).length;
        return text.length > 0
            && size <= maximum
            && !/[\\u0000-\\u001f\\u007f]/.test(text);
    };
    const finalControlActionable = () => {
        if (
            !(element instanceof HTMLButtonElement)
            || element.form !== form
            || element.disabled
            || element.matches(":disabled")
            || !element.isConnected
            || element.hidden
        ) {
            return false;
        }
        for (
            let current = element;
            current instanceof HTMLElement;
            current = current.parentElement
        ) {
            if (
                current.inert === true
                || current.hasAttribute("inert")
                || (current.getAttribute("aria-disabled") || "").trim().toLowerCase()
                    === "true"
            ) {
                return false;
            }
            const style = getComputedStyle(current);
            if (
                style.display === "none"
                || ["hidden", "collapse"].includes(style.visibility)
                || style.opacity === "0"
                || style.pointerEvents === "none"
                || style.contentVisibility === "hidden"
            ) {
                return false;
            }
        }
        return Array.from(element.getClientRects()).some(rectangle => (
            rectangle.width > 0 && rectangle.height > 0
        ));
    };
    const answerControlOwners = () => {
        if (!Array.isArray(expected.answerBindings) || typeof CSS.escape !== "function") {
            return null;
        }
        const owners = new Map();
        for (const binding of expected.answerBindings) {
            const wrappers = Array.from(document.querySelectorAll(
                `[data-automation-id="formField"][data-field-id="${
                    CSS.escape(binding.fieldId)
                }"]`
            ));
            if (wrappers.length !== 1) {
                return null;
            }
            const allControls = Array.from(
                wrappers[0].querySelectorAll("input,textarea,select")
            ).filter(control => control.form === form);
            let controls = [];
            if (binding.fieldType === "file") {
                controls = allControls.filter(
                    control => control instanceof HTMLInputElement
                        && control.type === "file"
                );
            } else if (binding.fieldType === "multi_select") {
                controls = allControls.filter(
                    control => control instanceof HTMLSelectElement && control.multiple
                );
            } else if (binding.fieldType === "select") {
                controls = allControls.filter(
                    control => control instanceof HTMLSelectElement && !control.multiple
                );
            } else if (binding.fieldType === "radio") {
                controls = allControls.filter(
                    control => control instanceof HTMLInputElement
                        && control.type === "radio"
                );
            } else if (["checkbox", "consent", "attestation"].includes(
                binding.fieldType
            )) {
                controls = allControls.filter(
                    control => control instanceof HTMLInputElement
                        && control.type === "checkbox"
                );
            } else if (binding.fieldType === "textarea") {
                controls = allControls.filter(
                    control => control instanceof HTMLTextAreaElement
                );
            } else {
                const expectedTypes = {
                    text: ["text", "search"],
                    email: ["email"],
                    phone: ["tel"],
                    url: ["url"],
                    number: ["number"],
                    date: ["date"]
                }[binding.fieldType] || [];
                controls = allControls.filter(
                    control => control instanceof HTMLInputElement
                        && expectedTypes.includes(control.type)
                );
            }
            const expectedControlCount = binding.fieldType === "radio"
                ? controls.length
                : 1;
            if (
                controls.length !== expectedControlCount
                || controls.length === 0
                || allControls.length !== controls.length
                || controls.some(control => (
                    control.disabled || !boundedName(control.name, 256)
                ))
            ) {
                return null;
            }
            for (const control of controls) {
                let expectedValues = [String(control.value)];
                if (control instanceof HTMLSelectElement && control.multiple) {
                    expectedValues = Array.from(control.selectedOptions)
                        .filter(option => !option.disabled)
                        .map(option => String(option.value));
                } else if (
                    control instanceof HTMLInputElement
                    && ["checkbox", "radio"].includes(control.type)
                ) {
                    expectedValues = control.checked ? [String(control.value)] : [];
                } else if (
                    control instanceof HTMLInputElement
                    && control.type === "file"
                ) {
                    expectedValues = Array.from(control.files || []);
                }
                const prior = owners.get(control.name);
                if (prior && prior.fieldId !== binding.fieldId) {
                    return null;
                }
                const owner = prior || {
                    fieldId: binding.fieldId,
                    expectedValues: [],
                    index: 0
                };
                owner.expectedValues.push(...expectedValues);
                owners.set(control.name, owner);
            }
        }
        return owners;
    };
    const systemEntryValid = (name, value, seen) => {
        if (typeof value !== "string" || seen.has(name)) {
            return false;
        }
        const controls = Array.from(form.elements).filter(
            control => control.name === name && !control.disabled
        );
        if (
            controls.length !== 1
            || !(controls[0] instanceof HTMLInputElement)
            || controls[0].type !== "hidden"
        ) {
            return false;
        }
        seen.add(name);
        const normalized = String(value).trim().toLowerCase();
        if (["jobId", "jobPostingId", "requisitionId"].includes(name)) {
            return secureEqual(normalized, expected.jobRequisition);
        }
        if (["site", "careerSite", "externalCareerSiteId"].includes(name)) {
            return secureEqual(normalized, expected.careerSite);
        }
        return ["_csrf", "csrfToken", "xsrfToken"].includes(name)
            && value.length > 0
            && encoder.encode(value).length <= 4096;
    };
    const formDataCommitment = async () => {
        if (
            !form
            || !/^[0-9a-f]{64}$/.test(String(expected.cvSha256 || ""))
        ) {
            return {valid: false};
        }
        let data;
        try {
            data = new FormData(form);
        } catch (_error) {
            return {valid: false};
        }
        const owners = answerControlOwners();
        if (owners === null) {
            return {valid: false};
        }
        const systemNames = new Set();
        let material = "workday-formdata-v1;";
        let entryCount = 0;
        let stringBytes = 0;
        let fileCount = 0;
        let estimatedBytes = encoder.encode(material).length;
        for (const [rawName, value] of data.entries()) {
            entryCount += 1;
            const name = String(rawName);
            if (
                entryCount > 256
                || !boundedName(name, 256)
            ) {
                return {valid: false};
            }
            const owner = owners.get(name);
            if (owner) {
                if (owner.index >= owner.expectedValues.length) {
                    return {valid: false};
                }
                const expectedValue = owner.expectedValues[owner.index];
                owner.index += 1;
                if (typeof expectedValue === "string") {
                    if (
                        typeof value !== "string"
                        || !secureEqual(
                            normalizeString(value),
                            normalizeString(expectedValue)
                        )
                    ) {
                        return {valid: false};
                    }
                } else if (
                    !(value instanceof File)
                    || !(expectedValue instanceof File)
                    || value.name !== expectedValue.name
                    || value.size !== expectedValue.size
                    || String(value.type || "application/octet-stream").toLowerCase()
                        !== String(
                            expectedValue.type || "application/octet-stream"
                        ).toLowerCase()
                ) {
                    return {valid: false};
                }
            } else if (!systemEntryValid(name, value, systemNames)) {
                return {valid: false};
            }
            if (typeof value === "string") {
                const normalized = normalizeString(value);
                const valueBytes = encoder.encode(normalized).length;
                stringBytes += valueBytes;
                if (stringBytes > 1048576) {
                    return {valid: false};
                }
                const entry = `S${frame(name)}${frame(normalized)}`;
                estimatedBytes += encoder.encode(entry).length;
                material += entry;
                continue;
            }
            if (!(value instanceof File)) {
                return {valid: false};
            }
            fileCount += 1;
            const filename = String(value.name);
            const mediaType = String(value.type || "application/octet-stream").toLowerCase();
            if (
                fileCount > 1
                || value.size <= 0
                || value.size > 20971520
                || !boundedName(filename, 512)
                || !boundedName(mediaType, 200)
            ) {
                return {valid: false};
            }
            let fileDigest;
            try {
                fileDigest = toHex(await crypto.subtle.digest(
                    "SHA-256",
                    await value.arrayBuffer()
                ));
            } catch (_error) {
                return {valid: false};
            }
            if (!secureEqual(fileDigest, expected.cvSha256)) {
                return {valid: false};
            }
            const entry = `F${frame(name)}${frame(filename)}${frame(mediaType)}${
                frame(String(value.size))
            }${frame(fileDigest)}`;
            estimatedBytes += encoder.encode(entry).length + value.size;
            material += entry;
        }
        if (
            entryCount === 0
            || fileCount !== 1
            || estimatedBytes > 25165824
            || Array.from(owners.values()).some(
                owner => owner.index !== owner.expectedValues.length
            )
        ) {
            return {valid: false};
        }
        return {
            valid: true,
            digest: toHex(await crypto.subtle.digest("SHA-256", encoder.encode(material)))
        };
    };
    const structurallyValid = () => Boolean(
        form
        && finalControlActionable()
        && element.isConnected
        && element.closest('[data-automation-id="reviewPage"]') !== null
        && document.querySelectorAll('[data-automation-id="reviewPage"]').length === 1
        && (element.getAttribute("type") || "submit").toLowerCase() === "submit"
        && !element.hasAttribute("form")
        && !element.hasAttribute("formaction")
        && !element.hasAttribute("formmethod")
        && (form.getAttribute("action") || "") === expected.action
        && (form.getAttribute("method") || "").toLowerCase() === expected.method
        && String(form.enctype || "").toLowerCase() === expected.encoding
    );
    const markerValid = () => {
        const selector = [
            '[data-automation-id="uploadCompleted"][data-upload-id]',
            '[data-automation-id="file-upload-success"][data-upload-id]',
            '[data-automation-id="attachmentStatus"][data-upload-id]'
        ].join(",");
        const visible = node => {
            const style = getComputedStyle(node);
            return !node.hidden
                && style.display !== "none"
                && style.visibility !== "hidden"
                && node.getClientRects().length > 0;
        };
        const matches = Array.from(document.querySelectorAll(selector))
            .filter(node => visible(node))
            .filter(node => (node.getAttribute("data-upload-id") || "") === expected.markerId);
        if (matches.length !== 1) {
            return false;
        }
        const marker = matches[0];
        const children = Array.from(marker.querySelectorAll(
            '[data-automation-id="uploadedFileName"]'
        )).filter(node => visible(node));
        const names = [
            (marker.getAttribute("data-file-name") || "").trim(),
            ...children.map(child => (child.textContent || "").trim())
        ].filter(Boolean);
        const digests = [
            (marker.getAttribute("data-file-sha256") || "").trim().toLowerCase(),
            ...Array.from(marker.querySelectorAll("[data-file-sha256]"))
                .filter(node => visible(node))
                .map(node => (
                    node.getAttribute("data-file-sha256") || ""
                ).trim().toLowerCase())
        ].filter(Boolean);
        return digests.length > 0
            && names.every(name => name.toLowerCase() === expected.uploadName.toLowerCase())
            && digests.every(digest => secureEqual(digest, expected.cvSha256));
    };
    const answerBindingsValid = async () => {
        if (!Array.isArray(expected.answerBindings) || typeof CSS.escape !== "function") {
            return false;
        }
        for (const binding of expected.answerBindings) {
            let material = "";
            if (binding.fieldType === "file") {
                material = `f:${expected.cvSha256}`;
            } else {
                const wrappers = Array.from(document.querySelectorAll(
                    `[data-automation-id="formField"][data-field-id="${
                        CSS.escape(binding.fieldId)
                    }"]`
                ));
                if (wrappers.length !== 1) {
                    return false;
                }
                const wrapper = wrappers[0];
                if (binding.fieldType === "multi_select") {
                    const controls = wrapper.querySelectorAll("select");
                    if (controls.length !== 1) {
                        return false;
                    }
                    const values = Array.from(controls[0].selectedOptions)
                        .map(option => option.value)
                        .sort();
                    material = "m:" + values.map(value => (
                        `${new TextEncoder().encode(value).length}:${value}`
                    )).join("");
                } else if (binding.fieldType === "select") {
                    const controls = wrapper.querySelectorAll("select");
                    if (controls.length !== 1) {
                        return false;
                    }
                    material = `s:${controls[0].value}`;
                } else if (binding.fieldType === "radio") {
                    const checked = wrapper.querySelectorAll('input[type="radio"]:checked');
                    if (checked.length !== 1) {
                        return false;
                    }
                    material = `s:${checked[0].value}`;
                } else if (["checkbox", "consent", "attestation"].includes(
                    binding.fieldType
                )) {
                    const controls = wrapper.querySelectorAll('input[type="checkbox"]');
                    if (controls.length !== 1) {
                        return false;
                    }
                    material = controls[0].checked ? "b:1" : "b:0";
                } else {
                    const controls = wrapper.querySelectorAll("input,textarea");
                    if (controls.length !== 1) {
                        return false;
                    }
                    material = `s:${controls[0].value}`;
                }
            }
            const buffer = await crypto.subtle.digest(
                "SHA-256",
                encoder.encode(material)
            );
            const digest = Array.from(new Uint8Array(buffer))
                .map(byte => byte.toString(16).padStart(2, "0"))
                .join("");
            if (!secureEqual(digest, binding.valueSha256)) {
                return false;
            }
        }
        return true;
    };
    if (
        !structurallyValid()
        || !markerValid()
        || !form.checkValidity()
    ) {
        return {released: false};
    }
    const before = snapshot();
    const stateDigest = toHex(
        await crypto.subtle.digest("SHA-256", encoder.encode(before))
    );
    if (
        !secureEqual(stateDigest, expected.stateDigest)
        || before !== snapshot()
        || !structurallyValid()
        || !markerValid()
        || !(await answerBindingsValid())
    ) {
        return {released: false};
    }
    const payload = await formDataCommitment();
    if (
        payload.valid !== true
        || !secureEqual(payload.digest, expected.payloadDigest)
        || before !== snapshot()
        || !structurallyValid()
        || !markerValid()
        || !form.checkValidity()
    ) {
        return {released: false};
    }
    HTMLFormElement.prototype.submit.call(form);
    return {released: true};
}
"""


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
    """Hash bounded outgoing form data without returning any field value or CV bytes."""

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

    material: list[bytes] = [_FORM_DATA_COMMITMENT_VERSION.encode("ascii")]
    entry_count = 0
    string_bytes = 0
    file_count = 0
    media_type = content_type.split(";", 1)[0].strip().casefold()

    if media_type in {"", "application/x-www-form-urlencoded"}:
        try:
            decoded = body.decode("utf-8", errors="strict")
            pairs = parse_qsl(
                decoded,
                keep_blank_values=True,
                strict_parsing=False,
                encoding="utf-8",
                errors="strict",
                max_num_fields=_MAX_FORM_DATA_ENTRIES,
            )
        except (UnicodeError, ValueError):
            return None
        for raw_name, raw_value in pairs:
            name = _bounded_form_name(raw_name, _MAX_FORM_FIELD_NAME_BYTES)
            if name is None:
                return None
            normalized_value = _normalize_form_string(raw_value)
            string_bytes += len(normalized_value.encode("utf-8"))
            entry_count += 1
            if entry_count > _MAX_FORM_DATA_ENTRIES or string_bytes > _MAX_FORM_DATA_STRING_BYTES:
                return None
            material.append(b"S" + _length_frame(name) + _length_frame(normalized_value))
    elif media_type == "multipart/form-data":
        try:
            encoded_content_type = content_type.encode("ascii", errors="strict")
            message = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: " + encoded_content_type + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
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
        for part in message.iter_parts():
            entry_count += 1
            if (
                entry_count > _MAX_FORM_DATA_ENTRIES
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
                normalized_value = _normalize_form_string(value)
                string_bytes += len(normalized_value.encode("utf-8"))
                if string_bytes > _MAX_FORM_DATA_STRING_BYTES:
                    return None
                material.append(b"S" + _length_frame(name) + _length_frame(normalized_value))
                continue

            filename = _bounded_form_name(filename_value, _MAX_FORM_FILENAME_BYTES)
            observed_media_type = _bounded_form_name(
                part.get_content_type().casefold(),
                _MAX_FORM_MEDIA_TYPE_BYTES,
            )
            file_count += 1
            if (
                filename is None
                or observed_media_type is None
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
                + _length_frame(observed_media_type)
                + _length_frame(str(len(payload)))
                + _length_frame(file_sha256)
            )
    else:
        return None

    if entry_count == 0 or file_count != 1:
        return None
    return hashlib.sha256(b"".join(material)).hexdigest()


def _request_body_bytes(request: Any, *, media_type: str) -> bytes | None:
    """Read one bounded Playwright body into an ephemeral byte string."""

    try:
        buffered = getattr(request, "post_data_buffer", None)
    except Exception:
        return None
    if isinstance(buffered, bytes):
        return buffered if len(buffered) <= _MAX_FORM_DATA_BODY_BYTES else None
    if isinstance(buffered, (bytearray, memoryview)):
        payload = bytes(buffered)
        return payload if len(payload) <= _MAX_FORM_DATA_BODY_BYTES else None
    if media_type not in {"", "application/x-www-form-urlencoded"}:
        return None
    try:
        text = getattr(request, "post_data", None)
    except Exception:
        return None
    if not isinstance(text, str):
        return None
    try:
        payload = text.encode("utf-8", errors="strict")
    except UnicodeError:
        return None
    return payload if len(payload) <= _MAX_FORM_DATA_BODY_BYTES else None


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
    raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)


class WorkdayNetworkGuard:
    """Exact-origin HTTPS and public-DNS policy for one candidate session."""

    def __init__(
        self,
        initial_url: str,
        *,
        resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
    ) -> None:
        self.expected_job_identity = workday_job_identity(initial_url)
        self.expected_hostname = self.expected_job_identity.hostname
        self._resolver = resolver
        self._dns_verified: set[str] = set()

    @staticmethod
    def _https_hostname(url: str) -> str:
        try:
            parsed = urlsplit((url or "").strip())
            port = parsed.port
        except ValueError as exc:
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        hostname = (parsed.hostname or "").lower()
        if (
            parsed.scheme.casefold() != "https"
            or not hostname
            or hostname != hostname.rstrip(".")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or port not in {None, 443}
            or any(ord(character) > 127 for character in hostname)
            or hostname == "localhost"
            or hostname.endswith((".localhost", ".local", ".internal"))
        ):
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            literal = None
        if literal is not None and not literal.is_global:
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        return hostname

    def require_allowed_url(self, url: str, *, main_frame: bool = True) -> None:
        hostname = self._https_hostname(url)
        if main_frame:
            observed_job = workday_job_identity(
                url,
                expected_hostname=self.expected_hostname,
            )
            if observed_job != self.expected_job_identity:
                raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
        elif hostname != self.expected_hostname and not any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in _WORKDAY_STATIC_ASSET_DOMAINS
        ):
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)

        if hostname in self._dns_verified:
            return
        try:
            answers = self._resolver(
                hostname,
                443,
                0,
                socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        addresses = {
            str(answer[4][0]).split("%", 1)[0]
            for answer in answers
            if len(answer) > 4 and answer[4]
        }
        if not addresses:
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        try:
            resolved = tuple(ipaddress.ip_address(address) for address in addresses)
        except ValueError as exc:
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        if any(not address.is_global for address in resolved):
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        self._dns_verified.add(hostname)


class _OneShotFinalRequestGate:
    """Ephemeral gate for one exact main-frame native form POST."""

    _SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

    def __init__(
        self,
        expectation: WorkdayBoundFinalCommitExpectation,
        *,
        page: Any,
    ) -> None:
        self.expectation = expectation
        self.page = page
        try:
            self.main_frame = page.main_frame
        except Exception as exc:
            raise ValueError("WORKDAY_FINAL_REQUEST_FRAME_INVALID") from exc
        if self.main_frame is None:
            raise ValueError("WORKDAY_FINAL_REQUEST_FRAME_INVALID")
        self.completed = asyncio.Event()
        self.receipt: WorkdayFinalActionReceipt | None = None
        self.rejected = False
        self.possibly_sent = False

    def reject(self) -> None:
        """Permanently close the pre-send gate without releasing a request."""

        if not self.completed.is_set():
            self.rejected = True
            self.completed.set()

    def evaluate(self, request: Any) -> bool | None:
        """Return True to send, False to abort, or None after the one allowed POST."""

        try:
            method = str(getattr(request, "method", "GET")).strip().upper()
        except Exception:
            method = ""
        if self.completed.is_set():
            if self.receipt is not None and method in self._SAFE_METHODS:
                return None
            return False
        # Background reads are ordinary browser noise and must not consume or
        # reject the exact irreversible gate.
        if method in self._SAFE_METHODS:
            return None
        if method != "POST":
            self.reject()
            return False
        try:
            is_navigation = request.is_navigation_request() is True
            frame = request.frame
            frame_page = frame.page
            resource_type = str(request.resource_type).strip().casefold()
            headers = getattr(request, "headers", {}) or {}
            content_type = str(headers.get("content-type", ""))[:256]
        except Exception:
            self.reject()
            return False
        if (
            not is_navigation
            or resource_type != "document"
            or frame is not self.main_frame
            or frame_page is not self.page
        ):
            self.reject()
            return False
        media_type = content_type.split(";", 1)[0].strip().casefold()
        body = _request_body_bytes(request, media_type=media_type)
        if body is None:
            self.reject()
            return False
        payload_sha256 = _canonical_form_data_payload_sha256(
            content_type=content_type,
            body=body,
            expected_cv_sha256=self.expectation.base.selected_cv_hash,
        )
        if payload_sha256 is None:
            self.reject()
            return False
        if not workday_v2_final_request_matches(
            self.expectation.request_contract,
            method=method,
            url=str(getattr(request, "url", "")),
            payload_sha256=payload_sha256,
        ):
            self.reject()
            return False
        self.receipt = WorkdayFinalActionReceipt.from_contract(self.expectation.request_contract)
        # From this point forward, route continuation may have released bytes.
        self.possibly_sent = True
        self.completed.set()
        return True


class PlaywrightWorkdayCandidateSession:
    """One lazily launched Workday page backed by a dedicated portal profile."""

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._lease: PortalSessionLease | None = None
        self._attachment: WorkdayAttachmentProof | None = None
        self._attachment_marker_id: str | None = None
        self._attachment_upload_name: str | None = None
        self._network_guard: WorkdayNetworkGuard | None = None
        self._final_request_gate: _OneShotFinalRequestGate | None = None
        self._clicked = False

    def _require_page(self) -> Any:
        if self._page is None:
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        return self._page

    async def navigate(self, url: str) -> None:
        if self._page is not None:
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        guard = WorkdayNetworkGuard(url)
        await asyncio.to_thread(guard.require_allowed_url, url)
        self._network_guard = guard
        try:
            portal = portal_session_for_url(
                url,
                self._settings.portal_browser_profile_root,
            )
        except PortalSessionError as exc:
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        if not portal.ready:
            raise WorkdayAdapterBlockedError(ReasonCode.SESSION_EXPIRED)

        lease = PortalSessionLease(
            portal,
            stale_minutes=self._settings.portal_session_lock_minutes,
        )
        try:
            lease.acquire()
        except PortalSessionError as exc:
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        self._lease = lease

        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            lease.release()
            self._lease = None
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc

        try:
            self._playwright = await async_playwright().start()
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(portal.profile_dir),
                headless=self._settings.portal_browser_headless,
                viewport={"width": 1280, "height": 900},
                service_workers="block",
            )
            await self._install_network_routes(self._context)
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
            if isinstance(exc, WorkdayAdapterBlockedError):
                raise
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc

    async def _install_network_routes(self, context: Any) -> None:
        route_web_socket = getattr(context, "route_web_socket", None)
        if not callable(route_web_socket):
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        await route_web_socket("**/*", self._block_web_socket)
        await context.route("**/*", self._guard_request)

    @staticmethod
    async def _block_web_socket(web_socket: Any) -> None:
        await web_socket.close(code=1008, reason="browser transport disabled")

    async def _guard_request(self, route: Any, request: Any) -> None:
        guard = self._network_guard
        gate = self._final_request_gate
        if guard is None:
            if gate is not None:
                gate.reject()
            await route.abort("blockedbyclient")
            return
        scheme = urlsplit(request.url).scheme.casefold()
        if scheme in {"data", "blob"} and not request.is_navigation_request():
            await route.continue_()
            return
        if scheme != "https":
            if gate is not None:
                gate.reject()
            await route.abort("blockedbyclient")
            return
        try:
            await asyncio.to_thread(
                guard.require_allowed_url,
                request.url,
                main_frame=request.is_navigation_request(),
            )
        except WorkdayAdapterBlockedError:
            if gate is not None:
                gate.reject()
            await route.abort("blockedbyclient")
            return
        try:
            method = str(getattr(request, "method", "")).strip().upper()
        except Exception:
            method = ""
        # Until an exact final gate is armed, this fixture-qualified transport
        # has no evidence-backed contract for any mutation-capable request.
        # Reversible upload/save endpoints must be separately version-qualified
        # before they can be allowlisted.
        if gate is None and method not in _OneShotFinalRequestGate._SAFE_METHODS:
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
        guard = self._network_guard
        if guard is None:
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        await asyncio.to_thread(guard.require_allowed_url, page.url)

    async def open_candidate_form(self) -> None:
        page = self._require_page()
        apply = page.locator('[data-automation-id="jobPostingApplyButton"]')
        if await apply.count() != 1 or not await apply.is_visible():
            raise WorkdayAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        await apply.click(timeout=_ACTION_TIMEOUT_MS)
        await page.wait_for_load_state("domcontentloaded")
        await self._assert_current_url()

        reuse = page.locator(
            '[data-automation-id="useMyLastApplication"], '
            'button:has-text("Use My Last Application")'
        )
        if await reuse.count() == 1 and await reuse.is_visible():
            await reuse.click(timeout=_ACTION_TIMEOUT_MS)
            await self._assert_current_url()
            return

        manual = page.locator(
            '[data-automation-id="applyManually"], button:has-text("Apply Manually")'
        )
        if await manual.count() == 1 and await manual.is_visible():
            await manual.click(timeout=_ACTION_TIMEOUT_MS)
            await self._assert_current_url()

    async def snapshot(self) -> WorkdayBrowserSnapshot:
        page = self._require_page()
        await self._assert_current_url()
        locale = await page.locator("html").get_attribute("lang") or "en"
        return WorkdayBrowserSnapshot(
            html=await page.content(),
            url=page.url,
            locale=locale,
        )

    async def _upload_marker_ids(self) -> set[str]:
        page = self._require_page()
        marker_ids: set[str] = set()
        for selector in _UPLOAD_COMPLETE_SELECTORS:
            nodes = page.locator(selector)
            count = await nodes.count()
            for index in range(count):
                node = nodes.nth(index)
                if not await node.is_visible():
                    continue
                marker_id = (await node.get_attribute("data-upload-id") or "").strip()
                if marker_id:
                    marker_ids.add(marker_id)
        return marker_ids

    async def _matching_upload_marker(
        self,
        *,
        expected_upload_name: str,
        expected_sha256: str,
    ) -> str | None:
        page = self._require_page()
        matches: list[str] = []
        for selector in _UPLOAD_COMPLETE_SELECTORS:
            nodes = page.locator(selector)
            for index in range(await nodes.count()):
                node = nodes.nth(index)
                if not await node.is_visible():
                    continue
                marker_id = (await node.get_attribute("data-upload-id") or "").strip()
                if not marker_id:
                    continue
                observed_names = [
                    value
                    for value in [(await node.get_attribute("data-file-name") or "").strip()]
                    if value
                ]
                filename_nodes = node.locator('[data-automation-id="uploadedFileName"]')
                filename_count = await filename_nodes.count()
                if filename_count > 4:
                    raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
                for filename_index in range(filename_count):
                    filename_node = filename_nodes.nth(filename_index)
                    if await filename_node.is_visible():
                        observed_name = (await filename_node.inner_text()).strip()
                        if observed_name:
                            observed_names.append(observed_name)

                observed_digests = [
                    value
                    for value in [
                        (await node.get_attribute("data-file-sha256") or "").strip().casefold()
                    ]
                    if value
                ]
                digest_nodes = node.locator("[data-file-sha256]")
                digest_count = await digest_nodes.count()
                if digest_count > 4:
                    raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
                for digest_index in range(digest_count):
                    digest_node = digest_nodes.nth(digest_index)
                    if await digest_node.is_visible():
                        observed_digest = (
                            (await digest_node.get_attribute("data-file-sha256") or "")
                            .strip()
                            .casefold()
                        )
                        if observed_digest:
                            observed_digests.append(observed_digest)

                digest_matches = bool(observed_digests) and all(
                    compare_digest(observed_digest, expected_sha256)
                    for observed_digest in observed_digests
                )
                name_matches = any(
                    observed_name.casefold() == expected_upload_name.casefold()
                    for observed_name in observed_names
                )
                if not digest_matches and not name_matches:
                    continue
                # Filename evidence is never sufficient. The ATS marker must
                # expose the exact selected-CV digest, and every additional
                # exposed identifier must agree with the expected upload.
                if not digest_matches or any(
                    observed_name.casefold() != expected_upload_name.casefold()
                    for observed_name in observed_names
                ):
                    raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
                matches.append(marker_id)
        unique = tuple(dict.fromkeys(matches))
        return unique[0] if len(unique) == 1 else None

    async def ensure_resume_attachment(
        self,
        *,
        resume_bytes: bytes,
        cv_id: str,
        expected_sha256: str,
    ) -> WorkdayAttachmentProof:
        page = self._require_page()
        if not compare_digest(hashlib.sha256(resume_bytes).hexdigest(), expected_sha256):
            raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        extension, mime_type = _resume_payload_kind(resume_bytes)
        local_name_digest = hashlib.sha256(
            token_bytes(32) + bytes.fromhex(expected_sha256)
        ).hexdigest()
        upload_name = f"resume-{local_name_digest[:24]}.{extension}"
        file_inputs = page.locator(
            '[data-automation-id="resumeUpload"] input[type="file"], '
            'input[data-automation-id="file-upload-input"][type="file"]'
        )
        if await file_inputs.count() != 1:
            raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        file_input = file_inputs.first
        before_marker_ids = await self._upload_marker_ids()
        try:
            await file_input.set_input_files(
                {
                    "name": upload_name,
                    "mimeType": mime_type,
                    "buffer": resume_bytes,
                },
                timeout=_ACTION_TIMEOUT_MS,
            )
            input_value = await file_input.input_value()
        except Exception as exc:
            raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED) from exc
        selected_basename = input_value.replace("\\", "/").rsplit("/", 1)[-1]
        if selected_basename.casefold() != upload_name.casefold():
            raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)

        marker_id: str | None = None
        for _poll in range(20):
            marker_id = await self._matching_upload_marker(
                expected_upload_name=upload_name,
                expected_sha256=expected_sha256,
            )
            if marker_id is not None and marker_id not in before_marker_ids:
                break
            marker_id = None
            await page.wait_for_timeout(100)
        if marker_id is None:
            raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)

        receipt = hashlib.sha256(
            (
                f"{expected_sha256}|{marker_id}|{hashlib.sha256(upload_name.encode()).hexdigest()}"
            ).encode()
        ).hexdigest()
        self._attachment_marker_id = marker_id
        self._attachment_upload_name = upload_name
        self._attachment = WorkdayAttachmentProof(
            cv_id=cv_id,
            cv_sha256=expected_sha256,
            upload_complete=True,
            receipt_sha256=receipt,
        )
        return self._attachment

    async def verify_resume_attachment(
        self,
        *,
        cv_id: str,
        expected_sha256: str,
    ) -> WorkdayAttachmentProof:
        proof = self._attachment
        marker_id = self._attachment_marker_id
        upload_name = self._attachment_upload_name
        if (
            proof is None
            or marker_id is None
            or upload_name is None
            or not proof.matches(cv_id=cv_id, cv_sha256=expected_sha256)
            or await self._matching_upload_marker(
                expected_upload_name=upload_name,
                expected_sha256=expected_sha256,
            )
            != marker_id
        ):
            return WorkdayAttachmentProof(
                cv_id=cv_id,
                cv_sha256=expected_sha256,
                upload_complete=False,
            )
        return proof

    @staticmethod
    async def _radio_by_value(wrapper: Any, value: str) -> Any:
        radios = wrapper.locator('input[type="radio"]')
        matches = []
        for index in range(await radios.count()):
            candidate = radios.nth(index)
            if await candidate.get_attribute("value") == value:
                matches.append(candidate)
        if len(matches) != 1:
            raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
        return matches[0]

    async def fill(self, decisions: tuple[AnswerDecisionV1, ...]) -> None:
        page = self._require_page()
        fields = {
            field.field_id: field
            for field in observe_workday_v2_fields((await self.snapshot()).html)
        }
        for decision in decisions:
            if decision.disposition is not AnswerDisposition.RESOLVED:
                continue
            field = fields.get(decision.field_id)
            if field is None:
                raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
            if (
                field.field_type is FieldType.FILE
                and decision.value == VERIFIED_ATTACHMENT_SENTINEL
            ):
                continue
            wrapper = page.locator(
                f'[data-automation-id="formField"][data-field-id="{field.field_id}"]'
            )
            if await wrapper.count() != 1:
                raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
            control = wrapper.locator("input, textarea, select").first
            value = decision.value
            try:
                if field.field_type in {FieldType.SELECT, FieldType.MULTI_SELECT}:
                    await control.select_option(
                        value=list(value) if isinstance(value, tuple) else str(value)
                    )
                elif field.field_type is FieldType.RADIO:
                    await (await self._radio_by_value(wrapper, str(value))).check()
                elif field.field_type in {
                    FieldType.CHECKBOX,
                    FieldType.CONSENT,
                    FieldType.ATTESTATION,
                }:
                    await (control.check() if value is True else control.uncheck())
                else:
                    await control.fill(str(value))
            except WorkdayAdapterBlockedError:
                raise
            except Exception as exc:
                raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED) from exc

    async def advance_reversible_step(self) -> None:
        """Click exactly one non-final Next control after adapter verification."""

        page = self._require_page()
        assessment = assess_workday_v2_snapshot(
            (await self.snapshot()).html,
            page.url,
        )
        if assessment.reason_code is not None:
            raise WorkdayAdapterBlockedError(assessment.reason_code)
        if assessment.state not in {
            WorkdayPageState.FORM,
            WorkdayPageState.RESUME_UPLOAD,
        }:
            raise WorkdayAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        next_button = page.locator('button[data-automation-id="bottom-navigation-next-button"]')
        if await next_button.count() != 1 or not await next_button.is_visible():
            raise WorkdayAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        if "submit" in (await next_button.inner_text()).strip().casefold():
            raise WorkdayAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        await next_button.click(timeout=_ACTION_TIMEOUT_MS)
        await page.wait_for_timeout(300)
        await self._assert_current_url()

    async def commit_final_action(
        self,
        expectation: WorkdayFinalCommitExpectation,
    ) -> WorkdayFinalActionReceipt:
        """Revalidate and release one exact POST through a one-shot route gate."""

        page = self._require_page()
        if self._clicked:
            raise WorkdayAdapterBlockedError(ReasonCode.PERMIT_REPLAYED)
        guard = self._network_guard
        if guard is None or expectation.job_identity != guard.expected_job_identity:
            raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
        snapshot = await self.snapshot()
        if assess_workday_v2_snapshot(
            snapshot.html, snapshot.url
        ).state is not WorkdayPageState.REVIEW or not compare_digest(
            hashlib.sha256(snapshot.html.encode("utf-8")).hexdigest(),
            expectation.pre_action_digest,
        ):
            raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
        request_contract = workday_v2_final_action_contract(
            snapshot.html,
            snapshot.url,
            expectation.job_identity,
        )
        if (
            request_contract is None
            or not compare_digest(request_contract.digest, expectation.final_action_binding)
            or not compare_digest(
                request_contract.digest,
                expectation.request_contract.digest,
            )
        ):
            raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
        proof = await self.verify_resume_attachment(
            cv_id=expectation.selected_cv_id,
            expected_sha256=expectation.selected_cv_hash,
        )
        if (
            not proof.matches(
                cv_id=expectation.selected_cv_id,
                cv_sha256=expectation.selected_cv_hash,
            )
            or proof.receipt_sha256 is None
            or not compare_digest(
                proof.receipt_sha256,
                expectation.attachment_receipt_sha256,
            )
        ):
            raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)

        review = page.locator('[data-automation-id="reviewPage"]')
        if await review.count() != 1 or not await review.is_visible():
            raise WorkdayAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        submit = review.locator('button[data-automation-id="submitApplication"]')
        if (
            await submit.count() != 1
            or not await submit.is_visible()
            or not await submit.is_enabled()
        ):
            raise WorkdayAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        handle = await submit.element_handle()
        if handle is None:
            raise WorkdayAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        try:
            control = await handle.evaluate(
                _CAPTURE_FINAL_CONTROL_JS,
                {
                    "cvSha256": expectation.selected_cv_hash,
                    "jobRequisition": expectation.job_identity.requisition,
                    "careerSite": expectation.job_identity.site,
                    "answerBindings": [
                        {
                            "fieldId": binding.field_id,
                            "fieldType": binding.field_type.value,
                            "valueSha256": binding.value_sha256,
                        }
                        for binding in expectation.answer_bindings
                    ],
                },
            )
        except Exception as exc:
            raise WorkdayAdapterBlockedError(ReasonCode.SELECTOR_DRIFT) from exc
        if (
            not isinstance(control, dict)
            or control.get("connected") is not True
            or control.get("inReview") is not True
            or control.get("reviewCount") != 1
            or control.get("type") != "submit"
            or control.get("explicitForm") is not False
            or control.get("explicitFormAction") is not False
            or control.get("explicitFormMethod") is not False
            or control.get("disabled") is not False
            or control.get("ariaDisabled") is not False
            or control.get("inert") is not False
            or control.get("actionable") is not True
            or control.get("stateStable") is not True
            or control.get("payloadValid") is not True
            or not isinstance(control.get("action"), str)
            or not isinstance(control.get("method"), str)
            or control.get("encoding") != "multipart/form-data"
            or not isinstance(control.get("stateDigest"), str)
            or len(control["stateDigest"]) != 64
            or any(character not in "0123456789abcdef" for character in control["stateDigest"])
            or not isinstance(control.get("payloadDigest"), str)
            or len(control["payloadDigest"]) != 64
            or any(character not in "0123456789abcdef" for character in control["payloadDigest"])
        ):
            raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
        element_contract = workday_v2_request_contract(
            urljoin(snapshot.url, control["action"]),
            control["method"],
            expectation.job_identity,
        )
        if element_contract is None or not compare_digest(
            element_contract.digest,
            expectation.request_contract.digest,
        ):
            raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
        try:
            bound_expectation = WorkdayBoundFinalCommitExpectation(
                base=expectation,
                request_contract=WorkdayBoundFinalRequestContract.bind(
                    element_contract,
                    control["payloadDigest"],
                ),
            )
        except ValueError as exc:
            raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED) from exc

        # Recheck the exact receipt after retaining the exact element handle.
        proof = await self.verify_resume_attachment(
            cv_id=expectation.selected_cv_id,
            expected_sha256=expectation.selected_cv_hash,
        )
        if (
            not proof.matches(
                cv_id=expectation.selected_cv_id,
                cv_sha256=expectation.selected_cv_hash,
            )
            or proof.receipt_sha256 is None
            or not compare_digest(
                proof.receipt_sha256,
                expectation.attachment_receipt_sha256,
            )
        ):
            raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        marker_id = self._attachment_marker_id
        upload_name = self._attachment_upload_name
        if not marker_id or not upload_name:
            raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)

        try:
            gate = _OneShotFinalRequestGate(bound_expectation, page=page)
        except ValueError as exc:
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        self._final_request_gate = gate
        self._clicked = True
        try:
            atomic_result = await handle.evaluate(
                _ATOMIC_FINAL_SUBMIT_JS,
                {
                    "action": control["action"],
                    "method": control["method"].strip().casefold(),
                    "encoding": control["encoding"],
                    "stateDigest": control["stateDigest"],
                    "payloadDigest": bound_expectation.request_contract.payload_sha256,
                    "markerId": marker_id,
                    "uploadName": upload_name,
                    "cvSha256": expectation.selected_cv_hash,
                    "jobRequisition": expectation.job_identity.requisition,
                    "careerSite": expectation.job_identity.site,
                    "answerBindings": [
                        {
                            "fieldId": binding.field_id,
                            "fieldType": binding.field_type.value,
                            "valueSha256": binding.value_sha256,
                        }
                        for binding in expectation.answer_bindings
                    ],
                },
            )
        except Exception as exc:
            # The atomic browser task contains the native irreversible submit.
            # If its transport response is lost, execution position is unknown.
            raise WorkdayFinalActionAmbiguousError(
                ReasonCode.FINAL_ACTION_UNCONFIRMED.value
            ) from exc
        if isinstance(atomic_result, dict) and atomic_result.get("released") is False:
            gate.reject()
            raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
        if not isinstance(atomic_result, dict) or atomic_result.get("released") is not True:
            raise WorkdayFinalActionAmbiguousError(ReasonCode.FINAL_ACTION_UNCONFIRMED.value)
        try:
            await asyncio.wait_for(
                gate.completed.wait(),
                timeout=_ACTION_TIMEOUT_MS / 1000,
            )
        except Exception as exc:
            raise WorkdayFinalActionAmbiguousError(
                ReasonCode.FINAL_ACTION_UNCONFIRMED.value
            ) from exc
        if gate.rejected:
            raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
        if gate.receipt is None:
            raise WorkdayFinalActionAmbiguousError(ReasonCode.FINAL_ACTION_UNCONFIRMED.value)
        return gate.receipt

    async def confirmation_reference(self) -> str | None:
        """Require one stable, browser-visible nonblank employer reference."""

        page = self._require_page()
        await self._assert_current_url()
        locator = page.locator('main[data-automation-id="confirmationPage"][data-application-id]')
        if await locator.count() != 1 or not await locator.is_visible():
            return None
        first = (await locator.get_attribute("data-application-id") or "").strip()
        if not first:
            return None
        await page.wait_for_timeout(250)
        await self._assert_current_url()
        locator = page.locator('main[data-automation-id="confirmationPage"][data-application-id]')
        if await locator.count() != 1 or not await locator.is_visible():
            return None
        second = (await locator.get_attribute("data-application-id") or "").strip()
        return first if first == second and second else None

    async def close(self) -> None:
        context, playwright, lease = self._context, self._playwright, self._lease
        self._page = None
        self._context = None
        self._playwright = None
        self._lease = None
        self._network_guard = None
        self._attachment_marker_id = None
        self._attachment_upload_name = None
        self._final_request_gate = None
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


def playwright_workday_browser_factory(_url: str) -> WorkdayCandidateSession:
    """Create a lazy local session; no browser starts until ``navigate``."""

    return PlaywrightWorkdayCandidateSession()
