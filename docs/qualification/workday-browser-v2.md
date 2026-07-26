# Workday Browser v2 Qualification

Recorded: 2026-07-26

Achieved tier: `fixture_qualified`

This artifact records sanitized offline fixture qualification for
`workday` adapter `2.0.3`, selector `workday-candidate-v2.4`, using the
`two-phase-v2` execution contract.

## Evidence

- Nine sanitized HTML fixtures exercise login, MFA, challenge detection, closed
  jobs, already-applied handling, resume upload, review, selector drift, and
  visible confirmation classification.
- Fixture manifest digest:
  `b803a391f157e3cc98fcbb3ff9f8cf04083beab6f1adb326e66cf09262116fc2`.
- Fixture bytes are normalized to UTF-8 with LF line endings before hashing, so
  the same committed evidence is verified on Windows and Linux checkouts.
- The fixture contract is covered by automated, offline tests.
- Adapter v2.0.3 requires an explicit, exact-job POST action and binds its
  redacted site, requisition, method, and target contract into the form
  fingerprint. Action-less controls fail closed.
- The transport revalidates the exact DOM, form, action, and CV receipt, retains
  one exact browser element, and atomically compares the full live form state,
  reviewed per-field answer digests, and the exact upload marker. Filename-only
  attachment evidence is rejected: the marker and selected file bytes must
  both match the selected CV SHA-256.
- Every successful answer control must belong to exactly one reviewed field
  binding. Unreviewed hidden controls and page defaults fail closed. Only exact
  job/site identity fields and the tiny `_csrf`, `csrfToken`, and `xsrfToken`
  hidden-control set may exist outside reviewed decisions.
- Static parsing, the capture task, and the retained-element atomic task all
  reject a disabled or ARIA-disabled button, an inert ancestor, hidden or
  zero-area geometry, and CSS states that prevent the exact final control from
  being actionable.
- Before arming the gate, the retained form is committed as ordered,
  length-framed FormData including each file's field name, filename, media type,
  byte size, and content SHA-256. The same retained-element task recomputes this
  redacted commitment immediately before native submission.
- The route gate accepts only one exact canonical-URL, `document` resource,
  main-frame navigation POST from the session page. It parses the bounded
  ephemeral multipart or URL-encoded body and recomputes the same commitment
  before releasing bytes. Fetch/XHR, other non-document requests, and iframe
  POSTs fail closed; background GETs do not consume the gate. Raw request data
  and answer values never enter receipts or logs.
- Before that exact one-shot gate is armed, all mutation-capable requests are
  denied. No live upload/save request contract has been qualified, which is one
  reason the live form scope remains empty and final execution remains disabled.
- Post-action browser errors are indeterminate, and a green result additionally
  requires the same visible application reference in the exact post-action
  snapshot and the stable live-browser read.
- No external network request or irreversible final action was performed.

## Remaining gates

- Real-URL dry run: pending.
- Live canary: pending.
- Qualified live form scope: empty.
- Final external action: disabled.

Fixture qualification cannot authorize live submission. A later tier requires
its own exact, reviewed evidence, a non-empty qualified form fingerprint scope,
and exact reversible upload-request and ATS upload-receipt evidence proving the
selected CV bytes before final-request qualification.

## Privacy boundary

The report stores only sanitized fixture identifiers, bounded state and reason
codes, and cryptographic digests. It contains no employer location, candidate
identity, application answers or materials, browser state, page content, or
authentication data.
