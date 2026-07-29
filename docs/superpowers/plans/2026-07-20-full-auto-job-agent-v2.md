# Full-Auto Job Apply Agent v2 — Implementation Plan

> **Historical and superseded.** This plan is retained for provenance only.
> It is not a current runbook, acceptance result, or authorization to send an
> application. The current architecture keeps private content and Ollama on the
> local runner, requires an explicit **Send application** action, and has no
> unattended final submission. The first-five ATS implementations have 87
> sanitized fixtures but zero real-URL dry runs, zero live canaries, zero
> qualified form scopes, and zero final executors. Do not execute the old
> zero-touch, WhatsApp-outbound, or live-submission steps without a new reviewed
> specification. See
> [`docs/qualification/README.md`](../../qualification/README.md).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the human-in-the-loop Job-apply-agent into a zero-touch full-auto applier that discovers LinkedIn jobs from an uploaded CV, applies via a rate-governed smart Easy Apply flow, ingests WhatsApp job links + text posts, and auto-sends the CV to WhatsApp/email recruiter contacts.

**Architecture:** Extend the existing FastAPI + Celery + SQLAlchemy pipeline (Approach A). New modules plug into the five existing Celery stages. A shared Redis-backed rate governor throttles all LinkedIn actions. An LLM-backed profile builder generates `user_profile.yaml` from a CV PDF. A three-layer form-answer resolver ("form_brain") drives a generic Easy Apply step walker. Discovery and WhatsApp outbound are new task chains that feed the same scoring/generation stages.

**Tech Stack:** Python 3.11, FastAPI, Celery + Redis, SQLAlchemy + Alembic, Pydantic v2 / pydantic-settings, Playwright (async, Chromium), structlog, pytest. Bridge: Node.js + whatsapp-web.js. LLM: pluggable OpenAI/Anthropic via `llm.client`.

## Global Constraints

- Python ≥ 3.11; use `from __future__ import annotations` in every new module (matches existing files).
- All data exchange uses Pydantic models or dataclasses — no raw dicts for core logic (existing standard).
- **Never bypass or solve CAPTCHA/bot-detection.** On any challenge page, switch to draft/pause + alert. This is a hard rule.
- New parsers → `jobs/parsers/`; new submitters → `submitters/`; follow existing abstract base-class patterns.
- Every new feature ships with pytest unit tests in `tests/`. Existing 76 tests must keep passing.
- Playwright tests run against **saved HTML snapshots**, never live LinkedIn, in CI.
- Config is read only through `core.config.get_settings()` (cached singleton). Add new fields there; never read `os.environ` directly in feature code.
- LLM calls go only through `llm.client.get_llm_client()` / an injected `LLMClient`. Tests inject `MockClient` or a fake — never hit a real API in tests.
- Secrets via env vars / `.env` only. No plaintext credentials in code or committed files.
- Commit after every task with a conventional-commit message (`feat:`, `test:`, `chore:`).

## File Structure

**New files:**
- `profile/builder.py` — CV text → `UserProfile` via LLM; writes YAML + version row.
- `core/governor.py` — Redis-backed rate governor + circuit breaker + kill switch.
- `submitters/form_brain.py` — three-layer Easy Apply answer resolver.
- `submitters/selectors.py` — centralized LinkedIn selector fallback chains.
- `submitters/linkedin_v2.py` — generic Easy Apply step walker (supersedes `submitters/linkedin.py`).
- `submitters/email_sender.py` — SMTP CV sender.
- `discovery/__init__.py`, `discovery/login.py`, `discovery/query_builder.py`, `discovery/linkedin_search.py` — LinkedIn discovery.
- `ingestion/text_post_parser.py` — LLM job classifier/extractor for text-only WhatsApp posts.
- `worker/outbound.py` — WhatsApp DM + email outbound applier task.
- `worker/discovery_tasks.py` — discovery beat task.
- `worker/digest.py` — daily digest task.
- `api/routes/profile.py` — resume upload + profile endpoints.
- `api/routes/control.py` — kill switch + governor status endpoints.
- Alembic migrations under `migrations/versions/`.
- Test files mirroring each module under `tests/`.
- Test fixtures under `tests/fixtures/` (saved HTML snapshots, sample CVs, sample WhatsApp posts).

**Modified files:**
- `core/config.py` — new settings fields.
- `db/models.py` — new columns + `AnswerCache`, `OutboundContact` tables.
- `match/scoring.py` — `MIN_APPLY_SCORE` policy in `decide_action`.
- `worker/tasks.py` — governor gating, priority drain, discovery_source, LinkedIn v2 submitter, re-score hook.
- `worker/celery_app.py` — beat schedule + new task routes.
- `api/main.py` — register `profile` and `control` routers.
- `api/routes/webhook.py` — WhatsApp media (PDF) handling + text-post routing.
- `bridge/whatsapp_bridge.js` — text-post forwarding + local send endpoint.
- `profile/loader.py` — expose a setter to swap the cached profile after rebuild.
- `pyproject.toml` — new optional deps (`playwright` already in `[browser]`; add `aiosmtplib` if chosen).

---

# Phase 1 — CV Intelligence

Goal: upload a CV (dashboard or WhatsApp) → LLM builds the whole profile → YAML regenerated + versioned → queued jobs re-scored. Everything downstream depends on the generated profile, so this is first.

### Task 1.1: Add all new configuration fields

**Files:**
- Modify: `core/config.py`
- Test: `tests/test_config.py` (create)

**Interfaces:**
- Produces: `Settings` gains fields: `min_apply_score: float`, `queue_ttl_days: int`, `linkedin_daily_cap: int`, `linkedin_min_gap_s: int`, `linkedin_max_gap_s: int`, `active_hours: str`, `discovery_interval_h: int`, `discovery_pages_per_query: int`, `wa_outbound_daily_cap: int`, `wa_contact_dedup_days: int`, `smtp_host/port/user/password/from_addr: str/int`, `linkedin_browser_profile_dir: str`, `dry_run: bool`, `bridge_send_url: str`. Property `active_hours_range() -> tuple[int, int]` parsing `"09:00-21:00"` → `(9, 21)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
from core.config import Settings


def test_new_defaults_present():
    s = Settings(_env_file=None)
    assert s.min_apply_score == 40.0
    assert s.linkedin_daily_cap == 45
    assert s.linkedin_min_gap_s == 120
    assert s.linkedin_max_gap_s == 360
    assert s.discovery_interval_h == 3
    assert s.wa_outbound_daily_cap == 15
    assert s.wa_contact_dedup_days == 30
    assert s.dry_run is False


def test_active_hours_range_parses():
    s = Settings(_env_file=None, active_hours="09:00-21:00")
    assert s.active_hours_range() == (9, 21)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL — `AttributeError` / `min_apply_score` missing.

- [ ] **Step 3: Add the fields to `core/config.py`**

Insert after the existing `user_profile_path` field (before `# ── Derived helpers ──`):

```python
    # ── Full-auto policy ────────────────────────────────
    min_apply_score: float = 40.0
    queue_ttl_days: int = 7

    # ── LinkedIn rate governor ──────────────────────────
    linkedin_daily_cap: int = 45
    linkedin_min_gap_s: int = 120
    linkedin_max_gap_s: int = 360
    active_hours: str = "09:00-21:00"
    linkedin_browser_profile_dir: str = ".linkedin_profile"
    dry_run: bool = False

    # ── Discovery ───────────────────────────────────────
    discovery_interval_h: int = 3
    discovery_pages_per_query: int = 3

    # ── WhatsApp outbound + email ───────────────────────
    wa_outbound_daily_cap: int = 15
    wa_contact_dedup_days: int = 30
    bridge_send_url: str = "http://localhost:8100/send"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_addr: str = ""
```

Add this property inside the `# ── Derived helpers ──` block:

```python
    def active_hours_range(self) -> tuple[int, int]:
        """Parse ACTIVE_HOURS 'HH:MM-HH:MM' into (start_hour, end_hour)."""
        try:
            start, end = self.active_hours.split("-")
            return int(start.split(":")[0]), int(end.split(":")[0])
        except Exception:
            return 9, 21
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Update `.env.example`**

Append the new vars with defaults and one-line comments (mirror the block above).

- [ ] **Step 6: Commit**

```bash
git add core/config.py tests/test_config.py .env.example
git commit -m "feat: add full-auto, governor, discovery, and outbound settings"
```

### Task 1.2: CV → UserProfile extraction

**Files:**
- Create: `profile/builder.py`
- Test: `tests/test_profile_builder.py`
- Fixture: `tests/fixtures/sample_cv_text.txt` (paste ~40 lines of a realistic RF/telecom engineer CV text)

**Interfaces:**
- Consumes: `llm.client.LLMClient.generate_json`, `profile.models.UserProfile`, `profile.pdf_loader.extract_text_from_pdf`.
- Produces:
  - `async def build_profile_from_text(cv_text: str, client: LLMClient | None = None) -> UserProfile`
  - `async def build_profile_from_pdf(pdf_path: str, client: LLMClient | None = None) -> UserProfile`
  Both return a validated `UserProfile`; the extracted `cv_text` is stored in `resume.text`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_profile_builder.py
import pytest
from llm.client import LLMClient
from profile.builder import build_profile_from_text


class FakeCVClient(LLMClient):
    async def generate(self, prompt, system="", max_tokens=2000, temperature=0.3):
        return ""
    async def generate_json(self, prompt, system="", max_tokens=2000):
        return {
            "personal": {"name": "Ali Hamed", "email": "ali@example.com",
                         "phone": "+971500000000", "location": "Dubai, UAE",
                         "work_authorization": "UAE resident"},
            "links": {"linkedin": "https://linkedin.com/in/alihamed", "github": "", "portfolio": ""},
            "preferences": {"roles": ["RF Engineer", "RAN Engineer"],
                            "locations": ["Dubai", "Abu Dhabi", "Remote"],
                            "keywords": ["LTE", "5G", "NR", "RF planning"],
                            "seniority": ["mid", "senior"]},
        }


@pytest.mark.asyncio
async def test_build_profile_maps_all_sections():
    p = await build_profile_from_text("dummy cv text", client=FakeCVClient())
    assert p.personal.name == "Ali Hamed"
    assert p.personal.location == "Dubai, UAE"
    assert "RF Engineer" in p.preferences.roles
    assert "5G" in p.preferences.keywords
    assert p.resume.text == "dummy cv text"


@pytest.mark.asyncio
async def test_build_profile_tolerates_missing_sections():
    class Sparse(FakeCVClient):
        async def generate_json(self, prompt, system="", max_tokens=2000):
            return {"personal": {"name": "Jane Doe"}}
    p = await build_profile_from_text("x", client=Sparse())
    assert p.personal.name == "Jane Doe"
    assert p.preferences.roles == []  # default, no crash
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_profile_builder.py -v`
Expected: FAIL — `ModuleNotFoundError: profile.builder`.

- [ ] **Step 3: Implement `profile/builder.py`**

```python
"""Build a UserProfile from a CV via the LLM."""

from __future__ import annotations

import structlog

from llm.client import LLMClient, get_llm_client
from profile.models import UserProfile

logger = structlog.get_logger(__name__)

_EXTRACTION_PROMPT = """You are extracting a structured job-seeker profile from a CV.
Return ONLY JSON with this exact shape (omit unknown fields, never invent facts):
{{
  "personal": {{"name": "", "email": "", "phone": "", "location": "", "work_authorization": ""}},
  "links": {{"linkedin": "", "github": "", "portfolio": ""}},
  "preferences": {{
     "roles": ["job titles this person should target, inferred from their experience"],
     "locations": ["cities/countries they can work in, plus 'Remote' if applicable"],
     "keywords": ["hard skills / technologies from the CV"],
     "seniority": ["one or more of: entry, mid, senior, lead, director"]
  }}
}}
Do not fabricate certifications, visas, or clearances. If a field is unknown, leave it empty.

CV TEXT:
{cv_text}
"""


async def build_profile_from_text(cv_text: str, client: LLMClient | None = None) -> UserProfile:
    """Extract a validated UserProfile from raw CV text."""
    if client is None:
        client = get_llm_client()

    raw = await client.generate_json(
        prompt=_EXTRACTION_PROMPT.format(cv_text=cv_text[:12000]),
    )

    # Merge into UserProfile defaults; store CV text as resume text.
    data: dict = {
        "personal": raw.get("personal", {}) or {},
        "links": raw.get("links", {}) or {},
        "preferences": raw.get("preferences", {}) or {},
        "resume": {"text": cv_text},
    }
    profile = UserProfile(**data)
    logger.info(
        "profile_built_from_cv",
        name=profile.personal.name,
        roles=len(profile.preferences.roles),
        keywords=len(profile.preferences.keywords),
    )
    return profile


async def build_profile_from_pdf(pdf_path: str, client: LLMClient | None = None) -> UserProfile:
    """Extract a UserProfile from a PDF CV file."""
    from profile.pdf_loader import extract_text_from_pdf  # noqa: PLC0415

    text = extract_text_from_pdf(pdf_path)
    if not text.strip():
        raise ValueError(f"No extractable text in PDF: {pdf_path}")
    profile = await build_profile_from_text(text, client=client)
    profile.resume.pdf_path = pdf_path
    return profile
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_profile_builder.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add profile/builder.py tests/test_profile_builder.py tests/fixtures/sample_cv_text.txt
git commit -m "feat: build UserProfile from CV via LLM extraction"
```

### Task 1.3: Persist rebuilt profile (YAML + version + cache swap)

