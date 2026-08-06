# P1 (revised) — Lever-first capture and selector rebuild

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Supersedes** the P1 target in
[`2026-08-03-earned-autonomy-auto-apply-design.md`](../specs/2026-08-03-earned-autonomy-auto-apply-design.md)
§7, which names Greenhouse as the first target. That choice is no longer correct;
this document explains why and replaces it with Lever, using only evidence
gathered since — no part of the corrected priority is assumed.

**Goal:** one `Submission` with `outcome == "confirmed_submitted"`, backed by a
real `SubmissionEvidence` row and a `LIVE_CANARY_QUALIFIED` adapter record — the
same P1 exit criterion the design spec already defines, now aimed at the adapter
that can actually reach it.

**Architecture:** two operator-gated evidence captures (already tooled, zero new
code required to run them) feeding one selector-contract rewrite, which is the
only step this plan can't pre-write, because writing it now would mean
guessing — precisely what P0's Task 6 and the transport probe both exist to rule
out.

---

## Why Greenhouse was retargeted to Lever

Verified this session, reproducible without a browser:

```bash
for slug in gitlab stripe airbnb doordash pinterest; do
  curl -s -o /dev/null -w "$slug -> %{http_code} %{redirect_url}\n" \
    "https://boards.greenhouse.io/$slug"
done
```

All return `301` to `job-boards.greenhouse.io` — a platform-wide sunset, not a
per-tenant migration. The new domain's raw (pre-JS) HTML already shows
`<form method="get" ...>` and zero `data-field-id` occurrences — `curl`, no
Playwright, no application spent. `submitters/greenhouse_v1.py`'s
`_FIELD_WRAPPER_SELECTOR` and `submitters/greenhouse_playwright.py`'s
`structureReady()` both hard-require the opposite (`method=post`,
`enctype=multipart`, `data-field-id`). This is not a selector typo; the v1
adapter's entire premise doesn't hold against any real posting today. Fixing it
means a new transport (§6.1 of the design spec already flagged the field
extraction half of this; the transport half — GET vs POST — was found this
session and is strictly worse).

Lever, by contrast:

```bash
curl -sL "https://jobs.lever.co/gopuff/<posting>/apply" | grep -oE '<form[^>]*>'
# <form id="application-form" enctype="multipart/form-data" method="POST">
```

