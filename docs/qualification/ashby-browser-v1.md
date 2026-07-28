# Ashby Browser v1 Qualification

Recorded: 2026-07-27

Achieved tier: `fixture_qualified`

This artifact records sanitized offline fixture qualification for adapter
`ashby` `1.0.0`, selector `ashby-candidate-v1`, and execution contract
`two-phase-v2`.

## Evidence

- Thirteen sanitized fixtures cover exact application forms, React conditional
  controls, validation errors, upload progress, challenge, login, MFA, closed
  and already-applied states, confirmation, selector drift, and the
  `:has(proxy)` actionability adversary.
- Fixture manifest digest:
  `4f4ad23bc7f646929a7029abf30520c607069149735a6f40c02b9f461e532dc6`.
- The manifest hashes the exact committed fixture bytes; `.gitattributes`
  enforces LF line endings for sanitized HTML fixtures.
- Offline contract tests bind exact candidate identity, ordered rendered
  fields, answer digests, selected CV bytes, upload receipt, multipart payload,
  main-frame document request, and fresh confirmation evidence.
- A real local Chromium regression proves the final action inserts no submitter
  proxy, so proxy-sensitive CSS cannot invalidate the adjacent actionability
  check.
- An adapter-level regression proves final primitive invocation without an
  observed outbound request returns non-retryable `unknown`, never a validation
  exception or reviewable outcome.
- Once the one-use gate is armed, only exact `FORM_CHANGED` and
  `ATTACHMENT_UNVERIFIED` script results with no gate signal are provably
  pre-request. Evaluation exceptions, context loss, null, malformed or unknown
  results, and any contradictory gate signal are non-retryable `unknown`.
- No external request or irreversible employer action was performed.

## Remaining gates

- Real-URL dry run: pending.
- Live canary: pending.
- Qualified live form scope: empty.
- Final external action: disabled.

Fixture qualification cannot authorize live submission. Selector, protocol,
form, attachment, request, or evidence changes invalidate this report and
require a new qualification cycle.

## Privacy boundary

This report stores sanitized fixture filenames, bounded states and reason
codes, and cryptographic digests only. It contains no employer URL, candidate
identity, answers, CV content, cookies, browser state, or authentication data.
