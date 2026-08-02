"""Canonical, privacy-bounded protocol shared with the private runner.

Only opaque identifiers, bounded enums, version strings, timestamps, and
cryptographic digests are accepted.  Candidate identity, job URLs, CV
identifiers/hashes, answers, and arbitrary text have no representation here.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

PROTOCOL_VERSION = "jaa-control.v1"
CONTROL_AUDIENCE = "job-apply-control-plane"
RUNNER_AUDIENCE = "job-apply-private-runner"

Sha256Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ReleaseDigest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")]
SemanticVersion = Annotated[
    str,
    StringConstraints(pattern=r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"),
]
SignatureText = Annotated[
    str,
    StringConstraints(pattern=r"^(?:[A-Za-z0-9_-]{86}|)$"),
]


class StrictProtocolModel(BaseModel):
    """Forbid accidental schema expansion at every trust boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class EnvelopePurpose(StrEnum):
    RUNNER_HEARTBEAT = "runner.heartbeat.v1"
    RUNNER_REVIEW_GRANT = "runner.review_grant.v1"
    RUNNER_REVIEW_GRANT_REVOCATION = "runner.review_grant_revocation.v1"
    RUNNER_COMMAND_POLL = "runner.command_poll.v1"
    RUNNER_COMMAND_ACK = "runner.command_ack.v1"
    RUNNER_EVENT = "runner.event.v1"
    CONTROL_COMMAND = "control.command.v1"
    CONTROL_KILL_COMMAND = "control.kill_command.v1"


class AdapterCode(StrEnum):
    WORKDAY = "workday"
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    SMARTRECRUITERS = "smartrecruiters"


class RunnerStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    DRAINING = "draining"