**Files:**
- Create: `profile/writer.py`
- Modify: `profile/loader.py` (add `set_profile`)
- Test: `tests/test_profile_writer.py`

**Interfaces:**
- Consumes: `profile.models.UserProfile`, `db.models.UserProfileVersion`, `db.session.get_session_factory`.
- Produces:
  - `profile.loader.set_profile(profile: UserProfile) -> None` — replaces the module-cached profile.
  - `profile.writer.save_profile(profile: UserProfile, yaml_path: Path, db=None) -> int` — writes YAML (with a `.bak-<timestamp>` backup of any existing file), inserts a `UserProfileVersion` row (incrementing `version`), swaps the cache via `set_profile`, returns the new version number.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_profile_writer.py
from pathlib import Path
import yaml
from profile.models import UserProfile
from profile.writer import save_profile
from profile import loader


def test_save_profile_writes_yaml_and_swaps_cache(tmp_path, monkeypatch):
    # Isolate the DB write — save_profile must tolerate db=None (no version row)
    p = UserProfile()
    p.personal.name = "Ali Hamed"
    yaml_path = tmp_path / "user_profile.yaml"

    version = save_profile(p, yaml_path, db=None)
    assert version == 1
    assert yaml_path.exists()
    loaded = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert loaded["personal"]["name"] == "Ali Hamed"
    # Cache swapped
    assert loader.get_profile().personal.name == "Ali Hamed"


def test_save_profile_backs_up_existing(tmp_path):
    yaml_path = tmp_path / "user_profile.yaml"
    yaml_path.write_text("personal:\n  name: Old\n", encoding="utf-8")
    save_profile(UserProfile(), yaml_path, db=None)
    backups = list(tmp_path.glob("user_profile.yaml.bak-*"))
    assert len(backups) == 1
    assert "Old" in backups[0].read_text(encoding="utf-8")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_profile_writer.py -v`
Expected: FAIL — `ModuleNotFoundError: profile.writer`.

- [ ] **Step 3: Add `set_profile` to `profile/loader.py`**

Append:

```python
def set_profile(profile: UserProfile) -> None:
    """Replace the cached profile (used after an in-place rebuild)."""
    global _profile
    _profile = profile
```

- [ ] **Step 4: Implement `profile/writer.py`**

```python
"""Persist a rebuilt UserProfile to YAML with versioning."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import structlog
import yaml

from profile.loader import set_profile
from profile.models import UserProfile

logger = structlog.get_logger(__name__)


def save_profile(profile: UserProfile, yaml_path: Path, db=None) -> int:
    """Write profile YAML (with backup), record a version row, swap the cache.

    db=None skips the DB version row (used in tests); returns 1 in that case
    or the next version number when a DB session is provided.
    """
    yaml_path = Path(yaml_path)
    if yaml_path.exists():
        stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(yaml_path, yaml_path.with_suffix(yaml_path.suffix + f".bak-{stamp}"))

    payload = profile.model_dump()
    yaml_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    version = 1
    if db is not None:
        from db.models import UserProfileVersion  # noqa: PLC0415
        last = (
            db.query(UserProfileVersion)
            .order_by(UserProfileVersion.version.desc())
            .first()
        )
        version = (last.version + 1) if last else 1
        db.add(UserProfileVersion(
            profile_yaml=yaml_path.read_text(encoding="utf-8"),
            version=version,
        ))
        db.commit()

    set_profile(profile)
    logger.info("profile_saved", path=str(yaml_path), version=version)
    return version
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_profile_writer.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add profile/writer.py profile/loader.py tests/test_profile_writer.py
git commit -m "feat: persist rebuilt profile to versioned YAML and swap cache"
```

### Task 1.4: Re-score queued jobs after a profile rebuild

**Files:**
- Create: `worker/rescore.py`
- Test: `tests/test_rescore.py`

**Interfaces:**
- Consumes: `db.models.Job`, `JobStatus`, `match.scoring.score_job`, `profile.loader.get_profile`, `jobs.models.JobData`.
- Produces: `rescore_pending_jobs(db, profile) -> int` — re-scores every `Job` in status `EXTRACTED`/`SCORED`/`DRAFT`, updates `Job.score`, returns count updated. (Does not re-run generation; scoring only.)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rescore.py
from worker.rescore import rescore_pending_jobs
from profile.models import UserProfile


class _Job:
    def __init__(self, title):
        self.title = title; self.company = ""; self.location = ""
        self.employment_type = ""; self.seniority = ""; self.description = ""
        self.requirements = ""; self.apply_url = ""; self.source_url = "x"
        self.date_posted = ""; self.keywords = None
        self.status = None; self.score = None


class _Query:
    def __init__(self, jobs): self._jobs = jobs
    def filter(self, *a, **k): return self
    def all(self): return self._jobs


class _DB:
    def __init__(self, jobs): self._jobs = jobs; self.committed = False
    def query(self, *a, **k): return _Query(self._jobs)
    def commit(self): self.committed = True


def test_rescore_updates_scores():
    from db.models import JobStatus
    jobs = [_Job("RF Engineer")]
    jobs[0].status = JobStatus.SCORED
    prof = UserProfile(); prof.preferences.roles = ["RF Engineer"]
    n = rescore_pending_jobs(_DB(jobs), prof)
    assert n == 1
    assert jobs[0].score is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_rescore.py -v`
Expected: FAIL — `ModuleNotFoundError: worker.rescore`.

- [ ] **Step 3: Implement `worker/rescore.py`**

```python
"""Re-score queued jobs against the current profile."""

from __future__ import annotations

import json

import structlog

from db.models import Job, JobStatus
from jobs.models import JobData
from match.scoring import score_job

logger = structlog.get_logger(__name__)

_RESCORE_STATUSES = (JobStatus.EXTRACTED, JobStatus.SCORED, JobStatus.DRAFT)


def rescore_pending_jobs(db, profile) -> int:
    """Re-score not-yet-submitted jobs; returns the number updated."""
    rows = db.query(Job).filter(Job.status.in_(_RESCORE_STATUSES)).all()
    updated = 0
    for j in rows:
        job_data = JobData(
            title=j.title, company=j.company or "", location=j.location or "",
            employment_type=j.employment_type or "", seniority=j.seniority or "",
            description=j.description or "", requirements=j.requirements or "",
            apply_url=j.apply_url or "", source_url=j.source_url,
            date_posted=j.date_posted or "",
            keywords=json.loads(j.keywords) if j.keywords else [],
        )
        j.score = score_job(job_data, profile).total
        updated += 1
    db.commit()
    logger.info("rescored_pending_jobs", count=updated)
    return updated
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_rescore.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add worker/rescore.py tests/test_rescore.py
git commit -m "feat: re-score pending jobs after profile rebuild"
```

### Task 1.5: Resume upload endpoint + dashboard page

**Files:**
- Create: `api/routes/profile.py`
- Modify: `api/main.py` (register router)
- Modify: `api/templates/index.html` (add upload widget), `api/static/js/app.js` (upload handler)
- Test: `tests/test_profile_api.py`

**Interfaces:**
- Consumes: `profile.builder.build_profile_from_pdf`, `profile.writer.save_profile`, `worker.rescore.rescore_pending_jobs`, `core.utils.run_async`, `db.session.get_db`, `core.config.get_settings`.
- Produces: `POST /api/profile/resume` (multipart `file`) → saves PDF under `settings.user_profile_path` dir as `resume.pdf`, builds profile, saves YAML+version, re-scores, returns `{version, name, roles, keywords_count}`. `GET /api/profile` → returns current profile summary.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_profile_api.py
import io
from fastapi.testclient import TestClient
from unittest.mock import patch
from api.main import app
from profile.models import UserProfile

client = TestClient(app)


def _auth():
    from core.config import get_settings
    return {"Authorization": f"Bearer {get_settings().secret_key}"}


def test_upload_resume_builds_profile(tmp_path, monkeypatch):
    built = UserProfile(); built.personal.name = "Ali Hamed"
    built.preferences.roles = ["RF Engineer"]

    async def fake_build(path, client=None):
        return built

    with patch("api.routes.profile.build_profile_from_pdf", side_effect=fake_build), \
         patch("api.routes.profile.save_profile", return_value=3) as save_mock, \
         patch("api.routes.profile.rescore_pending_jobs", return_value=0):
        resp = client.post(
            "/api/profile/resume",
            headers=_auth(),
            files={"file": ("cv.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 3
    assert body["name"] == "Ali Hamed"
    assert save_mock.called
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_profile_api.py -v`
Expected: FAIL — 404 (route not registered).

- [ ] **Step 3: Implement `api/routes/profile.py`**

```python
"""Profile + resume upload routes."""

from __future__ import annotations

from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from core.config import Settings, get_settings
from core.utils import run_async
from db.session import get_db
from profile.builder import build_profile_from_pdf
from profile.loader import get_profile
from profile.writer import save_profile
from worker.rescore import rescore_pending_jobs

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/profile", tags=["profile"])


@router.post("/resume")
async def upload_resume(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Accept a CV PDF, rebuild the profile, re-score queued jobs."""
    yaml_path = settings.profile_path
    pdf_path = yaml_path.parent / "resume.pdf"
    pdf_path.write_bytes(await file.read())

    profile = await build_profile_from_pdf(str(pdf_path))
    profile.resume.pdf_path = str(pdf_path)
    version = save_profile(profile, yaml_path, db=db)
    rescored = rescore_pending_jobs(db, profile)

    logger.info("resume_uploaded", version=version, rescored=rescored)
    return {
        "version": version,
        "name": profile.personal.name,
        "roles": profile.preferences.roles,
        "keywords_count": len(profile.preferences.keywords),
        "rescored": rescored,
    }


@router.get("")
async def get_profile_summary():
    p = get_profile()
    return {
        "name": p.personal.name,
        "location": p.personal.location,
        "roles": p.preferences.roles,
        "keywords": p.preferences.keywords,
        "has_resume_pdf": bool(p.resume.pdf_path),
    }
```

- [ ] **Step 4: Register the router in `api/main.py`**

Add import alongside the others: `from api.routes.profile import router as profile_router`
And after the existing `app.include_router(...)` calls: `app.include_router(profile_router)`

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_profile_api.py -v`
Expected: PASS.

- [ ] **Step 6: Add the dashboard upload widget**

In `api/templates/index.html`, add a card with a drag-drop file input (`id="resumeInput"`) and an "Upload CV" button. In `api/static/js/app.js`, add a handler that POSTs the file to `/api/profile/resume` with the Bearer header and shows the returned name/roles. (Match the existing fetch/auth pattern already in `app.js`.)

- [ ] **Step 7: Commit**

```bash
git add api/routes/profile.py api/main.py api/templates/index.html api/static/js/app.js tests/test_profile_api.py
git commit -m "feat: resume upload endpoint + dashboard CV upload widget"
```

### Task 1.6: WhatsApp CV (PDF) upload path

**Files:**
- Modify: `api/routes/webhook.py` (handle `document` messages)
- Modify: `bridge/whatsapp_bridge.js` (forward PDF documents from the owner chat)
- Test: `tests/test_webhook_media.py`

**Interfaces:**
- Consumes: Meta media API (`GET /{media_id}` → url, then download with bearer), `build_profile_from_pdf`, `save_profile`, `rescore_pending_jobs`.
- Produces: in `api/routes/webhook.py`, a helper `async def _handle_document(msg, db, settings) -> bool` that, for PDF documents from an allowed sender, downloads the file, rebuilds the profile, and replies with a confirmation. Returns True if it handled a document.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_webhook_media.py
import pytest
from unittest.mock import AsyncMock, patch
from core.config import get_settings


