# Full-Auto Job Apply Agent v2 — Design Spec

**Date:** 2026-07-20
**Status:** Approved by user (design review 2026-07-20)
**Base:** Job-apply-agent (FastAPI + Celery + SQLite pipeline, 76 passing tests)

## 1. Goal

Turn the existing human-in-the-loop job agent into a **zero-touch, full-auto** applier that:

1. **Discovers** jobs itself by searching LinkedIn with queries derived from the user's uploaded CV, *and* keeps ingesting jobs from WhatsApp groups (links **and** text-only posts).
2. **Builds the user profile automatically** from an uploaded CV PDF (dashboard drag-drop or WhatsApp document message) — no manual `user_profile.yaml` editing.
3. **Applies to every plausible match without user interaction**, best matches first, within a hard account-safety budget.
4. **Responds to text-only WhatsApp job posts** by DM-ing the recruiter contact the CV plus a tailored intro (or emailing when only an email is given).

### User decisions (locked)

| Decision | Choice |
|---|---|
| Discovery | Active LinkedIn search **+** WhatsApp ingestion |
| LinkedIn risk budget | Moderate: ~40–50 applications/day, human-like pacing |
| CV upload channel | Both dashboard and WhatsApp |
| Text-only WhatsApp posts | Full-auto send CV + intro to extracted contact |
| Architecture | Approach A — extend the existing pipeline (no sidecar, no full vision agent) |

### Non-goals

- No CAPTCHA solving or bot-detection bypass, ever. Challenge ⇒ pause + alert (existing hard rule, kept).
- No applying through login-walled ATSs that require account creation (Workday stays draft-only).
- No LLM computer-use for whole-page driving; vision is a narrow per-field fallback only.

### Known risks (accepted by user)

- Automated applying violates LinkedIn ToS; mitigation is the rate governor (caps, jitter, active hours, circuit breaker), not evasion.
- Unofficial WhatsApp automation (whatsapp-web.js bridge) can get a number banned; mitigated by a 15/day outbound cap and per-contact dedup. Recommendation recorded: run the bridge on a secondary number.

## 2. Architecture

```
                    CV upload (dashboard / WhatsApp PDF)
                              │
                              ▼
                 profile/builder.py ──► user_profile.yaml (generated + versioned)
                              │
        ┌─────────────────────┴──────────────────────┐
        ▼                                            ▼
┌─ DISCOVERY (new) ─────────────┐   ┌─ INGESTION (exists, extended) ─────────┐
│ discovery/linkedin_search.py  │   │ bridge (groups) + Cloud API webhook    │
│ Celery beat, persistent       │   │ URL posts → ExtractedURL pipeline      │
│ browser session, CV-driven    │   │ TEXT posts → text_post_parser (new)    │
│ queries, emits parsed Jobs    │   └──────────────┬─────────────────────────┘
└──────────────┬────────────────┘                  │
               ▼                                   ▼
        score_job (exists) ──► priority queue (score desc, expiring)
               ▼
        generate_application (exists)
               ▼
┌─ RATE GOVERNOR (new, core/governor.py) — shared budget for ALL LinkedIn actions ─┐
│ cap 45/day · 120–360 s jittered gaps · active hours · challenge ⇒ cooldown+alert │
└──────────────┬───────────────────────────────────────────────────────────────────┘
               ▼
  submit: Easy Apply v2 (new) │ board APIs (exist) │ WhatsApp DM+CV (new) │ email+CV (new)
               ▼
  Submission log ──► WhatsApp daily digest + dashboard (budget gauge, kill switch)
```

All new capability plugs into the existing five-stage Celery pipeline; the DB, dedup, scoring, generation, and approval-enforcement layers are reused as-is.

## 3. Components

### 3.1 LinkedIn Discovery — `discovery/`

- **Session management:** persistent Chromium profile (`user-data-dir`), replacing per-run cookie injection. One-time `python -m discovery.login` opens a headed browser for manual login (handles 2FA); the session persists across runs.
- **Query generation:** built from the generated profile — `preferences.roles × preferences.locations`, Easy Apply filter (`f_AL=true`), posted within 24 h, newest first. Configurable pages per query.
- **In-browser extraction:** title / company / location / description scraped from the search results detail pane; inserts fully-parsed `Job` rows directly (`discovery_source="linkedin_search"`). Discovery **bypasses** the HTTP fetcher — LinkedIn blocks plain HTTP, and the existing pipeline would mark those URLs BLOCKED.
- **Dedup:** existing job-signature + apply-url-hash dedup applies unchanged.
- **Scheduling:** Celery beat every `DISCOVERY_INTERVAL_H` (default 3 h) inside active hours. Discovery page-loads consume governor budget at a fractional weight (search is cheaper than apply).