class DiscoverySourceCode(StrEnum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    SMARTRECRUITERS = "smartrecruiters"
    REMOTIVE = "remotive"
    GMAIL_ALERT = "gmail_alert"
    LINKEDIN_PARTNER = "linkedin_partner"
    GENERIC_JSONLD = "generic_jsonld"
    GENERIC_FEED = "generic_feed"


class SourceHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


class QualificationTierCode(StrEnum):
    DISABLED = "disabled"
    FIXTURE_QUALIFIED = "fixture_qualified"
    DRY_RUN_QUALIFIED = "dry_run_qualified"
    LIVE_CANARY_QUALIFIED = "live_canary_qualified"


class AutomationPolicyState(StrEnum):
    INACTIVE = "inactive"
    ACTIVE = "active"
    BLOCKED = "blocked"


class CommandAckStatus(StrEnum):
    RECEIVED = "received"
    REJECTED = "rejected"


class AttemptStage(StrEnum):
    QUEUED = "queued"
    INSPECTING = "inspecting"
    PREPARING = "preparing"
    READY = "ready"
    COMMITTING = "committing"
    VERIFYING = "verifying"
    FINISHED = "finished"


class AttemptOutcome(StrEnum):
    CONFIRMED_SUBMITTED = "confirmed_submitted"
    ALREADY_APPLIED = "already_applied"
    NEEDS_REVIEW = "needs_review"
    UNKNOWN = "unknown"
    FAILED_BEFORE_COMMIT = "failed_before_commit"
    DRAFT_ONLY = "draft_only"
    OPERATOR_CONFIRMED = "operator_confirmed"
    LEGACY_UNVERIFIED = "legacy_unverified"


class ReasonCode(StrEnum):
    RUNTIME_NOT_READY = "RUNTIME_NOT_READY"
    BUILD_MISMATCH = "BUILD_MISMATCH"
    ADAPTER_NOT_QUALIFIED = "ADAPTER_NOT_QUALIFIED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    MFA_REQUIRED = "MFA_REQUIRED"
    CHALLENGE_DETECTED = "CHALLENGE_DETECTED"
    FORM_CHANGED = "FORM_CHANGED"
    FORM_PLAN_INCOMPLETE = "FORM_PLAN_INCOMPLETE"
    REQUIRED_FIELD_UNKNOWN = "REQUIRED_FIELD_UNKNOWN"
    ATTACHMENT_UNVERIFIED = "ATTACHMENT_UNVERIFIED"
    FINAL_ACTION_UNCONFIRMED = "FINAL_ACTION_UNCONFIRMED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    JOB_CLOSED = "JOB_CLOSED"
    SELECTOR_DRIFT = "SELECTOR_DRIFT"
    STALE_INDETERMINATE = "STALE_INDETERMINATE"
    DRY_RUN_DISCARDED = "DRY_RUN_DISCARDED"
    DRAFT_ONLY = "DRAFT_ONLY"
    PERMIT_MISSING = "PERMIT_MISSING"
    PERMIT_EXPIRED = "PERMIT_EXPIRED"
    PERMIT_REPLAYED = "PERMIT_REPLAYED"
    PERMIT_BINDING_MISMATCH = "PERMIT_BINDING_MISMATCH"
    COMMAND_EXPIRED = "COMMAND_EXPIRED"
    COMMAND_REPLAYED = "COMMAND_REPLAYED"
    GOVERNOR_DENIED = "GOVERNOR_DENIED"
    OPERATOR_CANCELLED = "OPERATOR_CANCELLED"
    EVIDENCE_INVALID = "EVIDENCE_INVALID"
    UNSUPPORTED_CONTROL = "UNSUPPORTED_CONTROL"
    NETWORK_ERROR = "NETWORK_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class EvidenceType(StrEnum):
    EMPLOYER_APPLICATION_ID = "employer_application_id"
    SCHEMA_VALID_RECEIPT = "schema_valid_receipt"
    CANDIDATE_PORTAL_RECORD = "candidate_portal_record"
    ATS_VISIBLE_CONFIRMATION = "ats_visible_confirmation"


BoundedCounter = Annotated[int, Field(strict=True, ge=0, le=2_147_483_647)]


class PipelineCounters(StrictProtocolModel):
    discovered: BoundedCounter = 0
    source_occurrences: BoundedCounter = 0
    deduplicated: BoundedCounter = 0
    eligible: BoundedCounter = 0
    prepared: BoundedCounter = 0
    quarantined: BoundedCounter = 0
    employer_confirmed: BoundedCounter = 0


class AutomationPolicySummary(StrictProtocolModel):
    state: AutomationPolicyState
    revision: BoundedCounter = 0
    expires_at: AwareDatetime | None = None
    daily_remaining: Annotated[int, Field(strict=True, ge=0, le=25)] = 0
    hourly_remaining: Annotated[int, Field(strict=True, ge=0, le=5)] = 0
    kill_switch_active: bool = False

    @model_validator(mode="after")
    def authority_state_is_consistent(self) -> AutomationPolicySummary:
        if self.state is AutomationPolicyState.ACTIVE and self.expires_at is None:
            raise ValueError("active policy summary requires an expiry")
        if self.kill_switch_active and self.state is not AutomationPolicyState.BLOCKED:
            raise ValueError("active kill switch requires a blocked policy state")
        return self


class DiscoverySourceSummary(StrictProtocolModel):
    source: DiscoverySourceCode
    status: SourceHealth
    enabled_count: BoundedCounter
    source_count: BoundedCounter

    @model_validator(mode="after")
    def enabled_count_is_bounded(self) -> DiscoverySourceSummary:
        if self.enabled_count > self.source_count:
            raise ValueError("enabled source count cannot exceed source count")
        return self


class AdapterStatusSummary(StrictProtocolModel):
    adapter: AdapterCode
    qualification_tier: QualificationTierCode
    final_execution_enabled: bool = False
    qualified_form_scope_count: BoundedCounter = 0

    @model_validator(mode="after")
    def final_execution_requires_live_scope(self) -> AdapterStatusSummary:
        if self.final_execution_enabled and (
            self.qualification_tier is not QualificationTierCode.LIVE_CANARY_QUALIFIED
            or self.qualified_form_scope_count < 1
        ):
            raise ValueError("final execution requires a live-qualified scope")
        return self


def operations_summary_digest(
    *,
    pipeline: PipelineCounters,
    policy: AutomationPolicySummary,
    sources: tuple[DiscoverySourceSummary, ...],
    adapters: tuple[AdapterStatusSummary, ...],
) -> str:
    payload = {
        "pipeline": pipeline.model_dump(mode="json"),
        "policy": policy.model_dump(mode="json"),
        "sources": [item.model_dump(mode="json") for item in sources],
        "adapters": [item.model_dump(mode="json") for item in adapters],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


class HeartbeatPayload(StrictProtocolModel):
    boot_id: UUID
    release_digest: ReleaseDigest
    status: RunnerStatus
    pipeline: PipelineCounters | None = None
    policy: AutomationPolicySummary | None = None
    sources: tuple[DiscoverySourceSummary, ...] = Field(default=(), max_length=9)
    adapters: tuple[AdapterStatusSummary, ...] = Field(default=(), max_length=5)
    operations_digest: Sha256Digest | None = None

    @model_validator(mode="after")
    def operations_summary_is_complete_and_canonical(self) -> HeartbeatPayload:
        summary_present = bool(
            self.pipeline is not None
            or self.policy is not None
            or self.sources
            or self.adapters
            or self.operations_digest is not None
        )
        if not summary_present:
            return self
        if self.pipeline is None or self.policy is None or self.operations_digest is None:
            raise ValueError("operations heartbeat summary must be complete")
        source_codes = tuple(item.source.value for item in self.sources)
        adapter_codes = tuple(item.adapter.value for item in self.adapters)
        if source_codes != tuple(sorted(set(source_codes))):
            raise ValueError("source summaries must be unique and sorted")
        if adapter_codes != tuple(sorted(set(adapter_codes))):
            raise ValueError("adapter summaries must be unique and sorted")
        expected = operations_summary_digest(
            pipeline=self.pipeline,
            policy=self.policy,
            sources=self.sources,
            adapters=self.adapters,
        )
        if self.operations_digest != expected:
            raise ValueError("operations summary digest mismatch")
        return self


class ReviewGrantPayload(StrictProtocolModel):
    grant_id: UUID
    application_ref: UUID
    application_revision: Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]
    adapter: AdapterCode
    adapter_version: SemanticVersion
    form_fingerprint_digest: Sha256Digest
    reviewed_at: AwareDatetime


class ReviewGrantRevocationPayload(StrictProtocolModel):
    grant_id: UUID
    application_ref: UUID
    application_revision: Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]
    adapter: AdapterCode
    adapter_version: SemanticVersion
    form_fingerprint_digest: Sha256Digest
    reviewed_at: AwareDatetime
    grant_expires_at: AwareDatetime
    revoked_at: AwareDatetime

    @model_validator(mode="after")
    def validate_authority_window(self) -> ReviewGrantRevocationPayload:
        if not self.reviewed_at <= self.revoked_at < self.grant_expires_at:
            raise ValueError("revocation must fall within the original grant lifetime")
        return self