Confirmed on two independent tenants (gopuff, shieldai) via the
`ats_transport_probe.py` research (merged, PR #57) and cross-checked again via
plain `curl` this session. The transport `structureReady()` needs — native
POST, multipart — is what Lever actually serves. **The defect is narrower**:
`LEVER_FORM_SELECTOR` requires `data-qa="application-form"][data-posting-id]
[data-site]` on the `<form>` element; real markup has none of those three, only
`id="application-form"`. Fixable as a selector-contract update, not a
transport rewrite.

---

## What's already true — no further action needed

- [x] **Real operator profile.** `profile_version >= 1` against the containerized
  Postgres stack, zero `PROFILE_*` reason codes. Real name, contact, citizenship,
  work authorization (IL confirmed / US requires sponsorship / EU abstains —
  correctly, since it was never confirmed), target roles.
- [x] **`cv_routing.yaml`** — 16 real role-specific CVs (from the operator's own
  company × role CV set), routing config validates against `profile/cv_routing.py`'s
  schema.
- [x] **Infra.** Postgres, Redis, `celery-worker`, `celery-beat`, `web-api` — all
  up via `docker compose`, migrations applied, healthy.
- [x] **Both capture tools built and tested**, mirroring each other's safety
  design (never fills, never uploads, never submits; aborts before Step 2 if
  the blank form already fails a tripwire so a bad target can't cost a real
  application to learn):
  - `scripts/greenhouse_selector_capture.py` — kept for completeness; **do not
    run it yet**. The transport finding above means it will trip
    `FORM_METHOD_NOT_POST`/`FORM_ENCTYPE_NOT_MULTIPART` on the blank-form check
    and abort in seconds. Real value here is after a future Greenhouse
    transport rewrite, not before.
  - `scripts/lever_selector_capture.py` — the one to run. Confirmed via `curl`
    that its `_FORM_CANDIDATES`/tripwire logic matches real Lever markup shape;
    the transport-level tripwires should **not** fire on a normal posting.

## Greenhouse: what a future transport rewrite would need (not scheduled — scoping only)

Same read-only method used on Lever below, run against a live, junior-level
Greenhouse posting (`job-boards.greenhouse.io/waymark/jobs/4711827005`) to
de-risk *future* work, not to start it now. Confirms and sharpens §6.1's
finding:

- **Proof, not suspicion, that submission can't be a native form POST.**
  `method="get"` on a form with two `<input type="file">` children is not
  just wrong per `structureReady()` — it's structurally incapable of carrying
  file content at all (GET requests have no body). The real submit is
  necessarily a JS-driven API call. `SUBMIT_IS_XHR` was always the right
  tripwire name for this ATS.
- **Every field is identified by `id`, not `name`** — `first_name`,
  `last_name`, `preferred_name`, `email`, `phone`, `candidate-location`,
  `resume`, `cover_letter`, `school--0`, `degree--0`, and per-posting custom
  questions as `question_<numeric-id>` (a Greenhouse internal question ID,
  not a UUID like Lever's cards). Every `name` attribute is empty. Any future
  rewrite keys off `id`, full stop — there is nothing to read from `name`.
- **The real wrapper class is `.field-wrapper`**, not `data-field-id`
  (confirming §6.1) and not `data-testid` either, though `data-testid`
  appears elsewhere in the form and may be worth a second look when this work
  is actually scheduled.
- Two file inputs confirmed present on a real posting (resume + cover
  letter) — the existing `MULTIPLE_FILE_INPUTS` tripwire would correctly
  flag this exact posting as needing the single-file-first-proof rule applied
  carefully, or a different target chosen.

This does not change P1's priority. Lever remains the near-term target
because its transport already matches what the codebase assumes; Greenhouse
needs the transport rewrite itself before a capture is even worth running.
Recorded here so that whenever that work is scheduled, it starts from real
field-identity evidence instead of the assumptions that produced the original
22 fixtures.

## Read-only reconnaissance done since (2026-08-05, live browser, blank form only)

Loaded `https://jobs.lever.co/palantir/c4442730-2926-41ad-8c0e-5e5a6b4d14ae/apply`
in a real browser and read the rendered DOM — no field typed, no file chosen, no
button clicked. This is exactly Step 1 of `lever_selector_capture.py`, run by
hand once to sanity-check the tool's assumptions against a third live tenant
before anyone spends a real application. It is **not** a substitute for Task 1
below; it cannot observe network requests or the post-submit page.

Confirmed, third tenant, same as gopuff/shieldai: `id="application-form"`,
`method="post"`, `enctype="multipart/form-data"`, 1 file input, 1 submit
button. Identity fields carry `data-qa` directly on the input
(`name-input`/`email-input`/`phone-input`/`location-input`/`org-input`) inside
minimal wrappers, exactly as the plan already described.

Three corrections/additions to what Task 2 should expect, found only because
this tenant happens to use more form features than gopuff/shieldai did:

- **Two different UUID-namespace conventions for custom questions**, not one:
  `cards[<uuid>][fieldN]` (this tenant) and `surveysResponses[<uuid>][responses]
  [fieldN]` (the earlier gopuff sample). Both match the
  `looks_like_dynamic_survey_name` regex already in `lever_selector_capture.py`
  (`/\[[0-9a-f-]{20,}\]/`) — no tooling change needed, just don't hardcode either
  prefix in the Task 2 rewrite.
- **`urls[LinkedIn]` / `urls[GitHub]` / `urls[Portfolio]`** and a **location
  field backed by an autocomplete widget** (paired hidden `selectedLocation`
  field, "Loading" / "No location found" states) — more structure than the
  gopuff sample showed. Treat as additional stable, cross-tenant fields
  alongside the core five, pending confirmation on the next tenant.
- **A hidden `h-captcha-response` field** — this tenant's form has hCaptcha
  wired in. Tenant-specific, not a Lever-platform universal, but it means a
  human is required at submit time for *this* posting regardless of adapter
  correctness — pick a different tenant for the first canary if this one still
  has it when Task 3 gets there, or accept that Task 3's live canary needs a
  human solving a captcha, which the design spec's ladder already assumes.

The resume-upload widget's own DOM text already contains `"Couldn't auto-read
resume."`, `"Analyzing resume..."`, `"Success!"` before any file is chosen —
suggestive that it's async, but not proof. Only Task 1's real file-select with
network observation resolves this; treat it as still open.

## What's still open — genuinely blocked on the operator

### Task 1: Run the Lever capture — **requires the operator**

**Why an agent cannot do this:** it is a real job application. The tool
observes; it does not act. Someone has to pick a real posting, attach a real
CV, answer real questions, and click a real submit button, because that is the
only way to learn Lever's actual submit mechanism (is the resume upload
synchronous or does "Analyzing resume..." mean an async parse-then-attach flow
that needs different handling entirely) and get real field markup to build a
selector contract from.

- [ ] **Step 1: Pick a target.** One real, currently open Lever posting.
  Prefer: single resume upload (not multiple file inputs — `MULTIPLE_FILE_INPUTS`
  tripwire), and ideally no survey/EEO block for the first proof (each extra
  field shape is real coverage work the first proof doesn't need — the tool
  will flag any present via `dynamic_survey_name_fields` in its report either
  way).
- [ ] **Step 2: Run the tool.**
  ```powershell
  python scripts/lever_selector_capture.py --url https://jobs.lever.co/<tenant>/<posting-id>/apply
  ```
  Follow its 4 on-screen steps exactly — confirm the blank form, attach the CV
  and watch the upload indicator, fill the rest, then submit for real.
- [ ] **Step 3: Hand the output back.** `.capture/lever/capture.json` plus
  `form_blank.html`/`confirmation.html` (already sanitized — no typed values,
  no PII, safe to share). This is the input the next task cannot start
  without.

**Stop rule (from the design spec §9, unchanged):** if `capture.json` shows
`ASYNC_UPLOAD` or `SUBMIT_IS_XHR`, stop here and re-scope — do not proceed to
Task 2 with a transport model the evidence just contradicted.

### Task 2: Rewrite the Lever selector contract — **from evidence, not before**

Blocked on Task 1's output existing. Once it does:

- [ ] Replace `LEVER_FORM_SELECTOR` in `submitters/lever_v1.py` with the real
  form selector `capture.json.form_selector` reports (expected:
  `form#application-form`, matching the two independently-checked tenants —
  confirm against the actual capture rather than assuming).
- [ ] Rewrite `observe_lever_v1_fields`'s wrapper query from
  `[data-qa="application-field"][data-field-id]` to match
  `capture.json.controls_filled[*].wrapper_selector` — expected pattern is
  `li.application-question`, with per-field identity coming from each control's
  own `data-qa` (e.g. `name-input`, `email-input`) rather than a shared
  `data-field-id`, since real Lever has no `data-field-id` at all.
- [ ] Handle the three field shapes the docstring in
  `lever_selector_capture.py` documents separately, per what the capture
  actually shows for this posting:
  1. simple identity fields (`data-qa` per field, stable),
  2. resume upload (only build submit-time file handling here if
     `capture.json` confirms it's synchronous; if async, stop per the rule
     above),
  3. survey/EEO fields flagged in `dynamic_survey_name_fields` — these need a
     `name`-independent approach (label text, `data-qa="multiple-choice"` /
     `data-qa="checkboxes"` wrapper) since the `name` attribute itself is not
     stable across postings.
- [ ] Replace `tests/fixtures/lever_v1/*.html` with the sanitized captured
  markup (already PII-free; review once more before committing regardless).
- [ ] Update `lever_v1_form_fingerprint` and any qualification-evidence digest
  that encodes the old wrapper pattern — check for the same kind of
  `native_transport`-style version tag P0 found on the Greenhouse side
  (`GREENHOUSE_V1_NATIVE_TRANSPORT`) and bump it if present, so this is
  legible as a new selector-contract version, not a silent rewrite of what
  qualification evidence already vouches for.
- [ ] `ruff check .`, `ruff format --check .`, `pytest -q` — all green before
  commit, same bar as every other change this session.

### Task 3: Fixture-qualify, then dry-run, then the live canary — **operator present throughout**

Unchanged from the design spec's existing ladder (§2 qualification stages) —
not rewritten here because nothing learned this session changes it:

- [ ] Offline fixture suite passes against the rewritten contract.
- [ ] Real-Chromium rehearsal with `HTMLFormElement.prototype.submit` stubbed —
  confirms no request leaves before spending a real application on it.
- [ ] One real-URL dry run (`DRY_RUN=true`) against a **different** real Lever
  posting than the one captured, to catch overfitting to a single tenant's
  markup.
- [ ] One live canary — operator selects and approves the exact job,
  handles CAPTCHA/MFA manually, confirms via the employer's own confirmation
  email. This is the step that actually produces the P1 exit criterion.

---

## Exit Criteria (unchanged from the design spec's P1, restated for this adapter)

- [ ] One `Submission` with `outcome == "confirmed_submitted"`
- [ ] A `SubmissionEvidence` row satisfying `ck_submissions_confirmed_evidence`
- [ ] A `LIVE_CANARY_QUALIFIED` `AdapterQualificationRecord` for Lever
- [ ] `ruff check .`, `ruff format --check .`, `pytest -q` all pass

## What this plan deliberately does not do

- Does not write Task 2's selector contract now. Doing so before Task 1's
  output exists means guessing at the exact `wrapper_selector`/`data-qa`
  values, which is the fabrication this whole rebuild exists to prevent —
  the P0 execution notes already record what guessing here costs (twenty-two
  green fixtures against assumed Greenhouse markup that couldn't read a real
  page).
- Does not touch the safety switches (`DRY_RUN`, `DRAFT_ONLY`,
  `FINAL_SUBMIT_ENABLED`, `LIVE_AUTOMATION_ACKNOWLEDGED`) at any step. Task 3's
  dry run and live canary use the existing gated mechanisms exactly as designed,
  not a bypass.
- Does not resurrect the Greenhouse capture as a parallel effort. It's kept in
  the repo, tested, and correct for when a real transport rewrite is scoped —
  running it before then just spends effort re-confirming a finding already
  established for free via `curl`.