### 3.2 CV → Profile Builder — `profile/builder.py`

- Uses existing `pdf_loader.extract_text_from_pdf`, then LLM structured extraction into the existing `UserProfile` Pydantic model: personal details, links, skills, target roles (inferred from title trajectory), seniority, locations, per-skill years of experience, work-authorization defaults.
- Writes `user_profile.yaml` with a timestamped backup of the prior version; records a `ProfileVersion` row; hot-reloads the profile cache; the uploaded PDF becomes `resume.pdf_path` for all future submissions.
- **Upload paths:**
  - `POST /api/profile/resume` — multipart upload; new dashboard page with drag-drop.
  - WhatsApp: bridge forwards PDF document messages from the user's own chat; Cloud API webhook downloads media messages via the Meta media API. Both post into the same endpoint.
- After a rebuild, all QUEUED/DRAFT jobs are re-scored against the new profile.

### 3.3 Smart Easy Apply Form Filler — `submitters/form_brain.py` + Easy Apply v2

- **Three-layer answer resolution:**
  1. Deterministic map — name, email, phone, city, LinkedIn/portfolio URLs (from profile).
  2. `AnswerCache` lookup — normalized-question hash; LinkedIn recycles screening questions heavily, so this converges within days and eliminates most LLM calls.
  3. LLM answer from CV + job context with guardrails: numeric answers derived from the CV timeline; never invents certifications, visas, or clearances; result cached.
- **Generic step walker** replaces the fixed-selector flow in `submitters/linkedin.py`: each modal step is enumerated as labeled fields (input / textarea / select / radio / checkbox / fieldset / file), each resolved via form_brain. Selector fallback chains centralized in `submitters/selectors.py`. A vision-model call classifies any field the DOM walker cannot (narrow fallback only).
- **Abort-don't-lie rule:** a *required* field form_brain cannot answer confidently ⇒ use LinkedIn's discard flow, mark the application `NEEDS_REVIEW` with the blocking question text, continue with the next job. Never submit fabricated answers.
- **Verified submission:** success is recorded only when the post-submit confirmation dialog is detected; otherwise the attempt is `FAILED` (no phantom "applied" states).
- Resume upload uses the current profile's PDF; "follow company" checkbox is unchecked by default.

### 3.4 Rate Governor — `core/governor.py`

- Redis-backed shared budget across discovery and apply actions:
  - Hard cap `LINKEDIN_DAILY_CAP` (default **45**) applications/day.
  - Jittered gap between LinkedIn actions: uniform random `LINKEDIN_MIN_GAP_S`–`LINKEDIN_MAX_GAP_S` (defaults 120–360 s).
  - Active-hours window `ACTIVE_HOURS` (default 09:00–21:00 local).
  - Randomized dwell/scroll on job pages before acting.
- **Challenge circuit-breaker:** CAPTCHA / checkpoint / HTTP 999 detected anywhere ⇒ pause all LinkedIn automation for a cooldown (6 h, doubling per repeat within 7 days), send an immediate WhatsApp alert. Never solve, never bypass.
- **Priority drain:** the apply queue is consumed highest-score-first; jobs below `MIN_APPLY_SCORE` are skipped; queued jobs expire after `QUEUE_TTL_DAYS` (default 7).
- Dashboard: live budget gauge + global **kill switch** (pauses discovery, applying, and outbound).

### 3.5 WhatsApp Outbound Applier

- **Bridge upgrade (read → read/write):** the bridge exposes a small local HTTP endpoint (localhost-only, token-authed with `JOB_AGENT_TOKEN`) that the agent calls to send a message + document to a phone number. Bridge also forwards **text-only** group messages that pass a keyword prefilter (`hiring|vacancy|send cv|looking for|we are recruiting|…`).
- **Text-post parsing — `ingestion/text_post_parser.py`:** LLM classify + extract `{is_job, title, company, description, contact_phone, contact_email}`. Non-jobs dropped; jobs enter the normal scoring pipeline with `discovery_source="whatsapp_text"`.
- **Sending:** score ≥ `MIN_APPLY_SCORE` ⇒ tailored intro (existing recruiter-message generator) + CV PDF as a **DM to the extracted contact only** — never posted to the group. Email-only contacts ⇒ SMTP send (new `SMTP_*` config) with the same message + CV attached.
- **Safety budget:** `WA_OUTBOUND_DAILY_CAP` (default 15) DMs/day; `OutboundContact` dedup — never re-message the same normalized phone/email within 30 days.

