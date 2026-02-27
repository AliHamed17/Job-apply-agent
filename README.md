# AI Job Apply Agent

An AI-powered system that monitors WhatsApp for job links, extracts job postings, scores them against your profile, and generates tailored application materials with human approval.

## Architecture

```
WhatsApp Cloud API (user forwards job links → business number)
        │
        ▼
┌─── FastAPI (api/main.py) ──────────────────────────────────┐
│  POST /webhook/whatsapp   ← ingestion + interactive actions │
│  GET  /api/jobs           ← list extracted jobs              │
│  GET  /api/applications   ← approval dashboard              │
│  POST /api/applications/{id}/approve                         │
│  POST /api/applications/{id}/reject                          │
│  POST /api/ingest         ← manual URL ingestion             │
│  GET  /api/dashboard      ← pipeline summary stats           │
│  GET  /health | /metrics                                     │
└────────┬───────────────────────────────────────────────────┘
         │ enqueue
         ▼
┌─── Celery Workers ────────────────────────────────────────┐
│  1. process_message   → extract URLs                       │
│  2. process_url       → fetch + parse (JSON-LD/HTML)       │
│  3. score_job         → score vs profile → skip/draft      │
│  4. generate_app      → LLM cover letter + Q&A             │
│  5. submit_app        → submit (if approved) or draft-only │
└────┬──────────┬────────────┬──────────────────────────────┘
     ▼          ▼            ▼
  SQLite     Redis       LLM (OpenAI / Claude)
```

## WhatsApp Compliance

> **Important**: The official WhatsApp Cloud API cannot read messages from arbitrary groups. This system uses the **forward-to-bot** pattern:
>
> 1. You register a WhatsApp Business number
> 2. Users forward job links to that number in a 1:1 chat
> 3. The webhook receives forwarded messages and extracts URLs
> 4. Results and approval buttons are sent back via WhatsApp

## Quick Start

### Prerequisites

- Python 3.11+
- Redis (for Celery task queue)
- A WhatsApp Business Account (optional — you can use manual ingestion)
- An OpenAI or Anthropic API key (for LLM generation)

### 1. Clone and install

```bash
git clone https://github.com/AliHamed17/Job-apply-agent.git
cd Job-apply-agent
python -m venv .venv
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
# Linux/Mac
# source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. Configure environment

```bash
# Linux/Mac
cp .env.example .env
# Windows (cmd)
# copy .env.example .env
# Edit .env with your API keys
```

### 3. Edit your profile

Edit `user_profile.yaml` with your real details (name, resume, preferences).

### 4. Run the server

```bash
uvicorn api.main:app --reload --port 8000
```

The database (SQLite) is created automatically on first run.

### 5. Start Celery workers (optional — for async processing)

```bash
celery -A worker.celery_app worker --loglevel=info
```

### 6. Test with manual ingestion

```bash
curl -X POST http://localhost:8000/api/ingest \
  -H "Content-Type: application/json" \
  -d '{"url": "https://boards.greenhouse.io/example/jobs/12345"}'
```

### 7. WhatsApp webhook setup (production)

1. Create a Meta Developer Account and WhatsApp Business App
2. Get your Phone Number ID, API Token, and App Secret
3. Add them to `.env`
4. Run `ngrok http 8000` to get a public HTTPS URL
5. In Meta Developer Console → WhatsApp → Configuration:
   - Set webhook URL: `https://your-ngrok.ngrok.io/webhook/whatsapp`
   - Set verify token: your `WHATSAPP_VERIFY_TOKEN` value
   - Subscribe to `messages` webhook field

## Application Modes

| Mode | Env Var | Default | Behavior |
|------|---------|---------|----------|
| Draft Only | `DRAFT_ONLY=true` | **true** | Generate applications but never auto-submit |
| Auto Apply | `AUTO_APPLY=true` | **false** | Auto-submit jobs that meet `AUTO_APPLY_THRESHOLD` (only if DRAFT_ONLY=false) |
| Auto Apply Threshold | `AUTO_APPLY_THRESHOLD=80.0` | **80.0** | Minimum score required for auto-apply when enabled |

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | No | Health check |
| GET | `/metrics` | Bearer | Pipeline metrics |
| GET/POST | `/webhook/whatsapp` | Meta signature | WhatsApp webhook |
| GET | `/api/jobs` | Bearer | List jobs (filters: status, min_score, platform, has_application, date range; sorting supported) |
| GET | `/api/jobs/{id}` | Bearer | Get job details |
| GET | `/api/applications` | Bearer | List applications |
| GET | `/api/applications/{id}` | Bearer | Get application details |
| POST | `/api/applications/{id}/approve` | Bearer | Approve and queue for submission |
| POST | `/api/applications/{id}/reject` | Bearer | Reject application |
| POST | `/api/applications/{id}/retry-submit` | Bearer | Retry submission for an approved application |
| POST | `/api/applications/{id}/interview-prep` | Bearer | Generate a tailored interview prep brief for the application |
| GET | `/api/submissions` | Bearer | List submission queue entries (status/error/platform) |
| GET | `/api/dashboard` | Bearer | Pipeline summary stats |
| POST | `/api/ingest` | Bearer | Manually ingest a URL |

