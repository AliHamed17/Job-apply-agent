import pytest

from core.credentials import CredentialAccessDisabledError, CredentialVault
from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.portal_login import PortalLoginSubmitter


def test_credential_vault_is_disabled():
    with pytest.raises(CredentialAccessDisabledError):
        CredentialVault.get_credential_for_url(
            "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
        )


def test_portal_login_submitter():
    sub = PortalLoginSubmitter()
    job = JobData(
        title="NVIDIA AI Lead",
        company="NVIDIA",
        apply_url="https://nvidia.wd5.myworkdayjobs.com/job/100",
    )
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
    gen_app = GeneratedApplication(
        cover_letter="Cover letter text",
        qa_answers={"years_of_experience": "6"},
    )
    result = await sub.submit(job, gen_app, {})
    assert result.status == "draft_only"
    assert result.reason_code == "PORTAL_SESSION_REQUIRED"
    assert result.confirmation_id is None
