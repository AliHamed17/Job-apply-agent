# P0 — Profile Truth and Live Liabilities Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the four verified defects that make every downstream submission gate refuse for reasons unrelated to the ATS adapter, and make the operator's real identity readable by the code that actually reads it.

**Architecture:** Five independent code changes plus one operator-executed data bootstrap. Nothing here touches an ATS adapter, spends a real application, or relaxes a safety invariant. Each task is independently revertible.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy, Pydantic v2, Celery, pytest, ruff.

**Spec:** [`2026-08-03-earned-autonomy-auto-apply-design.md`](../specs/2026-08-03-earned-autonomy-auto-apply-design.md) §6, §7 (P0).

## Global Constraints

- **Never** introduce an LLM call on any sensitive-field path. Tests assert zero provider calls.
- **Never** store, read, or type a credential. No new `*_password` setting may be added.
- Legal and demographic facts live **only** in `evidence.user_confirmed`, written **only** through `PUT /api/profile/onboarding`.
- `ruff` runs before tests in CI; a lint failure masks every downstream test result. Run `ruff check .` and `ruff format --check .` before every commit.
- Existing test files use the `auth_headers` fixture for authenticated API calls. Any new API test omitting it will 401 under CI's real `SECRET_KEY`.
- Run the full suite (`pytest -q`) before the final commit of each task, not just the new test.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `core/governor.py` | Evaluate active hours in the configured local timezone | 1 |
| `core/config.py` | Own `active_hours`, `active_hours_timezone`; lose all Indeed credentials | 1, 2 |
| `submitters/indeed.py` | **Deleted** — the only password-typing submitter | 2 |
| `worker/tasks.py` | Lose the unreachable v3 block that constructs `IndeedSubmitter(password=…)` | 2 |
| `api/routes/profile.py` | Accept and persist the full confirmed-fact set | 3 |
| `jobs/parsers/israeli_boards.py` | Return `[]`/`None` for challenge, error and soft-404 pages | 4 |
| `worker/autopilot.py` | Stop shadowing the qualification-aware descriptor resolver | 5 |
| `core/application_audit.py` | Allow `qualified_autopilot` as an audit actor | 5 |

---

## Task 1: Governor active hours in Asia/Jerusalem

**Why:** `within_active_hours` compares `datetime.now(UTC).hour` against `ACTIVE_HOURS="09:00-21:00"`, while the signed autopilot policy uses Asia/Jerusalem 08:00–21:00. The effective send window is ~12:00–24:00 Israel time: it blocks the operator's entire working morning, permits midnight sends, and disagrees with the policy, so a policy-allowed decision still fails `GOVERNOR_DENIED` at the commit boundary. Every dry run attempted during a working morning fails for a reason indistinguishable from a broken adapter.

**Files:**
- Modify: `core/config.py` (near line 181, `active_hours`)
- Modify: `core/governor.py:102-104` (`within_active_hours`)
- Test: `tests/test_governor.py`

**Interfaces:**
- Consumes: `Settings.active_hours_range() -> tuple[int, int]` (exists, unchanged).
- Produces: `Settings.active_hours_timezone: str` (default `"Asia/Jerusalem"`), consumed by `RateGovernor.within_active_hours`.

- [ ] **Step 1: Write the failing test**

In `tests/test_governor.py`:

```python
from datetime import UTC, datetime

from core.governor import RateGovernor


def test_active_hours_evaluated_in_configured_timezone(settings_factory, fake_store):
    """09:00 UTC is 12:00 in Jerusalem; 06:00 UTC is 09:00 — both inside 08:00-21:00.

    Before the fix, 06:00 UTC compared 6 against (8, 21) and refused, which
    blocked the operator's whole working morning.
    """
    s = settings_factory(active_hours="08:00-21:00", active_hours_timezone="Asia/Jerusalem")

    morning = RateGovernor(s, fake_store, now_fn=lambda: datetime(2026, 8, 3, 6, 0, tzinfo=UTC))
    assert morning.within_active_hours() is True

    midnight = RateGovernor(s, fake_store, now_fn=lambda: datetime(2026, 8, 3, 21, 30, tzinfo=UTC))
    assert midnight.within_active_hours() is False


def test_active_hours_treats_naive_now_as_utc(settings_factory, fake_store):
    s = settings_factory(active_hours="08:00-21:00", active_hours_timezone="Asia/Jerusalem")
    g = RateGovernor(s, fake_store, now_fn=lambda: datetime(2026, 8, 3, 6, 0))
    assert g.within_active_hours() is True
```