**Auth**: Set `SECRET_KEY` in `.env`, then pass `Authorization: Bearer <your-secret-key>` header.

For local-only development, `ALLOW_INSECURE_AUTH_BYPASS=true` can temporarily allow requests without a token when `SECRET_KEY=change-me` and `APP_ENV!=prod`.

Set `TRUSTED_HOSTS` to the hostnames allowed to serve this API (comma-separated). In production, do not use `*`.

## Running Tests

```bash
pytest tests/ -v
```

For a quick local smoke check before opening a PR, run:

```bash
pytest -q
```

## Project Structure

```
job-agent/
├── api/                    # FastAPI application
│   ├── main.py             # App with auth, rate limit, CORS middleware
│   └── routes/             # Webhook, jobs, applications, dashboard
├── core/                   # Configuration and logging
│   ├── config.py           # Pydantic settings from env vars
│   └── logging.py          # structlog setup with correlation IDs
├── db/                     # Database layer
│   ├── models.py           # SQLAlchemy ORM (Message, URL, Job, Application, Submission)
│   └── session.py          # Engine + session factory
├── ingestion/              # WhatsApp ingestion
│   ├── whatsapp_webhook.py # (legacy, replaced by api/routes/webhook.py)
│   └── url_utils.py        # URL normalize, hash, expand, dedup
├── jobs/                   # Job extraction
│   ├── fetcher.py          # HTTP fetch with retries, robots.txt, caching
│   ├── extractor.py        # Parser orchestrator
│   ├── models.py           # JobData Pydantic model
│   └── parsers/            # JSON-LD, HTML heuristic, Greenhouse, Lever
├── llm/                    # LLM integration
│   ├── client.py           # Pluggable interface (OpenAI / Anthropic)
│   ├── generation.py       # Cover letter, recruiter msg, Q&A generation
│   └── prompts.py          # Prompt templates with guardrails
├── match/                  # Job scoring
│   └── scoring.py          # Weighted scoring + action decision
├── profile/                # User profile
│   ├── models.py           # UserProfile Pydantic model
│   └── loader.py           # YAML loader with validation
├── submitters/             # Job board integrations
│   ├── base.py             # Abstract interface + DraftOnly + Registry
│   ├── greenhouse.py       # Greenhouse Harvest API
│   └── lever.py            # Lever Postings API
├── worker/                 # Async task pipeline
│   ├── celery_app.py       # Celery configuration
│   └── tasks.py            # 5-stage pipeline with approval enforcement
├── tests/                  # Unit tests
├── .env.example            # Environment variables template
├── user_profile.yaml       # User profile template
└── pyproject.toml          # Project metadata and dependencies
```

## Security Checklist

| Item | Status | Notes |
|------|--------|-------|
| No plaintext credentials | ✅ | All secrets via env vars / `.env` |
| Webhook signature verification | ✅ | X-Hub-Signature-256 from Meta |
| API bearer token auth | ✅ | Middleware checks `SECRET_KEY` |
| Rate limiting | ✅ | Per-IP middleware, Celery rate limits |
| Allowed sender whitelist | ✅ | `ALLOWED_SENDERS` env var |
| DRAFT_ONLY default | ✅ | No auto-submission without explicit opt-in |
| Approval enforcement | ✅ | Submit task validates `status == APPROVED` |
| robots.txt compliance | ✅ | Checked before fetching pages |
| Polite crawling | ✅ | Configurable delay between fetches |
| No CAPTCHA bypass | ✅ | Detects and switches to draft-only |
| Correlation IDs in logs | ✅ | structlog with request tracing |
| PII in logs | ⚠️ | Avoid logging full message bodies in production |
| Data encryption at rest | ⚠️ | Use disk-level encryption for SQLite/Postgres |
| CORS restricted | ⚠️ | Currently localhost only; configure for production |
| Host header allowlist | ✅ | Enforced via `TRUSTED_HOSTS` middleware |

