"""Ephemeral local Playwright transport for Greenhouse browser v1.

The transport never reads Chrome or Edge profiles or password stores.  It
launches one isolated context, blocks WebSockets and unexpected network
destinations, uploads immutable in-memory CV bytes, and exposes only a narrow
candidate-session protocol to the two-phase adapter.
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
from collections.abc import Callable
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from io import BytesIO
from secrets import token_bytes
from typing import Any
from urllib.parse import urlsplit

from core.config import Settings, get_settings
from core.submission_domain import (
    VERIFIED_ATTACHMENT_SENTINEL,
    AnswerDecisionV1,
    AnswerDisposition,
    FieldType,
    ReasonCode,
)
from submitters.greenhouse_identity import (
    GreenhouseApplicationIdentity,
    GreenhouseIdentityError,
    parse_greenhouse_candidate_url,
)
from submitters.greenhouse_v1 import (
    GREENHOUSE_CONFIRMATION_SELECTOR,
    GREENHOUSE_V1_NATIVE_TRANSPORT,
    GreenhouseAdapterBlockedError,
    GreenhouseAnswerBinding,
    GreenhouseAtomicCommitExpectation,
    GreenhouseAtomicCommitObservation,
    GreenhouseAttachmentProof,
    GreenhouseBrowserSnapshot,
    GreenhouseCandidateSession,
    GreenhousePageState,
    GreenhousePayloadBinding,
    GreenhouseReviewedAnswerBinding,
    GreenhouseSubmitterBinding,
    assess_greenhouse_v1_snapshot,
    detect_greenhouse_variant,
    greenhouse_v1_dom_commitment,
    greenhouse_v1_form_fingerprint,
    greenhouse_visible_confirmation_digest,
    observe_greenhouse_v1_fields,
)

_NAVIGATION_TIMEOUT_MS = 45_000
_ACTION_TIMEOUT_MS = 8_000
_UPLOAD_COMPLETE_SELECTORS = (
    '[data-qa="resume-upload-complete"][data-upload-id]',
    '[data-qa="file-upload-success"][data-upload-id]',
    "[data-greenhouse-upload-complete][data-upload-id]",
)
_RESUME_INPUT_SELECTOR = (
    '[data-field-id="resume"] input[type="file"], '
    '[data-field-id="resume_upload"] input[type="file"], '
    'input#resume[type="file"], '
    'input[name="resume"][type="file"]'
)
_SCOPED_FINAL_ACTION_SELECTOR = (
    'button#submit_app[type="submit"], '
    'button[data-qa="submit-application"][type="submit"], '
    'button[data-greenhouse-submit][type="submit"]'
)
_ACTION_BOARD_KEYS = frozenset({"board", "board_id", "board_token", "for"})
_ACTION_JOB_KEYS = frozenset(
    {
        "gh_jid",
        "job",
        "job_id",
        "job_token",
        "posting_id",
        "requisition_id",
    }
)
_SYSTEM_CONTROL_NAMES = frozenset({"authenticity_token", "csrf_token", "_csrf", "utf8"})
_MAX_NATIVE_BODY_BYTES = 12 * 1024 * 1024
_MAX_NATIVE_FILE_BYTES = 8 * 1024 * 1024
_MAX_NATIVE_TEXT_BYTES = 1024 * 1024
_MAX_NATIVE_ENTRIES = 128
_MULTIPART_CONTENT_TYPE = re.compile(
    r"""multipart/form-data\s*;\s*boundary=(?:"([^"]{1,70})"|([^;\s]{1,70}))""",
    re.IGNORECASE,
)
_MULTIPART_BOUNDARY = re.compile(r"^[0-9A-Za-z'()+_,./:=?-]{1,70}$")
_FINAL_CONTROL_STATE_SCRIPT = """
(button, form) => {
  const result = {
    connected: false,
    exactForm: false,
    disabled: true,
    ariaDisabled: true,
    inert: true,
    hidden: true,
    cssActionable: false,
    hasRect: false,
    actionable: false,
  };
  try {
    if (!(button instanceof HTMLButtonElement) || !(form instanceof HTMLFormElement)) {
      return result;
    }
    const connected = button.isConnected && form.isConnected;
    const exactForm = (
      button.form === form
      && form.contains(button)
      && !button.hasAttribute("form")
    );
    const disabled = button.disabled || button.matches(":disabled");
    let ariaDisabled = false;
    let inert = false;
    let hidden = false;
    let cssActionable = true;
    for (
      let current = button;
      current instanceof HTMLElement;
      current = current.parentElement
    ) {
      ariaDisabled = ariaDisabled
        || String(current.getAttribute("aria-disabled") || "").trim().toLowerCase()
          === "true";
      inert = inert || current.inert === true || current.hasAttribute("inert");
      hidden = hidden
        || current.hidden
        || String(current.getAttribute("aria-hidden") || "").trim().toLowerCase()
          === "true";
      const style = getComputedStyle(current);
      cssActionable = cssActionable
        && style.display !== "none"
        && !["hidden", "collapse"].includes(style.visibility)
        && style.opacity !== "0"
        && style.pointerEvents !== "none"
        && style.contentVisibility !== "hidden";
    }
    const hasRect = Array.from(button.getClientRects()).some(
      rectangle => rectangle.width > 0 && rectangle.height > 0
    );
    return {
      connected,
      exactForm,
      disabled,
      ariaDisabled,
      inert,
      hidden,
      cssActionable,
      hasRect,
      actionable: Boolean(
        connected
        && exactForm
        && !disabled
        && !ariaDisabled
        && !inert
        && !hidden
        && cssActionable
        && hasRect
      ),
    };
  } catch {
    return result;
  }
}
"""
_ACTION_IDENTITY_ENTRIES_SCRIPT = """
button => {
  const form = button.form;
  if (!form || !form.isConnected || !button.isConnected) return null;
  const normalized = raw => String(raw || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  const kind = key => {
    if (["board", "board_id", "board_token", "for"].includes(key)) return "board";
    if (
      ["gh_jid", "job", "job_id", "job_token", "posting_id", "requisition_id"]
        .includes(key)
    ) return "job";
    return null;
  };
  let data;
  try {
    data = new FormData(form, button);
  } catch {
    return null;
  }
  const entries = [];
  for (const [rawName, rawValue] of data.entries()) {
    const key = normalized(rawName);
    const category = kind(key);
    if (!category) continue;
    if (typeof rawValue !== "string") return null;
    entries.push([category, key, rawValue.trim()]);
  }
  return entries.sort(
    (left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right))
  );
}
"""
_FORM_PAYLOAD_COMMITMENT_SCRIPT = """
async (button, expected) => {
  const form = button.form;
  if (
    !form
    || !form.isConnected
    || !button.isConnected
    || !/^[0-9a-f]{64}$/.test(String(expected.cvSha256 || ""))
    || !Array.isArray(expected.reviewedAnswers)
    || !Array.isArray(expected.actionIdentityEntries)
    || typeof CSS.escape !== "function"
  ) {
    return null;
  }
  const encoder = new TextEncoder();
  const sha256 = async bytes => {
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest))
      .map(value => value.toString(16).padStart(2, "0"))
      .join("");
  };
  const base64Utf8 = value => {
    const bytes = encoder.encode(value);
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
    }
    return btoa(binary);
  };
  const normalizeText = value => value.replace(/\\r\\n|\\r|\\n/g, "\\r\\n");
  const normalized = raw => String(raw || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  const kind = key => {
    if (["board", "board_id", "board_token", "for"].includes(key)) return "board";
    if (
      ["gh_jid", "job", "job_id", "job_token", "posting_id", "requisition_id"]
        .includes(key)
    ) return "job";
    return null;
  };
  const actionIdentity = data => {
    const entries = [];
    for (const [rawName, rawValue] of data.entries()) {
      const key = normalized(rawName);
      const category = kind(key);
      if (!category) continue;
      if (typeof rawValue !== "string") return null;
      entries.push([category, key, rawValue.trim()]);
    }
    return entries.sort(
      (left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right))
    );
  };
  const digestMaterial = material => sha256(
    encoder.encode(JSON.stringify(material))
  );
  const supportedInputType = (control, fieldType) => {
    if (!(control instanceof HTMLInputElement)) return false;
    const expectedTypes = {
      text: "text",
      date: "date",
      number: "number",
      email: "email",
      phone: "tel",
      url: "url",
    };
    return control.type === expectedTypes[fieldType];
  };
  const observedAnswer = async reviewed => {
    if (
      !reviewed
      || typeof reviewed.fieldId !== "string"
      || typeof reviewed.fieldType !== "string"
      || !/^[0-9a-f]{64}$/.test(String(reviewed.valueSha256 || ""))
      || !Number.isInteger(reviewed.successfulEntryCount)
      || reviewed.successfulEntryCount < 0
      || reviewed.successfulEntryCount > 32
    ) {
      return null;
    }
    const escaped = CSS.escape(reviewed.fieldId);
    const wrappers = form.querySelectorAll([
      `[data-gh-field][data-field-id="${escaped}"]`,
      `[data-qa="application-field"][data-field-id="${escaped}"]`,
      `.field[data-field-id="${escaped}"]`,
      `fieldset[data-field-id="${escaped}"]`,
    ].join(","));
    if (wrappers.length !== 1) return null;
    const controls = Array.from(
      wrappers[0].querySelectorAll("input:not([type='hidden']), textarea, select")
    );
    if (!controls.length || controls.some(control => control.disabled)) return null;
    let controlName = "";
    let material = null;
    let entryCount = 0;
    const oneName = candidates => {
      const names = new Set(candidates.map(control => String(control.name || "")));
      return names.size === 1 ? Array.from(names)[0] : "";
    };
    if (reviewed.fieldType === "multi_select") {
      if (controls.length === 1 && controls[0] instanceof HTMLSelectElement) {
        if (!controls[0].multiple) return null;
        controlName = String(controls[0].name || "");
        const values = Array.from(controls[0].selectedOptions)
          .map(option => normalizeText(option.value))
          .sort();
        material = ["m", values];
        entryCount = values.length;
      } else if (
        controls.every(control => (
          control instanceof HTMLInputElement && control.type === "checkbox"
        ))
      ) {
        controlName = oneName(controls);
        const values = controls
          .filter(control => control.checked)
          .map(control => normalizeText(control.value))
          .sort();
        material = ["m", values];
        entryCount = values.length;
      } else {
        return null;
      }
    } else if (reviewed.fieldType === "radio") {
      if (!controls.every(control => (
        control instanceof HTMLInputElement && control.type === "radio"
      ))) {
        return null;
      }
      controlName = oneName(controls);
      const checked = controls.filter(control => control.checked);
      if (checked.length !== 1) return null;
      material = ["s", normalizeText(checked[0].value)];
      entryCount = 1;
    } else if (
      ["checkbox", "consent", "attestation"].includes(reviewed.fieldType)
    ) {
      if (
        controls.length !== 1
        || !(controls[0] instanceof HTMLInputElement)
        || controls[0].type !== "checkbox"
      ) {
        return null;
      }
      controlName = String(controls[0].name || "");
      material = ["b", controls[0].checked];
      entryCount = controls[0].checked ? 1 : 0;
    } else if (reviewed.fieldType === "file") {
      if (
        controls.length !== 1
        || !(controls[0] instanceof HTMLInputElement)
        || controls[0].type !== "file"
        || controls[0].files?.length !== 1
      ) {
        return null;
      }
      controlName = String(controls[0].name || "");
      const digest = await sha256(await controls[0].files[0].arrayBuffer());
      if (digest !== expected.cvSha256) return null;
      material = ["f", expected.cvSha256];
      entryCount = 1;
    } else {
      if (controls.length !== 1) return null;
      const control = controls[0];
      if (reviewed.fieldType === "textarea") {
        if (!(control instanceof HTMLTextAreaElement)) return null;
      } else if (reviewed.fieldType === "select") {
        if (!(control instanceof HTMLSelectElement) || control.multiple) return null;
      } else if (!supportedInputType(control, reviewed.fieldType)) {
        return null;
      }
      controlName = String(control.name || "");
      material = ["s", normalizeText(control.value)];
      entryCount = 1;
    }
    if (
      !controlName
      || encoder.encode(controlName).length > 256
      || entryCount !== reviewed.successfulEntryCount
    ) {
      return null;
    }
    const valueSha256 = await digestMaterial(material);
    const controlNameSha256 = await sha256(encoder.encode(controlName));
    if (
      valueSha256 !== reviewed.valueSha256
      || (
        reviewed.controlNameSha256
        && reviewed.controlNameSha256 !== controlNameSha256
      )
    ) {
      return null;
    }
    return {
      fieldId: reviewed.fieldId,
      fieldType: reviewed.fieldType,
      valueSha256,
      successfulEntryCount: entryCount,
      controlNameSha256,
      controlName,
    };
  };
  let data;
  try {
    data = new FormData(form, button);
  } catch {
    return null;
  }
  const observedAnswers = [];
  for (const reviewed of expected.reviewedAnswers) {
    const observed = await observedAnswer(reviewed);
    if (!observed) return null;
    observedAnswers.push(observed);
  }
  if (
    observedAnswers.length !== expected.reviewedAnswers.length
    || new Set(observedAnswers.map(item => item.fieldId)).size
      !== observedAnswers.length
    || new Set(observedAnswers.map(item => item.controlNameSha256)).size
      !== observedAnswers.length
  ) {
    return null;
  }
  const byName = new Map(observedAnswers.map(item => [item.controlName, item]));
  const seenByField = new Map(observedAnswers.map(item => [item.fieldId, []]));
  const systemCounts = new Map();
  const systemNames = new Set(["authenticity_token", "csrf_token", "_csrf", "utf8"]);
  const submitName = String(button.name || "");
  const submitValue = String(button.value || "");
  let submitCount = 0;
  const actualIdentity = actionIdentity(data);
  if (
    actualIdentity === null
    || JSON.stringify(actualIdentity) !== JSON.stringify(expected.actionIdentityEntries)
  ) {
    return null;
  }
  for (const [rawName, rawValue] of data.entries()) {
    const name = String(rawName);
    const answer = byName.get(name);
    if (answer) {
      seenByField.get(answer.fieldId).push(rawValue);
      continue;
    }
    if (kind(normalized(name))) {
      continue;
    }
    if (submitName && name === submitName) {
      if (typeof rawValue !== "string" || rawValue !== submitValue) return null;
      submitCount += 1;
      continue;
    }
    if (systemNames.has(name)) {
      const controls = Array.from(form.elements).filter(control => (
        control instanceof HTMLInputElement
        && control.type === "hidden"
        && !control.disabled
        && control.form === form
        && control.name === name
      ));
      if (
        controls.length !== 1
        || typeof rawValue !== "string"
        || (name === "utf8" && rawValue !== "✓")
        || (name !== "utf8" && !rawValue)
      ) {
        return null;
      }
      systemCounts.set(name, (systemCounts.get(name) || 0) + 1);
      continue;
    }
    return null;
  }
  if (
    Array.from(systemCounts.values()).some(count => count !== 1)
    || (submitName ? submitCount !== 1 : submitCount !== 0)
  ) {
    return null;
  }
  let resumeControlNameSha256 = null;
  for (const answer of observedAnswers) {
    const values = seenByField.get(answer.fieldId);
    if (!Array.isArray(values) || values.length !== answer.successfulEntryCount) {
      return null;
    }
    let material;
    if (answer.fieldType === "file") {
      if (
        values.length !== 1
        || !(values[0] instanceof File)
        || await sha256(await values[0].arrayBuffer()) !== expected.cvSha256
      ) {
        return null;
      }
      material = ["f", expected.cvSha256];
      if (resumeControlNameSha256 !== null) return null;
      resumeControlNameSha256 = answer.controlNameSha256;
    } else if (answer.fieldType === "multi_select") {
      if (values.some(value => typeof value !== "string")) return null;
      material = ["m", values.map(value => normalizeText(value)).sort()];
    } else if (
      ["checkbox", "consent", "attestation"].includes(answer.fieldType)
    ) {
      if (values.some(value => typeof value !== "string")) return null;
      material = ["b", values.length === 1];
    } else {
      if (values.length !== 1 || typeof values[0] !== "string") return null;
      material = ["s", normalizeText(values[0])];
    }
    if (await digestMaterial(material) !== answer.valueSha256) return null;
  }
  if (resumeControlNameSha256 === null) return null;
  const canonical = [];
  let routedCvCount = 0;
  let totalTextBytes = 0;
  for (const [rawName, rawValue] of data.entries()) {
    if (canonical.length >= 128) return null;
    const name = String(rawName);
    if (!name || encoder.encode(name).length > 256) return null;
    if (typeof rawValue === "string") {
      const value = normalizeText(rawValue);
      const valueBytes = encoder.encode(value);
      totalTextBytes += valueBytes.length;
      if (valueBytes.length > 262144 || totalTextBytes > 1048576) return null;
      canonical.push(["t", base64Utf8(name), base64Utf8(value)]);
      continue;
    }
    if (!(rawValue instanceof File)) return null;
    const filename = String(rawValue.name || "");
    const contentType = String(rawValue.type || "").trim().toLowerCase();
    if (
      filename.includes("/")
      || filename.includes("\\\\")
      || encoder.encode(filename).length > 255
      || !contentType
      || encoder.encode(contentType).length > 160
      || rawValue.size > 8388608
    ) {
      return null;
    }
    const digest = await sha256(await rawValue.arrayBuffer());
    if (digest === expected.cvSha256) routedCvCount += 1;
    canonical.push([
      "f",
      base64Utf8(name),
      base64Utf8(filename),
      base64Utf8(contentType),
      String(rawValue.size),
      digest,
    ]);
  }
  if (!canonical.length || routedCvCount !== 1) return null;
  const payloadCommitment = await sha256(
    encoder.encode(JSON.stringify(canonical))
  );
  const submitterBinding = submitName ? {
    controlNameSha256: await sha256(encoder.encode(submitName)),
    valueSha256: await digestMaterial(["s", normalizeText(submitValue)]),
  } : null;
  if (
    expected.submitterBinding !== undefined
    && JSON.stringify(submitterBinding) !== JSON.stringify(expected.submitterBinding)
  ) {
    return null;
  }
  return {
    payloadCommitment,
    answerBindings: observedAnswers.map(item => ({
      fieldId: item.fieldId,
      fieldType: item.fieldType,
      valueSha256: item.valueSha256,
      successfulEntryCount: item.successfulEntryCount,
      controlNameSha256: item.controlNameSha256,
    })),
    resumeControlNameSha256,
    submitterBinding,
  };
}
"""
_ATOMIC_NATIVE_SUBMIT_SCRIPT = """
async (button, expected) => {
  const form = button.form;
  const encoder = new TextEncoder();
  const sha256 = async bytes => {
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest))
      .map(value => value.toString(16).padStart(2, "0"))
      .join("");
  };
  const base64Utf8 = value => {
    const bytes = encoder.encode(value);
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
    }
    return btoa(binary);
  };
  const normalizeText = value => value.replace(/\\r\\n|\\r|\\n/g, "\\r\\n");
  const normalized = raw => String(raw || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  const kind = key => {
    if (["board", "board_id", "board_token", "for"].includes(key)) return "board";
    if (
      ["gh_jid", "job", "job_id", "job_token", "posting_id", "requisition_id"]
        .includes(key)
    ) return "job";
    return null;
  };
  const actionIdentity = data => {
    const entries = [];
    for (const [rawName, rawValue] of data.entries()) {
      const key = normalized(rawName);
      const category = kind(key);
      if (!category) continue;
      if (typeof rawValue !== "string") return null;
      entries.push([category, key, rawValue.trim()]);
    }
    return entries.sort(
      (left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right))
    );
  };
  const digestMaterial = material => sha256(
    encoder.encode(JSON.stringify(material))
  );
  const supportedInputType = (control, fieldType) => {
    if (!(control instanceof HTMLInputElement)) return false;
    const expectedTypes = {
      text: "text",
      date: "date",
      number: "number",
      email: "email",
      phone: "tel",
      url: "url",
    };
    return control.type === expectedTypes[fieldType];
  };
  const observedAnswer = async reviewed => {
    if (
      !reviewed
      || typeof CSS.escape !== "function"
      || typeof reviewed.fieldId !== "string"
      || typeof reviewed.fieldType !== "string"
      || !/^[0-9a-f]{64}$/.test(String(reviewed.valueSha256 || ""))
      || !/^[0-9a-f]{64}$/.test(String(reviewed.controlNameSha256 || ""))
      || !Number.isInteger(reviewed.successfulEntryCount)
      || reviewed.successfulEntryCount < 0
      || reviewed.successfulEntryCount > 32
    ) {
      return null;
    }
    const escaped = CSS.escape(reviewed.fieldId);
    const wrappers = form.querySelectorAll([
      `[data-gh-field][data-field-id="${escaped}"]`,
      `[data-qa="application-field"][data-field-id="${escaped}"]`,
      `.field[data-field-id="${escaped}"]`,
      `fieldset[data-field-id="${escaped}"]`,
    ].join(","));
    if (wrappers.length !== 1) return null;
    const controls = Array.from(
      wrappers[0].querySelectorAll("input:not([type='hidden']), textarea, select")
    );
    if (!controls.length || controls.some(control => control.disabled)) return null;
    const oneName = candidates => {
      const names = new Set(candidates.map(control => String(control.name || "")));
      return names.size === 1 ? Array.from(names)[0] : "";
    };
    let controlName = "";
    let material = null;
    let entryCount = 0;
    if (reviewed.fieldType === "multi_select") {
      if (controls.length === 1 && controls[0] instanceof HTMLSelectElement) {
        if (!controls[0].multiple) return null;
        controlName = String(controls[0].name || "");
        const values = Array.from(controls[0].selectedOptions)
          .map(option => normalizeText(option.value))
          .sort();
        material = ["m", values];
        entryCount = values.length;
      } else if (
        controls.every(control => (
          control instanceof HTMLInputElement && control.type === "checkbox"
        ))
      ) {
        controlName = oneName(controls);
        const values = controls
          .filter(control => control.checked)
          .map(control => normalizeText(control.value))
          .sort();
        material = ["m", values];
        entryCount = values.length;
      } else {
        return null;
      }
    } else if (reviewed.fieldType === "radio") {
      if (!controls.every(control => (
        control instanceof HTMLInputElement && control.type === "radio"
      ))) {
        return null;
      }
      controlName = oneName(controls);
      const checked = controls.filter(control => control.checked);
      if (checked.length !== 1) return null;
      material = ["s", normalizeText(checked[0].value)];
      entryCount = 1;
    } else if (
      ["checkbox", "consent", "attestation"].includes(reviewed.fieldType)
    ) {
      if (
        controls.length !== 1
        || !(controls[0] instanceof HTMLInputElement)
        || controls[0].type !== "checkbox"
      ) {
        return null;
      }
      controlName = String(controls[0].name || "");
      material = ["b", controls[0].checked];
      entryCount = controls[0].checked ? 1 : 0;
    } else if (reviewed.fieldType === "file") {
      if (
        controls.length !== 1
        || !(controls[0] instanceof HTMLInputElement)
        || controls[0].type !== "file"
        || controls[0].files?.length !== 1
      ) {
        return null;
      }
      controlName = String(controls[0].name || "");
      if (
        await sha256(await controls[0].files[0].arrayBuffer())
        !== expected.cvSha256
      ) {
        return null;
      }
      material = ["f", expected.cvSha256];
      entryCount = 1;
    } else {
      if (controls.length !== 1) return null;
      const control = controls[0];
      if (reviewed.fieldType === "textarea") {
        if (!(control instanceof HTMLTextAreaElement)) return null;
      } else if (reviewed.fieldType === "select") {
        if (!(control instanceof HTMLSelectElement) || control.multiple) return null;
      } else if (!supportedInputType(control, reviewed.fieldType)) {
        return null;
      }
      controlName = String(control.name || "");
      material = ["s", normalizeText(control.value)];
      entryCount = 1;
    }
    if (
      !controlName
      || encoder.encode(controlName).length > 256
      || entryCount !== reviewed.successfulEntryCount
      || await sha256(encoder.encode(controlName)) !== reviewed.controlNameSha256
      || await digestMaterial(material) !== reviewed.valueSha256
    ) {
      return null;
    }
    return {...reviewed, controlName};
  };
  const visible = node => {
    if (!node || !node.isConnected) return false;
    const style = getComputedStyle(node);
    return (
      !node.hidden
      && node.getAttribute("aria-hidden") !== "true"
      && style.display !== "none"
      && style.visibility !== "hidden"
      && style.opacity !== "0"
    );
  };
  const finalControlActionable = () => {
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
    ) {
      return false;
    }
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
    return Array.from(button.getClientRects()).some(
      rectangle => rectangle.width > 0 && rectangle.height > 0
    );
  };
  const attachmentReady = () => {
    const markers = Array.from(document.querySelectorAll(expected.uploadMarkerSelector))
      .filter(visible)
      .filter(node => String(node.getAttribute("data-upload-id") || "").trim()
        === expected.uploadMarkerId)
      .filter(node => {
      const digest = String(node.getAttribute("data-file-sha256") || "")
        .trim()
        .toLowerCase();
      const attributeName = String(node.getAttribute("data-file-name") || "").trim();
      const nested = node.querySelector('[data-qa="uploaded-file-name"]');
      const observedName = attributeName || String(nested?.textContent || "").trim();
      const nameMatches = (
        !observedName
        || observedName.toLowerCase() === expected.uploadName.toLowerCase()
      );
      if (digest) {
        return digest === expected.cvSha256 && nameMatches;
      }
      return (
        Boolean(observedName)
        && observedName.toLowerCase() === expected.uploadName.toLowerCase()
      );
    });
    const resumeInputs = Array.from(form.querySelectorAll(expected.resumeInputSelector));
    return (
      markers.length === 1
      && resumeInputs.length === 1
      && resumeInputs[0].files?.length === 1
      && resumeInputs[0].files[0].name.toLowerCase()
        === expected.uploadName.toLowerCase()
    );
  };
  const structureReady = () => (
    form
    && form.isConnected
    && finalControlActionable()
    && form.__greenhouseAtomicCommitMarker === expected.formMarker
    && expected.nativeTransport === "native-multipart-form-post-v1"
    && String(form.getAttribute("method") || "").trim().toLowerCase() === "post"
    && String(form.getAttribute("enctype") || "").trim().toLowerCase()
      === "multipart/form-data"
    && ["", "_self"].includes(
      String(form.getAttribute("target") || "").trim().toLowerCase()
    )
    && new URL(form.getAttribute("action") || location.href, location.href).href
      === expected.resolvedAction
    && String(button.getAttribute("type") || "").trim().toLowerCase() === "submit"
    && !["formaction", "formmethod", "formenctype", "formtarget"]
      .some(name => button.hasAttribute(name))
    && !button.disabled
    && form.checkValidity()
    && form.outerHTML === expected.formOuterHtml
  );
  if (!structureReady()) return "FORM_CHANGED";
  if (!attachmentReady()) return "ATTACHMENT_UNVERIFIED";

  let data;
  try {
    data = new FormData(form, button);
  } catch {
    return "FORM_CHANGED";
  }
  if (!Array.isArray(expected.reviewedAnswers)) return "FORM_CHANGED";
  const observedAnswers = [];
  for (const reviewed of expected.reviewedAnswers) {
    const observed = await observedAnswer(reviewed);
    if (!observed) return "FORM_CHANGED";
    observedAnswers.push(observed);
  }
  if (
    observedAnswers.length !== expected.reviewedAnswers.length
    || new Set(observedAnswers.map(item => item.fieldId)).size
      !== observedAnswers.length
    || new Set(observedAnswers.map(item => item.controlNameSha256)).size
      !== observedAnswers.length
  ) {
    return "FORM_CHANGED";
  }
  const byName = new Map(observedAnswers.map(item => [item.controlName, item]));
  const seenByField = new Map(observedAnswers.map(item => [item.fieldId, []]));
  const systemCounts = new Map();
  const systemNames = new Set(["authenticity_token", "csrf_token", "_csrf", "utf8"]);
  const submitName = String(button.name || "");
  const submitValue = String(button.value || "");
  let submitCount = 0;
  const identityEntries = actionIdentity(data);
  if (
    identityEntries === null
    || JSON.stringify(identityEntries) !== JSON.stringify(expected.actionIdentityEntries)
  ) {
    return "FORM_CHANGED";
  }
  for (const [rawName, rawValue] of data.entries()) {
    const name = String(rawName);
    const answer = byName.get(name);
    if (answer) {
      seenByField.get(answer.fieldId).push(rawValue);
      continue;
    }
    if (kind(normalized(name))) continue;
    if (submitName && name === submitName) {
      if (typeof rawValue !== "string" || rawValue !== submitValue) {
        return "FORM_CHANGED";
      }
      submitCount += 1;
      continue;
    }
    if (systemNames.has(name)) {
      const controls = Array.from(form.elements).filter(control => (
        control instanceof HTMLInputElement
        && control.type === "hidden"
        && !control.disabled
        && control.form === form
        && control.name === name
      ));
      if (
        controls.length !== 1
        || typeof rawValue !== "string"
        || (name === "utf8" && rawValue !== "✓")
        || (name !== "utf8" && !rawValue)
        || systemCounts.has(name)
      ) {
        return "FORM_CHANGED";
      }
      systemCounts.set(name, 1);
      continue;
    }
    return "FORM_CHANGED";
  }
  if (submitName ? submitCount !== 1 : submitCount !== 0) return "FORM_CHANGED";
  let resumeControlNameSha256 = null;
  for (const answer of observedAnswers) {
    const values = seenByField.get(answer.fieldId);
    if (!Array.isArray(values) || values.length !== answer.successfulEntryCount) {
      return "FORM_CHANGED";
    }
    let material;
    if (answer.fieldType === "file") {
      if (
        values.length !== 1
        || !(values[0] instanceof File)
        || await sha256(await values[0].arrayBuffer()) !== expected.cvSha256
      ) {
        return "ATTACHMENT_UNVERIFIED";
      }
      material = ["f", expected.cvSha256];
      if (resumeControlNameSha256 !== null) return "ATTACHMENT_UNVERIFIED";
      resumeControlNameSha256 = answer.controlNameSha256;
    } else if (answer.fieldType === "multi_select") {
      if (values.some(value => typeof value !== "string")) return "FORM_CHANGED";
      material = ["m", values.map(value => normalizeText(value)).sort()];
    } else if (
      ["checkbox", "consent", "attestation"].includes(answer.fieldType)
    ) {
      if (values.some(value => typeof value !== "string") || values.length > 1) {
        return "FORM_CHANGED";
      }
      material = ["b", values.length === 1];
    } else {
      if (values.length !== 1 || typeof values[0] !== "string") {
        return "FORM_CHANGED";
      }
      material = ["s", normalizeText(values[0])];
    }
    if (await digestMaterial(material) !== answer.valueSha256) {
      return "FORM_CHANGED";
    }
  }
  const submitterBinding = submitName ? {
    controlNameSha256: await sha256(encoder.encode(submitName)),
    valueSha256: await digestMaterial(["s", normalizeText(submitValue)]),
  } : null;
  if (
    resumeControlNameSha256 !== expected.resumeControlNameSha256
    || JSON.stringify(submitterBinding) !== JSON.stringify(expected.submitterBinding)
  ) {
    return "FORM_CHANGED";
  }
  const rawEntries = [];
  const canonical = [];
  let routedCvCount = 0;
  let totalTextBytes = 0;
  for (const [rawName, rawValue] of data.entries()) {
    if (canonical.length >= 128) return "FORM_CHANGED";
    const name = String(rawName);
    if (!name || encoder.encode(name).length > 256) return "FORM_CHANGED";
    if (typeof rawValue === "string") {
      const value = normalizeText(rawValue);
      const valueBytes = encoder.encode(value);
      totalTextBytes += valueBytes.length;
      if (valueBytes.length > 262144 || totalTextBytes > 1048576) {
        return "FORM_CHANGED";
      }
      rawEntries.push([name, rawValue]);
      canonical.push(["t", base64Utf8(name), base64Utf8(value)]);
      continue;
    }
    if (!(rawValue instanceof File)) return "ATTACHMENT_UNVERIFIED";
    const filename = String(rawValue.name || "");
    const contentType = String(rawValue.type || "").trim().toLowerCase();
    if (
      filename.includes("/")
      || filename.includes("\\\\")
      || encoder.encode(filename).length > 255
      || !contentType
      || encoder.encode(contentType).length > 160
      || rawValue.size > 8388608
    ) {
      return "ATTACHMENT_UNVERIFIED";
    }
    const digest = await sha256(await rawValue.arrayBuffer());
    if (digest === expected.cvSha256) routedCvCount += 1;
    rawEntries.push([name, rawValue]);
    canonical.push([
      "f",
      base64Utf8(name),
      base64Utf8(filename),
      base64Utf8(contentType),
      String(rawValue.size),
      digest,
    ]);
  }
  if (routedCvCount !== 1) return "ATTACHMENT_UNVERIFIED";
  const commitment = await sha256(encoder.encode(JSON.stringify(canonical)));
  if (commitment !== expected.payloadCommitment) return "FORM_CHANGED";

  if (!structureReady()) return "FORM_CHANGED";
  if (!attachmentReady()) return "ATTACHMENT_UNVERIFIED";

  const finalSubmitName = String(button.getAttribute("name") || "");
  const finalSubmitValue = String(button.value || "");
  let submitProxy = null;
  const removeSubmitProxy = () => {
    if (!submitProxy || !submitProxy.isConnected) return true;
    try {
      const parent = submitProxy.parentNode;
      if (parent) parent.removeChild(submitProxy);
    } catch {
      return false;
    }
    return !submitProxy.isConnected;
  };
  const rejectAfterProxy = reason => {
    removeSubmitProxy();
    return reason;
  };
  if (finalSubmitName) {
    try {
      submitProxy = document.createElement("input");
      submitProxy.type = "hidden";
      submitProxy.name = finalSubmitName;
      submitProxy.value = finalSubmitValue;
      submitProxy.setAttribute(
        "data-greenhouse-atomic-submitter-proxy",
        expected.formMarker
      );
      button.before(submitProxy);
    } catch {
      return rejectAfterProxy("FORM_CHANGED");
    }
  }

  const postProxyStructureReady = () => {
    if (
      !form
      || !form.isConnected
      || !button.isConnected
      || form.__greenhouseAtomicCommitMarker !== expected.formMarker
      || expected.nativeTransport !== "native-multipart-form-post-v1"
      || String(form.getAttribute("method") || "").trim().toLowerCase() !== "post"
      || String(form.getAttribute("enctype") || "").trim().toLowerCase()
        !== "multipart/form-data"
      || !["", "_self"].includes(
        String(form.getAttribute("target") || "").trim().toLowerCase()
      )
      || new URL(form.getAttribute("action") || location.href, location.href).href
        !== expected.resolvedAction
      || String(button.getAttribute("type") || "").trim().toLowerCase() !== "submit"
      || String(button.getAttribute("name") || "") !== finalSubmitName
      || String(button.value || "") !== finalSubmitValue
      || ["formaction", "formmethod", "formenctype", "formtarget"]
        .some(name => button.hasAttribute(name))
      || !form.checkValidity()
    ) {
      return false;
    }
    const proxySelector = "input[data-greenhouse-atomic-submitter-proxy]";
    const proxies = Array.from(form.querySelectorAll(proxySelector));
    if (finalSubmitName) {
      if (
        proxies.length !== 1
        || proxies[0] !== submitProxy
        || submitProxy.form !== form
        || submitProxy.parentNode !== button.parentNode
        || submitProxy.nextSibling !== button
        || submitProxy.type !== "hidden"
        || submitProxy.disabled
        || submitProxy.name !== finalSubmitName
        || submitProxy.value !== finalSubmitValue
        || submitProxy.getAttribute("data-greenhouse-atomic-submitter-proxy")
          !== expected.formMarker
      ) {
        return false;
      }
    } else if (proxies.length !== 0 || submitProxy !== null) {
      return false;
    }
    const clone = form.cloneNode(true);
    if (!(clone instanceof HTMLFormElement)) return false;
    const clonedProxies = Array.from(clone.querySelectorAll(proxySelector));
    if (finalSubmitName) {
      if (clonedProxies.length !== 1) return false;
      clonedProxies[0].remove();
    } else if (clonedProxies.length !== 0) {
      return false;
    }
    return clone.outerHTML === expected.formOuterHtml;
  };
  const finalReleaseFailure = () => {
    let currentData;
    try {
      currentData = new FormData(form);
    } catch {
      return "FORM_CHANGED";
    }
    const currentEntries = Array.from(currentData.entries());
    if (currentEntries.length !== rawEntries.length) return "FORM_CHANGED";
    for (let index = 0; index < rawEntries.length; index += 1) {
      const [expectedName, expectedValue] = rawEntries[index];
      const [currentName, currentValue] = currentEntries[index];
      if (currentName !== expectedName) return "FORM_CHANGED";
      if (typeof expectedValue === "string") {
        if (typeof currentValue !== "string" || currentValue !== expectedValue) {
          return "FORM_CHANGED";
        }
      } else if (
        !(currentValue instanceof File)
        || currentValue !== expectedValue
        || currentValue.name !== expectedValue.name
        || currentValue.type !== expectedValue.type
        || currentValue.size !== expectedValue.size
      ) {
        return "ATTACHMENT_UNVERIFIED";
      }
    }
    const currentIdentityEntries = actionIdentity(currentData);
    if (
      currentIdentityEntries === null
      || JSON.stringify(currentIdentityEntries)
        !== JSON.stringify(expected.actionIdentityEntries)
    ) {
      return "FORM_CHANGED";
    }
    if (!postProxyStructureReady()) return "FORM_CHANGED";
    if (!attachmentReady()) return "ATTACHMENT_UNVERIFIED";
    return null;
  };
  let finalFailure;
  try {
    finalFailure = finalReleaseFailure();
  } catch {
    return rejectAfterProxy("FORM_CHANGED");
  }
  if (finalFailure !== null) return rejectAfterProxy(finalFailure);
  try {
    if (!finalControlActionable()) {
      return rejectAfterProxy("FORM_CHANGED");
    }
  } catch {
    return rejectAfterProxy("FORM_CHANGED");
  }
  try {
    HTMLFormElement.prototype.submit.call(form);
  } catch {
    // Invocation is an ambiguity boundary: the browser may already have
    // started the exact POST, so keep the proxy and let the outbound gate
    // decide whether the request left.
    return "NATIVE_SUBMIT_INVOKED";
  }
  return "NATIVE_SUBMIT_INVOKED";
}
"""


