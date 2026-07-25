"""Live Full-Auto Application Submitter for Open Public Greenhouse / Lever Job Postings."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog

from jobs.models import JobData
from llm.generation import GeneratedApplication
from profile.loader import get_profile
from submitters.greenhouse import GreenhouseSubmitter

logger = structlog.get_logger(__name__)

# Sample Live Open Greenhouse Job Posting
GREENHOUSE_TEST_JOB_URL = "https://boards.greenhouse.io/embed/job_app?for=testcompany&token=123456"


async def execute_live_greenhouse_application():
    print("\n" + "=" * 90)
    print("EXECUTING LIVE FULL-AUTO GREENHOUSE APPLICATION FOR ALI HAMED")
    print("=" * 90)

    profile = get_profile()
    cv_path = str(Path("./cvs/Ali_Hamed_CV_AI_Engineer.pdf").resolve())

    print(f"\n[PROFILE] Candidate: {profile.personal.name} ({profile.personal.email})")
    print(f"[CV PATH] {cv_path} (Exists: {Path(cv_path).exists()})")

    job_data = JobData(
        title="Senior AI Engineer",
        company="Greenhouse Target Employer",
        location="Tel Aviv, Israel",
        description="Build Python PyTorch RAG LLM containerized pipelines and automated test suites.",
        requirements="Python, PyTorch, RAG, LLM, Docker, Kubernetes, Jenkins",
        apply_url=GREENHOUSE_TEST_JOB_URL,
        source_url=GREENHOUSE_TEST_JOB_URL,
    )

    gen_app = GeneratedApplication(
        cover_letter=(
            "Dear Hiring Team,\n\n"
            "I am writing to express my strong interest in the Senior AI Engineer role. "
            "I have architected and deployed 3 production AI agent tools using Python, PyTorch, and RAG architectures.\n\n"
            "Best regards,\nAli Hamed"
        ),
        qa_answers={
            "first_name": "Ali",
            "last_name": "Hamed",
            "email": profile.personal.email,
            "phone": profile.personal.phone,
            "work_authorization": "Yes",
        },
    )

    submitter = GreenhouseSubmitter()

    print(f"\n[STEP 1] Initializing Submitter: {submitter.platform_name}")
    print(f"[STEP 2] Launching Playwright Browser Automation for {job_data.apply_url}...")

    profile_dict = profile.model_dump() if hasattr(profile, "model_dump") else profile.__dict__

    res = await submitter._submit_via_browser(
        job=job_data,
        application=gen_app,
        user_profile=profile_dict,
        resume_path=cv_path,
    )

    print("\n" + "=" * 90)
    print("GREENHOUSE SUBMISSION RESULT:")
    print("=" * 90)
    print(f"   * Success Status:      {res.success}")
    print(f"   * Platform:            {res.platform}")
    print(f"   * Submission Status:   {res.status.upper()}")
    if res.error:
        print(f"   * Log / Detail:        {res.error}")
    print("=" * 90 + "\n")


if __name__ == "__main__":
    asyncio.run(execute_live_greenhouse_application())
