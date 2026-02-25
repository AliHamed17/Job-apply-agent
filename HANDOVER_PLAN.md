# AI Job Apply Agent — Handover & Implementation Plan

## Project Overview
This is a production-grade, modular system designed to automate the job application pipeline while maintaining a **Human-in-the-Loop** approval process. It monitors WhatsApp for job links, extracts details, scores them against a user profile, generates custom application materials using LLMs, and handles submissions.

### Core Stack
- **Framework**: FastAPI (API & Webhooks)
- **Task Queue**: Celery + Redis (Asynchronous Pipeline)
- **Database**: SQLAlchemy + SQLite (MVP)
- **LLM**: Pluggable interface for OpenAI (GPT-4o) and Anthropic (Claude 3.5 Sonnet)
- **Parsing**: BeautifulSoup4 + LXML (JSON-LD, Schema.org, Platform-specific heuristics)
- **UI**: Vanilla JS + CSS (Modern, Dark Mode Dashboard)

---

## Current Status (MVP Completed)
The foundation is solid and verified with **76 unit tests (all passing)**.

### Implemented Modules:
1.  **Ingestion Layer**: WhatsApp Cloud API webhook handler with interactive button routing (Approve/Skip/Edit).
2.  **Job Discovery**: 
    - URL expansion (Shorteners) and normalization.
    - Parsers for **Greenhouse**, **Lever**, **JSON-LD (Schema.org)**, and a **General HTML Heuristic** fallback.
3.  **Scoring Engine**: Weighted multi-factor matching (Title, Keywords, Location/Remote, Seniority, Employment Type).
4.  **LLM Generation**: Generates Cover Letters, Recruiter Messages, and Q&A answers with strict guardrails (no fabrication, placeholder detection).
5.  **Submission System**: 
    - **Greenhouse Harvest API** (Candidate creation).
    - **Lever Postings API** (Form data submission).
    - **DraftOnly Submitter** (Fallback for manual application or unsupported boards).
6.  **Worker Pipeline**: 5-stage Celery task chain with **Approval Enforcement** (won't submit unless status is `APPROVED`).
7.  **Dashboard UI**: A sleek, dark-themed management portal to review pending applications, edit cover letters, and track pipeline stats.

---

## Implementation Plan for Next Phases

### Phase 8: Containerization & Deployment
**Goal**: Make the system "one-click" deployable for testers.
1.  **Dockerfile**: Create multi-stage builds for the API and Celery workers.
2.  **Docker Compose**: Orchestrate `web-api`, `celery-worker`, `redis`, and `prometheus/grafana` for monitoring.
3.  **Reverse Proxy**: Setup Nginx or Caddy with SSL for the webhook endpoint.

### Phase 9: Scaling & Platform Expansion
**Goal**: Support more job boards and a larger volume of messages.
1.  **Workday Parser/Submitter**: Investigate Workday's candidate APIs (requires cautious handling due to authentication variability).
2.  **PostgreSQL Migration**: Switch from SQLite to Postgres for better concurrency and PII data encryption at rest.
3.  **PDF Parsing**: Allow the `LOADER` to read actual PDF resumes instead of just `user_profile.yaml` text.

### Phase 10: AI & Guardrail Enhancements
1.  **Vision Integration**: Use GPT-4o-vision or Claude 3.5 Sonnet to "look" at job pages that are heavily obfuscated or use canvas-based layouts.
2.  **Adversarial Testing**: Build a test suite of "fake" job posts to see if the LLM correctly identifies missing info vs. fabricating it.
3.  **Feedback Loop**: Implement a system where the user can "fix" a cover letter in the dashboard, and the corrected version is used to fine-tune future prompts via few-shot learning.

---

## Developer Instructions for Claude

### Coding Standards
- **Aesthetics First**: Every UI change must feel premium. Dynamic animations, glassmorphism, and Outfit/Inter typography are required.
- **Strict Typing**: Use Pydantic models for all data exchange. No raw dictionaries for core logic.
- **Safety**: Never bypass CAPTCHAs. If a bot check is detected, the submitter MUST fallback to `DRAFT_ONLY`.
- **Modularity**: New parsers go in `jobs/parsers/`, new submitters in `submitters/`. Follow the existing abstract base class patterns.

### Reviewer Expectations (Antigravity Role)
As the reviewer, I will be checking for:
1.  **Security**: Meta signature verification on webhooks and Bearer token auth on APIs.
2.  **Compliance**: Adherence to `robots.txt` and polite crawling delays.
3.  **Resilience**: Proper error handling in the `submit_application_task` to prevent double-submissions or silent failures.
4.  **Test Coverage**: Every new feature requires corresponding unit tests in the `tests/` directory.

---

## Getting Started
1.  **Environment**: Ensure `.env` is configured with all necessary API keys.
2.  **Redis**: Ensure Redis is running (local or docker).
3.  **Workers**: `celery -A worker.celery_app worker --loglevel=info`
4.  **API**: `uvicorn api.main:app --reload`
5.  **Verify**: Run `pytest tests/` before making any changes.