@dataclass(frozen=True, slots=True, repr=False)
class _ValidatedFinalAction:
    form_handle: Any
    button_handle: Any
    resolved_action: str
    native_transport: str
    action_binding: str
    action_identity_entries: tuple[tuple[str, str, str], ...]


@dataclass(slots=True, repr=False)
class _OutboundPostGate:
    expected_hostname: str
    expected_identity: GreenhouseApplicationIdentity
    expected_action_url: str
    expected_transport: str
    expected_payload_commitment: str
    expected_answer_bindings: tuple[GreenhouseAnswerBinding, ...]
    expected_resume_control_name_sha256: str
    expected_submitter_binding: GreenhouseSubmitterBinding | None
    expected_cv_sha256: str
    expected_main_frame: Any
    event: asyncio.Event
    closed: bool = False
    request_may_have_left: bool = False
    outbound_request_sha256: str | None = None
    reason_code: ReasonCode | None = None


def _blocked_atomic_observation(
    expectation: GreenhouseAtomicCommitExpectation,
    reason_code: ReasonCode,
) -> GreenhouseAtomicCommitObservation:
    """Return a typed proof that the exact outbound boundary was not crossed."""

    return GreenhouseAtomicCommitObservation(
        expected_hostname=expectation.expected_hostname,
        expected_identity=expectation.expected_identity,
        fields=expectation.fields,
        variant=expectation.variant,
        form_fingerprint=expectation.form_fingerprint,
        action_binding=expectation.action_binding,
        dom_commitment=expectation.dom_commitment,
        resolved_action_url=expectation.resolved_action_url,
        native_transport=expectation.native_transport,
        payload_commitment=expectation.payload_commitment,
        answer_bindings=expectation.answer_bindings,
        resume_control_name_sha256=expectation.resume_control_name_sha256,
        submitter_binding=expectation.submitter_binding,
        cv_id=expectation.cv_id,
        cv_sha256=expectation.cv_sha256,
        cv_receipt_sha256=expectation.cv_receipt_sha256,
        final_action_invoked=False,
        request_may_have_left=False,
        reason_code=reason_code,
    )


