"""User profile Pydantic models."""

from __future__ import annotations

from pydantic import BaseModel, Field


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


class CoverLetterConfig(BaseModel):
    style: str = "professional but personable, concise (3-4 paragraphs)"


class Personal(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    work_authorization: str = ""


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
