"""Prompt templates for LLM-based application generation."""

from __future__ import annotations

_SYSTEM_BASE = (
    "You are a professional job application assistant. You help craft tailored, "
    "honest job applications based ONLY on the candidate's actual profile and resume.\n\n"
    "CRITICAL RULES:\n"
    "1. NEVER invent degrees, certifications, or work experience not in the profile.\n"
    "2. NEVER fabricate company names, project names, or technologies not mentioned.\n"
    "3. If information is missing, insert [PLACEHOLDER: describe what's needed].\n"
    "4. Be professional, concise, and genuine.\n"
    "5. Highlight relevant skills and experience that genuinely match the job.\n"
    "6. Use the candidate's specified cover letter style preference.\n"
)


def build_system_prompt(few_shot_examples: list[dict] | None = None) -> str:
    """Build the system prompt, optionally injecting few-shot correction examples.

    Args:
        few_shot_examples: List of dicts with keys ``"bad"``, ``"good"``, and
                           optional ``"note"``.  Sourced from the
                           ``cover_letter_feedback`` DB table via
                           ``GET /api/feedback/examples``.

    Returns:
        System prompt string ready for the LLM.
    """
    if not few_shot_examples:
        return _SYSTEM_BASE

    lines = [_SYSTEM_BASE, "\n\n## Cover Letter Style Examples (learn from these corrections)\n"]
    for i, ex in enumerate(few_shot_examples, start=1):
        note_suffix = f"  Note: {ex['note']}" if ex.get("note") else ""
        lines.append(f"\n### Example {i}{note_suffix}\n")
        lines.append(f"**ORIGINAL (sub-optimal):**\n{ex['bad'].strip()}\n")
        lines.append(f"**CORRECTED (preferred style):**\n{ex['good'].strip()}\n---")
    lines.append("\n\nApply the style and tone from the CORRECTED examples above.\n")
    return "".join(lines)


# ── Legacy constant for backwards compatibility (no few-shot) ─────────────
SYSTEM_PROMPT = build_system_prompt()

# ── Cover Letter Prompt ───────────────────────────────────────────────────
COVER_LETTER_PROMPT = """\
Write a tailored cover letter for the following job application.

## Job Details
- Title: {job_title}
- Company: {company}
- Location: {location}
- Description: {description}

## Candidate Profile
- Name: {name}
- Current Location: {user_location}
- Work Authorization: {work_authorization}

## Resume
{resume_text}

## Style Preference
{cover_letter_style}

Write the cover letter now. Address it to the hiring team at {company}.
If any critical information is missing, use [PLACEHOLDER: ...] markers.
"""

# ── Recruiter Message Prompt ──────────────────────────────────────────────
RECRUITER_MESSAGE_PROMPT = """\
Write a short, friendly recruiter message (2-3 sentences) expressing interest \
in the following position.

Job: {job_title} at {company}
Candidate: {name}
Key skills: {key_skills}

Keep it brief and professional — this is for a cold outreach or LinkedIn message.
"""

# ── Q&A Answers Prompt ────────────────────────────────────────────────────
QA_ANSWERS_PROMPT = """\
Answer the following common job application questions based on the candidate's profile.

## Candidate Profile
- Name: {name}
- Location: {user_location}
- Work Authorization: {work_authorization}

## Resume
{resume_text}

## Job
- Title: {job_title}
- Company: {company}

## Questions to Answer
Provide answers as a JSON object with these keys:
{{
    "why_this_company": "Why do you want to work at {company}?",
    "why_this_role": "Why are you interested in this role?",
    "salary_expectations": "What are your salary expectations?",
    "notice_period": "What is your notice period / earliest start date?",
    "work_authorization": "Are you authorized to work in this location?",
    "relevant_experience": "Describe your most relevant experience for this role."
}}

Use ONLY facts from the profile. Salary expectation: {salary_min}–{salary_max} {currency}.
If info is missing, use [PLACEHOLDER: ...].

Respond with the JSON object only.
"""
