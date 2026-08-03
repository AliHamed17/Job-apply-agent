# Earned-Autonomy Auto-Apply — Design

**Status:** approved 2026-08-03. Supersedes the zero-touch model in
[`2026-07-20-full-auto-job-agent-v2-design.md`](2026-07-20-full-auto-job-agent-v2-design.md),
which remains historical.

## 1. Goal

The operator wants applications submitted on his behalf across LinkedIn, the Israeli
boards (Drushim, AllJobs, JobMaster, jobs.co.il) and the major ATSs (Workday,
Greenhouse, Lever, Ashby, SmartRecruiters): every field filled, the right CV of twelve
attached, screening questions answered, submitted — with as little of his interaction as
possible.

This design delivers that. It differs from the operator's initial framing in one
respect, for reasons given in §3: the removal of his interaction is **earned per
adapter from evidence** rather than assumed on day one.

## 2. What is fully automatic

Everything upstream of the final submit click, with zero interaction:

discovery → dedup → CV routing (12 role-specific CVs) → scoring → form inspection →
per-field fill planning → cover-letter generation → screening answers → CV attachment.

No part of this design asks the operator to fill a form field, choose a CV, or write a
cover letter.

## 3. The one human touch, and how it is removed

A batch approval of prepared applications is the only human touch. It is removed
per-adapter by a four-stage ladder that already exists in the codebase
(`core/adapter_qualification_service.py`, `submitters/platforms.py`):

| Stage | Evidence required | Send behaviour |
|---|---|---|
| 1 `fixture_qualified` | offline sanitized fixtures pass | batch review |
| 2 `dry_run_qualified` | one real URL, form opens and resolves, stops before the irreversible action | batch review |
| 3 `live_canary_qualified` | **one** real submit with employer-confirmed evidence | batch review |
| 4 `PROVEN` *(new)* | 10 confirmed sends / 5 form shapes / 3 companies, 90-day window, zero hard corrections | **auto-send, no interaction** |

Stage 4 is the operator's stated goal and this design's destination. Stages 1–3 exist;
stage 4 is specified in §7 (P3).

### Why earned rather than assumed

Three verified facts, not preferences:

1. **The project already shipped unreviewed auto-apply and it fabricated results.**
   `DrushimSubmitter.submit()` returned `success=True, status="submitted"` with a
   synthesised confirmation id without opening a browser. Fifty-four PRs since exist to
   make that class of failure impossible.
2. **Fixtures do not test markup, they test assumptions about markup.** The Greenhouse
   adapter passes 22 fixtures and cannot read a real Greenhouse page (§6.1). No quantity
   of fixture-writing substitutes for stage 2.
3. **Configuration can be untruthful while all code is correct.** At time of writing the
   operator's `user_profile.yaml` is unedited template data — `resume.text` reads
   `"JANE DOE … 5 years … TechCorp"` against ~2 years' real experience. Unreviewed
   auto-send would have described him falsely to real employers. Every test passes; the
   SQL evidence constraint holds; the defect is in data. Code-level gates cannot catch
   this, which is precisely what stage 3's "a human looks at one real canary" buys.

Applications are irreversible and most ATSs bar reapplication to the same employer for
6–12 months, so the cost of a wrong send is one permanently spent opportunity.

## 4. Invariants preserved

These are existing properties of the system. This design does not relax any of them.

- **No fabricated success.** `outcome == "confirmed_submitted"` requires employer-side
  evidence bound to the exact attempt, form fingerprint and CV hash, enforced in
  `worker/submission_commands.py` **and** by the SQL `ck_submissions_confirmed_evidence`
  constraint in `db/models.py`. A Python defect alone cannot persist a false green.
- **No passwords.** Login is a persistent browser profile: the operator signs in manually
  once in the agent's Chromium; session cookies persist. No credential is stored, typed
  or read. Two existing violations were found and deleted in P0: `submitters/indeed.py`
  (`INDEED_PASSWORD`) and `submitters/linkedin.py` (`LINKEDIN_PASSWORD`). The second was
  not in the original survey; the guard test written for the first found it.
- **No CAPTCHA or bot-check bypass.** Detection terminalises that one attempt and flags it.
- **Legal and demographic answers come only from operator-confirmed facts** in
  `evidence.user_confirmed`, never from LLM inference and never from CV-extracted facts.
  No confirmed answer ⇒ abstain and flag. Never guess.
- **Local-only private data.** CVs, answers, materials, browser sessions and Ollama
  inference stay on the private runner. The Vercel control plane receives redacted
  coordination metadata only.

## 5. Design decisions

Six subsystem designs collided on `core/form_planning.py`,
`core/submission_domain.py` and `submitters/greenhouse_v1.py`. Resolutions:

