"""SmartRecruiters submitter with API-first and browser fallback behavior."""

from __future__ import annotations

import httpx

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import BaseSubmitter, SubmissionResult
from submitters.browser_fallback import run_browser_form_fill


class SmartRecruitersSubmitter(BaseSubmitter):
    platform_name = "smartrecruiters"

    def __init__(self, api_key: str = ""):
        self.api_key = api_key

    def can_submit(self, job: JobData) -> bool:
        url = (job.apply_url or job.source_url).lower()
        return "smartrecruiters.com" in url

    async def submit(self, job: JobData, application: GeneratedApplication, user_profile: dict, resume_path: str | None = None) -> SubmissionResult:
        apply_url = job.apply_url or job.source_url
        if not self.api_key:
            return await run_browser_form_fill(platform_name=self.platform_name, apply_url=apply_url, application=application, user_profile=user_profile, resume_path=resume_path)
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(apply_url, headers={"Authorization": f"Bearer {self.api_key}"}, json={"cover_letter": application.cover_letter})
            if resp.status_code in (200, 201):
                return SubmissionResult(True, self.platform_name, "submitted")
            if resp.status_code in (401, 403, 429):
                return await run_browser_form_fill(platform_name=self.platform_name, apply_url=apply_url, application=application, user_profile=user_profile, resume_path=resume_path)
            return SubmissionResult(False, self.platform_name, "failed", error=f"HTTP {resp.status_code}")
        except Exception:
            return await run_browser_form_fill(platform_name=self.platform_name, apply_url=apply_url, application=application, user_profile=user_profile, resume_path=resume_path)
