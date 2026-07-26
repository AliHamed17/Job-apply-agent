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
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/Mac
pip install -e ".[dev]"
```

### 2. Configure environment

```bash
copy .env.example .env
# Edit .env with your API keys
```

### 3. Edit your profile

```bash
copy user_profile.yaml.example user_profile.yaml
```

Edit the ignored `user_profile.yaml` with your real details. Personal profiles,
`cv_routing.yaml`, and the `cvs/` directory are local-only; commit only their
sanitized `.example` templates.

### 4. Run the server

```bash
uvicorn api.main:app --reload --port 8000
```

The database (SQLite) is created automatically on first run.

### 5. Start Celery workers (optional — for async processing)

```bash
celery -A worker.celery_app worker --loglevel=info
```

For a local qualification run without Redis, keep `DRY_RUN=true` and
`DRAFT_ONLY=true`, then start the fail-closed scheduler:

```bash
python -m scripts.run_safe_automation
```

It runs discovery immediately and then every `DISCOVERY_INTERVAL_H`. LinkedIn
challenges pause only LinkedIn; the rate-limited public remote-jobs fallback can
continue on its own six-hour cadence. The runner refuses placeholder profiles
and never enables final submission.

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
| Auto Prepare | `AUTO_APPLY=true` | **false** | Mark high-score jobs eligible for an explicit review batch |
| Dry Run | `DRY_RUN=true` | **true in `.env.example`** | Prevent every external submitter from running |

## Continuous preparation and controlled submission

The scheduler can discover, score, route a CV, and prepare every suitable job
without repeated instructions. A score never approves an employment
application. Review one application or select an exact batch in the dashboard;
the submission worker then handles the approved set.

### Enabling continuous preparation

Set these in `.env` (see `.env.example`):

```bash
AUTO_APPLY=true
MIN_APPLY_SCORE=40
TASKS_ALWAYS_EAGER=false
```

- `AUTO_APPLY=true` — computes eligibility for the review queue; it does not
  bypass approval.
- `MIN_APPLY_SCORE=40` — minimum score for batch-review eligibility.
- `TASKS_ALWAYS_EAGER=false` — uses Redis, Celery worker, and Celery Beat for
  scheduled processing.

Bring up the full stack (Postgres, Redis, web API, worker, and the scheduler) with:

```bash
docker-compose up -d
```

`celery-beat` is the scheduler that periodically triggers LinkedIn discovery (`DISCOVERY_INTERVAL_H`) and digest/outbound jobs; `celery-worker` consumes the `ingestion`, `processing`, `llm`, `submission`, and `discovery` queues.

### One-time browser sessions

LinkedIn discovery and Easy Apply drive a real, persistent browser profile rather than the LinkedIn API (there is no public API for this). Before enabling full-auto, log in once interactively so the session cookie is saved to `LINKEDIN_BROWSER_PROFILE_DIR` (default `.linkedin_profile/`):

```bash
python -m discovery.login
```

A browser window opens to the LinkedIn login page — complete login and any 2FA challenge manually. The script detects a successful login (redirect to `/feed`) and closes the window, leaving the authenticated profile on disk for the worker to reuse. Re-run this whenever the session expires or LinkedIn forces a re-login.

For Workday and employer portals, create a tenant-isolated session without
extracting browser passwords:

```bash
python -m scripts.portal_session_bootstrap "https://employer.wd5.myworkdayjobs.com/job/..."
```

See [Employer application automation](docs/employer-automation.md) for the
NVIDIA/Workday flow, exact batch approval, confirmed evidence, platform
coverage, audit history, and deployment constraints.

### Uploading your CV

Full-auto scoring and application generation are driven by your parsed CV/profile. Update it any time via either path:

- **Dashboard**: `POST /api/profile/resume` with a PDF file (multipart `file` field) — rebuilds your profile and re-scores pending jobs.
- **WhatsApp**: send the CV as a PDF document to the bot number — the webhook detects `document` messages with `mime_type: application/pdf`, rebuilds the profile, and re-scores the queue the same way.

Uploads are streamed with a configurable `MAX_RESUME_BYTES` limit (10 MB by
default), validated as readable non-encrypted PDFs, and parsed before the active
resume/profile is replaced. Docker stores these files under the shared
`profile-data/` runtime directory, which is intentionally ignored by Git.

### Governor caps and the kill switch

All LinkedIn actions go through `core/governor.py`'s `RateGovernor`, which enforces, independent of scoring:

- **Daily cap** — `LINKEDIN_DAILY_CAP` applications per day.
- **Active hours** — `ACTIVE_HOURS` (e.g. `09:00-21:00`); no actions outside this window.
- **Jittered gaps** — a random delay between `LINKEDIN_MIN_GAP_S` and `LINKEDIN_MAX_GAP_S` between actions, to avoid bot-like bursts.
- **Cooldown / circuit breaker** — a LinkedIn challenge (CAPTCHA/checkpoint) trips an exponentially increasing cooldown (up to 48h) before any further automated action.
- **Kill switch** — an operator override that halts all automated LinkedIn actions immediately, independent of the other checks.

Check status or flip the kill switch via the control API:

```bash
curl -X POST -H "Authorization: Bearer $SECRET_KEY" http://localhost:8000/api/control/kill     # stop everything now
curl -X POST -H "Authorization: Bearer $SECRET_KEY" http://localhost:8000/api/control/resume   # resume
curl -H "Authorization: Bearer $SECRET_KEY" http://localhost:8000/api/control/status            # current governor state
```

### ACCOUNT-RISK WARNING

> **Read before enabling FULL_AUTO.**
>
> - **LinkedIn**: automating actions against linkedin.com (including Easy Apply) violates LinkedIn's Terms of Service. LinkedIn actively detects and can permanently ban accounts for automated activity, regardless of rate limiting, jitter, or the governor's caps — those controls reduce detection risk and blast radius, they do not eliminate it. Use a LinkedIn account you are willing to lose, not your primary professional identity. You are solely responsible for compliance with LinkedIn's ToS in your jurisdiction.
> - **WhatsApp**: the CV-upload-by-WhatsApp path in this document refers to the official WhatsApp Cloud API webhook. If you are instead using an unofficial WhatsApp Web automation bridge (e.g. a `whatsapp-web.js`-style bridge, see `bridge/`) to send or receive messages, be aware this also violates WhatsApp's Terms of Service and can result in the connected phone number being banned. Prefer the official Cloud API webhook wherever possible; treat any unofficial bridge as higher-risk and disposable.
> - Start with `DRY_RUN=true` and/or `DRAFT_ONLY=true` and watch the dashboard before flipping on full auto-submit.

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | No | Health check |
| GET | `/metrics` | Bearer | Pipeline metrics |
| GET/POST | `/webhook/whatsapp` | Meta signature | WhatsApp webhook |
| GET | `/api/jobs` | Bearer | List jobs (filter by status, min_score) |
| GET | `/api/jobs/{id}` | Bearer | Get job details |
| GET | `/api/applications` | Bearer | List applications |
| GET | `/api/applications/{id}` | Bearer | Get application details |
| POST | `/api/applications/{id}/approve` | Bearer | Approve and queue for submission |
| POST | `/api/applications/{id}/reject` | Bearer | Reject application |
| GET | `/api/dashboard` | Bearer | Pipeline summary stats |
| GET | `/api/dashboard/insights` | Bearer | Actionable backlog, stale-work, bottleneck, and top-opportunity insights |
| POST | `/api/ingest` | Bearer | Manually ingest a URL |

**Auth**: Set `SECRET_KEY` in `.env`, then pass `Authorization: Bearer <your-secret-key>` header.

## Operator Insights

Use `GET /api/dashboard/insights` when you need an actionable operations view instead of only aggregate dashboard counts. The endpoint returns:

- queue depth by pipeline stage (`urls_pending`, `jobs_extracted`, `applications_draft`, submission states);
- stale work based on a configurable `stale_hours` query parameter;
- bottleneck recommendations with severity and remediation steps;
- top scored opportunities still awaiting action;
- recent job events within a configurable `window_days` window.

Example:

```bash
curl -H "Authorization: Bearer $SECRET_KEY" \
  "http://localhost:8000/api/dashboard/insights?window_days=14&stale_hours=12&limit=5"
