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
        """Minimum viable job: must have title and not be a placeholder."""
        t = self.title.strip()
        if not t or len(t) < 3:
            return False
        # Reject placeholders like {{position.name}}, %JOB_TITLE%, or very short generic ones
        forbidden = ["{{", "}}", "position.name", "company.name", "loading...", "template"]
        lower_t = t.lower()
        if any(f in lower_t for f in forbidden):
            return False
        return True
