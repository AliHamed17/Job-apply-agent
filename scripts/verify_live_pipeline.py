"""Master Live Pipeline Verification Script for CV Alignment and Submissions."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog

from core.credentials import CredentialVault
from jobs.models import JobData
from match.scoring import score_job
from profile.cv_routing import RoutingJob, load_routing_config, route_cv
from profile.loader import get_profile
from profile.spotlight_matcher import match_portfolio_spotlight

logger = structlog.get_logger(__name__)


def test_cv_alignment_pipeline(job_title: str, company: str, description: str, apply_url: str):
    print("\n" + "=" * 90)
    print(f"PIPELINE VERIFICATION: {job_title} at {company}")
    print("=" * 90)

    profile = get_profile()
    cred = CredentialVault.get_credential_for_url(apply_url)

    job_data = JobData(
        title=job_title,
        company=company,
        location="Israel",
        description=description,
        apply_url=apply_url,
    )

    # 1. Score job against candidate profile
    score_res = score_job(job_data, profile)

    # 2. CV Alignment Routing (Select 1 of 12 CVs)
    routing_cfg = load_routing_config("cv_routing.yaml")
    routing_job = RoutingJob(title=job_title, description=description)
    routing_decision = route_cv(routing_job, routing_cfg)
    selected_cv_id = routing_decision.selected_cv_id or "software-engineer"

    # 3. Portfolio Spotlight Showcase
    spotlight = match_portfolio_spotlight(job_data, profile)

    # 4. Resume PDF File Check
    cv_filename = f"Ali_Hamed_CV_{selected_cv_id.replace('-', '_').title()}.pdf"
    cv_file_path = Path(f"./cvs/{cv_filename}")
    if not cv_file_path.exists():
        cv_file_path = Path("./cvs/Ali_Hamed_CV_AI_Engineer.pdf")

    print(f"\n1. Candidate Name:       {profile.personal.name}")
    print(f"2. Email Address:        {profile.personal.email}")
    print(f"3. Login Credentials:    {cred.username}")
    print(f"4. Match Score:          {score_res.total:.1f}/100.0")
    print(f"5. Aligned CV Selected:  '{selected_cv_id}' ({cv_file_path.name})")
    print(f"6. Target Portal URL:    {apply_url}")
    print(f"7. Showcase Experience:  {spotlight.showcase_text[:120]}...")

    print("\n" + "=" * 90)
    print("CV ALIGNMENT & SUBMISSION PREPARATION COMPLETE")
    print("=" * 90 + "\n")


if __name__ == "__main__":
    test_cv_alignment_pipeline(
        job_title="Senior AI & RAG Engineer",
        company="NVIDIA Israel",
        description="Develop deep learning models, PyTorch pipelines, RAG architectures, CUDA systems, and C++ backends.",
        apply_url="https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
    )
