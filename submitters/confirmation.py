"""Fail-closed employer-evidence verification shared by ATS adapters.

Generic success text and navigation are useful diagnostics, but they are not
proof that an employer accepted one exact application.  A positive result from
this module therefore requires an adapter-authored rule plus bindings to the
attempt, form plan, post-action nonce, and verified CV attachment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from bs4 import BeautifulSoup, Tag
from pydantic import ValidationError

from core.submission_domain import EvidenceType, SubmissionEvidence
from submitters.base import SubmissionResult

# Backwards-readable adapter name for the domain's canonical evidence enum.
EvidenceChannel = EvidenceType


@dataclass(frozen=True, slots=True)
class AdapterEvidenceRule:
    """One adapter-version-specific evidence rule allowed for an attempt."""

    rule_id: str
    channel: EvidenceChannel
    visible_selector: str | None = None


@dataclass(frozen=True, slots=True)
class SubmissionEvidenceExpectation:
    """Immutable evidence binding minted before the external final action."""

    attempt_id: int
    platform: str
    adapter_version: str
    selector_version: str
    form_fingerprint: str
    attached_cv_hash: str
    attachment_verified: bool
    post_action_nonce: str
    final_action_at: datetime
    allowed_rules: tuple[AdapterEvidenceRule, ...]

    @property
    def allowed_rule_ids(self) -> tuple[str, ...]:
        """Expose bounded rule IDs for redacted traces and diagnostics."""
        return tuple(rule.rule_id for rule in self.allowed_rules)


@dataclass(frozen=True, slots=True)
class SubmissionEvidenceObservation:
    """Redacted adapter observation made after the final action.

    ``evidence_reference`` must be an employer/application reference, never
    page text or a form answer.  ``visible_selector`` is allowed only for the
    visible-confirmation channel and is checked against both pre- and
    post-action DOM snapshots.
    """

    attempt_id: int
    platform: str
    adapter_version: str
    selector_version: str
    form_fingerprint: str
    attached_cv_hash: str
    post_action_nonce: str
    rule_id: str
    channel: EvidenceChannel
    evidence_reference: str
    observed_at: datetime
    observed_after_final_action: bool
    was_present_before_action: bool
    visible_selector: str | None = None
    computed_visible: bool | None = None
    response_status: int | None = None
    response_schema_valid: bool = False


# A short alias keeps adapter call sites readable while making the stronger
# contract name discoverable to API and domain code.
EvidenceBinding = SubmissionEvidenceExpectation


@dataclass(frozen=True, slots=True)
class ConfirmationEvidence:
    """Result of checking one adapter observation against its expectation."""

    confirmed: bool
    evidence: SubmissionEvidence | None = None
    evidence_reference: str | None = None
    reason_code: str = "FINAL_ACTION_UNCONFIRMED"
    evidence_digest: str | None = None


def _nonblank(value: str | None) -> bool:
    return bool(value and value.strip())


def _is_hidden(element: Tag) -> bool:
    """Conservatively reject hidden elements and elements under hidden parents."""
    current: Tag | None = element
    while current is not None:
        if current.has_attr("hidden"):
            return True
        if str(current.get("aria-hidden", "")).strip().casefold() == "true":
            return True
        if current.name == "input" and str(current.get("type", "")).casefold() == "hidden":
            return True

        style = str(current.get("style", "")).replace(" ", "").casefold()
        if any(
            marker in style
            for marker in (
                "display:none",
                "visibility:hidden",
                "opacity:0",
            )
        ):
            return True

        class_value = current.get("class")
        if isinstance(class_value, str):
            classes = {class_value.casefold()}
        elif class_value is None:
            classes = set()
        else:
            classes = {str(item).casefold() for item in class_value}
        if classes.intersection({"hidden", "sr-only", "visually-hidden", "d-none"}):
            return True
        parent = current.parent
        current = parent if isinstance(parent, Tag) else None
    return False


def _selected_visible_markup(html: str, selector: str) -> str | None:
    """Return canonical selected markup only when exactly one visible node exists."""
    if not _nonblank(selector):
        return None
    try:
        matches = BeautifulSoup(html or "", "html.parser").select(selector)
    except Exception:
        return None
    visible = [element for element in matches if not _is_hidden(element)]
    if len(visible) != 1:
        return None
    return str(visible[0])


def _matching_rule(
    expectation: SubmissionEvidenceExpectation,
    observation: SubmissionEvidenceObservation,
) -> AdapterEvidenceRule | None:
    values = (
        expectation.platform,
        expectation.adapter_version,
        expectation.selector_version,
        expectation.form_fingerprint,
        expectation.attached_cv_hash,
        expectation.post_action_nonce,
    )
    final_action_is_aware = (
        expectation.final_action_at.tzinfo is not None
        and expectation.final_action_at.utcoffset() is not None
    )
    if (
        expectation.attempt_id < 1
        or not expectation.attachment_verified
        or not all(_nonblank(value) for value in values)
        or not expectation.allowed_rules
        or any(not _nonblank(rule.rule_id) for rule in expectation.allowed_rules)
        or len(expectation.allowed_rule_ids) != len(set(expectation.allowed_rule_ids))
        or not _nonblank(observation.rule_id)
        or not final_action_is_aware
    ):
        return None
    bindings_match = (
        observation.attempt_id == expectation.attempt_id
        and observation.platform == expectation.platform
        and observation.adapter_version == expectation.adapter_version
        and observation.selector_version == expectation.selector_version
        and observation.form_fingerprint == expectation.form_fingerprint
        and observation.attached_cv_hash == expectation.attached_cv_hash
        and observation.post_action_nonce == expectation.post_action_nonce
    )
    if not bindings_match:
        return None
    matching = [rule for rule in expectation.allowed_rules if rule.rule_id == observation.rule_id]
    if len(matching) != 1:
        return None
    rule = matching[0]
    if (
        rule.channel is not observation.channel
        or rule.visible_selector != observation.visible_selector
        or (
            rule.channel is EvidenceType.VISIBLE_POST_CLICK_CONFIRMATION
            and not _nonblank(rule.visible_selector)
        )
        or (
            rule.channel is not EvidenceType.VISIBLE_POST_CLICK_CONFIRMATION
            and rule.visible_selector is not None
        )
    ):
        return None
    return rule


def verify_submission_evidence(
    expectation: SubmissionEvidenceExpectation,
    observation: SubmissionEvidenceObservation,
    *,
    post_action_html: str = "",
    pre_action_html: str = "",
) -> ConfirmationEvidence:
    """Verify adapter-specific, attempt-bound employer evidence.

    The caller cannot turn generic text, a redirect, or a bare HTTP status into
    evidence.  Visible evidence must be new after the action; API evidence must
    have passed the adapter's schema validation.
    """
    rule = _matching_rule(expectation, observation)
    if rule is None:
        return ConfirmationEvidence(False, reason_code="EVIDENCE_BINDING_MISMATCH")
    if not observation.observed_after_final_action:
        return ConfirmationEvidence(False, reason_code="EVIDENCE_NOT_POST_ACTION")
    if (
        observation.observed_at.tzinfo is None
        or observation.observed_at.utcoffset() is None
        or observation.observed_at < expectation.final_action_at
    ):
        return ConfirmationEvidence(False, reason_code="EVIDENCE_TIMESTAMP_INVALID")
    if observation.was_present_before_action:
        return ConfirmationEvidence(False, reason_code="EVIDENCE_PREEXISTED")
    if not _nonblank(observation.evidence_reference):
        return ConfirmationEvidence(False, reason_code="EVIDENCE_REFERENCE_MISSING")

    verified_reference = observation.evidence_reference.strip()
    channel_proof = verified_reference
    if observation.channel is EvidenceChannel.API_RECEIPT:
        if (
            observation.response_status is None
            or not 200 <= observation.response_status < 300
            or not observation.response_schema_valid
        ):
            return ConfirmationEvidence(False, reason_code="API_RECEIPT_INVALID")
    elif observation.channel is EvidenceChannel.VISIBLE_POST_CLICK_CONFIRMATION:
        selector = rule.visible_selector or ""
        if not _nonblank(pre_action_html):
            return ConfirmationEvidence(False, reason_code="PRE_ACTION_SNAPSHOT_MISSING")
        if observation.computed_visible is not True:
            return ConfirmationEvidence(False, reason_code="VISIBLE_EVIDENCE_HIDDEN")
        current = _selected_visible_markup(post_action_html, selector)
        if current is None:
            return ConfirmationEvidence(False, reason_code="VISIBLE_EVIDENCE_MISSING")
        try:
            previous = BeautifulSoup(pre_action_html, "html.parser").select(selector)
        except Exception:
            return ConfirmationEvidence(False, reason_code="PRE_ACTION_SNAPSHOT_INVALID")
        if previous:
            return ConfirmationEvidence(False, reason_code="EVIDENCE_PREEXISTED")
        channel_proof = sha256(current.encode("utf-8")).hexdigest()
        verified_reference = channel_proof
    elif observation.channel not in {
        EvidenceChannel.EMPLOYER_APPLICATION_ID,
        EvidenceChannel.CANDIDATE_PORTAL_RECORD,
    }:
        return ConfirmationEvidence(False, reason_code="EVIDENCE_CHANNEL_UNSUPPORTED")

    digest_material = "|".join(
        (
            str(observation.attempt_id),
            observation.platform,
            observation.adapter_version,
            observation.selector_version,
            observation.form_fingerprint,
            observation.attached_cv_hash,
            observation.rule_id,
            observation.channel.value,
            channel_proof,
        )
    )
    digest = sha256(digest_material.encode("utf-8")).hexdigest()
    reference_fields: dict[str, str] = {}
    if observation.channel is EvidenceType.EMPLOYER_APPLICATION_ID:
        reference_fields["employer_application_id"] = verified_reference
    elif observation.channel is EvidenceType.API_RECEIPT:
        reference_fields["api_receipt_id"] = verified_reference
    elif observation.channel is EvidenceType.CANDIDATE_PORTAL_RECORD:
        reference_fields["candidate_portal_reference"] = verified_reference
    try:
        evidence = SubmissionEvidence(
            attempt_id=observation.attempt_id,
            evidence_type=observation.channel,
            form_fingerprint=observation.form_fingerprint,
            attached_cv_hash=observation.attached_cv_hash,
            observed_at=observation.observed_at,
            digest=digest,
            **reference_fields,
        )
    except ValidationError:
        return ConfirmationEvidence(False, reason_code="EVIDENCE_BINDING_INVALID")
    return ConfirmationEvidence(
        True,
        evidence=evidence,
        evidence_reference=verified_reference,
        reason_code="EMPLOYER_VERIFIED",
        evidence_digest=digest,
    )


def detect_submission_confirmation(
    url: str,
    html: str,
    *,
    expectation: SubmissionEvidenceExpectation | None = None,
    observation: SubmissionEvidenceObservation | None = None,
    pre_action_html: str = "",
) -> ConfirmationEvidence:
    """Verify a bound observation; URL/text alone can never prove submission.

    ``url`` remains in the signature for legacy adapters, but redirects are
    deliberately ignored.  Likewise, phrases in ``html`` are not inspected as
    proof.  Existing adapters calling this without the new two-phase context
    fail closed.
    """
    del url
    if expectation is None or observation is None:
        return ConfirmationEvidence(False)
    return verify_submission_evidence(
        expectation,
        observation,
        post_action_html=html,
        pre_action_html=pre_action_html,
    )


def browser_submission_result(
    *,
    platform: str,
    page_url: str,
    html: str,
    expectation: SubmissionEvidenceExpectation | None = None,
    observation: SubmissionEvidenceObservation | None = None,
    pre_action_html: str = "",
) -> SubmissionResult:
    """Convert a post-action observation into a legacy-compatible result.

    This adapter boundary keeps ``SubmissionResult`` only for disabled legacy
    and dry-run implementations.  The two-phase executor returns the domain's
    typed ``CommitOutcome`` directly.
    """
    confirmation = detect_submission_confirmation(
        page_url,
        html,
        expectation=expectation,
        observation=observation,
        pre_action_html=pre_action_html,
    )
    if confirmation.confirmed:
        return SubmissionResult(
            success=True,
            platform=platform,
            status="submitted",
            confirmation_id=confirmation.evidence_reference,
            reason_code="EMPLOYER_VERIFIED",
            diagnostic_details={
                "terminal_reason": "EMPLOYER_VERIFIED",
                "evidence_type": (
                    confirmation.evidence.evidence_type.value
                    if confirmation.evidence is not None
                    else None
                ),
                "evidence_digest": confirmation.evidence_digest,
            },
        )

    low = (html or "").casefold()
    if any(
        marker in low
        for marker in (
            "captcha",
            "recaptcha",
            "hcaptcha",
            "verify you are human",
            "security challenge",
        )
    ):
        return SubmissionResult(
            success=False,
            platform=platform,
            status="unknown",
            error="SUBMIT_OUTCOME_UNKNOWN",
            reason_code="CHALLENGE_DETECTED",
        )
    terminal_reason = (
        "EVIDENCE_INVALID"
        if expectation is not None or observation is not None
        else "FINAL_ACTION_UNCONFIRMED"
    )
    return SubmissionResult(
        success=False,
        platform=platform,
        status="unknown",
        error="Final action has no employer-verified evidence",
        reason_code=terminal_reason,
        diagnostic_details={
            "terminal_reason": terminal_reason,
            "evidence_rejection": confirmation.reason_code,
        },
    )