If `settings_factory` / `fake_store` fixtures do not exist in `tests/test_governor.py`, read the top of that file and construct `Settings` and the store exactly the way the existing tests there do — do not invent a fixture.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_governor.py -k active_hours -v`
Expected: FAIL — `test_active_hours_evaluated_in_configured_timezone` asserts `True` but gets `False` (6 is not in `[8, 21)`), and `settings_factory` rejects the unknown `active_hours_timezone` field.

- [ ] **Step 3: Add the setting**

In `core/config.py`, beside `active_hours`:

```python
    active_hours: str = "08:00-21:00"
    active_hours_timezone: str = "Asia/Jerusalem"
```

Note the default value change from `"09:00-21:00"` to `"08:00-21:00"`, which aligns the governor with the signed policy window.

- [ ] **Step 4: Evaluate the hour in that timezone**

In `core/governor.py`, add to the imports:

```python
from zoneinfo import ZoneInfo
```

Replace `within_active_hours`:

```python
    # ── active hours ──────────────────────────────────
    def within_active_hours(self) -> bool:
        """Compare the wall-clock hour in the operator's timezone, not UTC.

        The signed autopilot policy expresses its window in Asia/Jerusalem, so
        evaluating it in UTC both refuses policy-allowed sends and permits
        sends the policy forbids.
        """
        start, end = self.s.active_hours_range()
        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        try:
            local = now.astimezone(ZoneInfo(self.s.active_hours_timezone))
        except Exception:
            local = now.astimezone(UTC)
        return start <= local.hour < end
```

The `except` clause keeps a mistyped timezone from bricking every send; it degrades to the previous UTC behaviour rather than raising inside `can_act()`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_governor.py -v`
Expected: PASS, including the pre-existing governor tests.

- [ ] **Step 6: Update `.env.example`**

Change the `ACTIVE_HOURS` line to `ACTIVE_HOURS=08:00-21:00` and add below it:

```
# Timezone the active-hours window is evaluated in. Must match the signed
# autopilot policy window, or a policy-allowed send fails GOVERNOR_DENIED.
ACTIVE_HOURS_TIMEZONE=Asia/Jerusalem
```

- [ ] **Step 7: Lint, full suite, commit**

```bash
ruff check . && ruff format --check . && pytest -q
git add core/config.py core/governor.py tests/test_governor.py .env.example
git commit -m "fix(governor): evaluate active hours in the configured timezone"
```

---

## Task 2: Delete the Indeed password path

**Why:** `submitters/indeed.py` reads `INDEED_PASSWORD` from settings and types it into a password field. It is unreachable only because `submit_application_task` returns early; ~140 lines below that `return`, dead v3 code still constructs `IndeedSubmitter(cookies_file=…, email=…, password=…)`. A single `return` statement is the whole barrier between the repository and a hard-constraint violation. Delete the capability, not the reachability.

**Files:**
- Delete: `submitters/indeed.py`
- Modify: `worker/tasks.py` (remove the unreachable block after the `return` in `submit_application_task`)
- Modify: `core/config.py:159-164` (remove `indeed_cookies_file`, `indeed_email`, `indeed_password`)
- Modify: `.env.example`
- Test: `tests/test_adversarial.py`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing. This task only removes. `submitters/platforms.py:236` keeps its `platform="indeed"` **descriptor** — that is routing/identity metadata with no import of the deleted module, and removing it would change adapter identity digests for no safety gain.

- [ ] **Step 1: Write the failing test**

In `tests/test_adversarial.py`:

```python
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_no_submitter_reads_a_password():
    """A submitter that can type a password is a hard-constraint violation.

    Login is a persistent browser profile: the operator signs in manually once
    and session cookies persist. No code path may hold a credential.
    """
    offenders = []
    for path in (REPO_ROOT / "submitters").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\bsettings\.\w*password\w*|\bINDEED_PASSWORD\b|getenv\(\s*['\"][A-Z_]*PASSWORD", text):
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(0)}")
    assert offenders == [], f"submitters must never read a credential: {offenders}"


def test_settings_expose_no_indeed_credentials():
    from core.config import Settings

    fields = set(Settings.model_fields)
    assert not {f for f in fields if "indeed" in f}, "Indeed credentials must be gone"
```

Note this deliberately does not match `submitters/email_sender.py`'s `smtp_password`, which is an outbound-notification transport, not a candidate credential typed into an employer form. If the regex catches it, narrow the glob to exclude `email_sender.py` and say why in a comment — do not weaken the pattern.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_adversarial.py -k password -v`
Expected: FAIL — offenders lists `submitters/indeed.py`, and `Settings` still exposes three `indeed_*` fields.

- [ ] **Step 3: Delete the submitter**

```bash
git rm submitters/indeed.py
```

- [ ] **Step 4: Delete the unreachable v3 block**

In `worker/tasks.py`, `submit_application_task` currently ends with:

```python
    return {
        "state": "blocked",
        "reason_code": "DATABASE_COMMAND_REQUIRED",
    }

    # Unreachable v3 implementation retained for one release as migration
    # context. It is removed after old broker messages have expired.
    from profile.loader import get_profile
    ...
```

Delete everything from the `# Unreachable v3 implementation` comment to the end of the function body, so the function ends at that `return`. Read the file to find the exact end of the function (the next top-level `@shared_task` or `def` at module indentation) and remove only up to it — do not delete the following task.

- [ ] **Step 5: Remove the settings**

In `core/config.py`, delete the three lines `indeed_cookies_file`, `indeed_email`, `indeed_password`, and narrow the section comment at line 156 from `# ── Browser-automation credentials (LinkedIn / Indeed) ──` to `# ── Browser-automation session settings (LinkedIn) ──`.

- [ ] **Step 6: Remove from `.env.example`**

Delete any `INDEED_COOKIES_FILE`, `INDEED_EMAIL`, `INDEED_PASSWORD` lines.

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_adversarial.py -k password -v`
Expected: PASS.

Then: `pytest -q`
Expected: PASS. If any test imports `submitters.indeed` or asserts on the deleted v3 block, delete that test — it covers removed behaviour. Do **not** re-add the module to satisfy a test.

- [ ] **Step 8: Lint and commit**

```bash
ruff check . && ruff format --check . && pytest -q
git add -A
git commit -m "fix(submitters): delete the Indeed password path and dead v3 block"
```

---

## Task 3: Extend the confirmed-fact set

**Why:** ATS forms repeatedly ask facts the schema cannot hold, so every such field abstains forever and the operator hand-confirms the same answers on every application. `OnboardingProfileUpdate` is the only writer permitted to persist them, and it currently models eight keys.

**Files:**
- Modify: `api/routes/profile.py:33-70` (`OnboardingProfileUpdate`) and the write path at `:190-235`
- Modify: `user_profile.yaml.example`
- Modify: `docs/employer-automation.md` (its documented key list includes keys the resolver never looks up)
- Test: `tests/test_profile_onboarding.py` (create if absent)

**Interfaces:**
- Consumes: `ProfileEvidence.user_confirmed: dict[str, str]`, `profile_write_transaction`, `load_profile_snapshot` (all exist).
- Produces: `evidence.user_confirmed` keys, jurisdiction-suffixed for legal facts: `work_authorization:il`, `work_authorization:us`, `visa_sponsorship:il`, `visa_sponsorship:us`; plain keys for the rest: `security_clearance`, `notice_period`, `salary_expectation`, `salary_currency`, `availability_date`, `years_experience`, `years_experience_number`, `languages`, `relocation`, `work_mode`, `highest_degree`, `how_did_you_hear`, `demographic_disclosure`. Task 1.4 of P1 (`core/answer_slots.py`) resolves form labels **to** these keys; this task only makes them writable.

**Jurisdiction keying is the point, not a detail.** `work_authorization` as a single flat key would let an Israeli authorization fact answer *"Are you legally authorized to work in the United States?"* — a false legal claim on a real application. Today that question abstains, and the abstention is the only thing preventing the claim. Suffixed keys preserve the abstention.

- [ ] **Step 1: Write the failing test**

Create `tests/test_profile_onboarding.py`:

```python
def test_onboarding_persists_jurisdiction_keyed_legal_facts(client, auth_headers):
    payload = {
        "legal_name": "Test Operator",
        "primary_email": "operator@example.com",
        "phone": "+972500000000",
        "location": "Tel Aviv, Israel",
        "search_locations": ["Tel Aviv, Israel", "Remote"],
        "work_authorization_il": "Israeli citizen, authorized without sponsorship",
        "sponsorship_il": "No sponsorship required",
        "notice_period": "30 days",
        "salary_expectation": "35000",
        "salary_currency": "ILS",
        "years_experience": "2",
        "availability_date": "2026-09-01",
        "relocation": "No",
        "work_mode": "Hybrid",
        "highest_degree": "M.Sc. Information Systems",
        "languages": "Hebrew (native), English (fluent), Arabic (native)",
        "demographic_disclosure": "decline",
    }
    r = client.put("/api/profile/onboarding", json=payload, headers=auth_headers)
    assert r.status_code == 200, r.text

    r2 = client.get("/api/profile/onboarding", headers=auth_headers)
    confirmed = r2.json()["confirmed"]
    assert confirmed["work_authorization:il"] == "Israeli citizen, authorized without sponsorship"
    assert "work_authorization:us" not in confirmed
    assert confirmed["notice_period"] == "30 days"
    assert confirmed["years_experience"] == "2 years"
    assert confirmed["years_experience_number"] == "2"