| Concern | Decision |
|---|---|
| Cover-letter provenance | One `AnswerProvenance.GENERATED_MATERIAL`, keyed on the claim-set digest from `Application.material_claims_json`. Rejected both self-digest variants — text that certifies itself certifies unaudited prose. |
| Question catalog | One module, `core/answer_slots.py`, owning slot ids, aliases, jurisdiction and polarity. Not two parallel catalogs. |
| Optional sensitive / EEO fields | Bind a **reviewed blank** for optional sensitive fields, rejecting only `field.required`. A blanket block on every `OPERATOR_REQUIRED` field makes every EEO-bearing form permanently unsendable. |
| Cross-form answer reuse | No widening before the first canary. In P2, widen **non-sensitive slots only**, on a pure conjunction of `(adapter, selector_version, field_type, canonical_key, normalized_label, option_set_hash, required)`. Sensitive answers stay form-scoped; their portability mechanism is the answer bank. |
| Multi-file uploads | Blocked at inspect with `UNSUPPORTED_CONTROL`. Supporting them breaks the single-file payload commitment in four coupled places. |

### 5.1 The answer bank

`evidence.user_confirmed` is the sole source for facts that carry legal or personal
weight. The recurring keys added in P0 are `security_clearance`, `notice_period`,
`salary_expectation`, `salary_currency`, `availability_date`, `years_experience` (which
derives both `"2 years"` and `years_experience_number` `"2"` from one input),
`languages`, `relocation`, `work_mode`, `highest_degree`, `how_did_you_hear` and
`demographic_disclosure`. It is written **only** through
`PUT /api/profile/onboarding`, which already forbids extra keys, strips control
characters, merges preserving unknown keys, and versions inside
`profile_write_transaction`.

Keys live in a `[a-z0-9_]` namespace: `canonical_fact_key`
(`profile/models.py:133`) rewrites every non-alphanumeric character to `_`, so a
colon-suffixed key silently normalises to the underscore form. An empty value removes
the key rather than storing `""`, so an unset fact abstains.

A `promote_to_profile` path that would convert a form-scoped answer into portable
cross-employer truth was rejected: it validated neither token, jurisdiction nor polarity,
and accepted uncatalogued keys.

**Legal facts are jurisdiction-keyed and polarity-declared.** `work_authorization_il` is
not `work_authorization_us`. Every alias declares its jurisdiction and whether it asserts
*authorized* or *requires sponsorship*. A label naming a jurisdiction with no matching
confirmed fact abstains; any alias with undeclared polarity abstains. Today
`"Are you legally authorized to work in the United States?"` abstains, and that
abstention is the only thing preventing a false legal claim — the design must not
collapse US and Israeli authorization into one flat key.

**Consent and attestation are bound to the notice text**, not to a category. The fact is
scoped `profile:user_confirmed:consent_<normalized_disclosure_digest>`; an unseen notice
abstains, and thereafter resolves only for a byte-identical normalized disclosure. A
blanket `consent = true` would, at stage 4, silently accept *"I certify I am not bound by
any non-compete and consent to a background investigation and to arbitration"*.
Attestation auto-answer is restricted to a reviewed table of truth-of-statement wordings;
"acknowledge" or "certify" in employer-authored text is never permission.

### 5.2 Abstention as the growth mechanism

An unknown question stops that one application and flags it. The operator answers it once
through the onboarding form; it is then permanently correct for every future application.
Per-slot abstention metrics (P2) name which fact to add next, so the answer bank is grown
from measurement rather than guessed at up front.

This is why no LLM answer-generation or slot-classification layer is built: deterministic
resolution plus abstention metrics first, revisited only against a measured residual touch
rate.

## 6. Verified defects motivating P0/P1

All confirmed against the tree at `d7c166d`.

### 6.1 The Greenhouse adapter cannot read a real Greenhouse page

- Every wrapper branch of `_FIELD_WRAPPER_SELECTOR` requires `data-field-id`, which real
  Greenhouse never emits ⇒ zero wrappers ⇒ `SELECTOR_DRIFT` on first inspection.
- `_MAX_SNAPSHOT_BYTES = 256 KiB` raises a bare `ValueError` against `snapshot()`
  returning `page.content()`; real pages exceed it ⇒ hard wall on the first navigation,
  surfacing as a generic `FORM_INSPECTION_FAILED`.

### 6.2 Governor active hours are evaluated in the wrong timezone

`within_active_hours` compares `datetime.now(UTC).hour` against `ACTIVE_HOURS="09:00-21:00"`
while the signed policy uses Asia/Jerusalem 08:00–21:00. The effective send window is
~12:00–24:00 Israel time: it blocks the operator's working morning, permits midnight
sends, and disagrees with the policy, so a policy-allowed decision fails
`GOVERNOR_DENIED` at the commit boundary. Every dry run attempted during a working
morning fails for a reason indistinguishable from a broken adapter.

