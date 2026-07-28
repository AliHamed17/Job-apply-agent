# SmartRecruiters candidate browser v1

SmartRecruiters candidate browser v1 is **fixture qualified only**. Its
descriptor is version `1.0.0`, selector
`smartrecruiters-candidate-v1`, and has an empty qualified form scope. Final
external execution therefore remains disabled.

## Candidate and API transports are separate

The browser adapter accepts only the exact public candidate host and a numeric
public posting route. It resolves the posting UUID from one cross-checked,
read-only candidate-page metadata observation. It never converts a public ID,
slug, or title into a UUID.

The protected SmartRecruiters Application API is a separate, disabled
capability. It requires OAuth scope `candidate_applications_manage`, an
explicitly enabled transport, and an exact qualified posting scope. The legacy
one-step shim is inert: it cannot call an API, accept an HTTP 2xx as proof, or
fall back silently between API and browser transports.

## Review contract

The candidate form observer preserves one global order for:

- identity and document controls;
- screening questions and exact option identifiers;
- repeatable groups and their indexes;
- currently visible conditional branches;
- voluntary diversity controls;
- privacy, AI, imprint, diversity, and informational disclosures; and
- consent or attestation controls bound to those disclosures.

Missing privacy-policy markup creates a visible synthetic
`no_privacy_policy_notice` in the review plan. The persisted/API representation
for disclosures contains bounded public summaries and SHA-256 references; it
never adds candidate answers or raw link targets.

Demographic, consent, attestation, legal, authorization, nationality,
citizenship, clearance, certification, and other sensitive facts remain
operator-confirmed only. A changed conditional branch or disclosure set
invalidates the form fingerprint and requires reinspection.

## Attachment and final action

The browser transport uses a dedicated local Playwright profile. It does not
read Chrome or Edge password stores. The selected CV is hash-verified before
the browser sees it. The final multipart request must contain those exact bytes
once and must target:

`/candidate-experience/postings/{observed-posting-uuid}/applications`

The final browser call rechecks the retained native submit button, every
reviewed control, upload marker, disclosure, form target, validity, and the
entire ancestor actionability chain. The check catches outer `aria-disabled`,
`inert`, hidden, zero-geometry, `pointer-events`, `content-visibility`, and
`:has(...)` proxy guards. It performs no await or DOM mutation between the last
recheck and `requestSubmit`.

Once the native primitive is invoked, a missing, rejected, timed-out, or
otherwise ambiguous outbound gate is `unknown` with
`FINAL_ACTION_UNCONFIRMED`. It is never a retryable pre-send failure. Green
requires a new, exact, visible SmartRecruiters confirmation bound to the
posting UUID and an application reference; generic success text, hidden or
pre-existing markup, a redirect, or an HTTP status is insufficient.

## Qualification status

- Fixture contract: passed.
- Real-URL dry run: pending.
- Live canary: pending.
- Qualified form scope: empty.
- Final external action: disabled.

No real employer URL, candidate identity, CV content, answers, browser state,
or external request was used for this fixture-only qualification.