def _action_identity_kind(raw_key: str) -> str | None:
    key = re.sub(r"[^a-z0-9]+", "_", (raw_key or "").strip().casefold()).strip("_")
    if key in _ACTION_BOARD_KEYS:
        return "board"
    if key in _ACTION_JOB_KEYS:
        return "job"
    return None


def _validated_action_identity_entries(
    raw_entries: object,
    expected_identity: GreenhouseApplicationIdentity,
) -> tuple[tuple[str, str, str], ...]:
    if not isinstance(raw_entries, list) or len(raw_entries) > 32:
        raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
    entries: list[tuple[str, str, str]] = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, list) or len(raw_entry) != 3:
            raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
        category, raw_key, raw_value = (str(value).strip() for value in raw_entry)
        key = re.sub(r"[^a-z0-9]+", "_", raw_key.casefold()).strip("_")
        if (
            category != _action_identity_kind(key)
            or not key
            or not raw_value
            or len(key) > 120
            or len(raw_value) > 160
        ):
            raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
        if category == "board" and raw_value.casefold() != expected_identity.board_token:
            raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
        if category == "job" and raw_value != expected_identity.job_token:
            raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
        entries.append((category, key, raw_value.casefold() if category == "board" else raw_value))
    canonical = tuple(sorted(entries))
    categories = tuple(entry[0] for entry in canonical)
    if len(canonical) != len(set(canonical)) or len(categories) != len(set(categories)):
        raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
    return canonical


