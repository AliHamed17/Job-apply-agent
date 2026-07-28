# Lever Browser v1 Qualification

Recorded: 2026-07-27

Achieved tier: `fixture_qualified`

This artifact records sanitized offline fixture qualification for the `lever`
adapter `1.0.0`, selector `lever-candidate-v2`, using the `two-phase-v2`
execution contract.

## Evidence

- Twenty-eight sanitized HTML fixtures cover exact candidate identity, standard
  and custom controls, selected-CV attachment, consent, login, MFA, challenge,
  closed and already-applied states, selector drift, form-action drift,
  unreviewed controls, prompt injection, outer-wrapper actionability, a CSS
  `:has(...)` mutation guard, and exact visible confirmation.
- Fixture manifest digest:
  `24a32ca624d34c99d3c3d275c5ea8a6e1351f88c6ad99a7bcc77cd354e01f215`.
- Adversarial tests bind every reviewed decision and the selected CV bytes to
  the exact native multipart payload.
- No external network request or irreversible final action was performed.

## Remaining gates

- Real-URL dry run: pending.
- Live canary: pending.
- Qualified live form scope: empty.
- Final external action: disabled.

Fixture qualification cannot authorize live submission. A later tier requires
its own exact reviewed evidence, a non-empty form-fingerprint scope, and a new
selector version if the real candidate form differs from these fixtures.

## Privacy boundary

The report stores only sanitized fixture identifiers, bounded state and reason
codes, and cryptographic digests. It contains no employer location, candidate
identity, application answer, CV content, browser state, page content, or
authentication data.
