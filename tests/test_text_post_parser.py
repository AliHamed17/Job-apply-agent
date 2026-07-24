import pytest
from llm.client import LLMClient
from ingestion.text_post_parser import looks_like_job, parse_text_post


def test_prefilter():
    assert looks_like_job("We are hiring an RF Engineer, send CV to hr@x.com") is True
    assert looks_like_job("Good morning everyone ☀️") is False


class _LLM(LLMClient):
    async def generate(self, *a, **k): return ""
    async def generate_json(self, *a, **k):
        return {"is_job": True, "title": "RF Engineer", "company": "TelcoX",
                "description": "5G RF role", "contact_phone": "+971500000000",
                "contact_email": "hr@telcox.com"}


@pytest.mark.asyncio
async def test_parse_extracts_contact():
    r = await parse_text_post("Hiring RF Engineer, WhatsApp +971500000000", client=_LLM())
    assert r.is_job is True
    assert r.contact_phone == "+971500000000"
    assert r.contact_email == "hr@telcox.com"


@pytest.mark.asyncio
async def test_prefilter_short_circuits_non_jobs():
    class _Boom(LLMClient):
        async def generate(self, *a, **k): raise AssertionError("no LLM")
        async def generate_json(self, *a, **k): raise AssertionError("no LLM")
    r = await parse_text_post("happy friday!", client=_Boom())
    assert r.is_job is False