### 6.3 Qualified autopilot can never dispatch

`dispatch_qualified_autopilot` defaults `descriptor_resolver` to `adapter_for_url` and
forwards it unconditionally, so `_create_one` never reaches
`effective_live_descriptor_for_plan` and every autopilot send raises
`ADAPTER_NOT_QUALIFIED`. Every existing test injects a resolver, so nothing covers the
production default. Stage 4 could not have worked even with qualification evidence.

### 6.4 A password path exists

`submitters/indeed.py` reads `INDEED_PASSWORD` and types it. ~400 lines of dead v3 code
constructing `IndeedSubmitter(password=…)` are kept unreachable by a single early
`return` in `worker/tasks.py`. Both are deleted; a test asserts no submitter references a
password environment variable.

### 6.5 Israeli parsers fabricate on unreadable pages

A 403, a CAPTCHA page and a soft-404 each return a confident `JobData` — verified to
produce `JobData(title="Are you a robot?")` from a bot check. Fixed by a challenge/error
marker precheck returning `[]`, and by returning `None` when the title came only from a
fallback selector and company, location and all labelled sections are empty.

### 6.6 Discovery finds no Greenhouse jobs

`employer_catalog.yaml` is absent, so `discovery/catalog.py` returns `()` and the mesh
polls only Remotive. Separately, `catalog.py` derives `tenant_key = parts[0]`, so
`boards.greenhouse.io/embed/job_app?token=123` yields `tenant_key='embed'` — a bogus
source 404ing every ten minutes forever, and `'job_app'` as the posting id, which is a
dedup key, producing two Jobs and two submissions per requisition.

## 7. Phases

Each phase states its exit criterion and the operator's manual surface.

This document is the umbrella design for all five phases. It is **not** a single
implementation plan: each phase gets its own plan written against this spec, and P1's plan
in particular cannot be written honestly until P1's markup capture has run, because its
selector work depends on what the capture finds. The immediate plan covers **P0 only**.

**P0 — Profile truth and the live liabilities.** Fix §6.2, §6.3, §6.4, §6.5; extend the
confirmed-fact set; execute the profile bootstrap. Note that editing `user_profile.yaml`
alone accomplishes nothing: `load_versioned_profile_snapshot` reads the
`UserProfileVersion` table and falls back to YAML only when empty, returning
`version=None`, which cannot receive a final-submit permit. Only
`PUT /api/profile/onboarding` and the CV upload route mint a version.
*Exit:* zero `PROFILE_*` reason codes from `GET /api/automation/status`,
`latest_profile_version(db) >= 1`, no password reference in `submitters/`, Israeli
parsers return `[]` for 403/CAPTCHA/soft-404 fixtures, autopilot regression test reaches
`queued` with no injected resolver.
*Operator:* upload the real CV, fill the onboarding form (~20 answers). One sitting.

**P1 — The first employer-confirmed Greenhouse submission.** Capture real markup from a
manual application; re-point selectors to `greenhouse-candidate-v10`; scope the DOM
snapshot; real required-marker detection; reviewed blanks for optional sensitive fields;
refixture and regenerate the qualification report **in one commit**; offline and real-
Chromium rehearsal; real-URL dry run; one live canary.
First target must be a classic server-rendered `boards.greenhouse.io` form with no US EEO
block, no consent checkbox and no cover-letter file input — each of those costs 1–3 real
applications to qualify and none is needed for the proof. Fed by pasting one URL; discovery
is not on this path.
*Exit:* one `Submission` with `outcome == "confirmed_submitted"`, a `SubmissionEvidence`
row satisfying `ck_submissions_confirmed_evidence`, and a `LIVE_CANARY_QUALIFIED`
`AdapterQualificationRecord`.
*Operator:* one manual application inside a capture window (~10 min); watch the visible
browser during dry-run iterations; check the canary and the employer's confirmation email.

**P2 — Zero-touch on Greenhouse.** Per-slot abstention metrics first, as the instrument
that directs all further resolution work. Cover letter from audited material; EEO decline
(the current detector matches no veteran or disability self-identification set); consent
bound to notice text; non-sensitive cross-form reuse; batch inspect and one send button;
per-platform caps and pacing; global ceilings counted as a union of the autopilot
reservation ledger and the operator path; per-adapter challenge breaker and one global stop.
*Exit:* 10 consecutive Greenhouse applications reach `blockers: []` with zero operator
confirmations across ≥3 form shapes; no slot abstains more than once; caps hold under a
25-way concurrency test.
*Operator:* answer each genuinely new question once; one button per batch.

