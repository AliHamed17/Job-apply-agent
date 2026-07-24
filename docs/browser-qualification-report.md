# Browser qualification report

Date: 2026-07-24

Scope: automated and sanitized-fixture qualification only. No live LinkedIn URL
was supplied, no live smoke run was performed, and no application was
submitted.

## Results

- Guard refuses `DRY_RUN=false`: pass.
- Guard refuses missing or incorrect operator authentication: pass.
- Guard refuses non-LinkedIn URLs: pass.
- Dry-run walker reaches the final-submit control without clicking it: pass.
- Dry-run walker confirms the discard chain and terminates
  `DRY_RUN_DISCARDED`: pass.
- A failed discard fails qualification: enforced by implementation.
- Required unknown facts terminate `REQUIRED_FIELD_UNKNOWN`: pass.
- CAPTCHA detection never solves or bypasses the challenge and trips cooldown:
  pass.
- Trace allowlist excludes answers, field labels, CV text, cookies, page
  content, personal identifiers, and URLs: pass.
- Sanitized fixtures cover common Easy Apply, required-field refusal, resume
  upload, session expiry, selector drift, CAPTCHA, and missing confirmation:
  pass.

Full local regression result: 221 passed, 1 PostgreSQL-only skipped.

Live qualification remains intentionally pending until an authenticated
operator supplies one explicit LinkedIn job URL and runs the guarded command
with `DRY_RUN=true`. A live result qualifies only if the report ends with
`qualified: true` and `terminal_reason: DRY_RUN_DISCARDED`.
