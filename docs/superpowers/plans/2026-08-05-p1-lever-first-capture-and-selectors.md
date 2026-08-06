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
- **A hidden `h-captcha-response` field, plus a visible `div#h-captcha`
  widget.** Update after checking four tenants total (Palantir, Voltus,
  Metabase, Collate): **all four** have it. Correcting the earlier note in
  this doc — this looks like a Lever-platform default, not a per-tenant
  opt-in, though four is still a small sample. Practically this means: don't
  bother screening candidate postings for captcha-absence, there may not be
  one. It doesn't change anything about the plan — Task 3's live canary
  already assumes a human solves any CAPTCHA/MFA challenge, so this is the
  ladder working as designed, not a new blocker.

The resume-upload widget's own DOM text already contains `"Couldn't auto-read
resume."`, `"Analyzing resume..."`, `"Success!"` before any file is chosen —
suggestive that it's async, but not proof. Only Task 1's real file-select with
network observation resolves this; treat it as still open.

## Task 1: DONE — the operator ran the capture (2026-08-06)

Real capture completed against `jobs.lever.co/collate/bc4a840c-71f1-4190-8826-6b42d236e375/apply`
(the posting screened as simplest — see the screening note preserved below).
Result: **zero tripwires**, `proceed: true`. Specifically:

- The real submit is `POST` + `document` + navigating (not XHR) with
  `content_type: multipart/form-data`, to the same `/apply` URL — confirms
  the transport model this adapter already assumed.
- The resume upload did **not** fire a request during the file-selection
  phase — `ASYNC_UPLOAD` did not trip. (A separate `/parseResume` XHR was
  observed, but bucketed under the post-submit phase, not file-selection;
  the file itself travels inside the main multipart POST regardless.)
- `.capture/lever/capture.json` stays local (`.capture/` is gitignored by
  design, not an oversight here). Its `form_blank.html` — sanitized, no typed
  values, no PII — is committed directly as the new
  `tests/fixtures/lever_v1/application_basic.html`, which is the durable,
  reviewable copy of this evidence.

Screening note (preserved from before the capture ran): five real postings
were checked read-only for simplicity first; Collate came out cleanest — 0
dynamic-UUID survey fields, 1 file input, no EEO/consent language, 22 total
fields, versus 6-7 survey fields on everything else checked (Voltus,
Metabase, Palantir).

## Task 2: substantially done — evidence-based rewrite landed (2026-08-06)

`submitters/lever_v1.py` and `submitters/platforms.py` rewritten from the
real capture, not from assumption. What changed and why, in the same order
as the original task list:

- [x] `LEVER_FORM_SELECTOR` → `form#application-form`. Real markup carries no
  `data-qa`/`data-posting-id`/`data-site` on the form element at all; `_exact_form`
  no longer tries to re-verify posting identity from form attributes that
  don't exist — identity is established by the navigation that already
  happened before snapshotting.
- [x] `observe_lever_v1_fields`'s wrapper query → `li.application-question`
  (confirmed, not `capture.json`-inferred). field_id is now derived from each
  control's `name` attribute via `_field_id_from_name` (sanitized to satisfy
  `_FIELD_ID_RE`, e.g. `urls[LinkedIn]` → `urls_LinkedIn_`) rather than a
  `data-field-id` that does not exist in reality — `data-qa` exists only on
  some controls (name-input, email-input, ...) and not others that are just
  as real (the five `urls[...]` fields), so `name` is the one identifier
  every real control actually has.
- [x] Simple identity fields and the resume field: handled, proven against
  the real fixture (`tests/fixtures/lever_v1/application_basic.html`, now the
  real sanitized Collate markup, not hand-authored).
- [x] The location field's extra hidden `selectedLocation` companion control
  (an autocomplete widget backing a single logical field) — not anticipated
  in the original task list, found during the rewrite. `lever_v1_final_action_binding`
  now maps a wrapper to one field_id and recognises companion hidden controls
  sharing that wrapper, rather than treating the second named control as an
  unexplained field and blocking with `FORM_CHANGED`.
