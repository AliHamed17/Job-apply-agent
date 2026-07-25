import pytest
from core.credentials import CredentialVault
from jobs.models import JobData
from llm.generation import GeneratedApplication
from profile.loader import get_profile
from submitters.portal_login import PortalLoginSubmitter


def test_credential_vault():
    cred_nvidia = CredentialVault.get_credential_for_url("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite")
    assert "ali.h.10j@gmail.com" in cred_nvidia.username
    assert "nvidia" in cred_nvidia.domain or "workday" in cred_nvidia.domain

    cred_default = CredentialVault.get_credential_for_url("https://unknowncompany.com/apply")
    assert cred_default.username == "ali.h.10j@gmail.com"


def test_portal_login_submitter():
    sub = PortalLoginSubmitter()
    job = JobData(title="NVIDIA AI Lead", company="NVIDIA", apply_url="https://nvidia.wd5.myworkdayjobs.com/job/100")
    assert sub.can_submit(job) is True


@pytest.mark.asyncio
async def test_portal_login_submission():
    sub = PortalLoginSubmitter()
    job = JobData(
        title="Senior AI Engineer",
        company="NVIDIA",
        apply_url="https://nvidia.wd5.myworkdayjobs.com/job/1001",
        description="Build LLMs and RAG systems",
    )
    profile = get_profile()
    gen_app = GeneratedApplication(
        cover_letter="Cover letter text",
        qa_answers={"years_of_experience": "6"},
    )
    result = await sub.submit(job, gen_app, profile.__dict__)
    assert result.success is True
    assert "portal-auth" in result.confirmation_id
