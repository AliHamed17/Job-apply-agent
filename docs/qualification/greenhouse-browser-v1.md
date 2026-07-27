# Greenhouse Browser v1 Qualification

Recorded: 2026-07-27

Achieved tier: `fixture_qualified`

This artifact records sanitized offline fixture qualification for the
`greenhouse` adapter `1.0.0`, selector `greenhouse-candidate-v9`, using the
`two-phase-v2` execution contract.

## Evidence

- Twenty-two sanitized HTML fixtures exercise hosted, embedded, and job-ID forms;
  sensitive consent controls; conditional fields; login, challenge, closed,
  and already-applied states; attachment verification; validation errors;
  selector drift; inert final controls; named-submitter CSS actionability
  drift; and strict visible confirmation classification.
- Fixture manifest digest:
  `98fd27896ab0be06beecd64572a24fdedcc0fb62f96993686278bb80417ee4b7`.
- Fixture bytes are normalized to UTF-8 with LF line endings before hashing, so
  the same committed evidence is verified on Windows and Linux checkouts.
- Every manifest case is reassessed by the offline
  `assess_greenhouse_v1_snapshot` contract test.
- Offline adversarial contract tests prove the atomic boundary revalidates the
  exact candidate identity, fields and variant, form fingerprint, retained
  form and submitter, every successful action-identity control, each exact
  reviewed answer value or blank/unchecked state, DOM commitment, routed-CV
  receipt under the exact reviewed resume-control name, and ordered native
  multipart payload commitment. The tests block non-navigation fetches, all
  unarmed mutation methods (including profile and upload-like paths), route
  aliases, optional sensitive or operator-required defaults, changed answers,
  missing or duplicate controls, CV bytes under a cover-letter control, same
  filename with wrong bytes, duplicate resumes, and extra files before they
  leave. Static readiness, Playwright enabled/capture checks, the Python
  boundary, and the final retained-control atomic check all reject disabled,
  ARIA-disabled, inert, hidden, CSS-non-actionable, detached, zero-area, or
  wrongly associated final controls, including state on an ancestor outside
  the committed form. A real-Chromium loopback regression applies the exact
  `:has()` rule that makes the final control non-actionable only after the
  named-submitter proxy appears. It proves that the proxy is inserted before
  the final release checks, the control is rejected immediately before the
  native call, no submit call occurs, and the proxy is removed. A second
  real-Chromium case makes `getClientRects()` throw only on the third and final
  actionability probe. The primitive returns `FORM_CHANGED` without rejecting
  its evaluation, invokes no submit, removes the proxy, and restores the
  original `FormData`. A companion case throws from the intrinsic submit stub
  after invocation; the primitive preserves the proxy and reports invocation
  so the outbound gate, rather than a false pre-request downgrade, decides
  ambiguity. Adapter-level tests prove an invoked final action with no
  gate-observed request becomes typed, non-retryable `unknown`, never a
  validation error or retryable review result. A total reason/stage table
  exercises every stable reason code before invocation, after invocation, and
  after a request may have left. Transport tests prove that evaluation/context
  loss, `None`, malformed objects, and unrecognized statuses after gate arming
  are all non-retryable unknowns. Only exact `FORM_CHANGED` and
  `ATTACHMENT_UNVERIFIED` script returns prove a pre-request stop. If the gate
  observed a request but the script returned a contradictory pre-request
  status, the gate wins and the result remains invoked and unknown. The tests
  also reject confirmation digests that do not match the exact post-action
  snapshot.
- No external network request or irreversible final action was performed.

The fixture-qualified transport permits local file selection only. It blocks
any asynchronous upload mutation. A later async-upload contract must qualify
one exact endpoint, method, resume control, CV digest, and upload receipt under
a new selector/protocol version; no broad upload route allowance is accepted.

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
