# Workday Browser v2 Qualification

Recorded: 2026-07-26

Achieved tier: `fixture_qualified`

This artifact records sanitized offline fixture qualification for
`workday` adapter `2.0.0`, selector `workday-candidate-v2`, using the
`two-phase-v2` execution contract.

## Evidence

- Nine sanitized HTML fixtures exercise login, MFA, challenge detection, closed
  jobs, already-applied handling, resume upload, review, selector drift, and
  visible confirmation classification.
- Fixture manifest digest:
  `2f0dcda0a633b37472010ca7d87ba3681a7b9a28f39ecba893c04020c252dc99`.
- The fixture contract is covered by automated, offline tests.
- No external network request or irreversible final action was performed.

## Remaining gates

- Real-URL dry run: pending.
- Live canary: pending.
- Qualified live form scope: empty.
- Final external action: disabled.

Fixture qualification cannot authorize live submission. A later tier requires
its own exact, reviewed evidence and a non-empty qualified form fingerprint
scope.

## Privacy boundary

The report stores only sanitized fixture identifiers, bounded state and reason
codes, and cryptographic digests. It contains no employer location, candidate
identity, application answers or materials, browser state, page content, or
authentication data.