def _base64_utf8(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _normalized_form_text(value: str) -> str:
    return re.sub(r"\r\n|\r|\n", "\r\n", value)


def _canonical_payload_commitment(entries: list[list[str]]) -> str:
    encoded = json.dumps(
        entries,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _answer_material_sha256(material: list[object]) -> str:
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _multipart_payload_commitment(
    *,
    body: bytes,
    content_type: str,
    expected_cv_sha256: str,
    expected_answer_bindings: tuple[GreenhouseAnswerBinding, ...],
    expected_resume_control_name_sha256: str,
    expected_identity: GreenhouseApplicationIdentity,
    expected_submitter_binding: GreenhouseSubmitterBinding | None,
) -> str:
    """Hash one strict native multipart payload without returning its values."""

    if (
        re.fullmatch(r"[0-9a-f]{64}", expected_cv_sha256) is None
        or re.fullmatch(r"[0-9a-f]{64}", expected_resume_control_name_sha256) is None
        or not expected_answer_bindings
        or not body
        or len(body) > _MAX_NATIVE_BODY_BYTES
        or "\r" in content_type
        or "\n" in content_type
    ):
        raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
    match = _MULTIPART_CONTENT_TYPE.fullmatch(content_type.strip())
    boundary = next((value for value in match.groups() if value), "") if match else ""
    if not boundary or _MULTIPART_BOUNDARY.fullmatch(boundary) is None:
        raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
    delimiter = b"--" + boundary.encode("ascii")
    if not body.startswith(delimiter + b"\r\n") or not (
        body.endswith(b"\r\n" + delimiter + b"--\r\n") or body.endswith(b"\r\n" + delimiter + b"--")
    ):
        raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)

    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + content_type.encode("ascii") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    )
    if message.defects or not message.is_multipart():
        raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
    parts = list(message.iter_parts())
    if not parts or len(parts) > _MAX_NATIVE_ENTRIES:
        raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)

    canonical: list[list[str]] = []
    bindings_by_name = {
        binding.control_name_sha256: binding for binding in expected_answer_bindings
    }
    if (
        len(bindings_by_name) != len(expected_answer_bindings)
        or len(
            [
                binding
                for binding in expected_answer_bindings
                if binding.reviewed.field_type is FieldType.FILE
                and binding.control_name_sha256 == expected_resume_control_name_sha256
            ]
        )
        != 1
    ):
        raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
    observed_by_field: dict[str, list[tuple[str, object]]] = {
        binding.reviewed.field_id: [] for binding in expected_answer_bindings
    }
    system_counts: dict[str, int] = {}
    identity_categories: set[str] = set()
    submitter_count = 0
    total_text_bytes = 0
    routed_resume_cv_count = 0
    for part in parts:
        if part.defects or part.is_multipart():
            raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
        raw_headers = tuple((name.casefold(), value) for name, value in part.raw_items())
        if (
            not raw_headers
            or len(raw_headers) > 2
            or any(name not in {"content-disposition", "content-type"} for name, _ in raw_headers)
            or sum(name == "content-disposition" for name, _ in raw_headers) != 1
        ):
            raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
        disposition = str(part.get("Content-Disposition") or "")
        if len(disposition.encode("utf-8")) > 1024 or part.get_content_disposition() != "form-data":
            raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
        parameters = part.get_params(header="content-disposition", unquote=True) or []
        names = [value for key, value in parameters[1:] if str(key).casefold() == "name"]
        filenames = [value for key, value in parameters[1:] if str(key).casefold() == "filename"]
        if (
            len(names) != 1
            or len(filenames) > 1
            or not isinstance(names[0], str)
            or not names[0]
            or len(names[0].encode("utf-8")) > 256
        ):
            raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
        name = names[0]
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)

        if not filenames:
            if any(header == "content-type" for header, _ in raw_headers):
                if (
                    part.get_content_type().casefold() != "text/plain"
                    or (part.get_content_charset() or "utf-8").casefold() != "utf-8"
                ):
                    raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
            if len(payload) > 262_144:
                raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
            try:
                value = _normalized_form_text(payload.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED) from exc
            total_text_bytes += len(value.encode("utf-8"))
            if total_text_bytes > _MAX_NATIVE_TEXT_BYTES:
                raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
            name_sha256 = hashlib.sha256(name.encode("utf-8")).hexdigest()
            answer_binding = bindings_by_name.get(name_sha256)
            if answer_binding is not None:
                if answer_binding.reviewed.field_type is FieldType.FILE:
                    raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
                observed_by_field[answer_binding.reviewed.field_id].append(("text", value))
            elif (
                expected_submitter_binding is not None
                and name_sha256 == expected_submitter_binding.control_name_sha256
            ):
                if _answer_material_sha256(["s", value]) != expected_submitter_binding.value_sha256:
                    raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
                submitter_count += 1
            else:
                action_kind = _action_identity_kind(name)
                if action_kind is not None:
                    if (
                        action_kind in identity_categories
                        or (
                            action_kind == "board"
                            and value.casefold() != expected_identity.board_token
                        )
                        or (action_kind == "job" and value != expected_identity.job_token)
                    ):
                        raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
                    identity_categories.add(action_kind)
                elif name in _SYSTEM_CONTROL_NAMES:
                    if (
                        (name == "utf8" and value != "✓")
                        or (name != "utf8" and not value)
                        or system_counts.get(name, 0) != 0
                    ):
                        raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
                    system_counts[name] = 1
                else:
                    raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
            canonical.append(["t", _base64_utf8(name), _base64_utf8(value)])
            continue

        filename = filenames[0]
        if (
            not isinstance(filename, str)
            or "/" in filename
            or "\\" in filename
            or len(filename.encode("utf-8")) > 255
            or len(payload) > _MAX_NATIVE_FILE_BYTES
            or sum(header == "content-type" for header, _ in raw_headers) != 1
        ):
            raise GreenhouseAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        file_type = part.get_content_type().strip().casefold()
        if not file_type or len(file_type.encode("utf-8")) > 160:
            raise GreenhouseAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        digest = hashlib.sha256(payload).hexdigest()
        name_sha256 = hashlib.sha256(name.encode("utf-8")).hexdigest()
        answer_binding = bindings_by_name.get(name_sha256)
        if (
            answer_binding is None
            or answer_binding.reviewed.field_type is not FieldType.FILE
            or name_sha256 != expected_resume_control_name_sha256
            or not filename
            or not payload
            or digest != expected_cv_sha256
            or routed_resume_cv_count != 0
        ):
            raise GreenhouseAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        observed_by_field[answer_binding.reviewed.field_id].append(("file", digest))
        routed_resume_cv_count += 1
        canonical.append(
            [
                "f",
                _base64_utf8(name),
                _base64_utf8(filename),
                _base64_utf8(file_type),
                str(len(payload)),
                digest,
            ]
        )

    for binding in expected_answer_bindings:
        reviewed = binding.reviewed
        observed = observed_by_field[reviewed.field_id]
        if len(observed) != reviewed.successful_entry_count:
            raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
        if reviewed.field_type is FieldType.FILE:
            material: list[object] = ["f", expected_cv_sha256]
        elif reviewed.field_type is FieldType.MULTI_SELECT:
            if any(kind != "text" for kind, _ in observed):
                raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
            material = ["m", sorted(str(value) for _, value in observed)]
        elif reviewed.field_type in {
            FieldType.CHECKBOX,
            FieldType.CONSENT,
            FieldType.ATTESTATION,
        }:
            if any(kind != "text" for kind, _ in observed) or len(observed) > 1:
                raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
            material = ["b", len(observed) == 1]
        else:
            if (
                len(observed) != 1
                or observed[0][0] != "text"
                or not isinstance(observed[0][1], str)
            ):
                raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
            material = ["s", observed[0][1]]
        if _answer_material_sha256(material) != reviewed.value_sha256:
            raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)

    expected_submitter_count = 1 if expected_submitter_binding is not None else 0
    if routed_resume_cv_count != 1:
        raise GreenhouseAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
    if submitter_count != expected_submitter_count:
        raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
    return _canonical_payload_commitment(canonical)


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
    raise GreenhouseAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)


