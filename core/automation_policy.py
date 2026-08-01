"""Signed, immutable authority contracts for qualified autopilot.

Fit quality and environment flags are inputs, never authority.  Authority is
created only by an authenticated local activation that signs one exact policy
revision with a private Ed25519 key kept outside Git.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from datetime import UTC, timedelta
from enum import StrEnum
from typing import Literal
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

AUTOMATION_POLICY_SCHEMA_VERSION: Literal["auto-submit-policy.v1"] = "auto-submit-policy.v1"
AUTOMATION_DECISION_SCHEMA_VERSION: Literal["auto-submit-decision.v1"] = "auto-submit-decision.v1"
AUTOMATION_POLICY_MAX_DAYS = 30
AUTOMATION_POLICY_TIMEZONE: Literal["Asia/Jerusalem"] = "Asia/Jerusalem"
AUTOMATION_POLICY_ACTIVE_START: Literal["08:00"] = "08:00"
AUTOMATION_POLICY_ACTIVE_END: Literal["21:00"] = "21:00"

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SIGNATURE_PATTERN = r"^[A-Za-z0-9_-]{86}$"
_SAFE_TOKEN_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
_REASON_PATTERN = r"^[A-Z][A-Z0-9_]{1,63}$"


class AutomationGeography(StrEnum):
    ISRAEL = "israel"
    WORLDWIDE_REMOTE = "worldwide_remote"
    EMEA_REMOTE = "emea_remote"


class PolicyAuthoritySource(StrEnum):
    LOCAL_OPERATOR = "local_operator"
    VERCEL_SIGNED_KILL = "vercel_signed_kill"


class QualifiedFormContractV1(BaseModel):
    """One exact adapter/version and privacy-safe semantic form class."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    adapter_name: str = Field(pattern=_SAFE_TOKEN_PATTERN, max_length=64)
    adapter_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", max_length=32)
    selector_version: str = Field(min_length=1, max_length=64)
    form_contract_digest: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("selector_version")
    @classmethod
    def selector_is_bounded(cls, value: str) -> str:
        if re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", value) is None:
            raise ValueError("selector version must be a bounded token")
        return value


class AutoSubmitPolicyV1(BaseModel):
    """The complete, locally signed 30-day autopilot authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["auto-submit-policy.v1"] = AUTOMATION_POLICY_SCHEMA_VERSION
    policy_id: UUID
    revision: int = Field(ge=1, le=2_147_483_647)
    role_families: tuple[str, ...] = Field(min_length=1, max_length=64)
    geographies: tuple[AutomationGeography, ...] = Field(min_length=1, max_length=3)
    minimum_fit_score: float = Field(default=85.0, ge=85.0, le=100.0)
    daily_limit: int = Field(default=25, ge=1, le=25)
    hourly_limit: int = Field(default=5, ge=1, le=5)
    company_limit: int = Field(default=2, ge=1, le=2)
    company_window_days: Literal[14] = 14
    permitted_adapters: tuple[str, ...] = Field(min_length=1, max_length=5)
    qualified_form_contracts: tuple[QualifiedFormContractV1, ...] = Field(
        min_length=1,
        max_length=100,
    )
    active_start: Literal["08:00"] = AUTOMATION_POLICY_ACTIVE_START
    active_end: Literal["21:00"] = AUTOMATION_POLICY_ACTIVE_END
    timezone: Literal["Asia/Jerusalem"] = AUTOMATION_POLICY_TIMEZONE
    profile_version: int = Field(ge=1)
    routing_config_digest: str = Field(pattern=_SHA256_PATTERN)
    cv_manifest_digest: str = Field(pattern=_SHA256_PATTERN)
    fit_qualification_digest: str = Field(pattern=_SHA256_PATTERN)
    confirmed_answer_revision: str = Field(pattern=_SHA256_PATTERN)
    activated_at: AwareDatetime
    expires_at: AwareDatetime

    @field_validator("minimum_fit_score")
    @classmethod
    def finite_score(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("minimum fit score must be finite")
        return value

    @field_validator("role_families", "permitted_adapters")
    @classmethod
    def bounded_unique_tokens(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(dict.fromkeys(values)) != values:
            raise ValueError("policy tokens must be ordered and unique")
        if any(re.fullmatch(_SAFE_TOKEN_PATTERN, value) is None for value in values):
            raise ValueError("policy tokens must be bounded slugs")
        return values

    @field_validator("geographies")
    @classmethod
    def unique_geographies(
        cls,
        values: tuple[AutomationGeography, ...],
    ) -> tuple[AutomationGeography, ...]:
        if tuple(dict.fromkeys(values)) != values:
            raise ValueError("geographies must be ordered and unique")
        return values

    @model_validator(mode="after")
    def bounded_authority_window(self) -> AutoSubmitPolicyV1:
        activated = self.activated_at.astimezone(UTC)
        expires = self.expires_at.astimezone(UTC)
        if expires <= activated:
            raise ValueError("policy expiry must follow activation")
        if expires - activated > timedelta(days=AUTOMATION_POLICY_MAX_DAYS):
            raise ValueError("policy authority cannot exceed 30 days")
        if self.hourly_limit > self.daily_limit:
            raise ValueError("hourly limit cannot exceed daily limit")
        permitted = set(self.permitted_adapters)
        if any(scope.adapter_name not in permitted for scope in self.qualified_form_contracts):
            raise ValueError("qualified form scope uses an unpermitted adapter")
        scope_keys = [
            (
                scope.adapter_name,
                scope.adapter_version,
                scope.selector_version,
                scope.form_contract_digest,
            )
            for scope in self.qualified_form_contracts
        ]
        if len(scope_keys) != len(set(scope_keys)):
            raise ValueError("qualified form contracts must be unique")
        return self

    @property
    def payload_digest(self) -> str:
        return hashlib.sha256(canonical_policy_bytes(self)).hexdigest()


class SignedAutoSubmitPolicyV1(BaseModel):
    """Ed25519 envelope persisted as the authoritative policy revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key_id: UUID
    policy: AutoSubmitPolicyV1
    signature: str = Field(pattern=_SIGNATURE_PATTERN)


