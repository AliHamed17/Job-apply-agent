"""User profile models with explicit fact provenance.

The convenience fields on :class:`UserProfile` are intentionally separate
from evidence.  A value merely appearing on a CV never makes it safe to use
for a legal, authorization, or demographic application answer.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.sensitive_policy import (
    contains_prompt_injection,
    contains_sensitive_text,
)
from core.sensitive_policy import (
    is_sensitive_fact_key as shared_sensitive_fact_key,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

SENSITIVE_FACT_KEYS = frozenset(
    {
        "age",
        "attestation",
        "attestations",
        "citizenship",
        "clearance",
        "consent",
        "demographic",
        "demographics",
        "country_of_birth",
        "country_of_origin",
        "date_of_birth",
        "birth_date",
        "dob",
        "disability",
        "ethnicity",
        "gender",
        "immigration_status",
        "license",
        "licenses",
        "licensing",
        "marital_status",
        "military_status",
        "nationality",
        "native_country",
        "place_of_birth",
        "pronoun",
        "pronouns",
        "legal_status",
        "race",
        "religion",
        "security_clearance",
        "sex",
        "sexual_orientation",
        "sponsorship",
        "visa",
        "visa_sponsorship",
        "veteran_status",
        "work_authorization",
        "work_permit",
        "right_to_work",
    }
)
LLM_SAFE_CONFIRMED_FACT_KEYS = frozenset(
    {
        "primary_language",
        "backend_framework",
        "database_skill",
        "cloud_platform",
        "container_platform",
        "iac_tool",
        "data_tool",
        "ml_framework",
        "frontend_language",
        "frontend_framework",
        "test_framework",
        "automation_tool",
        "operating_system",
        "embedded_language",
        "realtime_system",
        "analytics_tool",
        "pipeline_tool",
        "api_style",
        "version_control",
        "highest_degree",
        "skills",
        "years_experience",
        "notice_period",
        "salary_expectations",
        "availability_date",
        "preferred_start",
        "portfolio_summary",
    }
)
CV_ARTIFACT_FACT_KEYS = frozenset(
    {
        "primary_language",
        "backend_framework",
        "database_skill",
        "cloud_platform",
        "container_platform",
        "iac_tool",
        "data_tool",
        "ml_framework",
        "frontend_language",
        "frontend_framework",
        "test_framework",
        "automation_tool",
        "operating_system",
        "embedded_language",
        "realtime_system",
        "analytics_tool",
        "pipeline_tool",
        "api_style",
        "version_control",
        "highest_degree",
        "relevant_experience",
        "technical_summary",
    }
)


def canonical_fact_key(value: str) -> str:
    """Return a stable, policy-safe key for evidence lookup."""

    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value.strip().lower())).strip("_")


def is_sensitive_fact_key(value: str) -> bool:
    """Whether a fact belongs to a category an LLM may never answer."""

    return shared_sensitive_fact_key(value)


class SalaryPreference(BaseModel):
    min: int = 0
    max: int = 0
    currency: str = "USD"


class Preferences(BaseModel):
    roles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_ok: bool = True
    hybrid_ok: bool = True
    onsite_ok: bool = True
    salary: SalaryPreference = Field(default_factory=SalaryPreference)
    keywords: list[str] = Field(default_factory=list)
    blacklist_companies: list[str] = Field(default_factory=list)
    seniority: list[str] = Field(default_factory=list)


class Links(BaseModel):
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""


class Resume(BaseModel):
    text: str = ""
    pdf_path: str = ""
    pdf_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")


class CoverLetterConfig(BaseModel):
    style: str = "professional but personable, concise (3-4 paragraphs)"


class Personal(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    # Accepted only so old local YAML and callers still parse.  It is excluded
    # from new snapshots and is never placed in an LLM prompt.  UserProfile's
    # compatibility validator quarantines the value as CV-extracted evidence.
    work_authorization: str = Field(default="", exclude=True)


class ProfileEvidence(BaseModel):
    """Facts separated by provenance; confirmed facts gate sensitive answers."""

    cv_extracted: dict[str, str] = Field(default_factory=dict)
    cv_extracted_by_artifact: dict[str, dict[str, str]] = Field(default_factory=dict)
    user_confirmed: dict[str, str] = Field(default_factory=dict)
    inferred_preferences: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_keys(self) -> ProfileEvidence:
        """Canonicalize evidence keys without promoting their provenance."""

        for field_name in ("cv_extracted", "user_confirmed", "inferred_preferences"):
            values = getattr(self, field_name)
            normalized: dict[str, str] = {}
            for key, value in values.items():
                canonical = canonical_fact_key(str(key))
                clean = str(value).strip()
                if not canonical or not clean:
                    continue
                if canonical in normalized and normalized[canonical] != clean:
                    raise ValueError(f"{field_name} contains conflicting canonical keys")
                normalized[canonical] = clean
            setattr(self, field_name, normalized)
        artifact_facts: dict[str, dict[str, str]] = {}
        for artifact_hash, values in self.cv_extracted_by_artifact.items():
            digest = str(artifact_hash).strip().casefold()
            if not _SHA256_RE.fullmatch(digest):
                raise ValueError("cv_extracted_by_artifact keys must be SHA-256 digests")
            normalized_facts: dict[str, str] = {}
            for key, value in values.items():
                canonical = canonical_fact_key(str(key))
                clean = str(value).strip()
                if not canonical or not clean:
                    continue
                if canonical in normalized_facts and normalized_facts[canonical] != clean:
                    raise ValueError("CV artifact facts contain conflicting canonical keys")
                normalized_facts[canonical] = clean
            artifact_facts[digest] = normalized_facts
        self.cv_extracted_by_artifact = artifact_facts
        return self

    def confirmed_fact(self, key: str) -> str | None:
        """Return exact operator-confirmed evidence, never a CV fallback."""

        value = self.user_confirmed.get(canonical_fact_key(key), "").strip()
        return value or None

    def llm_safe_confirmed_facts(self) -> dict[str, str]:
        """Confirmed facts that may be exposed to material-generation prompts."""

        return {
            key: value
            for key, value in self.user_confirmed.items()
            if value and key in LLM_SAFE_CONFIRMED_FACT_KEYS and not is_sensitive_fact_key(key)
        }

    def facts_for_cv(self, pdf_sha256: str) -> dict[str, str]:
        """Return only facts explicitly bound to the selected CV bytes."""

        digest = str(pdf_sha256).strip().casefold()
        if not _SHA256_RE.fullmatch(digest):
            return {}
        return dict(self.cv_extracted_by_artifact.get(digest, {}))


class CVArtifact(BaseModel):
    """Immutable text extraction whose identity is the source PDF's SHA-256."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pdf_sha256: str = Field(min_length=64, max_length=64)
    byte_size: int = Field(ge=0)
    extracted_text: str = Field(exclude=True, repr=False)

    @model_validator(mode="after")
    def validate_digest(self) -> CVArtifact:
        if not _SHA256_RE.fullmatch(self.pdf_sha256):
            raise ValueError("pdf_sha256 must be 64 lowercase hexadecimal characters")
        return self

    @property
    def artifact_id(self) -> str:
        return f"sha256:{self.pdf_sha256}"