@pytest.mark.asyncio
async def test_handle_document_rebuilds_profile(tmp_path):
    from api.routes import webhook
    settings = get_settings()
    msg = {"type": "document",
           "document": {"id": "MEDIA123", "mime_type": "application/pdf",
                        "filename": "cv.pdf"},
           "from": "15550001111"}

    with patch.object(webhook, "_download_media", new=AsyncMock(return_value=b"%PDF fake")), \
         patch.object(webhook, "build_profile_from_pdf", new=AsyncMock()) as build_mock, \
         patch.object(webhook, "save_profile", return_value=2), \
         patch.object(webhook, "rescore_pending_jobs", return_value=0), \
         patch.object(webhook, "_send_whatsapp_message", new=AsyncMock()) as send_mock:
        handled = await webhook._handle_document(msg, db=None, settings=settings)

    assert handled is True
    assert build_mock.called
    assert send_mock.called
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_webhook_media.py -v`
Expected: FAIL — `_handle_document` / `_download_media` do not exist.

- [ ] **Step 3: Implement the document path in `api/routes/webhook.py`**

Add imports at top:

```python
from profile.builder import build_profile_from_pdf
from profile.writer import save_profile
from worker.rescore import rescore_pending_jobs
```

Add helpers:

```python
async def _download_media(media_id: str, settings: Settings) -> bytes:
    """Fetch a WhatsApp media file by id via the Meta Graph API."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        meta = await client.get(
            f"https://graph.facebook.com/v18.0/{media_id}",
            headers={"Authorization": f"Bearer {settings.whatsapp_api_token}"},
        )
        url = meta.json().get("url", "")
        if not url:
            return b""
        resp = await client.get(
            url, headers={"Authorization": f"Bearer {settings.whatsapp_api_token}"}
        )
        return resp.content


async def _handle_document(msg: dict, db, settings: Settings) -> bool:
    """If msg is a PDF CV, rebuild the profile. Returns True if handled."""
    doc = msg.get("document", {})
    if doc.get("mime_type") != "application/pdf":
        return False
    sender = msg.get("from", "")
    content = await _download_media(doc.get("id", ""), settings)
    if not content:
        await _send_whatsapp_message(sender, "❌ Could not download the CV.", settings)
        return True

    from pathlib import Path
    pdf_path = Path(settings.user_profile_path).parent / "resume.pdf"
    pdf_path.write_bytes(content)
    profile = await build_profile_from_pdf(str(pdf_path))
    profile.resume.pdf_path = str(pdf_path)
    version = save_profile(profile, settings.profile_path, db=db)
    rescored = rescore_pending_jobs(db, profile) if db is not None else 0
    await _send_whatsapp_message(
        sender,
        f"✅ CV received. Profile v{version} rebuilt "
        f"({len(profile.preferences.roles)} target roles). Re-scored {rescored} jobs.",
        settings,
    )
    return True
```

In `receive_message`, before the interactive-button block, add:

```python
        if msg.get("type") == "document":
            if await _handle_document(msg, db, settings):
                processed += 1
                continue
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_webhook_media.py -v`
Expected: PASS.

- [ ] **Step 5: Bridge — forward PDF documents from the owner chat**

In `bridge/whatsapp_bridge.js`, in the message handler, detect `msg.hasMedia && msg.type === 'document'` in a 1:1 chat with the linked account and POST the downloaded base64 to a new agent endpoint `/api/profile/resume` (multipart). Gate on a config flag `FORWARD_OWNER_DOCS=true`. Keep it minimal — one `if` branch and a `fetch` with FormData.

- [ ] **Step 6: Commit**

```bash
git add api/routes/webhook.py bridge/whatsapp_bridge.js tests/test_webhook_media.py
git commit -m "feat: rebuild profile from a CV PDF sent over WhatsApp"
```

---

# Phase 2 — Rate Governor + FULL_AUTO Policy

Goal: one Redis-backed budget throttles every LinkedIn action; a circuit breaker pauses on challenges; a kill switch stops everything; `decide_action` gains the `MIN_APPLY_SCORE` gate. Must exist before any automation runs.

### Task 2.1: Governor budget core (cap, jitter, active hours)

**Files:**
- Create: `core/governor.py`
- Test: `tests/test_governor.py`

**Interfaces:**
- Consumes: `redis` (via `redis.from_url`), `core.config.get_settings`.
- Produces a `RateGovernor` class:
  - `__init__(self, settings, redis_client=None, now_fn=None, sleep_fn=None, rng=None)` — all side-effecting deps injectable for tests. `redis_client=None` falls back to an in-process dict store.
  - `applications_today() -> int`
  - `budget_remaining() -> int` (`daily_cap - applications_today()`)
  - `within_active_hours() -> bool`
  - `record_application() -> None` (increments today's counter with a day-scoped key)
  - `next_gap_seconds() -> int` (uniform random in `[min_gap_s, max_gap_s]` via injected `rng`)
  - `can_act() -> tuple[bool, str]` — False + reason when killed, in cooldown, out of hours, or over cap.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_governor.py
import random
from core.config import Settings
from core.governor import RateGovernor


def _gov(**over):
    s = Settings(_env_file=None, **over)
    # in-memory store, deterministic clock + rng
    clock = {"h": 12}
    return RateGovernor(
        s, redis_client=None,
        now_fn=lambda: type("T", (), {"hour": clock["h"], "strftime": lambda self, f: "20260720"})(),
        rng=random.Random(1),
    ), clock


def test_cap_and_record():
    gov, _ = _gov(linkedin_daily_cap=2)
    assert gov.budget_remaining() == 2
    gov.record_application()
    assert gov.applications_today() == 1
    gov.record_application()
    ok, reason = gov.can_act()
    assert ok is False and "cap" in reason.lower()


def test_active_hours():
    gov, clock = _gov(active_hours="09:00-21:00")
    assert gov.within_active_hours() is True
    clock["h"] = 23
    assert gov.within_active_hours() is False


def test_gap_within_bounds():
    gov, _ = _gov(linkedin_min_gap_s=120, linkedin_max_gap_s=360)
    for _ in range(20):
        g = gov.next_gap_seconds()
        assert 120 <= g <= 360
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_governor.py -v`
Expected: FAIL — `ModuleNotFoundError: core.governor`.

- [ ] **Step 3: Implement `core/governor.py` (budget portion)**

```python
"""Rate governor — shared budget + circuit breaker + kill switch for LinkedIn."""

from __future__ import annotations

import random
from datetime import datetime

import structlog

logger = structlog.get_logger(__name__)


class _MemoryStore:
    """Minimal in-process fallback when Redis is unavailable (tests/dev)."""
    def __init__(self):
        self._d: dict[str, str] = {}
    def get(self, k):  # noqa: D401
        v = self._d.get(k)
        return v.encode() if isinstance(v, str) else v
    def set(self, k, v, ex=None):
        self._d[k] = str(v)
    def incr(self, k):
        self._d[k] = str(int(self._d.get(k, "0")) + 1)
        return int(self._d[k])
    def delete(self, k):
        self._d.pop(k, None)


class RateGovernor:
    def __init__(self, settings, redis_client=None, now_fn=None, sleep_fn=None, rng=None):
        self.s = settings
        self.store = redis_client if redis_client is not None else _MemoryStore()
        self._now = now_fn or datetime.utcnow
        self._sleep = sleep_fn
        self._rng = rng or random.Random()

    # ── day-scoped counter ────────────────────────────
    def _day_key(self) -> str:
        return f"li:apps:{self._now().strftime('%Y%m%d')}"

    def applications_today(self) -> int:
        raw = self.store.get(self._day_key())
        return int(raw) if raw else 0

    def budget_remaining(self) -> int:
        return max(0, self.s.linkedin_daily_cap - self.applications_today())

    def record_application(self) -> None:
        self.store.incr(self._day_key())

    # ── active hours ──────────────────────────────────
    def within_active_hours(self) -> bool:
        start, end = self.s.active_hours_range()
        return start <= self._now().hour < end

    # ── jittered gap ──────────────────────────────────
    def next_gap_seconds(self) -> int:
        return self._rng.randint(self.s.linkedin_min_gap_s, self.s.linkedin_max_gap_s)

    # ── combined gate (cooldown + kill wired in Task 2.2/2.4) ──
    def can_act(self) -> tuple[bool, str]:
        if self.is_killed():
            return False, "kill switch active"
        if self.in_cooldown():
            return False, "in challenge cooldown"
        if not self.within_active_hours():
            return False, "outside active hours"
        if self.budget_remaining() <= 0:
            return False, "daily cap reached"
        return True, "ok"

    # placeholders overridden in later tasks
    def is_killed(self) -> bool:
        return (self.store.get("li:kill") or b"") == b"1"

    def in_cooldown(self) -> bool:
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_governor.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add core/governor.py tests/test_governor.py
git commit -m "feat: rate governor budget core (cap, jitter, active hours)"
```

### Task 2.2: Circuit breaker (challenge cooldown with doubling)

**Files:**
- Modify: `core/governor.py`
- Test: `tests/test_governor_cooldown.py`

**Interfaces:**
- Produces on `RateGovernor`:
  - `trip_cooldown() -> int` — records a challenge; sets a cooldown-until timestamp; cooldown length = `6h * 2**(recent_trips)` capped at 48h; returns the applied hours. `recent_trips` counted within a 7-day window.
  - `in_cooldown() -> bool` (override the placeholder) — True while `now < cooldown_until`.
  - `cooldown_remaining_s() -> int`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_governor_cooldown.py
import random
from core.config import Settings
from core.governor import RateGovernor


class Clock:
    def __init__(self, epoch=1_000_000): self.t = epoch
    def now(self):
        import datetime as d
        return d.datetime.utcfromtimestamp(self.t)


def _gov(clock):
    s = Settings(_env_file=None)
    return RateGovernor(s, redis_client=None, now_fn=clock.now, rng=random.Random(1))


def test_first_trip_is_six_hours_and_blocks():
    c = Clock(); gov = _gov(c)
    hours = gov.trip_cooldown()
    assert hours == 6
    assert gov.in_cooldown() is True
    c.t += 6 * 3600 + 1
    assert gov.in_cooldown() is False


def test_cooldown_doubles_on_repeat():
    c = Clock(); gov = _gov(c)
    assert gov.trip_cooldown() == 6
    c.t += 60
    assert gov.trip_cooldown() == 12
    c.t += 60
    assert gov.trip_cooldown() == 24
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_governor_cooldown.py -v`
Expected: FAIL — `trip_cooldown` missing.

- [ ] **Step 3: Implement the cooldown methods in `core/governor.py`**

Replace the `in_cooldown` placeholder and add the new methods:

```python
    def _epoch(self) -> int:
        n = self._now()
        return int(n.timestamp()) if hasattr(n, "timestamp") else 0

    def trip_cooldown(self) -> int:
        """Record a challenge and set/extend cooldown. Returns hours applied."""
        window_key = "li:trips"
        trips = int(self.store.get(window_key) or 0)
        hours = min(48, 6 * (2 ** trips))
        self.store.set(window_key, trips + 1, ex=7 * 24 * 3600)  # 7-day window
        until = self._epoch() + hours * 3600
        self.store.set("li:cooldown_until", until)
        logger.warning("governor_cooldown_tripped", hours=hours, trips=trips + 1)
        return hours

    def in_cooldown(self) -> bool:
        until = self.store.get("li:cooldown_until")
        return bool(until) and self._epoch() < int(until)

    def cooldown_remaining_s(self) -> int:
        until = self.store.get("li:cooldown_until")
        return max(0, int(until) - self._epoch()) if until else 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_governor_cooldown.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add core/governor.py tests/test_governor_cooldown.py
git commit -m "feat: governor challenge circuit breaker with doubling cooldown"
```

### Task 2.3: `MIN_APPLY_SCORE` policy in `decide_action`

**Files:**
- Modify: `match/scoring.py`
- Test: `tests/test_scoring.py` (add cases)

**Interfaces:**
- Modifies `decide_action` to accept `min_apply_score: float | None = None`. New rule: when `draft_only=False` and `auto_apply_enabled=True`, AUTO_APPLY if `score >= min_apply_score` (when provided) else the old `threshold`. `threshold` remains the sort/priority reference; `min_apply_score` is the gate.

- [ ] **Step 1: Write the failing test (append to `tests/test_scoring.py`)**

```python
def test_min_apply_score_gates_auto_apply():
    from match.scoring import decide_action, Action
    # score 50, threshold 80, but min_apply_score 40 → should AUTO_APPLY
    a = decide_action(score=50, auto_apply_enabled=True, draft_only=False,
                      threshold=80, min_apply_score=40)
    assert a == Action.AUTO_APPLY


def test_below_min_apply_score_is_draft():
    from match.scoring import decide_action, Action
    a = decide_action(score=30, auto_apply_enabled=True, draft_only=False,
                      threshold=80, min_apply_score=40)
    assert a == Action.DRAFT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_scoring.py -k min_apply -v`
Expected: FAIL — `decide_action() got an unexpected keyword argument 'min_apply_score'`.

- [ ] **Step 3: Modify `decide_action` in `match/scoring.py`**

```python
def decide_action(
    score: float,
    auto_apply_enabled: bool = False,
    draft_only: bool = True,
    skip_reason: str | None = None,
    threshold: float = AUTO_APPLY_THRESHOLD,
    min_apply_score: float | None = None,
) -> Action:
    if skip_reason:
        return Action.SKIP
    if score < SKIP_THRESHOLD:
        return Action.SKIP
    if draft_only:
        return Action.DRAFT
    gate = min_apply_score if min_apply_score is not None else threshold
    if auto_apply_enabled and score >= gate:
        return Action.AUTO_APPLY
    return Action.DRAFT
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_scoring.py -v`
Expected: PASS (existing + 2 new).

- [ ] **Step 5: Wire the gate into `worker/tasks.py`**

In `score_job_task` and `generate_application_task`, pass `min_apply_score=settings.min_apply_score` into `decide_action(...)`.

- [ ] **Step 6: Commit**

```bash
git add match/scoring.py worker/tasks.py tests/test_scoring.py
git commit -m "feat: MIN_APPLY_SCORE gate for full-auto applying"
```

### Task 2.4: Kill switch + governor status API

**Files:**
- Create: `api/routes/control.py`
- Modify: `core/governor.py` (add `kill()`, `resume()`), `api/main.py` (register router)
- Test: `tests/test_control_api.py`

**Interfaces:**
- Produces on `RateGovernor`: `kill()` sets `li:kill=1`; `resume()` deletes it; `is_killed()` already reads it; `status() -> dict` (remaining, killed, in_cooldown, cooldown_remaining_s, within_active_hours).
- Produces endpoints: `POST /api/control/kill`, `POST /api/control/resume`, `GET /api/control/status`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_control_api.py
from fastapi.testclient import TestClient
from api.main import app

client = TestClient(app)


def _auth():
    from core.config import get_settings
    return {"Authorization": f"Bearer {get_settings().secret_key}"}


def test_kill_and_status_roundtrip():
    assert client.post("/api/control/kill", headers=_auth()).status_code == 200
    assert client.get("/api/control/status", headers=_auth()).json()["killed"] is True
    assert client.post("/api/control/resume", headers=_auth()).status_code == 200
    assert client.get("/api/control/status", headers=_auth()).json()["killed"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_control_api.py -v`
Expected: FAIL — 404.

- [ ] **Step 3: Add `kill/resume/status` to `core/governor.py`**

```python
    def kill(self) -> None:
        self.store.set("li:kill", 1)

    def resume(self) -> None:
        self.store.delete("li:kill")

    def status(self) -> dict:
        return {
            "remaining": self.budget_remaining(),
            "applications_today": self.applications_today(),
            "killed": self.is_killed(),
            "in_cooldown": self.in_cooldown(),
            "cooldown_remaining_s": self.cooldown_remaining_s(),
            "within_active_hours": self.within_active_hours(),
        }
```

Also add a module-level singleton factory:

```python
_governor: RateGovernor | None = None


def get_governor() -> RateGovernor:
    global _governor
    if _governor is None:
        from core.config import get_settings
        settings = get_settings()
        client = None
        try:
            import redis  # noqa: PLC0415
            client = redis.from_url(settings.redis_url)
            client.ping()
        except Exception:
            client = None  # falls back to in-memory
        _governor = RateGovernor(settings, redis_client=client)
    return _governor
```

- [ ] **Step 4: Implement `api/routes/control.py`**

```python
"""Kill switch + governor status routes."""

from __future__ import annotations

from fastapi import APIRouter

from core.governor import get_governor

router = APIRouter(prefix="/api/control", tags=["control"])


@router.post("/kill")
async def kill():
    get_governor().kill()
    return {"status": "killed"}


@router.post("/resume")
async def resume():
    get_governor().resume()
    return {"status": "resumed"}


@router.get("/status")
async def status():
    return get_governor().status()
```

Register in `api/main.py`: `from api.routes.control import router as control_router` and `app.include_router(control_router)`.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_control_api.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add core/governor.py api/routes/control.py api/main.py tests/test_control_api.py
git commit -m "feat: kill switch + governor status API"
```

---

# Phase 3 — Smart Easy Apply (v2)

Goal: replace brittle selector-based form filling with a three-layer answer resolver, a generic step walker, abort-don't-lie, verified submission, and DRY_RUN. Governor-gated.

### Task 3.1: `AnswerCache` + `OutboundContact` models + migration

**Files:**
- Modify: `db/models.py`
- Create: `migrations/versions/20260720_0000_003_v2_tables.py`
- Test: `tests/test_v2_models.py`

**Interfaces:**
- Produces tables:
  - `AnswerCache(id, question_hash [unique, indexed], question_text, answer, source, created_at)`.
  - `OutboundContact(id, contact_hash [unique, indexed], channel, last_contacted_at, job_id, created_at)`.
  - New `Job` columns: `discovery_source: str`, `easy_apply: bool`, `expires_at: DateTime`.
  - New `Application` columns: `submission_channel: str`, `needs_review_reason: Text`.
  - New `JobStatus` member: `NEEDS_REVIEW = "needs_review"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_v2_models.py
def test_new_models_and_columns_import():
    from db.models import AnswerCache, OutboundContact, Job, Application, JobStatus
    assert hasattr(Job, "discovery_source")
    assert hasattr(Job, "easy_apply")
    assert hasattr(Application, "submission_channel")
    assert JobStatus.NEEDS_REVIEW.value == "needs_review"
    assert AnswerCache.__tablename__ == "answer_cache"
    assert OutboundContact.__tablename__ == "outbound_contacts"


def test_answer_cache_roundtrip(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from db.models import Base, AnswerCache
    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(AnswerCache(question_hash="h1", question_text="Years of Python?",
                       answer="5", source="llm"))
    db.commit()
    assert db.query(AnswerCache).filter_by(question_hash="h1").one().answer == "5"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_v2_models.py -v`
Expected: FAIL — imports missing.

- [ ] **Step 3: Edit `db/models.py`**

Add `Boolean` to the sqlalchemy import line. Add `NEEDS_REVIEW = "needs_review"` to `JobStatus`. Add columns to `Job` and `Application`. Append two new models:

```python
class AnswerCache(Base):
    """Cached answers to recurring application-form questions."""

    __tablename__ = "answer_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)
    question_hash = Column(String(64), unique=True, nullable=False)
    question_text = Column(Text, nullable=False)
    answer = Column(Text, nullable=False)
    source = Column(String(20), nullable=False)  # deterministic | cache | llm
    created_at = Column(DateTime, default=func.now(), nullable=False)

    __table_args__ = (Index("ix_answer_cache_hash", "question_hash"),)


class OutboundContact(Base):
    """Dedup record for WhatsApp/email recruiter outreach."""

    __tablename__ = "outbound_contacts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    contact_hash = Column(String(64), unique=True, nullable=False)
    channel = Column(String(20), nullable=False)  # whatsapp_dm | email
    last_contacted_at = Column(DateTime, default=func.now(), nullable=False)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    __table_args__ = (Index("ix_outbound_contact_hash", "contact_hash"),)
```

Add to `Job`: `discovery_source = Column(String(30), default="manual", nullable=True)`, `easy_apply = Column(Boolean, default=False, nullable=True)`, `expires_at = Column(DateTime, nullable=True)`. Also **change** `Job.extracted_url_id` to `nullable=True` — discovery and WhatsApp-text jobs have no originating `ExtractedURL` row, so this FK must be optional. (Discovery/outbound job inserts pass `extracted_url_id=None`.)
Add to `Application`: `submission_channel = Column(String(30), nullable=True)`, `needs_review_reason = Column(Text, nullable=True)`.

- [ ] **Step 4: Write the Alembic migration**

Create `migrations/versions/20260720_0000_003_v2_tables.py` with `create_table` for `answer_cache` and `outbound_contacts`, and `add_column` for the five new columns. Also alter `jobs.extracted_url_id` to nullable via `op.alter_column("jobs", "extracted_url_id", nullable=True)` (SQLite requires batch mode — use `with op.batch_alter_table("jobs") as batch: batch.alter_column("extracted_url_id", nullable=True)`; apply the `add_column` calls inside the same batch block). Set `down_revision` to the latest existing revision (`002_cover_letter_feedback`). Provide a full `downgrade()` that drops the new tables/columns and restores `extracted_url_id` to `nullable=False`.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_v2_models.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add db/models.py migrations/versions/20260720_0000_003_v2_tables.py tests/test_v2_models.py
git commit -m "feat: AnswerCache + OutboundContact models and v2 columns"
```

### Task 3.2: `form_brain` three-layer answer resolver

**Files:**
- Create: `submitters/form_brain.py`
- Test: `tests/test_form_brain.py`

**Interfaces:**
- Consumes: `db.models.AnswerCache`, `llm.client.LLMClient`, `profile.models.UserProfile`.
- Produces:
  - `normalize_question(q: str) -> str` (lowercase, collapse whitespace, strip punctuation).
  - `question_hash(q: str) -> str` (sha256 of normalized).
  - `class FieldSpec` dataclass: `label: str`, `kind: str` (`text|number|select|radio|checkbox|file|textarea`), `options: list[str]`, `required: bool`.
  - `class FormBrain(profile, client=None, db=None)` with `async def answer(self, field: FieldSpec, job) -> AnswerResult` where `AnswerResult(value: str | None, source: str, confident: bool)`. Resolution ladder: deterministic map → cache → LLM (then cache the LLM answer). Required + not confident → `value=None, confident=False`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_form_brain.py
import pytest
from llm.client import LLMClient
from profile.models import UserProfile
from submitters.form_brain import FormBrain, FieldSpec, normalize_question, question_hash


def _profile():
    p = UserProfile()
    p.personal.name = "Ali Hamed"; p.personal.email = "ali@example.com"
    p.personal.phone = "+971500000000"; p.personal.location = "Dubai, UAE"
    p.links.linkedin = "https://linkedin.com/in/alihamed"
    p.resume.text = "10 years RF engineering. LTE, 5G NR."
    return p


class _NoLLM(LLMClient):
    async def generate(self, *a, **k): raise AssertionError("LLM should not be called")
    async def generate_json(self, *a, **k): raise AssertionError("LLM should not be called")


class _LLM(LLMClient):
    def __init__(self, ans): self.ans = ans; self.calls = 0
    async def generate(self, *a, **k):
        self.calls += 1; return self.ans
    async def generate_json(self, *a, **k): return {}


def test_normalize_and_hash_stable():
    assert normalize_question("Years of  Python?  ") == normalize_question("years of python")
    assert question_hash("A") == question_hash("a")


@pytest.mark.asyncio
async def test_deterministic_email_no_llm():
    fb = FormBrain(_profile(), client=_NoLLM(), db=None)
    r = await fb.answer(FieldSpec(label="Email address", kind="text", options=[], required=True), job=None)
    assert r.value == "ali@example.com"
    assert r.source == "deterministic"


@pytest.mark.asyncio
async def test_llm_used_then_confident():
    llm = _LLM("8")
    fb = FormBrain(_profile(), client=llm, db=None)
    r = await fb.answer(FieldSpec(label="Years of RF experience", kind="number", options=[], required=True), job=None)
    assert r.value == "8"
    assert r.source == "llm"
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_unanswerable_required_not_confident():
    # LLM returns the refusal sentinel → not confident
    fb = FormBrain(_profile(), client=_LLM("UNKNOWN"), db=None)
    r = await fb.answer(FieldSpec(label="Do you hold a US Top Secret clearance?", kind="radio",
                                  options=["Yes", "No"], required=True), job=None)
    assert r.confident is False
    assert r.value is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_form_brain.py -v`
Expected: FAIL — `ModuleNotFoundError: submitters.form_brain`.

- [ ] **Step 3: Implement `submitters/form_brain.py`**

```python
"""Three-layer answer resolver for Easy Apply form fields."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