### 3.6 Full-auto policy

- Documented **FULL_AUTO preset**: `DRAFT_ONLY=false`, `AUTO_APPLY=true`, `MIN_APPLY_SCORE=40` (the old `AUTO_APPLY_THRESHOLD=80` gate is removed; jobs at or above `MIN_APPLY_SCORE` are eligible, and the queue drains highest-score-first), `TASKS_ALWAYS_EAGER=false` (real Celery + beat via existing docker-compose).
- `match/scoring.decide_action` gains the `MIN_APPLY_SCORE` policy; skip threshold unchanged.

## 4. Data model changes (Alembic migrations)

- `Job`: `discovery_source` (whatsapp_url | whatsapp_text | linkedin_search | manual), `easy_apply` (bool), `expires_at` (datetime).
- `Application`: `submission_channel` (linkedin_easy | board_api | whatsapp_dm | email), `needs_review_reason` (text).
- New `AnswerCache`: `question_hash` (unique), `question_text`, `answer`, `source` (deterministic | cache | llm), `created_at`.
- New `OutboundContact`: `contact_hash` (unique, normalized phone/email), `last_contacted_at`, `job_id`.
- New `ProfileVersion`: `id`, `source_filename`, `yaml_snapshot`, `created_at`.

## 5. Configuration additions

```
LINKEDIN_DAILY_CAP=45          LINKEDIN_MIN_GAP_S=120       LINKEDIN_MAX_GAP_S=360
ACTIVE_HOURS=09:00-21:00       DISCOVERY_INTERVAL_H=3       DISCOVERY_PAGES_PER_QUERY=3
MIN_APPLY_SCORE=40             QUEUE_TTL_DAYS=7
WA_OUTBOUND_DAILY_CAP=15       WA_CONTACT_DEDUP_DAYS=30
SMTP_HOST= SMTP_PORT= SMTP_USER= SMTP_PASSWORD= SMTP_FROM=
LINKEDIN_BROWSER_PROFILE_DIR=.linkedin_profile
DRY_RUN=false                  # walk Easy Apply fully but discard before final submit
```

## 6. Error handling

- **Transient** (network/timeout): 2 retries with backoff — existing Celery pattern, kept.
- **Permanent form failure / unanswerable required question:** `NEEDS_REVIEW` with reason; never silent, never guessed.
- **Challenge pages:** circuit-breaker (§3.4) — pause + WhatsApp alert.
- **Poison jobs:** parked after 2 submit attempts.
- **Daily WhatsApp digest:** applied / needs-review / failed counts with dashboard links.
- Submission success requires positive confirmation (dialog detected / API 2xx with id); anything else is FAILED.

## 7. Testing

- **Unit:** form_brain resolution ladder (mock LLM); governor budget math, active-hours, cooldown doubling; text-post parser against a fixture corpus of real-style Gulf job posts (Arabic + English); profile builder against sample CVs including `Ali_Hamed_Cv.pdf`; discovery HTML parsing from recorded fixtures.
- **Playwright offline:** Easy Apply step walker runs against saved HTML snapshots of the modal — no live LinkedIn in CI.
- **Manual smoke:** `DRY_RUN=true` walks a real Easy Apply form end-to-end and discards at the final step.
- Existing 76 tests must keep passing.

## 8. Dashboard additions

Resume upload page · discovery status panel (last run, jobs found) · rate-budget gauge · outbound DM/email log · NEEDS_REVIEW queue with the blocking question shown · global kill switch.

## 9. Delivery phases (implementation-plan input)

1. **P1 — CV intelligence:** profile builder + upload endpoints + dashboard page + WhatsApp media path. (Everything downstream depends on the generated profile.)
2. **P2 — Rate governor + FULL_AUTO policy:** governor, config, decide_action change, kill switch. (Must exist before any discovery/apply automation runs.)
3. **P3 — Easy Apply v2:** form_brain, AnswerCache, step walker, selectors module, DRY_RUN, abort-don't-lie.
4. **P4 — LinkedIn discovery:** login command, persistent session, query builder, in-browser extraction, beat schedule.
5. **P5 — WhatsApp outbound:** bridge send endpoint + text forwarding, text_post_parser, SMTP sender, outbound caps/dedup.
6. **P6 — Observability & polish:** daily digest, dashboard panels, docs, docker-compose beat service.