class CVArtifactFactV1(BaseModel):
    """One bounded non-sensitive fact extracted from exact CV bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    canonical_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    # Private source material never belongs in ordinary serialization or repr.
    value: str = Field(min_length=1, max_length=500, exclude=True, repr=False)
    source_quote: str = Field(min_length=1, max_length=800, exclude=True, repr=False)

    @model_validator(mode="after")
    def validate_fact(self) -> CVArtifactFactV1:
        if (
            self.canonical_name not in CV_ARTIFACT_FACT_KEYS
            or is_sensitive_fact_key(self.canonical_name)
            or self.value != self.value.strip()
            or self.source_quote != self.source_quote.strip()
            or contains_sensitive_text(self.value)
            or contains_sensitive_text(self.source_quote)
            or contains_prompt_injection(self.value)
            or contains_prompt_injection(self.source_quote)
        ):
            raise ValueError("CV artifact fact must be canonical, non-sensitive, and safe")
        return self


class SelectedCVFactCatalog(BaseModel):
    """Ephemeral fact catalog cryptographically scoped to one selected CV."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(
        default="selected-cv-fact-catalog-v1",
        pattern=r"^selected-cv-fact-catalog-v1$",
    )
    artifact: CVArtifact = Field(exclude=True, repr=False)
    facts: tuple[CVArtifactFactV1, ...] = Field(
        default=(),
        max_length=40,
        exclude=True,
        repr=False,
    )

    @model_validator(mode="after")
    def validate_catalog(self) -> SelectedCVFactCatalog:
        names = tuple(fact.canonical_name for fact in self.facts)
        if len(names) != len(set(names)):
            raise ValueError("CV artifact fact names must be unique")
        if names != tuple(sorted(names)):
            raise ValueError("CV artifact facts must use canonical sorted order")
        from profile.cv_facts import cv_fact_is_literal_source_bound

        if any(
            not cv_fact_is_literal_source_bound(fact, self.artifact.extracted_text)
            for fact in self.facts
        ):
            raise ValueError("CV artifact fact must be literal-source proven")
        return self

    @property
    def pdf_sha256(self) -> str:
        return self.artifact.pdf_sha256

    @property
    def source_text_sha256(self) -> str:
        return hashlib.sha256(self.artifact.extracted_text.encode("utf-8")).hexdigest()

    @property
    def catalog_sha256(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "pdf_sha256": self.pdf_sha256,
            "source_text_sha256": self.source_text_sha256,
            "facts": [
                {
                    "canonical_name": fact.canonical_name,
                    "value": fact.value,
                    "source_quote": fact.source_quote,
                }
                for fact in self.facts
            ],
        }
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, str]:
        """Return a fresh private mapping; callers cannot mutate the catalog."""

        return {fact.canonical_name: fact.value for fact in self.facts}


