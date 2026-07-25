"""Label-aware form filling shared by the browser-driven submitters.

Every browser submitter except LinkedIn v2 used to fill forms blind:

    if "yes" in options_lower and "no" in options_lower:
        await sel.select_option(label="Yes")     # regardless of the question
    if not current:
        await el.fill("0")                       # regardless of the question
    best_answer = next(iter(qa_answers.values())) # unrelated text, any field

That answers "Have you been convicted of a felony?" with Yes, "Do you
require visa sponsorship?" with Yes, and "Years of Python experience?"
with 0 — each a false statement submitted on the candidate's behalf, and
the last one self-disqualifying. LinkedIn v2 already did this properly via
FormBrain; this module makes that behavior reusable everywhere else.

The rule here is abort-don't-lie, matching submitters/linkedin_v2.py: read
the question, resolve it through FormBrain (deterministic map -> confirmed
evidence -> cache -> LLM, which abstains rather than guess), and if no
confident answer exists leave the field alone. A *required* field left
unanswered is reported back so the submitter can bail to NEEDS_REVIEW
instead of submitting something untrue. Optional unanswered fields are
simply skipped.
"""

from __future__ import annotations

import structlog

from submitters.form_brain import FieldSpec, FormBrain

logger = structlog.get_logger(__name__)

# Bounded so one pathological page can't spin the resolver forever.
_MAX_FIELDS_PER_KIND = 40


async def _label_for(page, el) -> str:
    """Best-effort question text for a form control.

    Tries <label for=...>, then aria-label, then the accessible name, then a
    wrapping <label>. Returns "" when nothing readable is found — callers
    treat an unlabelled field as unanswerable rather than guessing.
    """
    try:
        el_id = await el.get_attribute("id") or ""
        if el_id:
            # CSS ident escaping: ids from ATS templates often contain ":" etc.
            safe = el_id.replace("\\", "\\\\").replace('"', '\\"')
            lbl = page.locator(f'label[for="{safe}"]').first
            if await lbl.count() > 0:
                text = (await lbl.inner_text()).strip()
                if text:
                    return text
    except Exception:
        pass
    for attr in ("aria-label", "placeholder", "name"):
        try:
            val = (await el.get_attribute(attr) or "").strip()
            if val:
                return val
        except Exception:
            continue
    try:
        wrapper = el.locator("xpath=ancestor::label[1]")
        if await wrapper.count() > 0:
            text = (await wrapper.inner_text()).strip()
            if text:
                return text
    except Exception:
        pass
    return ""


async def _is_required(el, label: str) -> bool:
    """Whether the field must be answered for the form to submit."""
    for attr in ("required", "aria-required"):
        try:
            val = await el.get_attribute(attr)
        except Exception:
            continue
        if val is not None and val.lower() not in ("false", "0"):
            return True
    return "*" in label


async def _resolve(brain: FormBrain, label: str, kind: str, job, options=None):
    """Ask FormBrain for an answer, returning None when not confident."""
    try:
        result = await brain.answer(
            FieldSpec(label=label, kind=kind, options=list(options or [])), job
        )
    except Exception as exc:
        logger.warning("safe_fill_resolve_failed", label=label[:80], error=str(exc))
        return None
    if not result.confident or not result.value:
        return None
    return str(result.value)


async def fill_selects(page, brain: FormBrain, job) -> list[str]:
    """Answer visible <select> controls. Returns labels left unanswered.

    Only options actually present on the element are ever chosen, so the
    resolver cannot invent a value the form would reject.
    """
    blocked: list[str] = []
    selects = page.locator("select:visible")
    count = min(await selects.count(), _MAX_FIELDS_PER_KIND)
    for i in range(count):
        sel = selects.nth(i)
        try:
            if not await sel.is_editable():
                continue
            current = await sel.input_value()
            if current and current.strip():
                continue
            options = [o.strip() for o in await sel.locator("option").all_text_contents()]
            options = [o for o in options if o and o.lower() not in ("", "select...", "select")]
            if not options:
                continue
            label = await _label_for(page, sel)
            if not label:
                if await _is_required(sel, ""):
                    blocked.append("(unlabelled dropdown)")
                continue
            answer = await _resolve(brain, label, "select", job, options)
            match = next((o for o in options if o.lower() == (answer or "").lower()), None)
            if match is None and answer:
                # Accept a substring match so "Yes, I am authorized" resolves
                # against an option spelled slightly differently.
                match = next((o for o in options if (answer or "").lower() in o.lower()), None)
            if match is None:
                if await _is_required(sel, label):
                    blocked.append(label)
                logger.info("safe_fill_select_unanswered", label=label[:80])
                continue
            await sel.select_option(label=match)
        except Exception as exc:
            logger.warning("safe_fill_select_error", error=str(exc))
    return blocked