def test_onboarding_rejects_unknown_keys(client, auth_headers):
    r = client.put(
        "/api/profile/onboarding",
        json={"legal_name": "X", "not_a_field": "y"},
        headers=auth_headers,
    )
    assert r.status_code == 422
```

Read `tests/` for the exact `client` / `auth_headers` fixture names before writing; reuse them, do not define new ones.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_profile_onboarding.py -v`
Expected: FAIL — 422, because `extra="forbid"` rejects `work_authorization_il`, `notice_period`, and the rest.

- [ ] **Step 3: Extend the model**

In `api/routes/profile.py`, in `OnboardingProfileUpdate`, replace the two flat legal fields with jurisdiction-scoped ones and add the recurring facts. Keep `extra="forbid"`, keep both existing `field_validator("*")` validators (they apply to new fields automatically):

```python
    # Legal facts are jurisdiction-scoped: an Israeli authorization fact must
    # never answer a United States authorization question. A jurisdiction with
    # no confirmed fact abstains, which is the safe outcome.
    work_authorization_il: str = Field(default="", max_length=300)
    work_authorization_us: str = Field(default="", max_length=300)
    sponsorship_il: str = Field(default="", max_length=300)
    sponsorship_us: str = Field(default="", max_length=300)

    security_clearance: str = Field(default="", max_length=200)
    notice_period: str = Field(default="", max_length=100)
    salary_expectation: str = Field(default="", max_length=50)
    salary_currency: str = Field(default="", max_length=10)
    availability_date: str = Field(default="", max_length=40)
    years_experience: str = Field(default="", max_length=10)
    languages: str = Field(default="", max_length=300)
    relocation: str = Field(default="", max_length=100)
    work_mode: str = Field(default="", max_length=100)
    highest_degree: str = Field(default="", max_length=200)
    how_did_you_hear: str = Field(default="", max_length=200)
    demographic_disclosure: str = Field(default="", max_length=20)
```

Then relax the four now-optional identity fields' `min_length` only if the existing tests require it — read them first. Keep `legal_name`, `primary_email`, `phone`, `location`, `search_locations` exactly as they are.

Add a validator pinning the disclosure vocabulary:

```python
    @field_validator("demographic_disclosure")
    @classmethod
    def validate_disclosure(cls, value: str) -> str:
        if value and value not in {"decline", "disclose"}:
            raise ValueError("demographic_disclosure must be 'decline' or 'disclose'")
        return value
```

- [ ] **Step 4: Persist them**

In `update_onboarding_profile`, replace the `confirmed.update({...})` block. Empty strings must **remove** a key rather than store an empty fact, so an unset jurisdiction abstains instead of resolving to `""`:

```python
            confirmed = dict(evidence.user_confirmed)
            # Legal facts, jurisdiction-suffixed.
            for field_name, key in (
                ("work_authorization_il", "work_authorization:il"),
                ("work_authorization_us", "work_authorization:us"),
                ("sponsorship_il", "visa_sponsorship:il"),
                ("sponsorship_us", "visa_sponsorship:us"),
            ):
                value = getattr(payload, field_name)
                if value:
                    confirmed[key] = value
                else:
                    confirmed.pop(key, None)

            # Recurring non-legal facts.
            for key in (
                "security_clearance",
                "notice_period",
                "salary_expectation",
                "salary_currency",
                "availability_date",
                "languages",
                "relocation",
                "work_mode",
                "highest_degree",
                "how_did_you_hear",
                "demographic_disclosure",
            ):
                value = getattr(payload, key)
                if value:
                    confirmed[key] = value
                else:
                    confirmed.pop(key, None)

            # One operator input, two shapes: free-text forms want "2 years",
            # NUMBER controls want "2". Deriving both server-side keeps them
            # from drifting apart.
            if payload.years_experience:
                number = payload.years_experience.strip()
                confirmed["years_experience_number"] = number
                confirmed["years_experience"] = f"{number} years"
            else:
                confirmed.pop("years_experience_number", None)
                confirmed.pop("years_experience", None)
```

