# Ashby Browser v1

Ashby Browser v1 is a candidate-facing, versioned browser adapter. It is
currently **fixture qualified only**. Its descriptor has an empty qualified
form scope, so neither the dashboard nor the final-action registry can select
it for a real employer application.

## Candidate identity

The adapter accepts only the exact public candidate origin and routes:

- `jobs.ashbyhq.com/{board}/{posting-UUID}`
- `jobs.ashbyhq.com/{board}/{posting-UUID}/application`

The scheme must be HTTPS, the UUID must be canonical lowercase, and optional
query data is limited to bounded tracking keys. Near-match hosts, credentials,
alternate ports, fragments, API origins, and additional path segments fail
closed.

## Safe browser contract

1. React-rendered fields are observed in deterministic DOM order. A conditional
   field exists in the reviewed contract only while its explicit render marker
   agrees with actual visibility. Removing, reordering, redefining, or newly
   rendering a field changes the form contract and requires new review.
2. Every rendered control maps to exactly one reviewed field and control name.
   The only unreviewed successful controls are explicit hidden system fields.
   Every reviewed field must have exactly one typed answer decision.
3. The routed CV is read once from the private runner, SHA-256 verified, and
   uploaded under the exact reviewed file control. A fresh upload receipt must
   expose the exact generated filename and selected-CV digest. Filename-only,
   pending, failed, stale, duplicate, or wrong-control evidence is rejected.
4. The final request is one exact multipart POST to the canonical application
   route. It must be a main-frame `document` navigation from the retained page.
   Fetch/XHR, iframe, alternate target, different posting, extra fields,
   changed answers, wrong CV bytes, malformed multipart, and duplicate requests
   are aborted before bytes leave.
5. Browser-side capture uses `FormData(form, button)` and native
   `requestSubmit(form, button)`. It inserts no hidden submitter proxy. This
   prevents a `:has(proxy)` selector from changing final actionability between
   payload capture and release. The last computed-style and geometry check is
   immediately adjacent to the native primitive with no intervening DOM
   mutation or await.
6. Before the primitive is invoked, exact form, answer, attachment, or
   actionability drift is review-required. Once the final primitive is invoked,
   missing outbound evidence, a thrown submit handler, context loss, timeout,
   or missing confirmation is always `unknown` and cannot be retried
   automatically. After the one-use gate is armed, null, malformed, or
   unrecognized browser results and contradictory gate signals are also
   `unknown`; only the two exact pre-request failure statuses remain
   reviewable.
7. Only one new, visible, stable Ashby confirmation reference observed after
   the exact request may become employer-verified. Generic text, an HTTP 2xx,
   a redirect, a pre-existing marker, or email expectation never becomes
   green.

## Quarantined transports

The legacy `posting-public/application/create` request and its HTTP 200/201
success shim have been removed. The separately documented employer API
transport remains disabled and has no HTTP client. A later API release would
require legitimate employer-issued Basic authentication with the
`candidatesWrite` permission and separate qualification; browser mode never
silently falls back to it.

## Qualification state

- Adapter: `ashby` `1.0.0`
- Selector: `ashby-candidate-v1`
- Execution contract: `two-phase-v2`
- Current tier: `fixture_qualified`
- Real-URL dry run: pending
- Live canary: pending explicit approval for one exact job
- Qualified live form scope: empty
- Final external action: disabled

No real employer URL, candidate identity, CV, application answer, browser
session, or external mutation was used for this fixture release.
