"""Job data model — canonical representation of a job posting."""

from __future__ import annotations

from pydantic import BaseModel, Field


class JobData(BaseModel):
    """Canonical job posting extracted from a page."""

    title: str
    company: str = ""
    location: str = ""
    employment_type: str = ""   # full-time, part-time, contract, internship
    seniority: str = ""         # entry, mid, senior, lead, director
    description: str = ""
    requirements: str = ""
    apply_url: str = ""
    source_url: str = ""
    date_posted: str = ""
    keywords: list[str] = Field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        """Minimum viable job: must have title."""
        return bool(self.title.strip())