```

## Running Tests

```bash
pytest tests/ -v
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
├── user_profile.yaml.example # Sanitized profile template
└── pyproject.toml          # Project metadata and dependencies
```

## Security Checklist

| Item | Status | Notes |
|------|--------|-------|
| No plaintext portal credentials | ✅ | Dedicated signed-in profiles; no Chrome/Edge password extraction |
| Webhook signature verification | ✅ | X-Hub-Signature-256 from Meta |
| API bearer token auth | ✅ | Middleware checks `SECRET_KEY` |
| Rate limiting | ✅ | Per-IP middleware, Celery rate limits |
| Allowed sender whitelist | ✅ | `ALLOWED_SENDERS` env var |
| DRAFT_ONLY default | ✅ | No external submission by default |
| Approval enforcement | ✅ | Score eligibility never replaces exact operator approval |
| Verified confirmation | ✅ | Clicks/timeouts never count as submitted |
| Unknown reconciliation | ✅ | Indeterminate outcomes cannot auto-retry |
| robots.txt compliance | ✅ | Checked before fetching pages |
| Polite crawling | ✅ | Configurable delay between fetches |
| No CAPTCHA bypass | ✅ | Detects and switches to draft-only |
| Correlation IDs in logs | ✅ | structlog with request tracing |
| PII in logs | ⚠️ | Avoid logging full message bodies in production |
| Data encryption at rest | ⚠️ | Use disk-level encryption for SQLite/Postgres |
| CORS restricted | ⚠️ | Currently localhost only; configure for production |

## Edge Cases Handled

- **URL shorteners**: Expanded via HEAD requests (bit.ly, t.co, tinyurl, etc.)
- **Duplicate reposts**: Triple dedup — URL hash, apply_url hash, job signature
- **Multiple locations**: Parsed from JSON-LD arrays
- **Bot protection**: Detected via heuristics; gracefully switches to manual/draft
- **CAPTCHAs**: Never bypassed; switches to draft-only mode
- **Non-English postings**: Passed through (LLM handles multilingual content)
- **Pages with no jobs**: Classified and skipped

## License

MIT