class SelectedCVArtifact(BaseModel):
    """Local routing/upload binding around a content-addressed CV artifact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cv_id: str = Field(min_length=1, max_length=200)
    # Local-only operational data.  It is deliberately omitted from standard
    # serialization and repr so it cannot slip into audit events or telemetry.
    resolved_path: str = Field(min_length=1, exclude=True, repr=False)
    artifact: CVArtifact

    @property
    def pdf_sha256(self) -> str:
        return self.artifact.pdf_sha256

    @property
    def extracted_text(self) -> str:
        return self.artifact.extracted_text


class Attachment(BaseModel):
    path: str
    label: str = ""


class UserProfile(BaseModel):
    """Full user profile loaded from YAML config."""

    personal: Personal = Field(default_factory=Personal)
    links: Links = Field(default_factory=Links)
    resume: Resume = Field(default_factory=Resume)
    cover_letter: CoverLetterConfig = Field(default_factory=CoverLetterConfig)
    preferences: Preferences = Field(default_factory=Preferences)
    attachments: list[Attachment] = Field(default_factory=list)
    evidence: ProfileEvidence = Field(default_factory=ProfileEvidence)

    @model_validator(mode="before")
    @classmethod
    def quarantine_legacy_sensitive_fields(cls, value):
        """Parse legacy profiles without treating legacy fields as confirmed.

        Historically ``personal.work_authorization`` had no provenance.  It
        remains readable for compatibility, but a conservative migration puts
        it under ``evidence.cv_extracted``.  Only an explicit
        ``evidence.user_confirmed`` entry can be used by deterministic form
        policy, and sensitive facts remain forbidden from LLM prompts.
        """

        if not isinstance(value, dict):
            return value
        data = deepcopy(value)
        personal = data.get("personal")
        if not isinstance(personal, dict):
            return data
        legacy = str(personal.get("work_authorization") or "").strip()
        if not legacy:
            return data
        evidence = data.get("evidence")
        if not isinstance(evidence, dict):
            evidence = {}
            data["evidence"] = evidence
        cv_extracted = evidence.get("cv_extracted")
        if not isinstance(cv_extracted, dict):
            cv_extracted = {}
            evidence["cv_extracted"] = cv_extracted
        cv_extracted.setdefault("work_authorization", legacy)
        return data

    @property
    def full_name(self) -> str:
        return self.personal.name

    @property
    def keyword_set(self) -> set[str]:
        """Lowercase keyword set for matching."""
        return {k.lower() for k in self.preferences.keywords}

    @property
    def role_set(self) -> set[str]:
        """Lowercase role set for matching."""
        return {r.lower() for r in self.preferences.roles}

    @property
    def blacklist_set(self) -> set[str]:
        """Lowercase blacklisted companies."""
        return {c.lower() for c in self.preferences.blacklist_companies}