Leave the existing `citizenship` / `nationality` / `gender` / `disability` / `ethnicity` / `veteran_status` loop exactly as it is.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_profile_onboarding.py -v`
Expected: PASS. Then `pytest tests/ -k profile -v` — expected PASS; if a pre-existing test posts the old `work_authorization` / `sponsorship` keys, update it to the new field names.

- [ ] **Step 6: Update the example and the docs**

In `user_profile.yaml.example`, under `evidence.user_confirmed`, replace the flat legal keys with the jurisdiction-suffixed set and add the recurring keys, each with an empty string and a comment that empty means abstain.

In `docs/employer-automation.md`, correct the documented key list (lines ~86-104) to exactly the keys this task writes — it currently documents keys no resolver looks up.

- [ ] **Step 7: Lint, full suite, commit**

```bash
ruff check . && ruff format --check . && pytest -q
git add api/routes/profile.py tests/test_profile_onboarding.py user_profile.yaml.example docs/employer-automation.md
git commit -m "feat(profile): jurisdiction-keyed legal facts and the recurring answer set"
```

---

## Task 4: Israeli parsers fail closed on challenge and error pages

**Why:** `_parse_detail` accepts any non-empty title, `_TITLE_SELECTORS` ends in bare `h1`/`h2`/`h3`, and `JobData.is_complete` requires only a title of 3+ characters that is not a template placeholder. A CAPTCHA interstitial therefore yields a confident `JobData(title="Are you a robot?")`, and a 403 or soft-404 yields one just as readily. Such a job is then scored, generated for, and applied to. This is the exact defect class the project was rebuilt to eliminate.

**Files:**
- Modify: `jobs/parsers/israeli_boards.py`
- Test: `tests/test_israeli_boards.py`

**Interfaces:**
- Consumes: `_soup`, `_clean`, `_first`, `_labelled_section`, `_TITLE_SELECTORS` (all exist in the module).
- Produces: `_page_is_unreadable(soup) -> bool`, module-private. `parse_israeli_board` keeps its signature `(html: str, source_url: str) -> list[JobData]`.

- [ ] **Step 1: Write the failing test**

In `tests/test_israeli_boards.py`:

```python
from jobs.parsers.israeli_boards import parse_israeli_board

URL = "https://www.drushim.co.il/job/12345/"


def test_captcha_page_yields_no_jobs():
    html = "<html><body><h1>Are you a robot?</h1><p>Please verify.</p></body></html>"
    assert parse_israeli_board(html, URL) == []


def test_hebrew_permission_error_yields_no_jobs():
    html = "<html><body><h1>אין הרשאה</h1></body></html>"
    assert parse_israeli_board(html, URL) == []


def test_soft_404_yields_no_jobs():
    html = "<html><body><h1>הדף לא נמצא</h1></body></html>"
    assert parse_israeli_board(html, URL) == []


def test_bare_heading_with_no_job_signal_yields_no_jobs():
    """A page whose only signal is a generic h1 is not a job posting."""
    html = "<html><body><h1>Drushim</h1><p>Welcome to our site.</p></body></html>"
    assert parse_israeli_board(html, URL) == []


def test_real_posting_still_parses():
    html = """
    <html><body>
      <h1 class="job-title">מהנדס תוכנה משובץ</h1>
      <div class="company-name">Parallel Wireless</div>
      <div class="job-location">פתח תקווה</div>
      <div>תיאור התפקיד: פיתוח קושחה בסביבת לינוקס.</div>
      <div>דרישות התפקיד: C, C++, לינוקס.</div>
    </body></html>
    """
    jobs = parse_israeli_board(html, URL)
    assert len(jobs) == 1
    assert jobs[0].title == "מהנדס תוכנה משובץ"
    assert jobs[0].company == "Parallel Wireless"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_israeli_boards.py -v`
Expected: the four negative tests FAIL (each returns one confident `JobData`); `test_real_posting_still_parses` PASSES.

- [ ] **Step 3: Add the unreadable-page precheck**

In `jobs/parsers/israeli_boards.py`, after `_clean`, add:

```python
# Challenge, authorization and not-found markers. A page carrying one of these
# is not a posting no matter what its headings say — a CAPTCHA interstitial has
# an h1, so title presence alone cannot distinguish it from a real job.
# English vocabulary mirrors submitters/workday.py's challenge detection.
_UNREADABLE_MARKERS = (
    "are you a robot",
    "verify you are human",
    "unusual traffic",
    "access denied",
    "forbidden",
    "page not found",
    "not authorized",
    "enable javascript",
    "captcha",
    "אין הרשאה",
    "הדף לא נמצא",
    "לא נמצאה",
    "אינך מורשה",
    "אימות",
)


def _page_is_unreadable(soup: BeautifulSoup) -> bool:
    """True when the page is a challenge, error or soft-404 rather than a job."""
    head = " ".join(
        _clean(node.get_text(" ", strip=True))
        for node in soup.select("title, h1, h2")
    ).lower()
    return any(marker in head for marker in _UNREADABLE_MARKERS)
