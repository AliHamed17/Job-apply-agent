"""Prompt templates for LLM-based application generation."""

# ── System Prompt ────────────────────────────────────────
SYSTEM_PROMPT = """\
You are a professional job application assistant. You help craft tailored, \
honest job applications based ONLY on the candidate's actual profile and resume.

CRITICAL RULES:
1. NEVER invent degrees, certifications, or work experience not in the profile.
2. NEVER fabricate company names, project names, or technologies not mentioned.
3. If information is missing, insert [PLACEHOLDER: describe what's needed].
4. Be professional, concise, and genuine.
5. Highlight relevant skills and experience that genuinely match the job.
6. Use the candidate's specified cover letter style preference.
"""

# ── Cover Letter Prompt ──────────────────────────────────
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

# ── Recruiter Message Prompt ─────────────────────────────
RECRUITER_MESSAGE_PROMPT = """\
Write a short, friendly recruiter message (2-3 sentences) expressing interest \
in the following position.

Job: {job_title} at {company}
Candidate: {name}
Key skills: {key_skills}

Keep it brief and professional — this is for a cold outreach or LinkedIn message.
"""

# ── Q&A Answers Prompt ───────────────────────────────────
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


# ── Interview Prep Prompt ─────────────────────────────────
INTERVIEW_PREP_PROMPT = """\
Create a concise interview prep brief for this specific application.

## Job Details
- Title: {job_title}
- Company: {company}
- Location: {location}
- Description: {description}
- Requirements: {requirements}

## Candidate Profile
- Name: {name}
- Location: {user_location}
- Work Authorization: {work_authorization}
- Resume: {resume_text}

## Output Format
Return plain text with these sections in order:
1) Role Snapshot (5 bullets)
2) Likely Technical Questions (8 bullets with short prep hints)
3) Behavioral Stories to Prepare (5 bullets mapped to resume evidence)
4) Company-Fit Talking Points (5 bullets)
5) 30-60-90 Day Value Plan (3 bullets)

Rules:
- Use only information present in job/profile context.
- If a critical detail is missing, use [PLACEHOLDER: ...].
- Keep it practical and interview-focused.
"""
