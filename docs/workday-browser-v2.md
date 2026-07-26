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
   browser profile, observes the current form, and may upload or replace the
   routed CV to obtain browser-observed upload-complete evidence. It builds an
   expiring `FormPlanV1`, closes the browser, and stores the immutable plan. It
   creates no attempt or submission command and never clicks final submit.
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
5. Worker preflight and the single final click share one event loop. The
   database commits `committing` and consumes the permit before that click.
6. Only a new, visible Workday confirmation matching the exact attempt and CV
   can become employer-verified. Already-applied state is reported separately.

## Qualification state

- Adapter: `workday` `2.0.0`
- Selector: `workday-candidate-v2`
- Transport: isolated persistent local browser profile
- Current tier: `fixture_qualified`
- Real-URL dry run: pending explicit operator-selected URL
- Live canary: pending explicit approval for one exact job
- Final submission: disabled

Any selector, protocol, form fingerprint, or attachment-verification change
invalidates the affected qualification evidence. CAPTCHA and MFA pause for
manual handling. The adapter never reads Chrome or Edge password stores and
never attempts challenge bypass, stealth escalation, or proxy rotation.
