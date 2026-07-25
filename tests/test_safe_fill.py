"""Safe form filling — never answer a question you don't actually know.

The browser submitters used to answer every yes/no dropdown "Yes", every
empty number field "0", and drop an arbitrary unrelated qa_answer into any
text field whose label didn't match. On a real application that submits
false statements: "Yes" to a felony question, 0 to years of experience.

These tests pin the replacement behavior using a fake Playwright page, so
they run without a browser.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from submitters.form_brain import AnswerResult
from submitters.safe_fill import (
    fill_numeric,
    fill_selects,
    fill_text,
    needs_review_error,
)

# ── Fake Playwright surface ───────────────────────────────────────────


class FakeElement:
    def __init__(
        self,
        label="",
        value="",
        options=None,
        required=False,
        editable=True,
        visible=True,
        attrs=None,
    ):
        self.label = label
        self.value = value
        self.options = options or []
        self._attrs = {"id": label and f"id-{abs(hash(label)) % 9999}" or ""}
        if required:
            self._attrs["required"] = "true"
        self._attrs.update(attrs or {})
        self._editable = editable
        self._visible = visible
        self.filled = None
        self.selected = None

    async def get_attribute(self, name):
        return self._attrs.get(name)

    async def is_editable(self):
        return self._editable

    async def is_visible(self):
        return self._visible

    async def input_value(self):
        return self.value

    async def fill(self, value):
        self.filled = value

    async def select_option(self, label=None):
        self.selected = label

    def locator(self, sel):
        if "option" in sel:
            return FakeOptionList(self.options)
        return FakeLocator([])


class FakeOptionList:
    def __init__(self, options):
        self.options = options

    async def all_text_contents(self):
        return list(self.options)

    async def count(self):
        return len(self.options)


class FakeLocator:
    def __init__(self, elements):
        self.elements = elements

    async def count(self):
        return len(self.elements)

    def nth(self, i):
        return self.elements[i]

    @property
    def first(self):
        return self.elements[0] if self.elements else None


class FakeLabel:
    def __init__(self, text):
        self.text = text

    async def count(self):
        return 1

    async def inner_text(self):
        return self.text


class FakePage:
    """Routes locator() calls the way safe_fill uses them."""

    def __init__(self, selects=None, numbers=None, texts=None):
        self.selects = selects or []
        self.numbers = numbers or []
        self.texts = texts or []

    def locator(self, sel):
        if sel.startswith("label[for="):
            wanted = sel.split('"')[1]
            for el in self.selects + self.numbers + self.texts:
                if el._attrs.get("id") == wanted:
                    return FakeLocatorLabel(el.label)
            return FakeLocatorLabel(None)
        if sel == "select:visible":
            return FakeLocator(self.selects)
        if 'input[type="number"]' in sel:
            return FakeLocator(self.numbers)
        if "textarea" in sel or 'input[type="text"]' in sel:
            return FakeLocator(self.texts)
        return FakeLocator([])


class FakeLocatorLabel:
    def __init__(self, text):
        self.text = text

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self.text else 0

    async def inner_text(self):
        return self.text or ""


def brain_returning(mapping, default_confident=False):
    """FormBrain stub: confident only for labels present in `mapping`."""
    brain = AsyncMock()

    async def answer(field, job):
        for key, val in mapping.items():
            if key.lower() in field.label.lower():
                return AnswerResult(val, "test", True)
        return AnswerResult(None, "test", default_confident)

    brain.answer = answer
    return brain


# ── Selects ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_felony_question_is_not_auto_answered_yes():
    """The exact behavior that made the old code dangerous."""
    sel = FakeElement(
        label="Have you ever been convicted of a felony?",
        options=["Yes", "No"],
        required=True,
    )
    page = FakePage(selects=[sel])
    blocked = await fill_selects(page, brain_returning({}), job=None)

    assert sel.selected is None, "must not answer a felony question blind"
    assert blocked == ["Have you ever been convicted of a felony?"]


@pytest.mark.asyncio
async def test_visa_sponsorship_not_auto_answered_yes():
    sel = FakeElement(
        label="Do you require visa sponsorship?", options=["Yes", "No"], required=True
    )
    page = FakePage(selects=[sel])
    blocked = await fill_selects(page, brain_returning({}), job=None)

    assert sel.selected is None
    assert blocked == ["Do you require visa sponsorship?"]


@pytest.mark.asyncio
async def test_select_answered_when_brain_is_confident():
    sel = FakeElement(label="Are you authorized to work in Israel?", options=["Yes", "No"])
    page = FakePage(selects=[sel])
    blocked = await fill_selects(page, brain_returning({"authorized to work": "Yes"}), job=None)

    assert sel.selected == "Yes"
    assert blocked == []


@pytest.mark.asyncio
async def test_only_offered_options_are_chosen():
    """A confident answer that isn't on the menu must not be forced in."""
    sel = FakeElement(label="Preferred start", options=["Immediately", "1 month"])
    page = FakePage(selects=[sel])
    blocked = await fill_selects(page, brain_returning({"preferred start": "Next year"}), job=None)

    assert sel.selected is None
    assert blocked == []  # not required, so skipped rather than blocking


