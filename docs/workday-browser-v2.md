# Workday Browser v2

Workday Browser v2 is a candidate-facing, versioned browser adapter. It is
currently **fixture qualified only**. Its descriptor has an empty
live-canary form scope, so the final-action registry cannot select it for a
real submission.

The browser extra requires Playwright 1.48 or newer because the adapter blocks
WebSockets before creating a page. Older Playwright releases fail the runtime
readiness check and cannot inspect or submit.

## Safe flow

1. `POST /api/applications/{id}/inspect` opens the dedicated local Workday
   browser profile and observes the current form. Because this version is only
   fixture-qualified, its transport denies every mutation-capable network
   request before a final gate exists. A real upload/save endpoint therefore
   remains blocked until a later version qualifies an exact reversible request
   contract. Inspection fails closed rather than accepting unverified CV
   evidence. It creates no attempt or submission command.
2. The dashboard displays exact field types, options, constraints, answer
   provenance, blockers, adapter version, selector version, form fingerprint,
   and redacted selected-versus-attached CV references and hash prefixes. The
   attachment evidence source and recording time are persisted and displayed;
   a boolean assertion without those fields cannot enable preparation.
3. `Prepare application` is unavailable until the plan is current,
   answer-complete, evidence-bounded, attachment-verified, and bound to the
   current application, profile, and CV revisions. Persisting any different
   immutable plan for the same revision atomically clears prior preparation,
   so preparation cannot remain attached to an earlier plan fingerprint.
4. A future live-qualified worker preflight must open a fresh browser session
   from a private, hash-verified `AdapterPreflightContext`. API browser objects
   never cross a process, event loop, or operator-review delay.
5. Worker preflight and the single final action share one event loop. The
   database commits `committing` and consumes the permit before that action.
   Immediately before it, v2.0.3 re-verifies the exact DOM, attachment
   receipt, tenant, career site, requisition, reviewed fields, form fingerprint,
   explicit POST target, and absence of pre-existing confirmation. It retains
   one exact browser element and binds each live value to a reviewed,
   non-reversible answer digest. File bindings use the selected CV SHA-256, not
   a generic upload sentinel; filename-only evidence is rejected. Every
   successful answer control must map exactly once to a reviewed, resolved
   field binding. Unknown hidden controls and page defaults fail closed. The
   only unreviewed successful controls permitted are exact job/site identity
   fields and exact `_csrf`, `csrfToken`, or `xsrfToken` hidden controls.
   The retained final button must remain enabled, not ARIA-disabled, outside
   every inert subtree, visibly rendered with positive geometry, and
   pointer-actionable at both structural checks. The Python boundary consumes
   those exact capture facts and rejects a missing or contradictory value.
   The retained form is committed as ordered, length-framed FormData. File
   entries bind field name, filename, media type, size, and SHA-256 of the
   actual bytes. The same retained-element task recomputes that redacted
   commitment immediately before calling native form submission.
   A one-shot route gate then requires an exact canonical URL, POST method,
   main-frame navigation, `document` resource type, and the session page's own
   main frame. It parses the
   bounded ephemeral URL-encoded or multipart body and recomputes the same
   commitment before releasing bytes. Fetch/XHR, non-document or iframe POSTs,
   wrong payloads, and duplicate POSTs are aborted. Background GETs do not
   consume the gate.
   Raw answers, CV bytes, and request bodies never enter the transport receipt
   or logs. Any uncertainty after the exact request is released is
   indeterminate.
6. Only a new, visible Workday confirmation whose application reference is
   identical in the exact post-action snapshot and the stable live browser read
   can become employer-verified. Already-applied state is reported separately.

## Qualification state

- Adapter: `workday` `2.0.3`
- Selector: `workday-candidate-v2.4`
- Transport: isolated persistent local browser profile
- Current tier: `fixture_qualified`
- Real-URL dry run: pending explicit operator-selected URL
- Live canary: pending explicit approval for one exact job
- Final submission: disabled

Any selector, protocol, form fingerprint, or attachment-verification change
invalidates the affected qualification evidence. CAPTCHA and MFA pause for
manual handling. The adapter never reads Chrome or Edge password stores and
never attempts challenge bypass, stealth escalation, or proxy rotation.

Real qualification additionally requires a separately versioned, exact
reversible upload request contract and ATS upload receipt that prove the
selected CV bytes. This fixture-only release has no such live evidence, keeps
the qualified scope empty, and cannot enable final submission.