**P3 — Stage 4 and enough discovery to feed it.** The `PROVEN` tier and
`adapter_autonomy_grants` (self-digested, one active row per adapter, 30-day TTL).
Anti-bootstrap: only sends on canary-anchored contract digests count toward
`confirmed_send_count`, so a grant can never re-earn itself from its own unreviewed sends.
Promotion is one authenticated operator call, never automatic. Hard corrections
(`unknown`, reconciliation, `operator_confirmed`) revoke an active grant at the commit
boundary; `already_applied`, `CHALLENGE_DETECTED`, `SESSION_EXPIRED`, `MFA_REQUIRED`,
`JOB_CLOSED` and every retryable denial are explicitly **not** corrections — a metric that
punished safe refusal would create pressure against the CAPTCHA and consent invariants.
Discovery lands here because stage-4 thresholds need a catalog; P1 and P2 need pasted URLs
only. Fixing §6.6 is a prerequisite of this phase, not of the earlier ones.
*Exit:* an active `PROVEN` grant; a previously unseen Greenhouse form shape discovered,
inspected, planned and submitted with zero human interaction and employer-confirmed
evidence; the adversarial suite proves the grant inert on identity drift and revoked on any
hard correction.
*Operator:* hand-verify tenant tokens once when seeding the catalog; one read-only Gmail
OAuth; one grant activation per code release.

**P4 — Expand.** Lever, Ashby, SmartRecruiters (shape-compatible with Greenhouse, so all
of `core/form_planning.py` and `core/submission_domain.py` is shared — but each needs its
own capture and its own canary; nothing transfers). Workday/NVIDIA last, via
`persistent_profile` authentication. Israeli employer career pages as robots-gated generic
sources.
*Exit:* ≥2 additional adapters at `PROVEN`, each with its own canary evidence and cap.

## 8. Not building

- **Israeli-board scraping and submission** (Drushim, AllJobs, JobMaster, jobs.co.il).
  `submitters/israel_boards.py` returns `PORTAL_ADAPTER_REQUIRED`; they cannot be
  auto-submitted regardless of discovery, so a crawler there would be the project's
  largest fabrication and ban surface for zero ladder progress. They remain alert-only
  leads. Revisit after Greenhouse is `PROVEN`.
- **LinkedIn direct search.** `discovery/linkedin_search.py::run_discovery`,
  `discovery/ingest.py` and `discovery/query_builder.py` have no production caller and are
  deleted — a live, importable, highest-ban-risk path one call from re-activation.
  Ingestion is via job-alert email parsing. `LINKEDIN_DESCRIPTOR` stays hard-disabled.
- **LLM answer generation or slot classification at form-fill time.**
- **A batch-review web application** — rich surface for the interaction this design deletes.
  Batch inspect and wiring the existing atomic `batch-submit` survive; the rest does not.
- **`promote_to_profile`**, a second answer-bank API, an onboarding wizard.
- **Multi-file and cover-letter file upload**; async resume upload unless P1's capture
  proves it required.
- **Rebasing `feat/israeli-boards-real`** — verified 154 commits behind `main` and missing
  `mesh.py`, `contracts.py`, `persistence.py` and `http_client.py`; merging it would delete
  the v5 discovery mesh. Delete the branch.
- Any relaxation of `batch-submit` atomicity or of `_current_fixture_digest`, which
  requires a frozen literal gates block or the tier silently collapses to fixture.

## 9. Stop rules

1. **Transport gate (P1 capture):** if the Greenhouse submit is XHR/JSON, or the upload is
   asynchronous and the pinned-endpoint gate cannot be specified safely — stop and
   re-scope. Do not proceed to P2/P3.
2. **Iteration gate (P1 dry run):** if the dry run does not reach `blockers: []` within
   five selector iterations — stop and re-scope.
3. **Payoff gate (P1 canary):** if no employer-confirmed submission after three real
   applications — stop and re-examine the confirmation-evidence contract before spending a
   fourth.

Budget 2–3 real applications to earn the first canary.

## 10. Testing

- Fixture suites per adapter, preserving the existing adversarial case taxonomy and adding
  optional-sensitive reviewed blank, blank radio group, and second-file-control blocked.
- Real-Chromium rehearsal with `HTMLFormElement.prototype.submit` stubbed, asserting no
  request leaves — the last cheap failure surface before a real job is spent.
- Adversarial tests that a mutated `Application.cover_letter` makes the plan **invalid**;
  that a US authorization label cannot resolve from an Israeli fact; that a non-EEO option
  set containing "male"/"female" abstains; that an autonomy grant is inert on identity
  drift and revoked on hard correction; that 25 simultaneous dispatches against
  `daily_limit=5` yield exactly five sends.
- Assert **zero LLM provider calls** on every deterministic sensitive-field path.