```

Scanning only `title`/`h1`/`h2` rather than the whole body keeps a real posting that merely mentions one of these words in its description from being discarded.

- [ ] **Step 4: Split specific from fallback title selectors**

Replace `_TITLE_SELECTORS` with two tuples plus a compatibility alias, so existing importers keep working:

```python
# A specific selector is positive evidence that this page models a job. The
# bare-tag fallbacks match any page at all, so a title found only through them
# needs corroboration before the posting is trusted.
_SPECIFIC_TITLE_SELECTORS = (
    "h1.job-title",
    "h1[itemprop='title']",
    ".job-title-h1",
    ".jobTitle",
    "[data-testid='job-title']",
    ".job-title",
    "h2.job-title",
)
_FALLBACK_TITLE_SELECTORS = ("h1", "h2", "h3")
_TITLE_SELECTORS = _SPECIFIC_TITLE_SELECTORS + _FALLBACK_TITLE_SELECTORS
```

- [ ] **Step 5: Require corroboration for fallback-only titles**

In `_parse_detail`, replace the opening guard:

```python
def _parse_detail(soup: BeautifulSoup, source_url: str, board: str) -> JobData | None:
    if _page_is_unreadable(soup):
        logger.info("israeli_board_page_unreadable", board=board, source_url=source_url)
        return None

    title = _first(soup, _SPECIFIC_TITLE_SELECTORS)
    title_is_specific = bool(title)
    if not title:
        title = _first(soup, _FALLBACK_TITLE_SELECTORS)
    if not title:
        return None
```

Then compute the fields, and before constructing `JobData`, require corroboration when the title came only from a fallback:

```python
    company = _first(soup, _COMPANY_SELECTORS)
    location = _first(soup, _LOCATION_SELECTORS)
    description = _labelled_section(soup, _DESCRIPTION_LABELS)
    requirements = _labelled_section(soup, _REQUIREMENT_LABELS)

    # A generic heading with no company, no location and no labelled section is
    # a site chrome page, not a posting. Returning None loses nothing real and
    # prevents an invented job entering the pipeline.
    if not title_is_specific and not company and not location and not description and not requirements:
        logger.info("israeli_board_no_job_signal", board=board, source_url=source_url)
        return None
```

Keep the existing description fallbacks (`_DESCRIPTION_SELECTORS`, then `main`/`article`/`body`) **after** this guard, so page-body text cannot itself satisfy the corroboration check. Then build `JobData` from the already-computed `company`, `location`, `description`, `requirements` rather than re-querying.

- [ ] **Step 6: Apply the same precheck to the results-card path**

At the top of `parse_israeli_board`, after building the soup:

```python
    if _page_is_unreadable(soup):
        logger.info("israeli_board_page_unreadable", source_url=source_url)
        return []
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_israeli_boards.py -v`
Expected: PASS, all five.

Then `pytest -q`. If a pre-existing fixture asserts a job parses from markup that this guard now rejects, read that fixture: if it is a real posting, add a company or a labelled section to it; if it is chrome, update the expectation to `[]`.

- [ ] **Step 8: Lint and commit**

```bash
ruff check . && ruff format --check . && pytest -q
git add jobs/parsers/israeli_boards.py tests/test_israeli_boards.py
git commit -m "fix(parsers): israeli boards fail closed on challenge and error pages"
```

---

## Task 5: Stop shadowing the qualification-aware descriptor resolver

**Why:** `dispatch_qualified_autopilot` declares `descriptor_resolver: DescriptorResolver = adapter_for_url` and forwards it **unconditionally** into `create_submission_commands`, so `_create_one` never falls through to `effective_live_descriptor_for_plan` and every autopilot send raises `ADAPTER_NOT_QUALIFIED`. Note `session_checker` immediately below is forwarded *conditionally* — this is the same pattern applied inconsistently. Every existing test injects a resolver, so nothing covers the production default. Stage 4 could not have worked even with valid qualification evidence.

**Files:**
- Modify: `worker/autopilot.py` (signature near line 66; `create_kwargs` near line 118)
- Modify: `core/application_audit.py:25-31` (`_ALLOWED_ACTORS`)
- Test: `tests/test_v5_autopilot_policy.py`

**Interfaces:**
- Consumes: `create_submission_commands(db, requests, **kwargs)`, whose own default for `descriptor_resolver` is the qualification-aware path.
- Produces: `dispatch_qualified_autopilot(..., descriptor_resolver: DescriptorResolver | None = None, ...)`. Callers passing a resolver keep their behaviour; callers omitting it now reach qualification resolution.

- [ ] **Step 1: Write the failing test**

In `tests/test_v5_autopilot_policy.py`, mirroring an existing passing dispatch test but **omitting** the resolver injection:

```python
def test_dispatch_reaches_qualification_resolution_without_injected_resolver(
    db, qualified_autopilot_fixture
):
    """The production call site passes no resolver.

    Forwarding adapter_for_url unconditionally shadowed
    effective_live_descriptor_for_plan, so every real autopilot send raised
    ADAPTER_NOT_QUALIFIED while every test injected its way past the bug.
    """
    fx = qualified_autopilot_fixture
    result = dispatch_qualified_autopilot(
        db,
        application_id=fx.application_id,
        form_plan_id=fx.form_plan_id,
    )
    assert result.reason_code != "ADAPTER_NOT_QUALIFIED"
    assert result.state == "queued"
