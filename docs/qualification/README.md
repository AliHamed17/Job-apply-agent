# Qualification evidence

The first-five ATS adapters are **fixture-qualified only**. Their committed
evidence consists of 87 sanitized HTML fixtures:

- Workday: 9
- Greenhouse: 22
- Lever: 28
- Ashby: 13
- SmartRecruiters: 15

There have been zero real-URL dry runs, zero live canaries, zero qualified form
fingerprints/scopes, and zero enabled final executors. The presence of an
adapter or a confirmation fixture does not prove that a current employer page
can be submitted.

The paired platform JSON and Markdown reports are the source evidence.
[`adapter-matrix.json`](adapter-matrix.json) and
[`adapter-matrix.md`](adapter-matrix.md) are deterministic aggregate views.
Validate them without changing files:

```powershell
python scripts/build_adapter_qualification_matrix.py --check
```

To intentionally refresh the aggregate after a reviewed report change:

```powershell
python scripts/build_adapter_qualification_matrix.py --write
```

Qualification advances one exact adapter/version/form scope at a time:

`disabled → fixture_qualified → dry_run_qualified → live_canary_qualified`

Fixture qualification uses no employer network, candidate identity, CV
content, answers, cookies, or live application. A dry run must use one explicit
operator-selected URL and stop before the irreversible action. A live canary
requires separate explicit approval for that exact application. Selector,
protocol, form, attachment, request, or evidence drift resets the affected
scope to dry-run qualification.

## Local qualification authority

The checked-in reports remain fixture evidence only. Runtime advancement is
stored privately in `adapter_qualification_records`; the older
`browser_qualification_runs` table is telemetry and never grants inspection,
canary, policy, permit, or final-action authority.

An authenticated operator may qualify one already-prepared local application
with:

```text
POST /api/applications/{id}/qualification/dry-run
acknowledgement = RUN_REAL_URL_DRY_RUN
```

The endpoint refuses to run unless `DRY_RUN=true`, `DRAFT_ONLY=true`, final
submission is disabled, and the runner is the exact current release. It opens
only the explicit application URL, persists a 30-minute immutable form plan,
and records a redacted trace containing control types, resolver categories,
selector identity, attachment state, blockers, and digests. The private form
plan remains in the authoritative local application database. The separate
qualification trace stores no job URL, employer name, field label, question,
answer, CV text, email, cookies, or page content. A successful dry run enables
ordinary inspection only for the exact current adapter and runner release; it
cannot enable final execution.

After reviewing that exact plan, an authenticated operator may authorize one
canary with:

```text
POST /api/applications/{id}/qualification/canary
acknowledgement = SEND_QUALIFICATION_CANARY
```

The grant expires after five minutes and is bound to the application/revision,
job URL digest, form plan/fingerprint/semantic contract, adapter and selector
versions, routed CV hash, runner release, and a one-use nonce. It is consumed
when the durable attempt and command are created. Expired, changed, or replayed
grants fail closed. A timeout or crash after the irreversible action remains
`unknown` and grants no qualification.

Only a finished `confirmed_submitted` attempt with schema-valid employer
evidence promotes its semantic form-contract class to
`live_canary_qualified`. Operator reconciliation, `already_applied`, email
expectation, redirects, clicks, generic success text, legacy telemetry, and
dry-run completion cannot promote a scope. Code, selector, execution-contract,
fixture, or evidence drift removes the effective authority without rewriting
the historical audit rows.

No real URL dry run or live canary was performed while implementing this
framework. The committed first-five matrix therefore correctly remains at
`fixture_qualified`; a future canary still requires the user's exact job
selection and explicit approval.

Drushim and AllJobs alert links that resolve to one of these five ATS families
inherit only that ATS family's effective local qualification. Native Drushim
and AllJobs submission adapters remain disabled because no sanitized fixture,
real-URL dry run, employer-confirmed canary, or evidence contract exists for
their proprietary forms. LinkedIn Easy Apply also remains disabled for
unattended submission; read-only alerts may point to an independently
qualified external ATS.
