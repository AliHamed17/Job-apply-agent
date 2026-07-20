import pytest
from llm.client import LLMClient
from profile.builder import build_profile_from_text


class FakeCVClient(LLMClient):
    async def generate(self, prompt, system="", max_tokens=2000, temperature=0.3):
        return ""
    async def generate_json(self, prompt, system="", max_tokens=2000):
        return {
            "personal": {"name": "Ali Hamed", "email": "ali@example.com",
                         "phone": "+971500000000", "location": "Dubai, UAE",
                         "work_authorization": "UAE resident"},
            "links": {"linkedin": "https://linkedin.com/in/alihamed", "github": "", "portfolio": ""},
            "preferences": {"roles": ["RF Engineer", "RAN Engineer"],
                            "locations": ["Dubai", "Abu Dhabi", "Remote"],
                            "keywords": ["LTE", "5G", "NR", "RF planning"],
                            "seniority": ["mid", "senior"]},
        }


@pytest.mark.asyncio
async def test_build_profile_maps_all_sections():
    p = await build_profile_from_text("dummy cv text", client=FakeCVClient())
    assert p.personal.name == "Ali Hamed"
    assert p.personal.location == "Dubai, UAE"
    assert "RF Engineer" in p.preferences.roles
    assert "5G" in p.preferences.keywords
    assert p.resume.text == "dummy cv text"


@pytest.mark.asyncio
async def test_build_profile_tolerates_missing_sections():
    class Sparse(FakeCVClient):
        async def generate_json(self, prompt, system="", max_tokens=2000):
            return {"personal": {"name": "Jane Doe"}}
    p = await build_profile_from_text("x", client=Sparse())
    assert p.personal.name == "Jane Doe"
    assert p.preferences.roles == []  # default, no crash