import structlog

from llm.client import LLMClient, get_llm_client

logger = structlog.get_logger(__name__)

_UNKNOWN = "UNKNOWN"


def normalize_question(q: str) -> str:
    q = (q or "").lower().strip()
    q = re.sub(r"[^\w\s]", "", q)
    return re.sub(r"\s+", " ", q)


def question_hash(q: str) -> str:
    return hashlib.sha256(normalize_question(q).encode()).hexdigest()


@dataclass
class FieldSpec:
    label: str
    kind: str  # text|number|select|radio|checkbox|file|textarea
    options: list[str] = field(default_factory=list)
    required: bool = False


@dataclass
class AnswerResult:
    value: str | None
    source: str          # deterministic | cache | llm
    confident: bool


class FormBrain:
    def __init__(self, profile, client: LLMClient | None = None, db=None):
        self.profile = profile
        self.client = client
        self.db = db

    # ── layer 1: deterministic map ────────────────────
    def _deterministic(self, label: str) -> str | None:
        p = self.profile
        low = label.lower()
        table = [
            (("email",), p.personal.email),
            (("first name",), (p.personal.name.split()[0] if p.personal.name else "")),
            (("last name", "surname"), " ".join(p.personal.name.split()[1:]) if p.personal.name else ""),
            (("full name", "your name"), p.personal.name),
            (("phone", "mobile"), p.personal.phone),
            (("city", "location"), (p.personal.location.split(",")[0].strip() if p.personal.location else "")),
            (("linkedin",), p.links.linkedin),
            (("github",), p.links.github),
            (("portfolio", "website"), p.links.portfolio),
        ]
        for keys, val in table:
            if any(k in low for k in keys) and val:
                return val
        return None

    # ── layer 2: cache ────────────────────────────────
    def _cache_get(self, qh: str) -> str | None:
        if self.db is None:
            return None
        from db.models import AnswerCache  # noqa: PLC0415
        row = self.db.query(AnswerCache).filter(AnswerCache.question_hash == qh).first()
        return row.answer if row else None

    def _cache_put(self, qh: str, label: str, answer: str, source: str) -> None:
        if self.db is None:
            return
        from db.models import AnswerCache  # noqa: PLC0415
        if self.db.query(AnswerCache).filter(AnswerCache.question_hash == qh).first():
            return
        self.db.add(AnswerCache(question_hash=qh, question_text=label,
                                answer=answer, source=source))
        self.db.commit()

    # ── layer 3: LLM ──────────────────────────────────
    async def _llm(self, fspec: FieldSpec, job) -> str:
        client = self.client or get_llm_client()
        opts = f"\nChoose exactly one of: {fspec.options}" if fspec.options else ""
        job_ctx = f"\nJob: {getattr(job, 'title', '')} at {getattr(job, 'company', '')}" if job else ""
        prompt = (
            "Answer this job-application question using ONLY the candidate CV. "
            f"If the CV does not support a confident answer, reply exactly '{_UNKNOWN}'. "
            "Never invent certifications, visas, or clearances.\n"
            f"Question: {fspec.label}{opts}{job_ctx}\n\nCV:\n{self.profile.resume.text[:4000]}"
        )
        return (await client.generate(prompt=prompt, max_tokens=120, temperature=0.0)).strip()

    async def answer(self, field: FieldSpec, job) -> AnswerResult:
        qh = question_hash(field.label)

        det = self._deterministic(field.label)
        if det:
            return AnswerResult(det, "deterministic", True)

        cached = self._cache_get(qh)
        if cached is not None:
            return AnswerResult(cached, "cache", True)

        raw = await self._llm(field, job)
        if not raw or raw.upper() == _UNKNOWN:
            return AnswerResult(None, "llm", False)

        self._cache_put(qh, field.label, raw, "llm")
        return AnswerResult(raw, "llm", True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_form_brain.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add submitters/form_brain.py tests/test_form_brain.py
git commit -m "feat: three-layer Easy Apply answer resolver (deterministic/cache/LLM)"
```

### Task 3.3: Centralized selector fallback chains

**Files:**
- Create: `submitters/selectors.py`
- Test: `tests/test_selectors.py`

**Interfaces:**
- Produces constants (lists of CSS selectors, tried in order): `EASY_APPLY_BUTTON`, `SUBMIT_BUTTON`, `NEXT_BUTTON`, `REVIEW_BUTTON`, `DISCARD_BUTTON`, `DISCARD_CONFIRM_BUTTON`, `SUCCESS_DIALOG`, `FORM_FIELD_CONTAINER`. And a helper `join(selectors: list[str]) -> str` returning a comma-joined selector string for Playwright `.locator`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_selectors.py
from submitters import selectors


def test_selector_lists_nonempty_and_joinable():
    for name in ["EASY_APPLY_BUTTON", "SUBMIT_BUTTON", "NEXT_BUTTON",
                 "DISCARD_BUTTON", "SUCCESS_DIALOG"]:
        val = getattr(selectors, name)
        assert isinstance(val, list) and val
    joined = selectors.join(selectors.SUBMIT_BUTTON)
    assert "," in joined or joined == selectors.SUBMIT_BUTTON[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_selectors.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `submitters/selectors.py`**

```python
"""Centralized LinkedIn Easy Apply selector fallback chains."""

from __future__ import annotations

EASY_APPLY_BUTTON = [
    "button.jobs-apply-button",
    'button[aria-label*="Easy Apply"]',
    'button[data-control-name="jobdetails_topcard_inapply"]',
]
SUBMIT_BUTTON = [
    'button[aria-label*="Submit application"]',
    'button[data-control-name="submit_unify"]',
]
NEXT_BUTTON = [
    'button[aria-label*="Continue to next step"]',
    'button[data-control-name="continue_unify"]',
]
REVIEW_BUTTON = ['button[aria-label*="Review your application"]']
DISCARD_BUTTON = ['button[aria-label*="Dismiss"]', 'button[aria-label*="Discard"]']
DISCARD_CONFIRM_BUTTON = ['button[data-control-name="discard_application_confirm_btn"]',
                          'button[aria-label*="Discard"]']
SUCCESS_DIALOG = ['div.artdeco-modal:has-text("Application sent")',
                  'h2:has-text("Your application was sent")',
                  'div:has-text("Application sent")']
FORM_FIELD_CONTAINER = ['.jobs-easy-apply-form-section__grouping',
                        '.fb-dash-form-element',
                        '.jobs-easy-apply-form-element']


def join(selectors: list[str]) -> str:
    return ", ".join(selectors)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_selectors.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add submitters/selectors.py tests/test_selectors.py
git commit -m "feat: centralized Easy Apply selector fallback chains"
```

### Task 3.4: DOM field extractor (offline-testable)

**Files:**
- Create: `submitters/field_extractor.py`
- Test: `tests/test_field_extractor.py`
- Fixture: `tests/fixtures/easy_apply_step.html` (a saved Easy Apply modal step: name text input, a numeric "Years of experience" input, a Yes/No radio group, a file input)

**Interfaces:**
- Produces `parse_fields(html: str) -> list[FieldSpec]` using BeautifulSoup (already a dependency) — extracts label text, `kind`, `options`, and `required` from a modal step's HTML. This lets us unit-test field understanding without a live browser; the Playwright walker (Task 3.5) reuses it by passing `await page.content()`.

- [ ] **Step 1: Build the fixture** `tests/fixtures/easy_apply_step.html` — a minimal but realistic snippet containing: a labeled text input (`Full name`), a labeled number input (`Years of experience` marked required), a radio fieldset (`Are you legally authorized to work?` Yes/No), and `<input type="file">`.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_field_extractor.py
from pathlib import Path
from submitters.field_extractor import parse_fields

HTML = (Path(__file__).parent / "fixtures" / "easy_apply_step.html").read_text(encoding="utf-8")


def test_parses_all_field_kinds():
    fields = parse_fields(HTML)
    kinds = {f.kind for f in fields}
    assert "text" in kinds and "number" in kinds and "radio" in kinds and "file" in kinds
    yrs = next(f for f in fields if "years" in f.label.lower())
    assert yrs.required is True
    auth = next(f for f in fields if "authorized" in f.label.lower())
    assert set(o.lower() for o in auth.options) == {"yes", "no"}
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_field_extractor.py -v`
Expected: FAIL — module missing.

- [ ] **Step 4: Implement `submitters/field_extractor.py`**

```python
"""Parse Easy Apply modal HTML into FieldSpec objects (browser-free)."""

from __future__ import annotations

from bs4 import BeautifulSoup

from submitters.form_brain import FieldSpec


def _label_for(soup, el) -> str:
    eid = el.get("id")
    if eid:
        lab = soup.find("label", attrs={"for": eid})
        if lab and lab.get_text(strip=True):
            return lab.get_text(strip=True)
    if el.get("aria-label"):
        return el["aria-label"]
    fs = el.find_parent("fieldset")
    if fs and fs.find("legend"):
        return fs.find("legend").get_text(strip=True)
    return el.get("name", "")


def parse_fields(html: str) -> list[FieldSpec]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[FieldSpec] = []
    seen_radio_groups: set[str] = set()

    for el in soup.find_all(["input", "textarea", "select"]):
        typ = (el.get("type") or el.name).lower()
        required = el.has_attr("required") or el.get("aria-required") == "true"

        if el.name == "textarea":
            out.append(FieldSpec(_label_for(soup, el), "textarea", [], required)); continue
        if el.name == "select":
            opts = [o.get_text(strip=True) for o in el.find_all("option")]
            out.append(FieldSpec(_label_for(soup, el), "select", opts, required)); continue
        if typ == "file":
            out.append(FieldSpec(_label_for(soup, el), "file", [], required)); continue
        if typ == "number":
            out.append(FieldSpec(_label_for(soup, el), "number", [], required)); continue
        if typ in ("radio", "checkbox"):
            fs = el.find_parent("fieldset")
            group = (fs.find("legend").get_text(strip=True) if fs and fs.find("legend")
                     else el.get("name", ""))
            if group in seen_radio_groups:
                continue
            seen_radio_groups.add(group)
            opts = []
            scope = fs or soup
            for r in scope.find_all("input", attrs={"type": typ}):
                lab = _label_for(soup, r)
                if lab:
                    opts.append(lab)
            out.append(FieldSpec(group, typ, opts,
                                 required or (fs.has_attr("aria-required") if fs else False)))
            continue
        # default text
        out.append(FieldSpec(_label_for(soup, el), "text", [], required))
    return out
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_field_extractor.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add submitters/field_extractor.py tests/test_field_extractor.py tests/fixtures/easy_apply_step.html
git commit -m "feat: browser-free Easy Apply field extractor"
```

### Task 3.5: LinkedIn Easy Apply v2 submitter (walker + abort-don't-lie + DRY_RUN)

**Files:**
- Create: `submitters/linkedin_v2.py`
- Modify: `worker/tasks.py` (use `LinkedInV2Submitter`, governor-gated), keep `submitters/linkedin.py` as legacy fallback
- Test: `tests/test_linkedin_v2.py` (logic-level, no live browser)

**Interfaces:**
- Consumes: `submitters/form_brain.FormBrain`, `submitters/field_extractor.parse_fields`, `submitters/selectors`, `core.governor.get_governor`.
- Produces `class LinkedInV2Submitter(BaseSubmitter)` (`platform_name="linkedin"`), plus a pure helper made unit-testable:
  - `async def resolve_step(fields: list[FieldSpec], brain: FormBrain, job) -> StepPlan` where `StepPlan(fills: dict[label,value], blocked_by: str | None)`. `blocked_by` set to the first required field the brain couldn't answer.
  - `submit(...)` returns `SubmissionResult`: `status="submitted"` only after a success dialog; `status="draft_only"` if not Easy Apply / DRY_RUN discard; and marks the application `needs_review_reason` upstream when `blocked_by` is set (returned via `SubmissionResult.error` prefixed `NEEDS_REVIEW:`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_linkedin_v2.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_linkedin_v2.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `submitters/linkedin_v2.py`**

Write the module with:
- `@dataclass StepPlan: fills: dict; blocked_by: str | None = None`
- `async def resolve_step(fields, brain, job) -> StepPlan`: iterate fields; for each, call `await brain.answer(f, job)`; if `res.confident and res.value is not None` add to `fills`; else if `f.required` set `blocked_by = f.label` and break; ignore non-required unanswerable fields.
- `class LinkedInV2Submitter(BaseSubmitter)`: `can_submit` = url contains `linkedin.com/jobs`. `submit()` mirrors the browser bootstrap in the existing `submitters/linkedin.py` (persistent context via `settings.linkedin_browser_profile_dir` using `pw.chromium.launch_persistent_context`), then per modal step: `html = await page.content()`; `fields = parse_fields(html)`; `plan = await resolve_step(...)`; if `plan.blocked_by`: click discard chain (`selectors.DISCARD_BUTTON` → `DISCARD_CONFIRM_BUTTON`), return `SubmissionResult(success=True, platform="linkedin", status="draft_only", error=f"NEEDS_REVIEW:{plan.blocked_by}")`; else fill each field by label (reuse locators from `selectors`), upload `resume_path` to file inputs, click Next/Review; when `SUBMIT_BUTTON` visible and `not settings.dry_run` click it and confirm via `SUCCESS_DIALOG` → `status="submitted"`; if `settings.dry_run` click discard instead → `status="draft_only"`. Always `detect_captcha(html)` at the top of each step → on hit, call `get_governor().trip_cooldown()` and return `status="draft_only"` with `error="CAPTCHA"`.

Include the complete code (roughly 160 lines) following the structure of `submitters/linkedin.py:100-298`, substituting the walker and `selectors`/`parse_fields`/`resolve_step` calls.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_linkedin_v2.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Wire into `worker/tasks.py`**

In `submit_application_task`: import `LinkedInV2Submitter`; replace `LinkedInSubmitter(...)` in the `all_submitters` Tier-2 list with `LinkedInV2Submitter()`. Before submitting via any LinkedIn submitter, check `get_governor().can_act()`; if not ok, leave the application APPROVED (re-queue later) and log the reason. On a `SubmissionResult` whose `error` starts with `NEEDS_REVIEW:`, set `app.needs_review_reason` and `app.status = JobStatus.NEEDS_REVIEW`, `db_job.status = JobStatus.NEEDS_REVIEW`. On a real `submitted`, call `get_governor().record_application()` and set `app.submission_channel="linkedin_easy"`.

- [ ] **Step 6: Run the full suite**

Run: `pytest tests/ -v`
Expected: all pass (existing 76 + new).

- [ ] **Step 7: Commit**

```bash
git add submitters/linkedin_v2.py worker/tasks.py tests/test_linkedin_v2.py
git commit -m "feat: LinkedIn Easy Apply v2 walker with abort-don't-lie + DRY_RUN"
```

### Task 3.6: Priority apply-queue drainer + TTL expiry

Spec §3.4/§3.6: the 45/day budget must go to the best matches first, and stale jobs expire. In full-auto, generation no longer submits immediately — approved applications wait in a queue that a beat task drains highest-score-first under governor pacing.

**Files:**
- Create: `worker/drainer.py`
- Modify: `worker/tasks.py` (in full-auto, `generate_application_task` sets APPROVED but does NOT chain to submit), `worker/celery_app.py` (beat entry `drain-apply-queue` every 5 min + `expire-stale-jobs` daily)
- Test: `tests/test_drainer.py`

**Interfaces:**
- Consumes: `db.models.Application`, `Job`, `JobStatus`, `core.governor.RateGovernor`, `worker.tasks.submit_application_task`.
- Produces:
  - `select_next_application(db) -> int | None` — the `Application.id` of the APPROVED application whose `Job.score` is highest (ties broken by lowest job id); None if none.
  - `expire_stale_jobs(db, now, ttl_days) -> int` — set `EXTRACTED/SCORED/DRAFT` jobs older than `ttl_days` (by `created_at`) to `SKIPPED`; returns count.
  - `@shared_task drain_apply_queue_task()` — while `governor.can_act()` and an application is available: submit one (`submit_application_task.apply`), then stop after one per tick (Celery beat re-fires; pacing via `governor.next_gap_seconds()` handled inside the submit path). Returns number submitted this tick.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drainer.py
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from db.models import Base, Job, Application, JobStatus
from worker.drainer import select_next_application, expire_stale_jobs


def _db(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path/'d.db'}")
    Base.metadata.create_all(e)
    return sessionmaker(bind=e)()


def _job(db, score, status=JobStatus.APPROVED, created=None):
    j = Job(title="t", source_url="x", status=status, score=score)
    if created:
        j.created_at = created
    db.add(j); db.flush()
    return j


def test_select_highest_score_first(tmp_path):
    db = _db(tmp_path)
    j1 = _job(db, 50); j2 = _job(db, 90)
    for j in (j1, j2):
        db.add(Application(job_id=j.id, status=JobStatus.APPROVED))
    db.commit()
    app_id = select_next_application(db)
    picked = db.query(Application).filter(Application.id == app_id).one()
    assert picked.job_id == j2.id  # score 90 wins


def test_expire_stale(tmp_path):
    db = _db(tmp_path)
    old = datetime(2026, 7, 1); now = datetime(2026, 7, 20)
    _job(db, 30, status=JobStatus.SCORED, created=old)
    db.commit()
    n = expire_stale_jobs(db, now=now, ttl_days=7)
    assert n == 1
    assert db.query(Job).first().status == JobStatus.SKIPPED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_drainer.py -v`
Expected: FAIL — `ModuleNotFoundError: worker.drainer`.

- [ ] **Step 3: Implement `worker/drainer.py`**

```python
"""Priority drain of the approved-application queue + stale-job expiry."""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog
from celery import shared_task

from db.models import Application, Job, JobStatus

logger = structlog.get_logger(__name__)

_STALE_STATUSES = (JobStatus.EXTRACTED, JobStatus.SCORED, JobStatus.DRAFT)


def select_next_application(db) -> int | None:
    """Highest Job.score among APPROVED applications; ties → lowest job id."""
    row = (
        db.query(Application)
        .join(Job, Application.job_id == Job.id)
        .filter(Application.status == JobStatus.APPROVED)
        .order_by(Job.score.desc(), Job.id.asc())
        .first()
    )
    return row.id if row else None


def expire_stale_jobs(db, now: datetime, ttl_days: int) -> int:
    cutoff = now - timedelta(days=ttl_days)
    rows = (
        db.query(Job)
        .filter(Job.status.in_(_STALE_STATUSES), Job.created_at < cutoff)
        .all()
    )
    for j in rows:
        j.status = JobStatus.SKIPPED
    db.commit()
    logger.info("expired_stale_jobs", count=len(rows))
    return len(rows)


@shared_task(name="worker.drainer.drain_apply_queue_task")
def drain_apply_queue_task() -> int:
    from core.governor import get_governor          # noqa: PLC0415
    from db.session import get_session_factory      # noqa: PLC0415
    from worker.tasks import submit_application_task  # noqa: PLC0415

    gov = get_governor()
    ok, reason = gov.can_act()
    if not ok:
        logger.info("drain_skipped", reason=reason)
        return 0
    db = get_session_factory()()
    try:
        app_id = select_next_application(db)
        if app_id is None:
            return 0
        submit_application_task.apply(args=[app_id])  # governor.record_application in submit path
        return 1
    finally:
        db.close()


@shared_task(name="worker.drainer.expire_stale_jobs_task")
def expire_stale_jobs_task() -> int:
    from core.config import get_settings            # noqa: PLC0415
    from db.session import get_session_factory       # noqa: PLC0415

    db = get_session_factory()()
    try:
        return expire_stale_jobs(db, datetime.utcnow(), get_settings().queue_ttl_days)
    finally:
        db.close()
```

- [ ] **Step 4: Stop immediate submission in full-auto** — in `worker/tasks.py` `generate_application_task`, when `auto_approve` is True, keep setting status APPROVED but **remove** the direct `submit_application_task` chaining IF `not settings.tasks_always_eager` (real broker → drainer owns pacing). Under `tasks_always_eager` (local dev), keep the direct chain so eager tests still submit. Gate with: `if auto_approve and settings.tasks_always_eager: <submit now> else: <leave for drainer>`.

- [ ] **Step 5: Add beat entries to `worker/celery_app.py`**

At the top of `worker/celery_app.py`, add: `from celery.schedules import crontab`. If `app.conf.beat_schedule` was not created by an earlier task, initialize it first (`app.conf.beat_schedule = app.conf.beat_schedule or {}`). Then:

```python
    app.conf.beat_schedule["drain-apply-queue"] = {
        "task": "worker.drainer.drain_apply_queue_task",
        "schedule": 300.0,  # every 5 min; governor enforces gaps/caps
    }
    app.conf.beat_schedule["expire-stale-jobs"] = {
        "task": "worker.drainer.expire_stale_jobs_task",
        "schedule": crontab(hour=3, minute=0),
    }
```

Add `"worker.drainer.drain_apply_queue_task"` and `"worker.drainer.expire_stale_jobs_task"` to `task_routes` under the `submission` queue.

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_drainer.py -v`
Expected: PASS (2 tests).

- [ ] **Step 7: Commit**

```bash
git add worker/drainer.py worker/tasks.py worker/celery_app.py tests/test_drainer.py
git commit -m "feat: priority apply-queue drainer + stale-job expiry"
```

---

# Phase 4 — LinkedIn Discovery

Goal: log in once (persistent session), build CV-driven searches, scrape matching jobs directly into `Job` rows, run on a beat schedule. Governor-gated.

### Task 4.1: Persistent-session login command

**Files:**
- Create: `discovery/__init__.py`, `discovery/login.py`
- Test: `tests/test_discovery_login.py` (import/CLI-shape only; no live browser)

**Interfaces:**
- Produces `async def open_login(profile_dir: str) -> None` — launches a **headed** persistent Chromium context at `profile_dir`, navigates to LinkedIn login, and waits for the user to finish (polls until `feed` in URL or a timeout), then closes. `python -m discovery.login` runs it using `settings.linkedin_browser_profile_dir`. Also `def _is_logged_in(url: str) -> bool` (pure, testable).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_discovery_login.py
from discovery.login import _is_logged_in


def test_logged_in_detection():
    assert _is_logged_in("https://www.linkedin.com/feed/") is True
    assert _is_logged_in("https://www.linkedin.com/login") is False
    assert _is_logged_in("https://www.linkedin.com/checkpoint/challenge") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_discovery_login.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `discovery/login.py`** (and empty `discovery/__init__.py`)

```python
"""One-time LinkedIn login into a persistent browser profile."""

from __future__ import annotations

import asyncio

import structlog

logger = structlog.get_logger(__name__)


def _is_logged_in(url: str) -> bool:
    return "feed" in url and "login" not in url and "checkpoint" not in url


async def open_login(profile_dir: str, timeout_s: int = 180) -> None:
    from playwright.async_api import async_playwright  # noqa: PLC0415

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(profile_dir, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.linkedin.com/login")
        logger.info("login_waiting", hint="Complete login + 2FA in the opened window")
        for _ in range(timeout_s):
            if _is_logged_in(page.url):
                logger.info("login_detected")
                break
            await asyncio.sleep(1)
        await ctx.close()


if __name__ == "__main__":
    from core.config import get_settings
    asyncio.run(open_login(get_settings().linkedin_browser_profile_dir))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_discovery_login.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add discovery/__init__.py discovery/login.py tests/test_discovery_login.py
git commit -m "feat: one-time LinkedIn login into persistent browser profile"
```

### Task 4.2: CV-driven query builder

**Files:**
- Create: `discovery/query_builder.py`
- Test: `tests/test_query_builder.py`

**Interfaces:**
- Produces `build_search_urls(profile, pages_per_query=3) -> list[str]` — for each `role × location`, produce a LinkedIn jobs search URL with the Easy Apply filter (`f_AL=true`), `f_TPR=r86400` (last 24h), `sortBy=DD` (newest), and `start` pagination offsets (0, 25, 50…). URL-encodes keywords/location. Caps total URLs to avoid runaway (max 30).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_query_builder.py
from urllib.parse import parse_qs, urlparse
from profile.models import UserProfile
from discovery.query_builder import build_search_urls


def _p():
    p = UserProfile()
    p.preferences.roles = ["RF Engineer"]
    p.preferences.locations = ["Dubai"]
    return p


def test_builds_easy_apply_urls_with_pagination():
    urls = build_search_urls(_p(), pages_per_query=2)
    assert len(urls) == 2
    q = parse_qs(urlparse(urls[0]).query)
    assert q["f_AL"] == ["true"]
    assert "RF Engineer" in q["keywords"][0]
    assert q["location"] == ["Dubai"]
    starts = sorted(int(parse_qs(urlparse(u).query)["start"][0]) for u in urls)
    assert starts == [0, 25]


def test_total_url_cap():
    p = UserProfile()
    p.preferences.roles = [f"role{i}" for i in range(20)]
    p.preferences.locations = [f"loc{j}" for j in range(20)]
    assert len(build_search_urls(p, pages_per_query=3)) <= 30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_query_builder.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `discovery/query_builder.py`**

```python
"""Build LinkedIn job-search URLs from the user profile."""

from __future__ import annotations

from urllib.parse import urlencode

_BASE = "https://www.linkedin.com/jobs/search/"
_MAX_URLS = 30


def build_search_urls(profile, pages_per_query: int = 3) -> list[str]:
    roles = profile.preferences.roles or ["Engineer"]
    locations = profile.preferences.locations or [""]
    urls: list[str] = []
    for role in roles:
        for loc in locations:
            for page in range(pages_per_query):
                params = {
                    "keywords": role,
                    "location": loc,
                    "f_AL": "true",       # Easy Apply
                    "f_TPR": "r86400",    # last 24h
                    "sortBy": "DD",       # newest
                    "start": page * 25,
                }
                urls.append(f"{_BASE}?{urlencode(params)}")
                if len(urls) >= _MAX_URLS:
                    return urls
    return urls
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_query_builder.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add discovery/query_builder.py tests/test_query_builder.py
git commit -m "feat: CV-driven LinkedIn search URL builder"
```

### Task 4.3: In-browser job extraction from search results

**Files:**
- Create: `discovery/linkedin_search.py`
- Test: `tests/test_linkedin_search_parse.py`
- Fixture: `tests/fixtures/linkedin_search_results.html` (saved job-search results list markup: several `.job-card-container` entries with title, company, location, and a data job id)

**Interfaces:**
- Produces:
  - `parse_search_results(html: str) -> list[JobData]` (pure, testable) — extract title/company/location and build `apply_url` = `https://www.linkedin.com/jobs/view/<id>`, set `source_url` similarly, `keywords=[]`.
  - `async def run_discovery(db, profile, settings, governor) -> int` — for each search URL (governor-permitting), load in the persistent context, scroll, call `parse_search_results`, insert deduped `Job` rows (`discovery_source="linkedin_search"`, `easy_apply=True`, `status=EXTRACTED`), enqueue `score_job_task`. Returns jobs inserted.

- [ ] **Step 1: Build the fixture** `tests/fixtures/linkedin_search_results.html` with ~3 job cards using representative class names (`job-card-container__link`, `artdeco-entity-lockup__title`, `__subtitle`, `__caption`, and `data-job-id`).

- [ ] **Step 2: Write the failing test**

```python
# tests/test_linkedin_search_parse.py
from pathlib import Path
from discovery.linkedin_search import parse_search_results

HTML = (Path(__file__).parent / "fixtures" / "linkedin_search_results.html").read_text(encoding="utf-8")


def test_parse_extracts_jobs():
    jobs = parse_search_results(HTML)
    assert len(jobs) >= 3
    j = jobs[0]
    assert j.title and j.company
    assert "linkedin.com/jobs/view/" in j.apply_url
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_linkedin_search_parse.py -v`
Expected: FAIL — module missing.

- [ ] **Step 4: Implement `discovery/linkedin_search.py`**

Implement `parse_search_results` with BeautifulSoup keyed to the fixture's classes (with fallbacks), and `run_discovery` following the DB-insert + dedup pattern from `worker/tasks.py:151-199` (reuse `job_signature`, `url_hash`, `score_job_task`). Gate each URL load on `governor.can_act()` and sleep `governor.next_gap_seconds()` between loads; call `governor.record_application()`-equivalent counting is NOT used for discovery (discovery is read-only) — instead just respect kill/cooldown/active-hours via `can_act()`.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_linkedin_search_parse.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add discovery/linkedin_search.py tests/test_linkedin_search_parse.py tests/fixtures/linkedin_search_results.html
git commit -m "feat: LinkedIn search results parsing + discovery run"
```

### Task 4.4: Discovery beat task + schedule

**Files:**
- Create: `worker/discovery_tasks.py`
- Modify: `worker/celery_app.py` (beat schedule + route)
- Test: `tests/test_discovery_task.py`

**Interfaces:**
- Produces `@shared_task discover_jobs_task()` — opens a DB session + governor + profile and calls `run_discovery`; safe no-op (logs + returns 0) when killed/out-of-hours. Beat entry `discover-jobs` every `settings.discovery_interval_h` hours.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_discovery_task.py
from unittest.mock import patch


def test_discover_task_skips_when_killed():
    from worker import discovery_tasks
    class _Gov:
        def can_act(self): return (False, "kill switch active")
    with patch.object(discovery_tasks, "get_governor", return_value=_Gov()):
        assert discovery_tasks.discover_jobs_task() == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_discovery_task.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `worker/discovery_tasks.py`**

```python
"""Scheduled LinkedIn discovery task."""

from __future__ import annotations

import structlog
from celery import shared_task

from core.config import get_settings
from core.governor import get_governor
from core.utils import run_async

logger = structlog.get_logger(__name__)


@shared_task(name="worker.discovery_tasks.discover_jobs_task")
def discover_jobs_task() -> int:
    gov = get_governor()
    ok, reason = gov.can_act()
    if not ok:
        logger.info("discovery_skipped", reason=reason)
        return 0
    from db.session import get_session_factory  # noqa: PLC0415
    from profile.loader import get_profile      # noqa: PLC0415
    from discovery.linkedin_search import run_discovery  # noqa: PLC0415

    settings = get_settings()
    db = get_session_factory()()
    try:
        return run_async(run_discovery(db, get_profile(), settings, gov))
    finally:
        db.close()
```

- [ ] **Step 4: Add beat schedule to `worker/celery_app.py`**

After `app.conf.update(...)`, ensure the schedule dict exists, then add the entry additively (so entries from other tasks are not clobbered — all tasks edit the same `create_celery_app()` function):

```python
    from core.config import get_settings as _gs
    app.conf.beat_schedule = getattr(app.conf, "beat_schedule", None) or {}
    _interval = _gs().discovery_interval_h * 3600
    app.conf.beat_schedule["discover-jobs"] = {
        "task": "worker.discovery_tasks.discover_jobs_task",
        "schedule": float(_interval),
    }
```

Add `"worker.discovery_tasks.discover_jobs_task": {"queue": "discovery"}` to `task_routes`, and include `"worker"` already autodiscovered — add `discovery_tasks` import safety via `app.autodiscover_tasks(["worker"])` (already present).

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_discovery_task.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add worker/discovery_tasks.py worker/celery_app.py tests/test_discovery_task.py
git commit -m "feat: scheduled LinkedIn discovery beat task"
```

---

# Phase 5 — WhatsApp Outbound Applier

Goal: parse text-only job posts, and auto-send the CV + intro to the recruiter's WhatsApp number or email, with caps + dedup.

### Task 5.1: Text-post job parser

**Files:**
- Create: `ingestion/text_post_parser.py`
- Test: `tests/test_text_post_parser.py`
- Fixture: `tests/fixtures/whatsapp_posts.json` (list of `{text, expect_is_job}` — real-style English + Arabic Gulf posts, some with phone, some with email, some non-jobs)

**Interfaces:**
- Consumes: `llm.client.LLMClient.generate_json`.
- Produces:
  - `def looks_like_job(text: str) -> bool` — cheap keyword prefilter (pure).
  - `async def parse_text_post(text: str, client=None) -> ParsedPost` where `ParsedPost(is_job: bool, title: str, company: str, description: str, contact_phone: str, contact_email: str)`. Returns `is_job=False` immediately if the prefilter fails (saves an LLM call).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_text_post_parser.py
import pytest
from llm.client import LLMClient
from ingestion.text_post_parser import looks_like_job, parse_text_post


def test_prefilter():
    assert looks_like_job("We are hiring an RF Engineer, send CV to hr@x.com") is True
    assert looks_like_job("Good morning everyone ☀️") is False


class _LLM(LLMClient):
    async def generate(self, *a, **k): return ""
    async def generate_json(self, *a, **k):
        return {"is_job": True, "title": "RF Engineer", "company": "TelcoX",
                "description": "5G RF role", "contact_phone": "+971500000000",
                "contact_email": "hr@telcox.com"}


@pytest.mark.asyncio
async def test_parse_extracts_contact():
    r = await parse_text_post("Hiring RF Engineer, WhatsApp +971500000000", client=_LLM())
    assert r.is_job is True
    assert r.contact_phone == "+971500000000"
    assert r.contact_email == "hr@telcox.com"


@pytest.mark.asyncio
async def test_prefilter_short_circuits_non_jobs():
    class _Boom(LLMClient):
        async def generate(self, *a, **k): raise AssertionError("no LLM")
        async def generate_json(self, *a, **k): raise AssertionError("no LLM")
    r = await parse_text_post("happy friday!", client=_Boom())
    assert r.is_job is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_text_post_parser.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `ingestion/text_post_parser.py`**

```python
"""Classify + extract job info from text-only WhatsApp posts."""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from llm.client import LLMClient, get_llm_client

logger = structlog.get_logger(__name__)

_KEYWORDS = ("hiring", "vacancy", "vacancies", "send cv", "send resume", "looking for",
             "we are recruiting", "job opening", "apply", "position",
             "مطلوب", "توظيف", "وظيفة", "شاغر")  # Arabic: required / hiring / job / vacancy


def looks_like_job(text: str) -> bool:
    low = (text or "").lower()
    return any(k in low for k in _KEYWORDS)


@dataclass
class ParsedPost:
    is_job: bool = False
    title: str = ""
    company: str = ""
    description: str = ""
    contact_phone: str = ""
    contact_email: str = ""


_PROMPT = """Decide if this WhatsApp message is a job posting. If yes, extract fields.
Return ONLY JSON: {{"is_job": bool, "title": "", "company": "", "description": "",
"contact_phone": "", "contact_email": ""}}. Use "" for anything absent. Do not invent contacts.

MESSAGE:
{text}
"""


async def parse_text_post(text: str, client: LLMClient | None = None) -> ParsedPost:
    if not looks_like_job(text):
        return ParsedPost(is_job=False)
    client = client or get_llm_client()
    try:
        raw = await client.generate_json(prompt=_PROMPT.format(text=text[:2000]))
    except Exception as exc:
        logger.warning("text_post_parse_failed", error=str(exc))
        return ParsedPost(is_job=False)
    return ParsedPost(
        is_job=bool(raw.get("is_job")),
        title=raw.get("title", "") or "",
        company=raw.get("company", "") or "",
        description=raw.get("description", "") or "",
        contact_phone=raw.get("contact_phone", "") or "",
        contact_email=raw.get("contact_email", "") or "",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_text_post_parser.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add ingestion/text_post_parser.py tests/test_text_post_parser.py tests/fixtures/whatsapp_posts.json
git commit -m "feat: WhatsApp text-post job classifier/extractor"
```

### Task 5.2: Outbound contact dedup

**Files:**
- Create: `worker/outbound_dedup.py`
- Test: `tests/test_outbound_dedup.py`

**Interfaces:**
- Consumes: `db.models.OutboundContact`.
- Produces:
  - `normalize_contact(value: str) -> str` (strip spaces/dashes/`+` for phones; lowercase for emails).
  - `contact_hash(value: str) -> str`.
  - `can_contact(db, value: str, dedup_days: int, now: datetime) -> bool` — False if contacted within window.
  - `record_contact(db, value, channel, job_id, now) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_outbound_dedup.py
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from db.models import Base
from worker.outbound_dedup import normalize_contact, can_contact, record_contact


def _db(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path/'o.db'}")
    Base.metadata.create_all(e)
    return sessionmaker(bind=e)()


def test_normalize():
    assert normalize_contact("+971 50-000 0000") == "971500000000"
    assert normalize_contact("HR@Example.com ") == "hr@example.com"


def test_dedup_window(tmp_path):
    db = _db(tmp_path)
    now = datetime(2026, 7, 20, 12, 0, 0)
    assert can_contact(db, "+971500000000", 30, now) is True
    record_contact(db, "+971500000000", "whatsapp_dm", job_id=None, now=now)
    assert can_contact(db, "+971 50 000 0000", 30, now + timedelta(days=5)) is False
    assert can_contact(db, "+971500000000", 30, now + timedelta(days=31)) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_outbound_dedup.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `worker/outbound_dedup.py`**

```python
"""Dedup + record for outbound recruiter contacts."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta

from db.models import OutboundContact


def normalize_contact(value: str) -> str:
    v = (value or "").strip()
    if "@" in v:
        return v.lower()
    return re.sub(r"[^\d]", "", v)


def contact_hash(value: str) -> str:
    return hashlib.sha256(normalize_contact(value).encode()).hexdigest()


def can_contact(db, value: str, dedup_days: int, now: datetime) -> bool:
    ch = contact_hash(value)
    row = db.query(OutboundContact).filter(OutboundContact.contact_hash == ch).first()
    if not row:
        return True
    return row.last_contacted_at < now - timedelta(days=dedup_days)


def record_contact(db, value: str, channel: str, job_id, now: datetime) -> None:
    ch = contact_hash(value)
    row = db.query(OutboundContact).filter(OutboundContact.contact_hash == ch).first()
    if row:
        row.last_contacted_at = now
        row.channel = channel
    else:
        db.add(OutboundContact(contact_hash=ch, channel=channel,
                               last_contacted_at=now, job_id=job_id))
    db.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_outbound_dedup.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add worker/outbound_dedup.py tests/test_outbound_dedup.py
git commit -m "feat: outbound recruiter-contact dedup"
```

### Task 5.3: SMTP CV sender

**Files:**
- Create: `submitters/email_sender.py`
- Modify: `pyproject.toml` (add `aiosmtplib` to an `[email]` extra)
- Test: `tests/test_email_sender.py`

**Interfaces:**
- Produces `async def send_cv_email(to_addr, subject, body, pdf_path, settings, sender=None) -> bool` — builds a MIME message with the PDF attached and sends via SMTP; `sender` is an injectable async send function for tests. Returns True on success, False (logged) on failure. No-op returning False when `settings.smtp_host` is empty.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_email_sender.py
import pytest
from core.config import Settings
from submitters.email_sender import send_cv_email


@pytest.mark.asyncio
async def test_builds_and_sends(tmp_path):
    pdf = tmp_path / "cv.pdf"; pdf.write_bytes(b"%PDF-1.4 x")
    captured = {}
    async def fake_sender(message, host, port, username, password, start_tls):
        captured["to"] = message["To"]; captured["host"] = host
        captured["has_attachment"] = message.is_multipart()
    s = Settings(_env_file=None, smtp_host="smtp.test", smtp_user="u", smtp_password="p",
                 smtp_from_addr="me@test.com")
    ok = await send_cv_email("hr@x.com", "Application: RF Engineer", "Hello",
                             str(pdf), s, sender=fake_sender)
    assert ok is True
    assert captured["to"] == "hr@x.com"
    assert captured["has_attachment"] is True


@pytest.mark.asyncio
async def test_noop_without_smtp_host(tmp_path):
    s = Settings(_env_file=None, smtp_host="")
    ok = await send_cv_email("hr@x.com", "s", "b", None, s, sender=None)
    assert ok is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_email_sender.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `submitters/email_sender.py`**

```python
"""Send the CV by email via SMTP."""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


async def _default_sender(message, host, port, username, password, start_tls):
    import aiosmtplib  # noqa: PLC0415
    await aiosmtplib.send(message, hostname=host, port=port, username=username or None,
                          password=password or None, start_tls=start_tls)


async def send_cv_email(to_addr, subject, body, pdf_path, settings, sender=None) -> bool:
    if not settings.smtp_host:
        logger.info("smtp_not_configured")
        return False
    msg = EmailMessage()
    msg["From"] = settings.smtp_from_addr or settings.smtp_user
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    if pdf_path and Path(pdf_path).exists():
        msg.add_attachment(Path(pdf_path).read_bytes(), maintype="application",
                           subtype="pdf", filename="CV.pdf")
    send = sender or _default_sender
    try:
        await send(msg, settings.smtp_host, settings.smtp_port,
                   settings.smtp_user, settings.smtp_password, True)
        logger.info("cv_email_sent", to=to_addr)
        return True
    except Exception as exc:
        logger.error("cv_email_failed", to=to_addr, error=str(exc))
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_email_sender.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add submitters/email_sender.py pyproject.toml tests/test_email_sender.py
git commit -m "feat: SMTP CV email sender"
```

### Task 5.4: Bridge send endpoint + Python client

**Files:**
- Modify: `bridge/whatsapp_bridge.js` (add a localhost HTTP send endpoint + text-post forwarding)
- Create: `worker/bridge_client.py`
- Test: `tests/test_bridge_client.py`

**Interfaces:**
- Bridge (JS): start an Express (or `http`) server on `127.0.0.1:8100` with `POST /send {to, text, pdf_base64?}` guarded by the `JOB_AGENT_TOKEN` bearer; calls `client.sendMessage(chatId, media_or_text)`. Also forward group **text** messages (no URL) that pass a keyword check to the agent's `/api/ingest-text` endpoint.
- Python: `async def bridge_send(to: str, text: str, pdf_path: str | None, settings, http=None) -> bool` posts to `settings.bridge_send_url`; `http` injectable for tests.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bridge_client.py
import pytest
from core.config import Settings
from worker.bridge_client import bridge_send


@pytest.mark.asyncio
async def test_bridge_send_posts(tmp_path):
    pdf = tmp_path / "cv.pdf"; pdf.write_bytes(b"%PDF x")
    sent = {}
    class _Resp:
        status_code = 200
    class _HTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            sent["url"] = url; sent["to"] = json["to"]; sent["has_pdf"] = bool(json.get("pdf_base64"))
            return _Resp()
    s = Settings(_env_file=None, bridge_send_url="http://localhost:8100/send")
    ok = await bridge_send("971500000000", "Hi", str(pdf), s, http=_HTTP())
    assert ok is True
    assert sent["to"] == "971500000000"
    assert sent["has_pdf"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_bridge_client.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `worker/bridge_client.py`**

```python
"""HTTP client to the WhatsApp bridge send endpoint."""

from __future__ import annotations

import base64
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


async def bridge_send(to: str, text: str, pdf_path: str | None, settings, http=None) -> bool:
    payload = {"to": to, "text": text}
    if pdf_path and Path(pdf_path).exists():
        payload["pdf_base64"] = base64.b64encode(Path(pdf_path).read_bytes()).decode()
    headers = {"Authorization": f"Bearer {settings.secret_key}"}
    try:
        if http is None:
            import httpx  # noqa: PLC0415
            http = httpx.AsyncClient(timeout=30.0)
        async with http as client:
            resp = await client.post(settings.bridge_send_url, json=payload, headers=headers)
            ok = 200 <= resp.status_code < 300
            logger.info("bridge_send", to=to, ok=ok)
            return ok
    except Exception as exc:
        logger.error("bridge_send_failed", to=to, error=str(exc))
        return False
```

- [ ] **Step 4: Implement the bridge JS side** — add the `POST /send` server + text forwarding as described in Interfaces. Keep it behind config flags (`ENABLE_SEND=true`, `FORWARD_TEXT_POSTS=true`).

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_bridge_client.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add worker/bridge_client.py bridge/whatsapp_bridge.js tests/test_bridge_client.py
git commit -m "feat: WhatsApp bridge send endpoint + Python client"
```

### Task 5.5: Outbound applier task (orchestration)

**Files:**
- Create: `worker/outbound.py`
- Modify: `api/routes/webhook.py` (route text posts to outbound), add `POST /api/ingest-text`
- Test: `tests/test_outbound_task.py`

**Interfaces:**
- Consumes: `parse_text_post`, `score_job`/`decide_action`, `generate_recruiter_message`, `bridge_send`, `send_cv_email`, `outbound_dedup`, `get_governor`.
- Produces `async def process_text_post(text, db, settings, profile, governor, deps) -> str` — returns one of `"not_job" | "low_score" | "duplicate" | "capped" | "sent_whatsapp" | "sent_email" | "no_contact"`. `deps` bundles injectable senders for tests. Enforces `wa_outbound_daily_cap` via a governor day-counter (`wa:out:<date>`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_outbound_task.py
import pytest
from datetime import datetime
from types import SimpleNamespace
from profile.models import UserProfile


@pytest.mark.asyncio
async def test_sends_whatsapp_when_phone_present(monkeypatch, tmp_path):
    from worker import outbound
    calls = {}

    async def fake_parse(text, client=None):
        return outbound.ParsedPost(is_job=True, title="RF Engineer", company="X",
                                   description="5g", contact_phone="+971500000000",
                                   contact_email="")
    async def fake_bridge(to, text, pdf, settings, http=None):
        calls["wa"] = to; return True
    async def fake_email(*a, **k): return False

    prof = UserProfile(); prof.preferences.roles = ["RF Engineer"]; prof.resume.pdf_path = ""
    deps = SimpleNamespace(parse=fake_parse, bridge=fake_bridge, email=fake_email,
                           gen_msg=_gen, now=datetime(2026,7,20,12,0,0))

    class _Gov:
        def can_act(self): return (True, "ok")
        def wa_remaining(self): return 5
        def wa_record(self): calls["rec"] = True

    r = await outbound.process_text_post("Hiring RF Engineer +971500000000",
                                         db=_FakeDB(), settings=_settings(),
                                         profile=prof, governor=_Gov(), deps=deps)
    assert r == "sent_whatsapp"
    assert calls["wa"] == "+971500000000"


async def _gen(job, profile, client=None):
    return "Hello, I'm interested."
```

(Provide `_FakeDB` and `_settings` helpers in the test file: `_FakeDB` supports `can_contact`/`record_contact` via the real functions against an in-memory SQLite bound session; `_settings` = `Settings(_env_file=None, wa_outbound_daily_cap=15)`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_outbound_task.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `worker/outbound.py`**

Implement `process_text_post` with the resolution order: parse → `is_job?` (`not_job`) → build `JobData`, score, `decide_action(..., min_apply_score=settings.min_apply_score)`; below gate → `low_score`; governor `can_act` false or `wa_remaining()<=0` → `capped`; pick channel (phone → whatsapp via `deps.bridge`; else email via `deps.email`; neither → `no_contact`); dedup via `can_contact`; on send success `record_contact` + `governor.wa_record()` and return `sent_whatsapp`/`sent_email`. Re-export `ParsedPost` from `ingestion.text_post_parser`.

Also add these two methods to `RateGovernor` in `core/governor.py` (day-scoped `wa:out:<date>` counter against `wa_outbound_daily_cap`):

```python
    def _wa_key(self) -> str:
        return f"wa:out:{self._now().strftime('%Y%m%d')}"

    def wa_remaining(self) -> int:
        raw = self.store.get(self._wa_key())
        used = int(raw) if raw else 0
        return max(0, self.s.wa_outbound_daily_cap - used)

    def wa_record(self) -> None:
        self.store.incr(self._wa_key())
```

Add this test to `tests/test_governor.py`:

```python
def test_wa_outbound_cap():
    gov, _ = _gov(wa_outbound_daily_cap=1)
    assert gov.wa_remaining() == 1
    gov.wa_record()
    assert gov.wa_remaining() == 0
```

- [ ] **Step 4: Add `/api/ingest-text` and webhook routing**

`POST /api/ingest-text {text}` → enqueue/await `process_text_post`. In `receive_message`, when a text message has no URLs but `looks_like_job(text_body)`, route to the same path.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_outbound_task.py tests/test_governor.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add worker/outbound.py core/governor.py api/routes/webhook.py tests/test_outbound_task.py tests/test_governor.py
git commit -m "feat: WhatsApp/email outbound applier orchestration with caps + dedup"
```

---

# Phase 6 — Observability & Polish

Goal: daily digest, dashboard panels, docker-compose beat service, docs.

### Task 6.1: Daily digest task

**Files:**
- Create: `worker/digest.py`
- Modify: `worker/celery_app.py` (beat entry `daily-digest`, e.g. 20:00)
- Test: `tests/test_digest.py`

**Interfaces:**
- Produces `def build_digest(db, day) -> DigestSummary` (pure over DB rows) with counts: `applied`, `needs_review`, `failed`, `outbound_sent`; and `format_digest(summary) -> str`. A `@shared_task send_daily_digest_task()` composes it and sends via `_send_whatsapp_message` to `settings.allowed_sender_list[0]` if present.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_digest.py
from worker.digest import format_digest, DigestSummary


def test_format_digest_readable():
    s = DigestSummary(applied=12, needs_review=3, failed=1, outbound_sent=4)
    text = format_digest(s)
    assert "12" in text and "3" in text and "Needs review" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_digest.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `worker/digest.py`**

```python
"""Daily digest of applications + outbound activity."""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from celery import shared_task

logger = structlog.get_logger(__name__)


@dataclass
class DigestSummary:
    applied: int = 0
    needs_review: int = 0
    failed: int = 0
    outbound_sent: int = 0


def build_digest(db, day) -> DigestSummary:
    from sqlalchemy import func  # noqa: PLC0415
    from db.models import Job, JobStatus, OutboundContact  # noqa: PLC0415
    def _count(status):
        return db.query(func.count(Job.id)).filter(
            Job.status == status, func.date(Job.created_at) == day).scalar() or 0
    outbound = db.query(func.count(OutboundContact.id)).filter(
        func.date(OutboundContact.last_contacted_at) == day).scalar() or 0
    return DigestSummary(
        applied=_count(JobStatus.SUBMITTED),
        needs_review=_count(JobStatus.NEEDS_REVIEW),
        failed=_count(JobStatus.FAILED),
        outbound_sent=outbound,
    )


def format_digest(s: DigestSummary) -> str:
    return (f"📊 *Daily Job Agent Digest*\n"
            f"✅ Applied: {s.applied}\n"
            f"⚠️ Needs review: {s.needs_review}\n"
            f"❌ Failed: {s.failed}\n"
            f"📨 Outbound sent: {s.outbound_sent}")


@shared_task(name="worker.digest.send_daily_digest_task")
def send_daily_digest_task() -> str:
    from datetime import datetime  # noqa: PLC0415
    from core.config import get_settings  # noqa: PLC0415
    from core.utils import run_async  # noqa: PLC0415
    from db.session import get_session_factory  # noqa: PLC0415
    from api.routes.webhook import _send_whatsapp_message  # noqa: PLC0415

    settings = get_settings()
    db = get_session_factory()()
    try:
        summary = build_digest(db, datetime.utcnow().date())
        text = format_digest(summary)
        if settings.allowed_sender_list:
            run_async(_send_whatsapp_message(settings.allowed_sender_list[0], text, settings))
        return text
    finally:
        db.close()
```

- [ ] **Step 4: Add beat entry** in `worker/celery_app.py`:

```python
    from celery.schedules import crontab
    app.conf.beat_schedule["daily-digest"] = {
        "task": "worker.digest.send_daily_digest_task",
        "schedule": crontab(hour=20, minute=0),
    }
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_digest.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add worker/digest.py worker/celery_app.py tests/test_digest.py
git commit -m "feat: daily WhatsApp digest of pipeline activity"
```

### Task 6.2: Dashboard panels (status, budget, needs-review, outbound)

**Files:**
- Create: `api/routes/control.py` additions — `GET /api/control/overview` returning governor status + digest-style counts + needs-review list.
- Modify: `api/templates/index.html`, `api/static/js/app.js`, `api/static/css/style.css`
- Test: `tests/test_overview_api.py`

**Interfaces:**
- Produces `GET /api/control/overview` → `{governor: {...}, counts: {...}, needs_review: [{job_id, title, reason}]}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_overview_api.py
from fastapi.testclient import TestClient
from api.main import app

client = TestClient(app)


def _auth():
    from core.config import get_settings
    return {"Authorization": f"Bearer {get_settings().secret_key}"}


def test_overview_shape():
    r = client.get("/api/control/overview", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert "governor" in body and "counts" in body and "needs_review" in body
    assert "remaining" in body["governor"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_overview_api.py -v`
Expected: FAIL — route missing.

- [ ] **Step 3: Add the `overview` endpoint to `api/routes/control.py`**

```python
@router.get("/overview")
async def overview(db: Session = Depends(get_db)):
    from datetime import datetime
    from worker.digest import build_digest
    from db.models import Application, Job, JobStatus
    gov = get_governor().status()
    summary = build_digest(db, datetime.utcnow().date())
    rows = (db.query(Application, Job)
              .join(Job, Application.job_id == Job.id)
              .filter(Application.status == JobStatus.NEEDS_REVIEW)
              .limit(50).all())
    needs = [{"job_id": j.id, "title": j.title, "reason": a.needs_review_reason}
             for a, j in rows]
    return {"governor": gov,
            "counts": {"applied": summary.applied, "needs_review": summary.needs_review,
                       "failed": summary.failed, "outbound_sent": summary.outbound_sent},
            "needs_review": needs}
```

(Add the needed imports `Session`, `Depends`, `get_db` at the top of `control.py`.)

- [ ] **Step 4: Add the UI panels** — a governor gauge (remaining/cap, killed badge, cooldown timer), a counts row, a needs-review table, and Kill/Resume buttons wired to `/api/control/kill|resume`. Follow existing `app.js` fetch/render patterns.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_overview_api.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add api/routes/control.py api/templates/index.html api/static/js/app.js api/static/css/style.css tests/test_overview_api.py
git commit -m "feat: dashboard overview panel (budget, counts, needs-review, kill switch)"
```

### Task 6.3: docker-compose beat service + README FULL_AUTO section

**Files:**
- Modify: `docker-compose.yml` (add a `celery-beat` service + a `discovery` worker queue), `README.md`
- Test: `tests/test_docs_smoke.py`

**Interfaces:**
- Produces: a `celery-beat` compose service running `celery -A worker.celery_app beat`, a worker consuming the `discovery`, `submission`, `llm`, `processing`, `ingestion` queues; README documents the FULL_AUTO preset, one-time `python -m discovery.login`, and the account-safety caveats.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_docs_smoke.py
from pathlib import Path
import yaml


def test_compose_has_beat_service():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    assert "celery-beat" in compose["services"]


def test_readme_documents_full_auto():
    txt = Path("README.md").read_text(encoding="utf-8").lower()
    assert "full_auto" in txt or "full-auto" in txt
    assert "discovery.login" in txt
    assert "min_apply_score" in txt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_docs_smoke.py -v`
Expected: FAIL.

- [ ] **Step 3: Add the `celery-beat` service to `docker-compose.yml`**

Mirror the existing `celery-worker` service; change the command to `celery -A worker.celery_app beat --loglevel=info`; depends_on redis. Ensure the worker command consumes all queues: `celery -A worker.celery_app worker -Q ingestion,processing,llm,submission,discovery --loglevel=info`.

- [ ] **Step 4: Add a README "Full-Auto Mode" section**

Document: the FULL_AUTO env preset (`DRAFT_ONLY=false`, `AUTO_APPLY=true`, `MIN_APPLY_SCORE=40`, `TASKS_ALWAYS_EAGER=false`), the one-time `python -m discovery.login`, uploading a CV (dashboard/WhatsApp), the governor caps + kill switch, and an explicit **account-risk warning** for LinkedIn and unofficial WhatsApp automation.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_docs_smoke.py -v`
Expected: PASS.

- [ ] **Step 6: Full suite + commit**

```bash
pytest tests/ -v
git add docker-compose.yml README.md tests/test_docs_smoke.py
git commit -m "docs: FULL_AUTO preset + celery-beat compose service"
```

---

## Final verification

- [ ] Run the entire suite: `pytest tests/ -v` — all green (existing 76 + new).
- [ ] Apply migrations against a scratch DB: `alembic upgrade head` succeeds; `alembic downgrade -1` reverts the v2 tables cleanly.
- [ ] `python -c "from core.config import get_settings; get_settings()"` — settings load.
- [ ] Smoke `DRY_RUN=true`: with a logged-in session, one Easy Apply flow walks to the final step and discards without submitting (manual check, documented in README).

## Notes for the executor

- The spec's "ProfileVersion" is realized by the **existing** `UserProfileVersion` table — do not create a new one.
- Redis on native Windows is awkward; prefer running the worker/beat/redis via `docker-compose`. `TASKS_ALWAYS_EAGER=true` remains valid for local dev without Redis, but discovery/beat require a real broker.
- Never commit `.linkedin_profile/`, `bridge/.wwebjs_auth/`, `resume.pdf`, or `user_profile.yaml` with real data. Confirm they are in `.gitignore` before the first commit that could include them.
