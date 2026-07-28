# Lever Browser v1

Lever Browser v1 is a candidate-facing, versioned two-phase adapter. It is
currently **fixture qualified only**. Its descriptor has an empty qualified
form scope, so neither the dashboard nor a worker can perform a real final
action.

## Exact identity boundary

The adapter accepts only the exact public candidate hosts `jobs.lever.co` and
`jobs.eu.lever.co`. One application is bound to the region host, company site
slug, posting UUID, and canonical `/apply` route. API URLs, suffix lookalikes,
subdomains, credentials in URLs, query strings, fragments, non-default ports,
and non-posting routes fail closed.

The authenticated Lever integration API is a separate, disabled capability.
There is no automatic switch between candidate browser and API transports.
The old one-step submitter is an inert compatibility shim and performs no
network or browser work.

## Two-phase candidate flow

1. Inspection opens a fresh isolated Lever profile, observes the exact form,
   and selects immutable hash-verified CV bytes. It never activates the final
   control.
2. The observer records exact field order, control types, options,
   constraints, sensitivity, answer provenance, selected CV, profile
   revision, adapter version, selector version, and form fingerprint.
3. Any unknown field—including an optional browser default—blocks readiness.
   Consent and other sensitive facts require operator-confirmed evidence.
4. A future live-qualified preflight reconstructs the same form, fills only
   resolved reviewed decisions, and verifies the exact selected CV again.
5. The transport requires one visible enabled submit control, exact action,
   POST method, multipart encoding, and a payload in which every reviewed
   field is bound exactly once. Unknown controls, multiple files, file drift,
   background mutation, and form drift stop before the final action. A
   canonical actionability digest covers the retained button, form, and every
   ancestor—including ancestors outside the form—and is rechecked atomically
   for disabled, ARIA-disabled, inert, hidden, CSS, pointer, and positive-area
   state before native submission.
6. The one native submit is limited to one exact main-frame document POST.
   Any exception or missing evidence after the possible-send boundary is
   `unknown` and is never automatically retried.
7. Green requires one new, visible, exact-posting confirmation carrying a
   stable application reference. A redirect, generic phrase, hidden markup,
   stale confirmation, or HTTP status is not submission evidence.

## Qualification state

- Adapter: `lever` `1.0.0`
- Selector: `lever-candidate-v2`
- Transport: isolated local candidate browser
- Current tier: `fixture_qualified`
- Real-URL dry run: pending explicit operator-selected URL
- Live canary: pending explicit approval for one exact job
- Final submission: disabled

Any selector, payload protocol, form fingerprint, or attachment-proof change
resets the affected qualification scope. CAPTCHA and MFA pause for manual
handling. The adapter never extracts browser passwords and never attempts
challenge bypass, stealth escalation, proxy rotation, or an API fallback.