class AutoSubmitDecisionV1(BaseModel):
    """Immutable, redacted admission result for one exact application state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["auto-submit-decision.v1"] = AUTOMATION_DECISION_SCHEMA_VERSION
    policy_id: UUID
    policy_revision: int = Field(ge=1)
    policy_digest: str = Field(pattern=_SHA256_PATTERN)
    application_id: int = Field(ge=1)
    application_revision: int = Field(ge=1)
    job_digest: str = Field(pattern=_SHA256_PATTERN)
    company_digest: str = Field(pattern=_SHA256_PATTERN)
    fit_decision_digest: str = Field(pattern=_SHA256_PATTERN)
    form_plan_id: UUID
    form_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    form_contract_digest: str = Field(pattern=_SHA256_PATTERN)
    selected_cv_hash: str = Field(pattern=_SHA256_PATTERN)
    profile_version: int = Field(ge=1)
    confirmed_answer_revision: str = Field(pattern=_SHA256_PATTERN)
    adapter_name: str = Field(pattern=_SAFE_TOKEN_PATTERN, max_length=64)
    adapter_version: str = Field(max_length=32)
    selector_version: str = Field(max_length=64)
    fit_score: float = Field(ge=0.0, le=100.0)
    allowed: bool
    reason_codes: tuple[str, ...] = Field(default=(), max_length=32)
    evaluated_at: AwareDatetime
    authority_expires_at: AwareDatetime | None = None

    @field_validator("fit_score")
    @classmethod
    def finite_fit_score(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("fit score must be finite")
        return value

    @field_validator("reason_codes")
    @classmethod
    def bounded_reasons(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(dict.fromkeys(values)) != values:
            raise ValueError("reason codes must be ordered and unique")
        if any(re.fullmatch(_REASON_PATTERN, value) is None for value in values):
            raise ValueError("reason codes must be stable bounded tokens")
        return values

    @model_validator(mode="after")
    def outcome_is_consistent(self) -> AutoSubmitDecisionV1:
        if self.allowed == bool(self.reason_codes):
            raise ValueError("allowed decisions have no reasons; denied decisions require reasons")
        if self.allowed:
            if self.authority_expires_at is None:
                raise ValueError("allowed decision requires an authority expiry")
            if self.authority_expires_at <= self.evaluated_at:
                raise ValueError("allowed decision authority must be live")
        elif self.authority_expires_at is not None:
            raise ValueError("denied decisions cannot carry authority")
        return self

    @property
    def decision_digest(self) -> str:
        return hashlib.sha256(canonical_model_bytes(self)).hexdigest()


def canonical_model_bytes(model: BaseModel) -> bytes:
    return json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_policy_bytes(policy: AutoSubmitPolicyV1) -> bytes:
    return canonical_model_bytes(policy)


def _signature_text(signature: bytes) -> str:
    return base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")


def _signature_bytes(signature: str) -> bytes:
    return base64.urlsafe_b64decode(signature + "==")


def sign_auto_submit_policy(
    policy: AutoSubmitPolicyV1,
    *,
    key_id: UUID,
    private_key: Ed25519PrivateKey,
) -> SignedAutoSubmitPolicyV1:
    signature = private_key.sign(canonical_policy_bytes(policy))
    return SignedAutoSubmitPolicyV1(
        key_id=key_id,
        policy=policy,
        signature=_signature_text(signature),
    )


def verify_auto_submit_policy(
    signed: SignedAutoSubmitPolicyV1,
    *,
    public_key: Ed25519PublicKey,
) -> None:
    try:
        public_key.verify(
            _signature_bytes(signed.signature),
            canonical_policy_bytes(signed.policy),
        )
    except (InvalidSignature, ValueError) as exc:
        raise ValueError("AUTOMATION_POLICY_SIGNATURE_INVALID") from exc


__all__ = [
    "AUTOMATION_DECISION_SCHEMA_VERSION",
    "AUTOMATION_POLICY_ACTIVE_END",
    "AUTOMATION_POLICY_ACTIVE_START",
    "AUTOMATION_POLICY_MAX_DAYS",
    "AUTOMATION_POLICY_SCHEMA_VERSION",
    "AUTOMATION_POLICY_TIMEZONE",
    "AutoSubmitDecisionV1",
    "AutoSubmitPolicyV1",
    "AutomationGeography",
    "PolicyAuthoritySource",
    "QualifiedFormContractV1",
    "SignedAutoSubmitPolicyV1",
    "canonical_model_bytes",
    "canonical_policy_bytes",
    "sign_auto_submit_policy",
    "verify_auto_submit_policy",
]