async def fill_numeric(page, brain: FormBrain, job) -> list[str]:
    """Answer visible empty number inputs. Returns labels left unanswered."""
    blocked: list[str] = []
    inputs = page.locator('input[type="number"]')
    count = min(await inputs.count(), _MAX_FIELDS_PER_KIND)
    for i in range(count):
        el = inputs.nth(i)
        try:
            if not (await el.is_visible() and await el.is_editable()):
                continue
            if (await el.input_value()).strip():
                continue
            label = await _label_for(page, el)
            if not label:
                if await _is_required(el, ""):
                    blocked.append("(unlabelled number field)")
                continue
            answer = await _resolve(brain, label, "number", job)
            digits = "".join(ch for ch in (answer or "") if ch.isdigit() or ch == ".")
            if not digits:
                if await _is_required(el, label):
                    blocked.append(label)
                logger.info("safe_fill_numeric_unanswered", label=label[:80])
                continue
            await el.fill(digits)
        except Exception as exc:
            logger.warning("safe_fill_numeric_error", error=str(exc))
    return blocked


async def fill_text(page, brain: FormBrain, job, qa_answers: dict | None = None) -> list[str]:
    """Answer visible empty text inputs and textareas.

    A generated qa_answers entry is used only when its key genuinely matches
    the field's label. The previous behavior — falling back to whatever the
    first non-empty qa_answer happened to be — put unrelated prose into
    arbitrary fields, so it is deliberately not reproduced here.
    """
    blocked: list[str] = []
    qa_answers = qa_answers or {}
    fields = page.locator('input[type="text"]:visible, textarea:visible')
    count = min(await fields.count(), _MAX_FIELDS_PER_KIND)
    for i in range(count):
        el = fields.nth(i)
        try:
            if not await el.is_editable():
                continue
            if (await el.input_value()).strip():
                continue
            label = await _label_for(page, el)
            if not label:
                continue
            low = label.lower()

            answer = None
            for key, value in qa_answers.items():
                if not value:
                    continue
                tokens = [t for t in str(key).lower().split("_") if len(t) > 3]
                if tokens and all(t in low for t in tokens):
                    answer = str(value)
                    break
            if answer is None:
                answer = await _resolve(brain, label, "text", job)
            if not answer:
                if await _is_required(el, label):
                    blocked.append(label)
                logger.info("safe_fill_text_unanswered", label=label[:80])
                continue
            await el.fill(answer[:500])
        except Exception as exc:
            logger.warning("safe_fill_text_error", error=str(exc))
    return blocked


async def fill_form_safely(
    page, brain: FormBrain, job, qa_answers: dict | None = None
) -> list[str]:
    """Run every safe filler. Returns required questions left unanswered."""
    blocked: list[str] = []
    blocked += await fill_text(page, brain, job, qa_answers)
    blocked += await fill_numeric(page, brain, job)
    blocked += await fill_selects(page, brain, job)
    return blocked


def needs_review_error(blocked: list[str]) -> str:
    """Format blocked questions into the NEEDS_REVIEW marker the task parses.

    worker/tasks.py splits on "NEEDS_REVIEW:" and stores the remainder as
    Application.needs_review_reason, so the operator sees which questions
    actually stopped the submission.
    """
    shown = "; ".join(q[:60] for q in blocked[:3])
    if len(blocked) > 3:
        shown += f" (+{len(blocked) - 3} more)"
    return f"NEEDS_REVIEW:UNANSWERED_REQUIRED_FIELDS:{shown}"