```

Read the existing tests in that file for the real fixture name and the exact setup that produces a live-canary-qualified descriptor; reuse it rather than inventing `qualified_autopilot_fixture`. If no such fixture exists, build the qualified state the same way the nearest passing test does, minus the `descriptor_resolver=` argument.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_v5_autopilot_policy.py -k without_injected_resolver -v`
Expected: FAIL — `state == "quarantined"`, `reason_code == "ADAPTER_NOT_QUALIFIED"`.

- [ ] **Step 3: Default to None and forward conditionally**

In `worker/autopilot.py`, change the signature:

```python
    descriptor_resolver: DescriptorResolver | None = None,
```

and in `create_kwargs`, remove `"descriptor_resolver": descriptor_resolver` from the dict literal, adding it beside the existing `session_checker` block:

```python
        create_kwargs = {
            "settings": resolved_settings,
            "capabilities": capabilities,
            "now": (
                timestamp.astimezone(UTC).replace(tzinfo=None)
                if timestamp.tzinfo is not None
                else timestamp
            ),
        }
        if descriptor_resolver is not None:
            create_kwargs["descriptor_resolver"] = descriptor_resolver
        if session_checker is not None:
            create_kwargs["session_checker"] = session_checker
```

If `adapter_for_url` becomes an unused import, remove it; if it is still used elsewhere in the module, leave it.

- [ ] **Step 4: Allow the audit actor**

In `core/application_audit.py`, add to `_ALLOWED_ACTORS`:

```python
_ALLOWED_ACTORS = {
    "operator",
    "batch_operator",
    "whatsapp_operator",
    "qualified_autopilot",
    "worker",
    "system",
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_v5_autopilot_policy.py -v`
Expected: PASS, including every pre-existing test (they inject a resolver, which is still honoured).

- [ ] **Step 6: Lint, full suite, commit**

```bash
ruff check . && ruff format --check . && pytest -q
git add worker/autopilot.py core/application_audit.py tests/test_v5_autopilot_policy.py
git commit -m "fix(autopilot): stop shadowing qualification-aware descriptor resolution"
```

---

## Task 6: Operator profile bootstrap — **requires the operator**

**Why:** `load_versioned_profile_snapshot` reads the `UserProfileVersion` table and falls back to YAML only when that table is empty, returning `version=None` — which cannot receive a final-submit permit. **Editing `user_profile.yaml` by hand accomplishes nothing.** Only `PUT /api/profile/onboarding` and `POST /api/profile/resume` mint a version.

**This task cannot be completed by an agent.** Its inputs are facts only the operator possesses, and inventing them is precisely the failure this project exists to prevent. The current file is unedited template data: `resume.text` reads `"JANE DOE … 5 years … TechCorp"`, `locations` are San Francisco and New York, `seniority` is mid/senior/lead, salary is 120–200k USD, `blacklist_companies` is `["SpamCorp", "BadCompanyInc"]`, and `attachments` point at a nonexistent `./resume.pdf`.

**Files:**
- Modify: `user_profile.yaml` (operator-owned, git-ignored)

- [ ] **Step 1: Upload the real CV**

```bash
curl -X POST http://localhost:8000/api/profile/resume \
  -H "Authorization: Bearer $API_TOKEN" \
  -F "file=@cvs/Ali_Hamed_CV_Software_Engineer.pdf"
```

This rebuilds `resume.text` and `attachments` from the real PDF, mints a profile version, and preserves `evidence.user_confirmed` verbatim.

- [ ] **Step 2: Submit the onboarding payload**

`PUT /api/profile/onboarding` with real values for every field added in Task 3. This is the only API able to write the identity block. The operator must supply: legal name, email, phone, location, search locations, and each confirmed fact — including which jurisdictions his work authorization actually covers.

- [ ] **Step 3: Hand-edit what neither route touches**

In `user_profile.yaml`, correct `preferences.seniority` (to entry/mid), `preferences.salary` (band and currency), `preferences.roles`, `preferences.keywords`, and empty `blacklist_companies`. Then re-run Step 2 to mint a version that includes them.

- [ ] **Step 4: Verify both conditions**

```bash
curl -s http://localhost:8000/api/automation/status -H "Authorization: Bearer $API_TOKEN" | grep -o 'PROFILE_[A-Z_]*' || echo "no PROFILE_* reason codes"
```

Expected: no `PROFILE_*` reason codes, and `latest_profile_version(db)` returns a non-`None` version ≥ 1.

---

## Exit Criteria