## Edge Cases Handled

- **URL shorteners**: Expanded via HEAD requests (bit.ly, t.co, tinyurl, etc.)
- **Duplicate reposts**: Triple dedup — URL hash, apply_url hash, job signature
- **Multiple locations**: Parsed from JSON-LD arrays
- **Bot protection**: Detected via heuristics; gracefully switches to manual/draft
- **CAPTCHAs**: Never bypassed; switches to draft-only mode
- **Non-English postings**: Passed through (LLM handles multilingual content)
- **Pages with no jobs**: Classified and skipped

## Current Foundation and Missing Capabilities

### What you already have (solid foundation)

- URL/job/application/submission pipeline with operational APIs and dashboard controls (approve/retry/interview-prep/manual ingest).
- Safety-oriented submission behavior (including no CAPTCHA bypass policy and human-confirmation states).
- Auth-wall and bot-protection detection in fetching, with blocked-state handling.
- A single-profile flow and one-tenant auth model (`Bearer` secret + `user_profile.yaml`).

### What you’re still missing (highest-value items)

1. **Proactive top-of-funnel sourcing (priority #1)**
   - Ingestion is still mostly reactive (“link comes in → process”).
   - There is no native scheduled sourcing pipeline that runs daily role/location searches from configured search profiles.
   - Why highest ROI: more qualified opportunities per day generally improves outcomes more than downstream micro-optimizations.

2. **Outcome tracking after submit (priority #2)**
   - Current submission tracking captures submission state and confirmation metadata, but not full recruiter response lifecycle events.
   - Missing inbox-driven updates like `rejected`, `assessment_received`, `interview_scheduled`, etc.
   - Why it matters: without post-submit outcomes, funnel quality and follow-up automation remain limited.

3. **Dynamic ATS-targeted resume generation (priority #3)**
   - The pipeline generates tailored text artifacts (cover letter/recruiter/Q&A/interview prep), but resume generation is still external/static.
   - Missing per-job resume artifact generation + persistence in the application workflow.
   - Why it matters: resume relevance often drives response rate more than cover-letter polish alone.

4. **Multi-user / multi-tenant architecture (priority #4)**
   - Current architecture is single-user by design (global profile path + shared bearer token auth).
   - Missing tenant/user-scoped profiles, resume storage, and session/account boundaries.
   - Why it matters: required for SaaS-readiness and secure multi-user isolation.

5. **CAPTCHA-solving integration (intentional policy gap)**
   - The current design intentionally avoids bypassing CAPTCHA and safely falls back to draft/manual flow.
   - Recommendation: if ever introduced, keep this as an explicit opt-in enterprise module with legal/compliance review and auditable controls.

### Recommended build order (pragmatic)

1. Automated sourcing scheduler + source adapters (job boards/search APIs + dedup + quality scoring)
2. Inbox outcome ingestion (Gmail/IMAP webhook/polling + parser + status timeline)
3. Per-job resume tailoring service (template + ATS keyword control + PDF output + artifact storage)
4. Tenant model/auth overhaul (users, orgs, profile versions, storage isolation)
5. Optional advanced automation modules (CAPTCHA vendor integrations behind feature flags + audit logs)

### Short answer: what we miss most

If there is one missing direction to prioritize, it is **autonomous job discovery**. It unlocks the rest of the funnel by feeding fresh opportunities daily with near-zero manual effort.

## License

MIT


## Operations Runbook

### Redis behavior (required vs optional)
- API and workers run without Redis, but fall back to local/in-memory behavior.
- In multi-instance deployments, Redis is strongly recommended for shared rate limits/metrics and consistent operator views.

### Browser fallback semantics
- `requires_human_confirmation` means the automation filled the form but did not finalize submit.
- Treat this as an operational queue state requiring manual confirmation.

### Submission retry workflow
- Use `POST /api/applications/{id}/retry-submit` for retry-eligible submission states.
- Use `force=true` only for operator override workflows (cooldown protected + audit log event emitted).

### Suggested alerts
- High rate of `rate_limited` events.
- Rising `fetch_blocked_redirect_target` / auth-wall fetch errors.
- Growing `needs_human_confirmation` submission states without follow-up completion.