class GreenhouseNetworkGuard:
    """Exact-origin HTTPS and public-DNS policy for one candidate session."""

    def __init__(
        self,
        initial_url: str,
        *,
        resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
    ) -> None:
        try:
            candidate = parse_greenhouse_candidate_url(initial_url)
        except GreenhouseIdentityError as exc:
            raise GreenhouseAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        self.expected_hostname = candidate.hostname
        self.expected_identity = candidate.identity
        self._resolver = resolver
        self._dns_verified: set[str] = set()

    @staticmethod
    def _https_hostname(url: str) -> str:
        try:
            parsed = urlsplit((url or "").strip())
            port = parsed.port
        except ValueError as exc:
            raise GreenhouseAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
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
            raise GreenhouseAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            literal = None
        if literal is not None and not literal.is_global:
            raise GreenhouseAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        return hostname

    def require_allowed_url(self, url: str, *, main_frame: bool = True) -> None:
        hostname = self._https_hostname(url)
        if main_frame:
            try:
                parse_greenhouse_candidate_url(
                    url,
                    expected_hostname=self.expected_hostname,
                    expected_identity=self.expected_identity,
                )
            except GreenhouseIdentityError as exc:
                raise GreenhouseAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        elif hostname != self.expected_hostname:
            raise GreenhouseAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
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
            raise GreenhouseAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        addresses = {
            str(answer[4][0]).split("%", 1)[0]
            for answer in answers
            if len(answer) > 4 and answer[4]
        }
        if not addresses:
            raise GreenhouseAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        try:
            resolved = tuple(ipaddress.ip_address(address) for address in addresses)
        except ValueError as exc:
            raise GreenhouseAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        if any(not address.is_global for address in resolved):
            raise GreenhouseAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        self._dns_verified.add(hostname)