- [x] `_SYSTEM_CONTROL_NAMES` replaced wholesale: v2's assumption
  (`authenticity_token`/`csrf_token`/`_csrf`/`utf8` — a Rails convention Lever
  does not use) matched none of the real hidden fields observed
  (`accountId`, `linkedInData`, `origin`, `referer`, `timezone`,
  `socialReferralKey`, `socialSource`, `resumeStorageId`,
  `h-captcha-response`, `source`).
- [x] The visible submit button is `type="button"`, not `type="submit"` —
  a real hCaptcha-gated hidden button (`type="submit"`, no `name`, unrelated
  to the adapter's own click target) does the actual native submit.
  `lever_v1_final_action_binding`'s selector and submitter-identity payload
  updated to match.
- [x] `LEVER_V1_SELECTOR_VERSION` bumped `v2` → `v3`, and
  `submitters/platforms.py`'s matching descriptor entry updated in lockstep
  (required — `_descriptor()` refuses to instantiate the adapter on any
  mismatch between the two). Qualification tier deliberately set to
  `DRY_RUN_ONLY`, not re-claimed as `FIXTURE_QUALIFIED`: that tier means the
  *committed fixture baseline* passes, and only one of 28 fixtures has been
  migrated to real markup so far.
- [ ] Survey/EEO custom questions (Lever's `cards[<uuid>][fieldN]` /
  `surveysResponses[<uuid>][...]` patterns) — **not implemented**. Collate's
  own posting has none, so there is no completed-submission evidence to build
  this from yet, only the blank-form reconnaissance recorded earlier in this
  doc. `observe_lever_v1_fields` correctly `SELECTOR_DRIFT`s on any posting
  that has them, which is the right fail-closed behavior until real evidence
  exists — not a gap to guess through.
- [ ] Consent/attestation detection — **not implemented, and now known to
  need a different design than v2 assumed**. v2 read a `data-control-kind`
  attribute that does not exist anywhere in real Lever markup; a real consent
  question (seen in blank-form recon, not yet in a completed submission) is
  structurally just an ordinary radio question ("Yes, I consent" / "No, I do
  not consent"). Detecting it will need label-text matching, not a structural
  marker, and touches `core/form_planning.py`'s answer policy, not just this
  file. Explicitly out of scope for this pass.
- [ ] `LEVER_CONFIRMATION_SELECTOR` — **still the old, unverified v2 guess**.
  The real capture's confirmation-candidate search found no match on the
  actual post-submit page (`confirmation_selector: null` in the committed
  `capture.json`), so what real Lever shows after a successful submit remains
  unknown. Left unchanged rather than guessed.
- [x] **A significant new finding, not in the original task list, now fixed
  (2026-08-06)**: the real fixture's hCaptcha widget (`<div class="h-captcha">`,
  present on every real posting checked this session, not just this tenant)
  tripped `assess_lever_v1_snapshot`'s `CHALLENGE_DETECTED` logic, which
  treated mere presence of `.h-captcha`/`.g-recaptcha`/`iframe[src*="captcha"]`/
  `[data-captcha]` as an active, blocking challenge — meaning
  `assess_lever_v1_snapshot` could never reach `FORM` state on any real Lever
  page. Resolved by querying `.capture/lever/capture.json`'s network-request
  transcript across all three capture phases: the widget's div and its iframe
  (`newassets.hcaptcha.com/.../static/...`) are both already present at
  `form_load`, alongside only passive resources (`secure-api.js`,
  `checksiteconfig`); the actual active challenge (real puzzle images, the
  `image_label_area_select` endpoint) appears in the transcript only after the
  operator clicks submit. That's decisive evidence the four structural
  selectors are always-true false positives on real pages, not a genuine
  signal — so they were removed from `assess_lever_v1_snapshot`, keeping only
  the three text-marker checks (`"verify you are human"`, `"security
  challenge"`, `"complete the captcha"`), which still correctly catch
  `tests/fixtures/lever_v1/captcha.html` (its own heading/body text matches
  those markers independent of the structural check). The real safety
  boundary — qualification-tier gating on final execution, and the live-canary
  stage's existing requirement that a human handle CAPTCHA/MFA — is untouched;
  this only fixes a too-eager *inspection-time* heuristic. Confirmed via
  before/after test comparison: 31 failed/20 passed → 28 failed/23 passed,
  with the 3 newly-passing tests being exactly `application_basic.html`
  reaching `FORM` state plus two tests that depend on it, and the remaining 28
  failures byte-for-byte identical to the pre-fix set (the unmigrated-fixture
  issue in the follow-up list below, untouched by this change). Text markers
  alone are not proven to catch every real active-challenge presentation — no
  capture has observed the DOM during that moment — so this is evidence the
  old check was wrong, not a claim the new one is complete.
- [x] **Second finding, same day, much larger: `AnswerPolicyV1` could never
  deterministically resolve *any* real Lever field, not just custom
  questions.** Traced end to end (not assumed): every deterministic
  resolution path in `core/form_planning.py` — `_identity_value`,
  `_operator_approved`, the user-confirmed-evidence path, even the local-LLM
  path's `_allowed_llm_evidence_keys` gate — requires `field.canonical_name`
  to be set before it runs at all. `observe_lever_v1_fields` left it `None`
  for every field (no `data-canonical-name` attribute exists on real
  wrappers), a decision the v3 rewrite's own comment flagged as needing "a
  separate mechanism" later. That mechanism is now built: `lever_v1.py`'s
  new `_CANONICAL_NAME_BY_FIELD_ID` maps the real, stable field_ids
  (`name`, `email`, `phone`, `location`, `urls_LinkedIn_`, `urls_GitHub_`,
  `urls_Portfolio_`) to `core.submission_domain._CANONICAL_LABEL_ALIASES`
  keys — the same field_id → canonical-key fallback pattern
  `submitters/greenhouse_v1.py`'s `_canonical_name` already uses, not a new
  design. Every mapped field's real, *actually extracted* label was checked
  against `field_canonical_label_compatible` by direct measurement, not by
  eyeballing the HTML — because that check is stricter once canonical_name
  is set than when it's `None` (permissive `True`), a wrong mapping would
  have made resolution *worse* than doing nothing, not better. Two
  fields were caught exactly this way and are deliberately NOT mapped:
  `org` ("Current company") and `urls_Twitter_`/`urls_Other_` have no
  matching concept anywhere in `_CANONICAL_LABEL_ALIASES` at all — not
  "unmapped yet", genuinely no such concept exists in this policy (see the
  unresolved architecture gap below, which is the real consequence of this).
- [x] **Third finding, found while verifying the second: label extraction was
  silently wrong for any field whose control has rich nested UI state.**
  `observe_lever_v1_fields` took the whole `<label>` element's text; on real
  markup two fields (`resume`, `location`) wrap sibling
  `.application-field` content with the actual question inside their
  `<label>` — upload-progress status spans for resume ("Couldn't auto-read
  resume.", "Analyzing resume...", "Success!"), autocomplete dropdown chrome
  for location ("Loading", "No location found..."). The extracted label for
  resume was `"Resume/CV ATTACH RESUME/CV Couldn't auto-read resume. ..."`,
  not `"Resume/CV"` — which meant `field_is_reviewed_cv_attachment` returned
  `False` on the *one field every application needs*, silently, with no
  `SELECTOR_DRIFT` to surface it. Fixed by preferring the real, consistently-
  present `.application-label` div (confirmed 1:1 against all 11 fields in
  the real fixture) over the whole `<label>`, falling back to the old
  broader selector only if that div is absent. This is a real change to how
  `observe_lever_v1_fields` reads real markup, so `LEVER_V1_SELECTOR_VERSION`
  bumped `v3` → `v4` (with `submitters/platforms.py`'s descriptor and every
  hardcoded test string updated in lockstep, same discipline as the v2→v3
  bump) — old v3 fingerprints must not be treated as equivalent to v4 ones.
- [x] Fourth, smaller finding, same root cause as the third: even with the
  clean `.application-label` text, `"Resume/CV"` normalizes to `"resumecv"`
  (`core.sensitive_policy.normalize_policy_text` strips `/` without adding a
  space), which matched neither the `"resume"` nor `"cv"` alias alone.
  `"resume/cv"` — a real, observed label, not a guess — added to
  `_CANONICAL_LABEL_ALIASES`'s `resume`/`resume_upload`/`cv`/`cv_upload`
  entries. Narrow, additive, doesn't touch matching logic.
- [x] Verified cumulative effect empirically at every step (`python -c
  "..."` against the real fixture + `AnswerPolicyV1.plan_fields` directly,
  not just re-running pytest) rather than assuming the fixes composed
  correctly: before this pass, all 11 real fields abstained and
  `ready_for_permit` could never be reached; after, `resume`/`name`/`email`
  resolve with real, inspectable provenance
  (`verified_attachment`/`deterministic_identity`), and `phone`/`location`/
  `urls_LinkedIn_`/`urls_GitHub_`/`urls_Portfolio_` correctly abstain for a
  mundane, expected reason (the fake test profile has no data for them, not
  a mapping gap — a populated profile resolves them too). `org`/
  `urls_Twitter_`/`urls_Other_` still correctly abstain, for the real
  architecture reason below.
- [ ] **New, now the actual top blocker for Task 3 — bigger than the hCaptcha
  finding, and NOT fixed this session, deliberately**: Lever's real Playwright
  `fill()` (`submitters/lever_playwright.py:1114-1116`) raises on *any*
  non-`RESOLVED` decision rather than skipping it — confirmed a deliberate,
  coherent design shared with `submitters/smartrecruiters_playwright.py`
  (not shared by `greenhouse_playwright.py`/`workday_playwright.py`, which
  both skip). `LeverBrowserV1.inspect()` and `preflight()` each carry their
  own independent copy of the matching "any decision not RESOLVED blocks"
  check. Combined with the second finding above: `org`/`urls_Twitter_`/
  `urls_Other_` have no canonical concept anywhere in
  `_CANONICAL_LABEL_ALIASES`, so **nothing can ever resolve them** —not
  identity, not a pre-seeded `OperatorApprovedAnswer` row (`_operator_approved`
  *also* requires `canonical_name` to be set before it even queries), not a
  configured local LLM (`_allowed_llm_evidence_keys` returns empty for a
  label with no recognized canonical/semantic key, so `_local_llm` bails
  with `UNSUPPORTED_CLAIM` before generating anything). It's not even
  possible to represent "reviewed and intentionally left blank" as a
  workaround: `AnswerDecisionV1`'s own validator forbids a `RESOLVED`
  decision from carrying a blank string value. **Net effect: under the
  current domain model, `ready_for_permit` is structurally unreachable for
  any real Lever posting with even one generic optional field with no
  identity/CV/LLM-answerable concept — which, based on the one real capture,
  is ordinary, not an edge case** ("Current company" is exactly this kind of
  field on most real job applications). Fixing this for real needs a design
  decision in shared, safety-critical code (`core/submission_domain.py`/
  `core/form_planning.py`, used by all five adapters, not just Lever) —
  most plausibly a new disposition distinct from both `RESOLVED` and
  today's abstain-with-reason, representing "an operator explicitly reviewed
  and confirmed leaving this exact field blank," matched by
  `field_contract_fingerprint` (which already exists and already scopes to
  one exact real field instance) rather than requiring a generic
  `canonical_name` concept the field may never have. That's a genuine,
  wide-blast-radius architecture change, not a mechanical fix, and is
  deliberately left as a scoped-but-undone follow-up rather than guessed at
  here. The four browser-level tests that need a *ready* plan to exercise
  their own logic (attachment binding, confirmation-injection resistance,
  click-timeout handling, invalid-proof detection) now use a test-only
  `_assume_ready` helper (`tests/test_lever_browser_v1.py`) that synthesizes
  placeholder-valued `RESOLVED` decisions for whatever's still abstained,
  clearly documented as standing in for "assume an operator already
  reviewed this through a mechanism that doesn't exist yet" — proven correct
  the hard way, by first discovering that clearing only `plan.blockers`
  produced two tests that passed *for the wrong reason* (coincidentally
  matching `FORM_CHANGED` from the still-unresolved-decisions check, never
  reaching the confirmation-injection/proof-validation logic they were
  named for) before fixing `_assume_ready` to actually clear every decision.
- [x] `ruff check .`, `ruff format --check .` — clean on every touched file.
  Full suite (`pytest tests/ -q`, the reliable run — a first attempt piped
  through `tail -50` before backgrounding and silently discarded the earlier,
  alphabetically-first failures, producing a misleading 90-failed count;
  rerun with complete output redirected straight to a file instead): **26
  failed, 2941 passed, 20 skipped** — down from this session's 33-failing
  starting point (after the earlier v2→v3 rewrite + hCaptcha fix). Every one
  of the 26 is accounted for: 19 in `test_lever_v1_fixtures.py` + 1 in
  `test_lever_browser_v1.py` (the pre-existing, already-scoped unmigrated-
  fixture backlog, follow-up item 1), 2 in `test_adapter_qualification_matrix.py`
  (follow-up item 2's mixed-tier issue), 1 in `test_lever_v1_qualification_report.py`
  (the deliberately-stale Lever report), `test_webhook_ingest_text.py` and
  `test_control_plane_runner_scripts.py`'s one each (pre-existing/unrelated,
  confirmed via `git stash` comparison earlier this session) — and one new,
  real, narrow, and fully understood side effect:
  `test_v4_local_model_qualification.py::test_committed_local_model_report_is_aggregate_only`
  now fails because `scripts/evaluate_v4_local_model_qualification.py`'s
  `_SOURCE_FILES` set includes `core/submission_domain.py`, and its committed
  report's `source_integrity` hash no longer matches that file's content
  after the `resume/cv` alias addition above — the exact same
  "re-earn qualification after the source changed" situation this doc
  already describes for the Lever report, just for a different, unrelated
  qualification (a real local-model evaluation, `real_local_model: true`
  over 410 real cases — not something to trigger casually or fake by
  hand-editing the committed hash). Left honestly failing rather than
  silently patched.

### Follow-up (not done in this pass, scoped so the next session can pick it up)

1. Migrate or retire the other 24 `tests/fixtures/lever_v1/*.html` fixtures
   (currently v2 markup, will `SELECTOR_DRIFT` against v4). A full
   reconnaissance pass (per-fixture: real markup gap, exact failure cause,
   what the test is actually trying to prove, mutation-vs-retire read) is
   done and was used to plan this session's fixes; the remaining fixtures
   split roughly into: generic actionability mutations honestly buildable
   from `application_basic.html` with one labeled change each
   (`wrong_method.html`, `unreviewed_hidden_control.html`,
   `disabled_submit.html`, the four `outer_*` fixtures,
   `outer_has_proxy_guard.html`), fixtures whose whole premise no longer
   exists in v4 and should be retired outright rather than migrated
   (`invalid_action.html` — v3 already deleted the action-URL check its own
   code comment confirms this), and fixtures needing real evidence or an
   explicit hypothetical label because the one real capture never showed the
   scenario (`application_custom_select.html`, `application_radio_checkbox.html`,
   `application_consent.html` — note `application_consent.html`'s detection
   mechanism, `data-control-kind="consent"`, is itself unverified against any
   real page, a second-order gap, not just an unseen scenario).
   `test_fixture_set_is_sanitized_and_contains_no_live_identity` also needs
   `application_basic.html` added to its allowlist (it currently requires
   every fixture to contain the literal string `"sample-company"`, a v2-era
   convention the real fixture never had occasion to carry).
2. `docs/qualification/lever-browser-v1.json` and its `.md` counterpart are
   now stale (still claim v2, `fixture_qualified`) and were deliberately left
   unchanged rather than hand-edited to a false "passed" state — regenerating
   them honestly needs the fixture migration above done first, the same
   `chore(qualification): re-earn ... after the config change` pattern this
   repo already uses elsewhere. This has one more consequence than it looks:
   `scripts/build_adapter_qualification_matrix.py` (docstring: "the
   deterministic, fixture-only first-five ATS qualification matrix")
   structurally assumes all five first-wave adapters sit at the same
   `FIXTURE_QUALIFIED` tier and hard-fails on any registry/report mismatch —
   which Lever now is, correctly. `test_adapter_qualification_matrix.py`'s
   two tests fail as a result. Fixing this for real means either fast-tracking
   Lever's fixture migration back to parity with the other four, or teaching
   the matrix script to represent a mixed-tier cohort — a real design
   decision, not a mechanical fix, so left alone rather than guessed at here.
3. ~~Investigate the hCaptcha/`CHALLENGE_DETECTED` finding above~~ — done
   2026-08-06, see the checked item above.
4. Investigate `LEVER_CONFIRMATION_SELECTOR` — ask the operator what the real
   post-submit page showed (screenshot or plain description), since the
   capture tool itself found no match.
5. **The real top priority for Task 3**: design and implement a way to
   represent "an operator reviewed this exact field and confirmed leaving it
   blank" in `core/submission_domain.py`/`core/form_planning.py` — see the
   detailed finding above. Everything else in this follow-up list is
   independent of Task 3 readiness; this one isn't.
6. Re-earn `scripts/evaluate_v4_local_model_qualification.py`'s committed
   report (`test_v4_local_model_qualification.py::test_committed_local_model_report_is_aggregate_only`
   now fails on source-integrity, not logic — see above): unrelated to
   Lever, a side effect of `core/submission_domain.py` being one of that
   qualification's tracked source files. Needs an actual local-model
   evaluation run (`real_local_model: true`), not a quick fix — left
   honestly failing rather than faked.

### Task 3: Fixture-qualify, then dry-run, then the live canary — **operator present throughout**

Unchanged from the design spec's existing ladder (§2 qualification stages) —
not rewritten here because nothing learned this session changes the ladder
itself, only what has to happen before Task 3 can start. The hCaptcha
false-positive that previously blocked `assess_lever_v1_snapshot` from ever
reaching `FORM` state on a real page is fixed, and so is field-level
resolution for every identity-shaped field a profile actually has data for
(see Task 2 above) — but follow-up item 5 (no way to explicitly resolve a
generic optional field with no canonical concept) is now the real,
structural blocker for reaching `ready_for_permit` on a real page, not the
unmigrated-fixture or stale-qualification-report items:

- [ ] Offline fixture suite passes against the rewritten contract.
- [ ] Real-Chromium rehearsal with `HTMLFormElement.prototype.submit` stubbed —
  confirms no request leaves before spending a real application on it.
- [ ] One real-URL dry run (`DRY_RUN=true`) against a **different** real Lever
  posting than the one captured, to catch overfitting to a single tenant's
  markup.
- [ ] One live canary — operator selects and approves the exact job,
  handles CAPTCHA/MFA manually, confirms via the employer's own confirmation
  email. This is the step that actually produces the P1 exit criterion.

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
