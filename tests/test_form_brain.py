import pytest
from llm.client import LLMClient
from profile.models import UserProfile
from submitters.form_brain import FormBrain, FieldSpec, normalize_question, question_hash


def _profile():
    p = UserProfile()
    p.personal.name = "Ali Hamed"; p.personal.email = "ali@example.com"
    p.personal.phone = "+971500000000"; p.personal.location = "Dubai, UAE"
    p.links.linkedin = "https://linkedin.com/in/alihamed"
    p.resume.text = "10 years RF engineering. LTE, 5G NR."
    return p


class _NoLLM(LLMClient):
    async def generate(self, *a, **k): raise AssertionError("LLM should not be called")
    async def generate_json(self, *a, **k): raise AssertionError("LLM should not be called")


class _LLM(LLMClient):
    def __init__(self, ans): self.ans = ans; self.calls = 0
    async def generate(self, *a, **k):
        self.calls += 1; return self.ans
    async def generate_json(self, *a, **k): return {}


def test_normalize_and_hash_stable():
    assert normalize_question("Years of  Python?  ") == normalize_question("years of python")
    assert question_hash("A") == question_hash("a")


@pytest.mark.asyncio
async def test_deterministic_email_no_llm():
    fb = FormBrain(_profile(), client=_NoLLM(), db=None)
    r = await fb.answer(FieldSpec(label="Email address", kind="text", options=[], required=True), job=None)
    assert r.value == "ali@example.com"
    assert r.source == "deterministic"


@pytest.mark.asyncio
async def test_llm_used_then_confident():
    llm = _LLM("8")
    fb = FormBrain(_profile(), client=llm, db=None)
    r = await fb.answer(FieldSpec(label="Years of RF experience", kind="number", options=[], required=True), job=None)
    assert r.value == "8"
    assert r.source == "llm"
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_unanswerable_required_not_confident():
    # LLM returns the refusal sentinel → not confident
    fb = FormBrain(_profile(), client=_LLM("UNKNOWN"), db=None)
    r = await fb.answer(FieldSpec(label="Do you hold a US Top Secret clearance?", kind="radio",
                                  options=["Yes", "No"], required=True), job=None)
    assert r.confident is False
    assert r.value is None