class CommandPollPayload(StrictProtocolModel):
    boot_id: UUID
    max_commands: Literal[1] = 1


class ControlCommandPayload(StrictProtocolModel):
    command_id: UUID
    grant_id: UUID
    application_ref: UUID
    application_revision: Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]
    adapter: AdapterCode
    adapter_version: SemanticVersion
    form_fingerprint_digest: Sha256Digest
    action: Literal["send_application"] = "send_application"


class KillSwitchCommandPayload(StrictProtocolModel):
    command_id: UUID
    boot_id: UUID
    action: Literal["activate_kill_switch"] = "activate_kill_switch"
    reason_code: Literal["REMOTE_OPERATOR_KILL"] = "REMOTE_OPERATOR_KILL"


class CommandAckPayload(StrictProtocolModel):
    command_id: UUID
    ack_status: CommandAckStatus


class RunnerEventPayload(StrictProtocolModel):
    event_id: UUID
    command_id: UUID
    sequence: Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]
    stage: AttemptStage
    outcome: AttemptOutcome | None = None
    reason_code: ReasonCode | None = None
    evidence_type: EvidenceType | None = None
    evidence_digest: Sha256Digest | None = None
    occurred_at: AwareDatetime

    @model_validator(mode="after")
    def validate_terminal_evidence(self) -> RunnerEventPayload:
        is_finished = self.stage is AttemptStage.FINISHED
        if is_finished != (self.outcome is not None):
            raise ValueError("outcome is required exactly when stage is finished")
        if (self.evidence_type is None) != (self.evidence_digest is None):
            raise ValueError("evidence type and digest must be supplied together")
        if self.outcome is AttemptOutcome.CONFIRMED_SUBMITTED and self.evidence_type is None:
            raise ValueError("confirmed submission requires employer evidence")
        if self.outcome is AttemptOutcome.CONFIRMED_SUBMITTED and self.reason_code is not None:
            raise ValueError("confirmed submission cannot carry a failure reason")
        if (
            self.outcome is not AttemptOutcome.CONFIRMED_SUBMITTED
            and self.evidence_type is not None
        ):
            raise ValueError("employer evidence is reserved for confirmed submission")
        return self


