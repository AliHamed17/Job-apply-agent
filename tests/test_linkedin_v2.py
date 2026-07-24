import pytest
from profile.models import UserProfile
from submitters.form_brain import FormBrain, FieldSpec
from submitters.linkedin_v2 import resolve_step
from llm.client import LLMClient


class _LLM(LLMClient):
    def __init__(self, mapping): self.mapping = mapping
    async def generate(self, prompt, system="", max_tokens=2000, temperature=0.3):
        for k, v in self.mapping.items():
            if k in prompt.lower():
                return v
        return "UNKNOWN"
    async def generate_json(self, *a, **k): return {}


def _profile():
    p = UserProfile(); p.personal.name = "Ali Hamed"; p.personal.email = "a@e.com"
    p.resume.text = "10 years RF."
    return p


@pytest.mark.asyncio
async def test_resolve_step_fills_answerable_fields():
    fields = [FieldSpec("Email", "text", [], True),
              FieldSpec("Years of RF experience", "number", [], True)]
    brain = FormBrain(_profile(), client=_LLM({"years of rf": "10"}), db=None)
    plan = await resolve_step(fields, brain, job=None)
    assert plan.fills["Email"] == "a@e.com"
    assert plan.fills["Years of RF experience"] == "10"
    assert plan.blocked_by is None


@pytest.mark.asyncio
async def test_resolve_step_blocks_on_unanswerable_required():
    fields = [FieldSpec("Do you hold a Secret clearance?", "radio", ["Yes", "No"], True)]
    brain = FormBrain(_profile(), client=_LLM({}), db=None)  # returns UNKNOWN
    plan = await resolve_step(fields, brain, job=None)
    assert plan.blocked_by == "Do you hold a Secret clearance?"
