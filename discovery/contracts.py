"""Versioned, privacy-safe contracts for the v5 discovery mesh."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from jobs.models import JobData

_SEMVER_PATTERN = r"^[1-9]\d*\.\d+\.\d+$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
SourceType = Literal[
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
    "remotive",
    "gmail_alert",
    "linkedin_partner",
    "generic_jsonld",
    "generic_feed",
]


def stable_digest(payload: object) -> str:
    """Return a deterministic lower-case SHA-256 over one JSON value."""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DiscoverySourceDescriptor(BaseModel):
    """Capabilities and operating policy for one source implementation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_key: str = Field(min_length=1, max_length=255)
    source_type: SourceType
    semantic_version: str = Field(pattern=_SEMVER_PATTERN)
    configuration_digest: str = Field(default="0" * 64, pattern=_SHA256_PATTERN)
    transport: Literal["public_api", "oauth_mailbox", "partner_api", "permitted_web"]
    authentication_mode: Literal["none", "oauth_local", "partner", "operator_configured"]
    host: str = Field(min_length=1, max_length=255)
    cadence_seconds: int = Field(ge=60, le=86_400)
    supports_cursor: bool
    supports_conditional_requests: bool
    tenant_scoped: bool
    enabled: bool = True
    disabled_reason: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def disabled_sources_explain_why(self) -> DiscoverySourceDescriptor:
        if self.enabled == bool(self.disabled_reason):
            raise ValueError("disabled sources require exactly one disabled reason")
        return self


class DiscoveryCursor(BaseModel):
    """Opaque incremental checkpoint with HTTP validators."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_key: str = Field(min_length=1, max_length=255)
    catalog_key: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    cursor: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    etag: str | None = Field(default=None, max_length=255)
    last_modified: str | None = Field(default=None, max_length=255)
    last_seen_posting_at: datetime | None = None


class SearchIntentV1(BaseModel):
    """One deterministic role-family query derived from one configured CV."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["search-intent.v1"] = "search-intent.v1"
    intent_id: str = Field(pattern=_SHA256_PATTERN)
    cv_id: str = Field(min_length=1, max_length=255)
    titles: tuple[str, ...] = Field(min_length=1, max_length=30)
    skills: tuple[str, ...] = Field(default=(), max_length=100)
    seniority: tuple[str, ...] = Field(default=(), max_length=20)
    locations: tuple[str, ...] = Field(min_length=1, max_length=20)
    remote_regions: tuple[str, ...] = ("worldwide", "emea", "israel")


class EmployerCatalogEntry(BaseModel):
    """A configured or locally learned tenant identifier for a public feed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    catalog_key: str = Field(pattern=_SHA256_PATTERN)
    company_name: str = Field(min_length=1, max_length=300)
    ats: Literal[
        "greenhouse",
        "lever",
        "ashby",
        "smartrecruiters",
        "generic_jsonld",
        "generic_feed",
    ]
    tenant_key: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    region: Literal["global", "eu"] = "global"
    base_url: HttpUrl | None = None
    enabled: bool = True
    discovered_via: Literal["config", "alert", "manual", "feed"] = "config"


class JobSourceOccurrence(BaseModel):
    """One source observation before it is attached to a canonical local job."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    occurrence_key: str = Field(pattern=_SHA256_PATTERN)
    source_key: str = Field(min_length=1, max_length=255)
    catalog_key: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    external_posting_id: str | None = Field(default=None, max_length=255)
    normalized_url: str = Field(min_length=1, max_length=4096)
    normalized_url_hash: str = Field(pattern=_SHA256_PATTERN)
    revision_digest: str = Field(pattern=_SHA256_PATTERN)
    observed_at: datetime
    closed: bool = False


class DiscoveredPosting(BaseModel):
    """Canonical content plus its exact source identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job: JobData
    occurrence: JobSourceOccurrence


class DiscoveryPage(BaseModel):
    """One bounded fetch result and the checkpoint to commit with it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    postings: tuple[DiscoveredPosting, ...] = ()
    snapshot_occurrence_keys: tuple[str, ...] = ()
    cursor: DiscoveryCursor
    complete_snapshot: bool = False
    not_modified: bool = False
    restart_snapshot: bool = False
    retry_after_seconds: float | None = Field(default=None, ge=0, le=86_400)

    @model_validator(mode="after")
    def restart_is_control_only(self) -> DiscoveryPage:
        if self.restart_snapshot and (
            self.postings
            or self.snapshot_occurrence_keys
            or self.complete_snapshot
            or self.not_modified
        ):
            raise ValueError("snapshot restart pages cannot carry observations")
        return self