PayloadT = TypeVar("PayloadT", bound=StrictProtocolModel)


class SignedEnvelope(StrictProtocolModel, Generic[PayloadT]):
    protocol_version: Literal["jaa-control.v1"] = PROTOCOL_VERSION
    key_id: UUID
    purpose: EnvelopePurpose
    audience: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    nonce: UUID
    payload: PayloadT
    signature: SignatureText = ""


class HeartbeatEnvelope(SignedEnvelope[HeartbeatPayload]):
    purpose: Literal[EnvelopePurpose.RUNNER_HEARTBEAT] = EnvelopePurpose.RUNNER_HEARTBEAT


class ReviewGrantEnvelope(SignedEnvelope[ReviewGrantPayload]):
    purpose: Literal[EnvelopePurpose.RUNNER_REVIEW_GRANT] = EnvelopePurpose.RUNNER_REVIEW_GRANT


class ReviewGrantRevocationEnvelope(SignedEnvelope[ReviewGrantRevocationPayload]):
    purpose: Literal[EnvelopePurpose.RUNNER_REVIEW_GRANT_REVOCATION] = (
        EnvelopePurpose.RUNNER_REVIEW_GRANT_REVOCATION
    )


class CommandPollEnvelope(SignedEnvelope[CommandPollPayload]):
    purpose: Literal[EnvelopePurpose.RUNNER_COMMAND_POLL] = EnvelopePurpose.RUNNER_COMMAND_POLL


class CommandAckEnvelope(SignedEnvelope[CommandAckPayload]):
    purpose: Literal[EnvelopePurpose.RUNNER_COMMAND_ACK] = EnvelopePurpose.RUNNER_COMMAND_ACK


class RunnerEventEnvelope(SignedEnvelope[RunnerEventPayload]):
    purpose: Literal[EnvelopePurpose.RUNNER_EVENT] = EnvelopePurpose.RUNNER_EVENT


class ControlCommandEnvelope(SignedEnvelope[ControlCommandPayload]):
    purpose: Literal[EnvelopePurpose.CONTROL_COMMAND] = EnvelopePurpose.CONTROL_COMMAND


class KillSwitchCommandEnvelope(SignedEnvelope[KillSwitchCommandPayload]):
    purpose: Literal[EnvelopePurpose.CONTROL_KILL_COMMAND] = EnvelopePurpose.CONTROL_KILL_COMMAND


def canonical_unsigned_bytes(envelope: SignedEnvelope[StrictProtocolModel]) -> bytes:
    """Return the single canonical signing representation."""

    data = envelope.model_dump(mode="json", exclude={"signature"})
    return json.dumps(
        data,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_envelope_bytes(envelope: SignedEnvelope[StrictProtocolModel]) -> bytes:
    """Return a stable representation suitable for redacted audit digests."""

    data = envelope.model_dump(mode="json")
    return json.dumps(
        data,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


__all__ = [
    "CONTROL_AUDIENCE",
    "PROTOCOL_VERSION",
    "RUNNER_AUDIENCE",
    "AdapterCode",
    "AdapterStatusSummary",
    "AutomationPolicyState",
    "AutomationPolicySummary",
    "AttemptOutcome",
    "AttemptStage",
    "CommandAckEnvelope",
    "CommandAckPayload",
    "CommandAckStatus",
    "CommandPollEnvelope",
    "CommandPollPayload",
    "ControlCommandEnvelope",
    "ControlCommandPayload",
    "DiscoverySourceCode",
    "DiscoverySourceSummary",
    "EnvelopePurpose",
    "EvidenceType",
    "HeartbeatEnvelope",
    "HeartbeatPayload",
    "KillSwitchCommandEnvelope",
    "KillSwitchCommandPayload",
    "ReasonCode",
    "ReviewGrantEnvelope",
    "ReviewGrantPayload",
    "ReviewGrantRevocationEnvelope",
    "ReviewGrantRevocationPayload",
    "RunnerEventEnvelope",
    "RunnerEventPayload",
    "RunnerStatus",
    "PipelineCounters",
    "QualificationTierCode",
    "SignedEnvelope",
    "SourceHealth",
    "canonical_envelope_bytes",
    "canonical_unsigned_bytes",
    "operations_summary_digest",
]
