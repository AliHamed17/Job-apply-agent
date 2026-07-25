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

## Key Projects & Impact Metrics
{project_spotlights}

## Style Preference
{cover_letter_style}

Write the cover letter now. Address it to the hiring team at {company}.
Highlight specific relevant engineering accomplishments and metrics naturally.
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

Use ONLY facts from the profile. {salary_guidance}
If info is missing, use [PLACEHOLDER: ...].

Respond with the JSON object only.
"""

# Salary guidance is built rather than interpolated raw, because an unset
# range (min=max=0, the default in profile/models.py) used to render as the
# literal "Salary expectation: 0–0 ILS" and the model dutifully answered
# "0-0 ILS" on real applications.
SALARY_UNSET_GUIDANCE = (
    "The candidate has NOT specified a salary range. For "
    '"salary_expectations", do not state any number: say the expectation is '
    "open and best discussed once the role's scope is clear, and that the "
    "candidate is happy to align with the band for the position."
)

CV_ALIGNMENT_PROMPT = """\
## Job Details
- Title: {job_title}
- Seniority: {seniority}
- Description: {job_description}

## Selected CV ({cv_id})
{cv_text}

## All Available CV Options
{available_cvs_info}

## Task
Evaluate if the selected CV ({cv_id}) is a strong match for this job, or if
another CV option would be significantly better aligned.

Respond ONLY with a JSON object in this format:
{{
    "is_good_match": true/false,
    "alignment_score": float between 0.0 and 1.0,
    "reasoning": "Concise summary explaining why this CV matches or doesn't match",
    "suggested_cv_id": "cv-id-string" (or null if the selected CV is best)
}}
"""

# ── CV Routing Prompt ─────────────────────────────────────────────────────
CV_ROUTING_PROMPT = """\
Select the single best candidate CV for this job using only the supplied CV
excerpts. Prefer a strong match to the actual responsibilities and required
skills, not a generic CV. If the excerpts do not support a reliable choice,
return null for selected_cv_id.

## Job
- Title: {job_title}
- Seniority: {seniority}
- Description: {job_description}

## Candidate CV Options
{cv_options}

Respond ONLY with a JSON object in this format:
{{
    "selected_cv_id": "configured-cv-id" or null,
    "confidence": float between 0.0 and 1.0,
    "reasoning": "One concise evidence-based sentence"
}}
"""

def build_salary_guidance(salary_min: int, salary_max: int, currency: str) -> str:
    """Describe the salary expectation, or say it is unset — never '0–0'."""
    if not salary_min and not salary_max:
        return SALARY_UNSET_GUIDANCE
    if salary_min and salary_max:
        return f"Salary expectation: {salary_min}–{salary_max} {currency}."
    single = salary_min or salary_max
    qualifier = "from" if salary_min else "up to"
    return f"Salary expectation: {qualifier} {single} {currency}."