- [ ] `GET /api/automation/status` returns zero `PROFILE_*` reason codes
- [ ] `latest_profile_version(db) >= 1`
- [ ] `grep -rn "PASSWORD" submitters/` returns nothing
- [ ] `parse_israeli_board` returns `[]` for CAPTCHA, 403 and soft-404 fixtures
- [ ] The autopilot regression test reaches `state == "queued"` with no injected resolver
- [ ] `ruff check .`, `ruff format --check .` and `pytest -q` all pass

## Execution Notes (what the plan got wrong)

Recorded during execution, 2026-08-03. All five code tasks landed; Task 6 awaits the
operator.

1. **Task 2 found a second credential path.** The spec and this plan both named
   `submitters/indeed.py` as the only violation. The guard test found
   `submitters/linkedin.py:69` doing `os.getenv("LINKEDIN_PASSWORD")` and typing it, with
   its own docstring conceding LinkedIn "may trigger 2FA or CAPTCHA". Both modules had
   zero importers. Both deleted, along with `linkedin_email`/`linkedin_password`/
   `linkedin_cookies_file` — `LinkedInV2Submitter` takes no credentials and already uses a
   persistent profile. The dead block was 415 lines, not ~140.
2. **Task 3's key convention was impossible.** The plan specified `work_authorization:il`.
   `canonical_fact_key` (`profile/models.py:133`) rewrites every non-alphanumeric
   character to `_`, so the colon form silently normalises to `work_authorization_il`; the
   evidence namespace is `[a-z0-9_]` only. Caught by a test failing with `KeyError` on the
   colon form. Also, jurisdiction scoping was made **additive** rather than a rename:
   `work_authorization`/`sponsorship` are `min_length=1` required fields with a
   `model_validator` demanding citizenship-or-nationality, so renaming them would break
   the live contract and existing tests for no safety gain. The flat key now means
   "jurisdiction unspecified" and satisfies no country-named question.
3. **Task 1's test helper had to change.** `_gov` passed a duck-typed stub exposing only
   `.hour` and `.strftime`, which is also why `_epoch()` carries a defensive
   `hasattr(n, "timestamp")`. Replaced with a real tz-aware datetime rather than adding a
   second workaround to production code.
4. **Task 4's fabrication target was already fixed.** `discovery/israel_boards.py`'s
   `else "Software Engineer"` / `else "Drushim Employer"` lines are inside a **docstring**
   documenting the historical bug; the module delegates to `parse_israeli_board` and
   returns `None`. The live defect was in `jobs/parsers/israeli_boards.py`, where a
   CAPTCHA page's `<h1>` satisfied `is_complete` — the site's own landing page parsed as a
   job titled "Drushim".
5. **Task 5 could not be tested end-to-end.** The autopilot scenario's descriptor is
   synthetic (`qualified_form_scope=('ffff…')`, `selector_version='greenhouse-candidate-v9'`)
   and `plan.job_url` is `None`, so the real qualification path cannot resolve there. The
   test pins the defect itself — that no resolver is forwarded when none is supplied —
   rather than passing for the wrong reason. Real coverage lands with P1's fixture work.
   Also found: `qualified_autopilot` was missing from `_ALLOWED_ACTORS`, and
   `core/application_audit.py` silently relabels an unknown actor as `"system"`, so
   unattended sends were indistinguishable from routine worker activity in the audit trail.
6. **CI's lint gate is narrower than this plan's Global Constraints claimed.**
   `.github/workflows/ci.yml` runs `ruff check . --select E9,F63,F7,F82` repo-wide and the
   full ruleset only on **changed** files. The repo carries 107 pre-existing full-ruleset
   errors in untouched files. Consequence: appending to a file inherits its lint debt into
   the gate. `tests/test_adversarial.py` had four pre-existing errors, one of which was a
   test whose comment claimed a mismatched job yields SKIP while asserting nothing about
   it — it scores 22.5, above `SKIP_THRESHOLD` of 20, so the action is DRAFT. Fixed to
   assert the property that matters: such a job never reaches `AUTO_APPLY`.

## Self-Review Notes

Spec coverage checked against §6 and §7 (P0): §6.2 → Task 1, §6.4 → Task 2, §6.5 → Task 4, §6.3 → Task 5, answer bank (§5.1) → Task 3, profile bootstrap (§7 P0) → Task 6. §6.1 (Greenhouse selectors) and §6.6 (discovery catalog) are deliberately **out of scope** — they belong to P1 and P3 respectively.

Type consistency checked: `_page_is_unreadable` is used in both `_parse_detail` and `parse_israeli_board`; `_SPECIFIC_TITLE_SELECTORS` / `_FALLBACK_TITLE_SELECTORS` are referenced consistently and `_TITLE_SELECTORS` is retained as their concatenation for existing importers; `active_hours_timezone` is defined in Task 1 Step 3 before its use in Step 4.