@pytest.mark.asyncio
async def test_short_or_ambiguous_select_answer_is_not_fuzzy_matched():
    sel = FakeElement(
        label="Choose an availability preference",
        options=["No preference", "Not currently available"],
        required=True,
    )
    page = FakePage(selects=[sel])
    blocked = await fill_selects(
        page,
        brain_returning({"availability": "No"}),
        job=None,
    )

    assert sel.selected is None
    assert blocked == ["Choose an availability preference"]


@pytest.mark.asyncio
async def test_optional_unanswered_select_does_not_block():
    sel = FakeElement(label="How did you hear about us?", options=["Yes", "No"])
    page = FakePage(selects=[sel])
    blocked = await fill_selects(page, brain_returning({}), job=None)

    assert sel.selected is None
    assert blocked == []


@pytest.mark.asyncio
async def test_prefilled_select_is_left_alone():
    sel = FakeElement(label="Country", value="Israel", options=["Israel", "USA"])
    page = FakePage(selects=[sel])
    await fill_selects(page, brain_returning({"country": "USA"}), job=None)

    assert sel.selected is None


# ── Numeric ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_years_of_experience_not_auto_zeroed():
    el = FakeElement(label="Years of Python experience", required=True)
    page = FakePage(numbers=[el])
    blocked = await fill_numeric(page, brain_returning({}), job=None)

    assert el.filled is None, "0 would be false and self-disqualifying"
    assert blocked == ["Years of Python experience"]


@pytest.mark.asyncio
async def test_numeric_filled_when_brain_knows():
    el = FakeElement(label="Years of Python experience")
    page = FakePage(numbers=[el])
    blocked = await fill_numeric(page, brain_returning({"years of python": "4"}), job=None)

    assert el.filled == "4"
    assert blocked == []


@pytest.mark.asyncio
async def test_numeric_strips_prose_to_digits():
    el = FakeElement(label="Years of experience")
    page = FakePage(numbers=[el])
    await fill_numeric(page, brain_returning({"years": "about 5 years"}), job=None)

    assert el.filled == "5"


@pytest.mark.asyncio
async def test_numeric_uses_one_number_instead_of_concatenating_all_digits():
    el = FakeElement(label="Years of experience")
    page = FakePage(numbers=[el])
    await fill_numeric(
        page,
        brain_returning({"years": "10 years across 2 roles"}),
        job=None,
    )

    assert el.filled == "10"


# ── Text ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unrelated_qa_answer_is_not_sprayed_into_text_field():
    """The old fallback put the first qa_answer into any unmatched field."""
    el = FakeElement(label="What is your favourite programming language?")
    page = FakePage(texts=[el])
    qa = {"why_this_role": "I am excited about your mission and RAG pipelines."}

    blocked = await fill_text(page, brain_returning({}), job=None, qa_answers=qa)

    assert el.filled is None, "must not paste an unrelated answer"
    assert blocked == []


@pytest.mark.asyncio
async def test_matching_qa_answer_is_used():
    el = FakeElement(label="Why this role?")
    page = FakePage(texts=[el])
    qa = {"this_role": "Because it matches my LLM background."}

    await fill_text(page, brain_returning({}), job=None, qa_answers=qa)

    assert el.filled == "Because it matches my LLM background."


@pytest.mark.asyncio
async def test_required_unanswerable_text_blocks():
    el = FakeElement(label="Describe your security clearance", required=True)
    page = FakePage(texts=[el])
    blocked = await fill_text(page, brain_returning({}), job=None, qa_answers={})

    assert el.filled is None
    assert blocked == ["Describe your security clearance"]


# ── Reporting ─────────────────────────────────────────────────────────


def test_needs_review_error_is_parseable_by_the_worker():
    err = needs_review_error(["Years of experience", "Clearance level"])
    assert err.startswith("NEEDS_REVIEW:")
    # worker/tasks.py splits on this marker to set needs_review_reason
    reason = err.split("NEEDS_REVIEW:", 1)[1]
    assert "Years of experience" in reason


def test_needs_review_error_truncates_long_lists():
    err = needs_review_error([f"Question {i}" for i in range(10)])
    assert "+7 more" in err