class PlaywrightGreenhouseCandidateSession:
    """One lazily launched public Greenhouse candidate page."""

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._network_guard: GreenhouseNetworkGuard | None = None
        self._attachment: GreenhouseAttachmentProof | None = None
        self._attachment_marker_id: str | None = None
        self._attachment_upload_name: str | None = None
        self._last_final_action_binding: str | None = None
        self._outbound_gate: _OutboundPostGate | None = None
        self._clicked = False

    def _require_page(self) -> Any:
        if self._page is None:
            raise GreenhouseAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        return self._page

    async def navigate(self, url: str) -> None:
        if self._page is not None:
            raise GreenhouseAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        guard = GreenhouseNetworkGuard(url)
        await asyncio.to_thread(guard.require_allowed_url, url)
        self._network_guard = guard
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise GreenhouseAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
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
            await self._install_network_routes(self._context)
            self._page = await self._context.new_page()
            await self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=_NAVIGATION_TIMEOUT_MS,
            )
            await self._assert_current_url()
        except Exception as exc:
            await self.close()
            if isinstance(exc, GreenhouseAdapterBlockedError):
                raise
            raise GreenhouseAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc

    async def _install_network_routes(self, context: Any) -> None:
        route_web_socket = getattr(context, "route_web_socket", None)
        if not callable(route_web_socket):
            raise GreenhouseAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        await route_web_socket("**/*", self._block_web_socket)
        await context.route("**/*", self._guard_request)

    @staticmethod
    async def _block_web_socket(web_socket: Any) -> None:
        await web_socket.close(code=1008, reason="browser transport disabled")

    async def _guard_request(self, route: Any, request: Any) -> None:
        guard = self._network_guard
        if guard is None:
            await route.abort("blockedbyclient")
            return
        method = str(getattr(request, "method", "GET") or "GET").strip().upper()
        gate = self._outbound_gate
        if gate is not None and method not in {"GET", "HEAD", "OPTIONS"}:
            await self._guard_outbound_post(
                route,
                request,
                method=method,
                guard=guard,
                gate=gate,
            )
            return
        if gate is None and method not in {"GET", "HEAD", "OPTIONS"}:
            await route.abort("blockedbyclient")
            return
        if gate is not None and not gate.request_may_have_left and request.is_navigation_request():
            gate.closed = True
            gate.reason_code = ReasonCode.FORM_CHANGED
            gate.event.set()
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
        except GreenhouseAdapterBlockedError:
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def _guard_outbound_post(
        self,
        route: Any,
        request: Any,
        *,
        method: str,
        guard: GreenhouseNetworkGuard,
        gate: _OutboundPostGate,
    ) -> None:
        """Permit one exact native main-frame payload and abort every other mutation."""

        if gate.closed or gate.request_may_have_left:
            gate.reason_code = ReasonCode.FINAL_ACTION_UNCONFIRMED
            gate.event.set()
            await route.abort("blockedbyclient")
            return
        try:
            request_frame = request.frame
            resource_type = str(request.resource_type or "").strip().casefold()
            is_navigation = request.is_navigation_request() is True
        except Exception:
            request_frame = None
            resource_type = ""
            is_navigation = False
        if (
            method != "POST"
            or request.url != gate.expected_action_url
            or gate.expected_transport != GREENHOUSE_V1_NATIVE_TRANSPORT
            or not is_navigation
            or resource_type != "document"
            or request_frame != gate.expected_main_frame
        ):
            gate.closed = True
            gate.reason_code = ReasonCode.FORM_CHANGED
            gate.event.set()
            await route.abort("blockedbyclient")
            return
        try:
            candidate = parse_greenhouse_candidate_url(
                gate.expected_action_url,
                expected_hostname=gate.expected_hostname,
                expected_identity=gate.expected_identity,
            )
            if (
                request.url != gate.expected_action_url
                or candidate.hostname != guard.expected_hostname
            ):
                raise GreenhouseIdentityError("GREENHOUSE_EXACT_ACTION_CHANGED")
            await asyncio.to_thread(
                guard.require_allowed_url,
                request.url,
                main_frame=True,
            )
        except GreenhouseIdentityError:
            gate.closed = True
            gate.reason_code = ReasonCode.FORM_CHANGED
            gate.event.set()
            await route.abort("blockedbyclient")
            return
        except GreenhouseAdapterBlockedError:
            gate.closed = True
            gate.reason_code = ReasonCode.RUNTIME_NOT_READY
            gate.event.set()
            await route.abort("blockedbyclient")
            return

        try:
            all_headers = getattr(request, "all_headers", None)
            headers_value = all_headers() if callable(all_headers) else request.headers
            if inspect.isawaitable(headers_value):
                headers_value = await headers_value
            if not isinstance(headers_value, dict):
                raise ValueError("GREENHOUSE_REQUEST_HEADERS_INVALID")
            content_types = [
                str(value)
                for key, value in headers_value.items()
                if str(key).casefold() == "content-type"
            ]
            body_value = request.post_data_buffer
            if callable(body_value):
                body_value = body_value()
            if inspect.isawaitable(body_value):
                body_value = await body_value
            if len(content_types) != 1 or not isinstance(
                body_value,
                (bytes, bytearray, memoryview),
            ):
                raise ValueError("GREENHOUSE_REQUEST_BODY_INVALID")
            observed_commitment = _multipart_payload_commitment(
                body=bytes(body_value),
                content_type=content_types[0],
                expected_cv_sha256=gate.expected_cv_sha256,
                expected_answer_bindings=gate.expected_answer_bindings,
                expected_resume_control_name_sha256=(gate.expected_resume_control_name_sha256),
                expected_identity=gate.expected_identity,
                expected_submitter_binding=gate.expected_submitter_binding,
            )
            if observed_commitment != gate.expected_payload_commitment:
                raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
        except GreenhouseAdapterBlockedError as exc:
            gate.closed = True
            gate.reason_code = exc.reason_code
            gate.event.set()
            await route.abort("blockedbyclient")
            return
        except Exception:
            gate.closed = True
            gate.reason_code = ReasonCode.FORM_CHANGED
            gate.event.set()
            await route.abort("blockedbyclient")
            return

        gate.request_may_have_left = True
        gate.outbound_request_sha256 = hashlib.sha256(request.url.encode("utf-8")).hexdigest()
        gate.event.set()
        try:
            await route.continue_()
        except Exception:
            gate.reason_code = ReasonCode.FINAL_ACTION_UNCONFIRMED
            raise

    async def _assert_current_url(self) -> None:
        page = self._require_page()
        guard = self._network_guard
        if guard is None:
            raise GreenhouseAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        await asyncio.to_thread(guard.require_allowed_url, page.url)

    async def open_candidate_form(self) -> None:
        page = self._require_page()
        apply = page.locator(
            '[data-qa="apply-button"], a[href*="/embed/job_app"], a[href*="/apply"]'
        )
        if await apply.count() != 1 or not await apply.is_visible():
            raise GreenhouseAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        await apply.click(timeout=_ACTION_TIMEOUT_MS)
        await page.wait_for_load_state("domcontentloaded")
        await self._assert_current_url()

    async def snapshot(self) -> GreenhouseBrowserSnapshot:
        page = self._require_page()
        await self._assert_current_url()
        locale = await page.locator("html").get_attribute("lang") or "en"
        return GreenhouseBrowserSnapshot(
            html=await page.content(),
            url=page.url,
            locale=locale,
        )

    async def _upload_marker_ids(self) -> set[str]:
        page = self._require_page()
        marker_ids: set[str] = set()
        for selector in _UPLOAD_COMPLETE_SELECTORS:
            nodes = page.locator(selector)
            for index in range(await nodes.count()):
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
                observed_digest = (
                    (await node.get_attribute("data-file-sha256") or "").strip().casefold()
                )
                observed_name = (await node.get_attribute("data-file-name") or "").strip()
                if not observed_name:
                    filename = node.locator('[data-qa="uploaded-file-name"]')
                    if await filename.count() == 1 and await filename.is_visible():
                        observed_name = (await filename.inner_text()).strip()
                name_matches = (
                    not observed_name or observed_name.casefold() == expected_upload_name.casefold()
                )
                if observed_digest:
                    marker_matches = observed_digest == expected_sha256 and name_matches
                else:
                    marker_matches = (
                        bool(observed_name)
                        and observed_name.casefold() == expected_upload_name.casefold()
                    )
                if marker_matches:
                    matches.append(marker_id)
        unique = tuple(dict.fromkeys(matches))
        return unique[0] if len(unique) == 1 else None

    async def ensure_resume_attachment(
        self,
        *,
        resume_bytes: bytes,
        cv_id: str,
        expected_sha256: str,
    ) -> GreenhouseAttachmentProof:
        page = self._require_page()
        guard = self._network_guard
        if guard is None or self._clicked or self._outbound_gate is not None:
            raise GreenhouseAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        if hashlib.sha256(resume_bytes).hexdigest() != expected_sha256:
            raise GreenhouseAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        extension, mime_type = _resume_payload_kind(resume_bytes)
        if self._attachment is not None and self._attachment.matches(
            cv_id=cv_id, cv_sha256=expected_sha256
        ):
            return await self.verify_resume_attachment(
                cv_id=cv_id,
                expected_sha256=expected_sha256,
            )
        upload_name_digest = hashlib.sha256(
            token_bytes(32) + bytes.fromhex(expected_sha256)
        ).hexdigest()
        upload_name = f"resume-{upload_name_digest[:24]}.{extension}"
        file_inputs = page.locator(_RESUME_INPUT_SELECTOR)
        if await file_inputs.count() != 1:
            raise GreenhouseAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        file_input = file_inputs.first
        before_marker_ids = await self._upload_marker_ids()
        marker_id: str | None = None
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
            selected_basename = input_value.replace("\\", "/").rsplit("/", 1)[-1]
            if selected_basename.casefold() != upload_name.casefold():
                raise GreenhouseAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)

            for _poll in range(20):
                marker_id = await self._matching_upload_marker(
                    expected_upload_name=upload_name,
                    expected_sha256=expected_sha256,
                )
                if marker_id is not None and marker_id not in before_marker_ids:
                    break
                marker_id = None
                await page.wait_for_timeout(100)
        except GreenhouseAdapterBlockedError:
            raise
        except Exception as exc:
            raise GreenhouseAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED) from exc
        if marker_id is None:
            raise GreenhouseAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)

        receipt = hashlib.sha256(
            (
                f"{expected_sha256}|{marker_id}|{hashlib.sha256(upload_name.encode()).hexdigest()}"
            ).encode()
        ).hexdigest()
        self._attachment_marker_id = marker_id
        self._attachment_upload_name = upload_name
        self._attachment = GreenhouseAttachmentProof(
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
    ) -> GreenhouseAttachmentProof:
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
            return GreenhouseAttachmentProof(
                cv_id=cv_id,
                cv_sha256=expected_sha256,
                upload_complete=False,
            )
        return proof

    @staticmethod
    def _quoted_attribute(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    async def _field_wrapper(self, field_id: str) -> Any:
        page = self._require_page()
        quoted = self._quoted_attribute(field_id)
        wrapper = page.locator(
            f'[data-gh-field][data-field-id="{quoted}"], '
            f'[data-qa="application-field"][data-field-id="{quoted}"], '
            f'.field[data-field-id="{quoted}"], '
            f'fieldset[data-field-id="{quoted}"]'
        )
        if await wrapper.count() != 1:
            raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
        return wrapper.first

    @staticmethod
    async def _option_by_value(wrapper: Any, control_type: str, value: str) -> Any:
        controls = wrapper.locator(f'input[type="{control_type}"]')
        matches = []
        for index in range(await controls.count()):
            candidate = controls.nth(index)
            if await candidate.get_attribute("value") == value:
                matches.append(candidate)
        if len(matches) != 1:
            raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
        return matches[0]

    async def fill(self, decisions: tuple[AnswerDecisionV1, ...]) -> None:
        observed = {
            field.field_id: field
            for field in observe_greenhouse_v1_fields((await self.snapshot()).html)
        }
        for decision in decisions:
            if decision.disposition is not AnswerDisposition.RESOLVED:
                continue
            field = observed.get(decision.field_id)
            if field is None:
                raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
            if (
                field.field_type is FieldType.FILE
                and decision.value == VERIFIED_ATTACHMENT_SENTINEL
            ):
                continue
            wrapper = await self._field_wrapper(field.field_id)
            control = wrapper.locator("input:not([type='hidden']), textarea, select").first
            value = decision.value
            try:
                if field.field_type is FieldType.SELECT:
                    await control.select_option(str(value))
                elif field.field_type is FieldType.MULTI_SELECT:
                    values = tuple(value) if isinstance(value, tuple) else (str(value),)
                    if (
                        await control.evaluate("element => element.tagName.toLowerCase()")
                        == "select"
                    ):
                        await control.select_option(list(values))
                    else:
                        for option_value in values:
                            await (
                                await self._option_by_value(
                                    wrapper,
                                    "checkbox",
                                    option_value,
                                )
                            ).check()
                elif field.field_type is FieldType.RADIO:
                    await (await self._option_by_value(wrapper, "radio", str(value))).check()
                elif field.field_type in {
                    FieldType.CHECKBOX,
                    FieldType.CONSENT,
                    FieldType.ATTESTATION,
                }:
                    await (control.check() if value is True else control.uncheck())
                else:
                    await control.fill(str(value))
            except GreenhouseAdapterBlockedError:
                raise
            except Exception as exc:
                raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED) from exc

    async def settle_reversible_form(self) -> None:
        page = self._require_page()
        await page.wait_for_timeout(300)
        await self._assert_current_url()

    async def _validated_form_action(
        self,
        *,
        require_ready: bool,
    ) -> _ValidatedFinalAction | None:
        page = self._require_page()
        if require_ready:
            assessment = assess_greenhouse_v1_snapshot(
                (await self.snapshot()).html,
                page.url,
            )
            if assessment.state is not GreenhousePageState.REVIEW:
                return None
        else:
            await self._assert_current_url()
        forms = page.locator(
            "form#application_form, "
            'form[data-qa="application-form"], '
            "form[data-greenhouse-application]"
        )
        if await forms.count() != 1 or not await forms.is_visible():
            return None
        form = forms.first
        if require_ready:
            try:
                browser_valid = await form.evaluate("element => element.checkValidity()")
            except Exception:
                return None
            if browser_valid is not True:
                return None
            errors = form.locator(
                '[data-qa="validation-error"], [role="alert"].field-error, [aria-invalid="true"]'
            )
            for index in range(await errors.count()):
                if await errors.nth(index).is_visible():
                    return None
        method = (await form.get_attribute("method") or "").strip().casefold()
        if method != "post":
            return None
        enctype = (await form.get_attribute("enctype") or "").strip().casefold()
        target = (await form.get_attribute("target") or "").strip().casefold()
        if enctype != "multipart/form-data" or target not in {"", "_self"}:
            return None
        guard = self._network_guard
        if guard is None:
            return None
        try:
            resolved_action = str(
                await form.evaluate("form => form.action"),
            ).strip()
        except Exception:
            return None
        try:
            await asyncio.to_thread(
                guard.require_allowed_url,
                resolved_action,
                main_frame=True,
            )
        except GreenhouseAdapterBlockedError:
            return None
        submit = form.locator(_SCOPED_FINAL_ACTION_SELECTOR)
        if await submit.count() != 1 or not await submit.is_visible():
            return None
        if require_ready and not await submit.is_enabled():
            return None
        for override in ("formaction", "formmethod", "formenctype", "formtarget"):
            if await submit.get_attribute(override) is not None:
                return None
        try:
            form_handle = await form.element_handle()
            button_handle = await submit.element_handle()
            if form_handle is None or button_handle is None:
                return None
            control_state = await button_handle.evaluate(
                _FINAL_CONTROL_STATE_SCRIPT,
                form_handle,
            )
            if (
                not isinstance(control_state, dict)
                or control_state.get("connected") is not True
                or control_state.get("exactForm") is not True
                or control_state.get("disabled") is not False
                or control_state.get("ariaDisabled") is not False
                or control_state.get("inert") is not False
                or control_state.get("hidden") is not False
                or control_state.get("cssActionable") is not True
                or control_state.get("hasRect") is not True
                or control_state.get("actionable") is not True
            ):
                return None
        except Exception:
            return None
        try:
            raw_identity_entries = await button_handle.evaluate(
                _ACTION_IDENTITY_ENTRIES_SCRIPT,
            )
            action_identity_entries = _validated_action_identity_entries(
                raw_identity_entries,
                guard.expected_identity,
            )
        except GreenhouseAdapterBlockedError:
            return None
        except Exception:
            return None
        button_id = (await submit.get_attribute("id") or "").strip()
        button_qa = (await submit.get_attribute("data-qa") or "").strip()
        button_name = (await submit.get_attribute("name") or "").strip()
        button_value = (await submit.get_attribute("value") or "").strip()
        identity_binding = hashlib.sha256(
            json.dumps(
                action_identity_entries,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        binding = hashlib.sha256(
            (
                f"post|multipart/form-data|_self|{resolved_action}|"
                f"{button_id}|{button_qa}|{button_name}|{button_value}|"
                f"{identity_binding}|{_SCOPED_FINAL_ACTION_SELECTOR}|"
                f"{GREENHOUSE_V1_NATIVE_TRANSPORT}"
            ).encode()
        ).hexdigest()
        return _ValidatedFinalAction(
            form_handle=form_handle,
            button_handle=button_handle,
            resolved_action=resolved_action,
            native_transport=GREENHOUSE_V1_NATIVE_TRANSPORT,
            action_binding=binding,
            action_identity_entries=action_identity_entries,
        )

    async def observed_form_action_binding(self) -> str | None:
        observed = await self._validated_form_action(require_ready=False)
        return observed.action_binding if observed is not None else None

    async def final_action_ready(self) -> bool:
        validated = await self._validated_form_action(require_ready=True)
        if validated is None:
            return False
        self._last_final_action_binding = validated.action_binding
        return True

    async def final_action_binding(self) -> str | None:
        validated = await self._validated_form_action(require_ready=True)
        if validated is None:
            return None
        self._last_final_action_binding = validated.action_binding
        return validated.action_binding

    async def final_action_url(self) -> str | None:
        validated = await self._validated_form_action(require_ready=True)
        return validated.resolved_action if validated is not None else None

    @staticmethod
    async def _exact_form_outer_html(validated: _ValidatedFinalAction) -> str:
        value = await validated.form_handle.evaluate("form => form.outerHTML")
        outer_html = str(value or "")
        if not outer_html or len(outer_html.encode("utf-8")) > 256 * 1024:
            raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
        return outer_html

    async def commit_dom_commitment(self) -> str | None:
        validated = await self._validated_form_action(require_ready=True)
        if validated is None:
            return None
        try:
            return greenhouse_v1_dom_commitment(
                await self._exact_form_outer_html(validated),
            )
        except GreenhouseAdapterBlockedError:
            return None
        except Exception:
            return None

    async def commit_payload_binding(
        self,
        *,
        reviewed_answers: tuple[GreenhouseReviewedAnswerBinding, ...],
        expected_cv_sha256: str,
    ) -> GreenhousePayloadBinding | None:
        validated = await self._validated_form_action(require_ready=True)
        if validated is None or not reviewed_answers:
            return None
        try:
            value = await validated.button_handle.evaluate(
                _FORM_PAYLOAD_COMMITMENT_SCRIPT,
                {
                    "cvSha256": expected_cv_sha256,
                    "reviewedAnswers": [
                        {
                            "fieldId": reviewed.field_id,
                            "fieldType": reviewed.field_type.value,
                            "valueSha256": reviewed.value_sha256,
                            "successfulEntryCount": reviewed.successful_entry_count,
                        }
                        for reviewed in reviewed_answers
                    ],
                    "actionIdentityEntries": [
                        list(entry) for entry in validated.action_identity_entries
                    ],
                },
            )
        except Exception:
            return None
        if not isinstance(value, dict) or not isinstance(value.get("answerBindings"), list):
            return None
        try:
            answer_bindings = tuple(
                GreenhouseAnswerBinding(
                    reviewed=reviewed,
                    control_name_sha256=str(raw["controlNameSha256"]),
                )
                for reviewed, raw in zip(
                    reviewed_answers,
                    value["answerBindings"],
                    strict=True,
                )
                if (
                    isinstance(raw, dict)
                    and raw.get("fieldId") == reviewed.field_id
                    and raw.get("fieldType") == reviewed.field_type.value
                    and raw.get("valueSha256") == reviewed.value_sha256
                    and raw.get("successfulEntryCount") == reviewed.successful_entry_count
                )
            )
            if len(answer_bindings) != len(reviewed_answers):
                return None
            raw_submitter = value.get("submitterBinding")
            submitter_binding = (
                None
                if raw_submitter is None
                else GreenhouseSubmitterBinding(
                    control_name_sha256=str(raw_submitter["controlNameSha256"]),
                    value_sha256=str(raw_submitter["valueSha256"]),
                )
            )
            return GreenhousePayloadBinding(
                payload_commitment=str(value["payloadCommitment"]),
                answer_bindings=answer_bindings,
                resume_control_name_sha256=str(value["resumeControlNameSha256"]),
                submitter_binding=submitter_binding,
            )
        except (KeyError, TypeError, ValueError):
            return None

    async def atomic_commit(
        self,
        expectation: GreenhouseAtomicCommitExpectation,
    ) -> GreenhouseAtomicCommitObservation:
        """Revalidate, arm one exact payload, and invoke native submit once."""

        if self._clicked or self._outbound_gate is not None:
            return _blocked_atomic_observation(
                expectation,
                ReasonCode.PERMIT_REPLAYED,
            )
        try:
            guard = self._network_guard
            page = self._require_page()
            if (
                guard is None
                or expectation.expected_hostname != guard.expected_hostname
                or expectation.expected_identity != guard.expected_identity
            ):
                raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)

            await self._assert_current_url()
            validated = await self._validated_form_action(require_ready=True)
            if (
                validated is None
                or validated.action_binding != expectation.action_binding
                or validated.resolved_action != expectation.resolved_action_url
                or validated.native_transport != expectation.native_transport
            ):
                raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)

            snapshot = await self.snapshot()
            fields = observe_greenhouse_v1_fields(snapshot.html)
            variant = detect_greenhouse_variant(snapshot.html, snapshot.url)
            fingerprint = greenhouse_v1_form_fingerprint(
                fields,
                variant,
                validated.action_binding,
            )
            outer_html = await self._exact_form_outer_html(validated)
            dom_commitment = greenhouse_v1_dom_commitment(outer_html)
            if (
                fields != expectation.fields
                or variant is not expectation.variant
                or fingerprint != expectation.form_fingerprint
                or dom_commitment is None
                or dom_commitment != expectation.dom_commitment
            ):
                raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)

            proof = await self.verify_resume_attachment(
                cv_id=expectation.cv_id,
                expected_sha256=expectation.cv_sha256,
            )
            if (
                not proof.matches(
                    cv_id=expectation.cv_id,
                    cv_sha256=expectation.cv_sha256,
                )
                or proof.receipt_sha256 != expectation.cv_receipt_sha256
                or self._attachment_marker_id is None
                or self._attachment_upload_name is None
            ):
                raise GreenhouseAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
            receipt_sha256 = proof.receipt_sha256
            if receipt_sha256 is None:
                raise GreenhouseAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)

            payload_binding = await self.commit_payload_binding(
                reviewed_answers=tuple(binding.reviewed for binding in expectation.answer_bindings),
                expected_cv_sha256=expectation.cv_sha256,
            )
            if (
                payload_binding is None
                or payload_binding.payload_commitment != expectation.payload_commitment
                or payload_binding.answer_bindings != expectation.answer_bindings
                or payload_binding.resume_control_name_sha256
                != expectation.resume_control_name_sha256
                or payload_binding.submitter_binding != expectation.submitter_binding
            ):
                raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
            payload_commitment = payload_binding.payload_commitment
            main_frame = getattr(page, "main_frame", None)
            if main_frame is None:
                raise GreenhouseAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)

            form_marker = hashlib.sha256(token_bytes(32)).hexdigest()
            marker_armed = await validated.form_handle.evaluate(
                """
                (form, marker) => {
                  if (!form.isConnected) return false;
                  Object.defineProperty(form, "__greenhouseAtomicCommitMarker", {
                    configurable: true,
                    enumerable: false,
                    value: marker,
                    writable: false,
                  });
                  return form.__greenhouseAtomicCommitMarker === marker;
                }
                """,
                form_marker,
            )
            if marker_armed is not True:
                raise GreenhouseAdapterBlockedError(ReasonCode.FORM_CHANGED)
        except GreenhouseAdapterBlockedError as exc:
            return _blocked_atomic_observation(expectation, exc.reason_code)
        except Exception:
            return _blocked_atomic_observation(
                expectation,
                ReasonCode.RUNTIME_NOT_READY,
            )

        gate = _OutboundPostGate(
            expected_hostname=expectation.expected_hostname,
            expected_identity=expectation.expected_identity,
            expected_action_url=validated.resolved_action,
            expected_transport=validated.native_transport,
            expected_payload_commitment=payload_commitment,
            expected_answer_bindings=expectation.answer_bindings,
            expected_resume_control_name_sha256=expectation.resume_control_name_sha256,
            expected_submitter_binding=expectation.submitter_binding,
            expected_cv_sha256=expectation.cv_sha256,
            expected_main_frame=main_frame,
            event=asyncio.Event(),
        )
        self._outbound_gate = gate
        self._clicked = True
        final_action_invoked = False
        primitive_reason: ReasonCode | None = None
        try:
            status = await validated.button_handle.evaluate(
                _ATOMIC_NATIVE_SUBMIT_SCRIPT,
                {
                    "formMarker": form_marker,
                    "resolvedAction": validated.resolved_action,
                    "formOuterHtml": outer_html,
                    "actionIdentityEntries": [
                        list(entry) for entry in validated.action_identity_entries
                    ],
                    "reviewedAnswers": [
                        {
                            "fieldId": binding.reviewed.field_id,
                            "fieldType": binding.reviewed.field_type.value,
                            "valueSha256": binding.reviewed.value_sha256,
                            "successfulEntryCount": (binding.reviewed.successful_entry_count),
                            "controlNameSha256": binding.control_name_sha256,
                        }
                        for binding in expectation.answer_bindings
                    ],
                    "resumeControlNameSha256": expectation.resume_control_name_sha256,
                    "submitterBinding": (
                        {
                            "controlNameSha256": (
                                expectation.submitter_binding.control_name_sha256
                            ),
                            "valueSha256": expectation.submitter_binding.value_sha256,
                        }
                        if expectation.submitter_binding is not None
                        else None
                    ),
                    "nativeTransport": validated.native_transport,
                    "payloadCommitment": payload_commitment,
                    "uploadMarkerSelector": ", ".join(_UPLOAD_COMPLETE_SELECTORS),
                    "uploadMarkerId": self._attachment_marker_id,
                    "uploadName": self._attachment_upload_name,
                    "cvSha256": expectation.cv_sha256,
                    "resumeInputSelector": _RESUME_INPUT_SELECTOR,
                },
            )
            if status == "FORM_CHANGED":
                final_action_invoked = False
                primitive_reason = ReasonCode.FORM_CHANGED
            elif status == "ATTACHMENT_UNVERIFIED":
                final_action_invoked = False
                primitive_reason = ReasonCode.ATTACHMENT_UNVERIFIED
            elif status == "NATIVE_SUBMIT_INVOKED":
                final_action_invoked = True
                primitive_reason = None
            else:
                # The outbound gate is already armed and this prepared action
                # is consumed. Only the two exact pre-request statuses above
                # prove that the intrinsic boundary was not crossed.
                final_action_invoked = True
                primitive_reason = ReasonCode.FINAL_ACTION_UNCONFIRMED
        except Exception:
            # Once the gate is armed, an evaluation/context-loss exception
            # cannot prove whether the intrinsic submit call ran.
            final_action_invoked = True
            primitive_reason = ReasonCode.FINAL_ACTION_UNCONFIRMED

        if gate.request_may_have_left:
            # A gate-observed outbound request is stronger than a contradictory
            # script return. Keep the observation constructible and ambiguous.
            final_action_invoked = True
            if primitive_reason is not None:
                primitive_reason = ReasonCode.FINAL_ACTION_UNCONFIRMED

        if final_action_invoked and not gate.event.is_set():
            try:
                await asyncio.wait_for(gate.event.wait(), timeout=2.0)
            except TimeoutError:
                primitive_reason = ReasonCode.FINAL_ACTION_UNCONFIRMED
        gate.closed = True

        reason_code = gate.reason_code or primitive_reason
        if gate.request_may_have_left and reason_code is not None:
            reason_code = ReasonCode.FINAL_ACTION_UNCONFIRMED
        elif not gate.request_may_have_left and reason_code is None:
            reason_code = ReasonCode.FINAL_ACTION_UNCONFIRMED

        return GreenhouseAtomicCommitObservation(
            expected_hostname=guard.expected_hostname,
            expected_identity=guard.expected_identity,
            fields=fields,
            variant=variant,
            form_fingerprint=fingerprint,
            action_binding=validated.action_binding,
            dom_commitment=dom_commitment,
            resolved_action_url=validated.resolved_action,
            native_transport=validated.native_transport,
            payload_commitment=payload_commitment,
            answer_bindings=expectation.answer_bindings,
            resume_control_name_sha256=expectation.resume_control_name_sha256,
            submitter_binding=expectation.submitter_binding,
            cv_id=proof.cv_id,
            cv_sha256=proof.cv_sha256,
            cv_receipt_sha256=receipt_sha256,
            final_action_invoked=final_action_invoked,
            request_may_have_left=gate.request_may_have_left,
            outbound_request_sha256=gate.outbound_request_sha256,
            reason_code=reason_code,
        )

    async def confirmation_reference(self) -> str | None:
        """Return a digest only when one visible confirmation is stable."""

        page = self._require_page()
        await self._assert_current_url()
        first = greenhouse_visible_confirmation_digest(await page.content())
        locator = page.locator(GREENHOUSE_CONFIRMATION_SELECTOR)
        if first is None or await locator.count() != 1 or not await locator.is_visible():
            return None
        await page.wait_for_timeout(250)
        await self._assert_current_url()
        second = greenhouse_visible_confirmation_digest(await page.content())
        locator = page.locator(GREENHOUSE_CONFIRMATION_SELECTOR)
        if second is None or await locator.count() != 1 or not await locator.is_visible():
            return None
        if first != second:
            return None
        return second

    async def close(self) -> None:
        context, browser, playwright = self._context, self._browser, self._playwright
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._network_guard = None
        self._attachment = None
        self._attachment_marker_id = None
        self._attachment_upload_name = None
        self._last_final_action_binding = None
        self._outbound_gate = None
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


def playwright_greenhouse_browser_factory(_url: str) -> GreenhouseCandidateSession:
    """Create a lazy ephemeral session; no browser starts until navigation."""

    return PlaywrightGreenhouseCandidateSession()
