# Greenhouse Browser v1

Greenhouse Browser v1 is a candidate-facing, versioned browser adapter. It is
currently **fixture qualified only**. The central descriptor has an empty
qualified form scope, so the ordinary API cannot open an employer form and the
final-action registry cannot select it for a real submission.

## Safe flow

1. Offline inspection observes hosted, embedded, and job-ID form variants,
   exact controls, compliance sections, consent, and attachment state without
   clicking the final action.
2. The selected CV is read and hash-verified once, then selected from immutable
   in-memory bytes under a non-identifying session filename. The current
   fixture contract permits only a local file-input update; every HTTP mutation
   remains blocked until the exact final native form POST is armed. A form that
   starts an asynchronous upload therefore stops closed.
3. Required unknown, unsupported, sensitive, consent, validation, attachment,
   or conditional-form uncertainty produces a partial plan and stops.
4. A future qualified preflight must open a new ephemeral browser, replay only
   reviewed decisions, bind the exact candidate identity, form fields and
   variant, form fingerprint, form and button handles, action-critical hidden
   and visible successful controls, every exact reviewed answer value and
   blank/unchecked state, the exact reviewed resume-control name and CV hash,
   exact resolved action URL, native multipart transport, ordered payload
   commitment, DOM commitment, exact retained button/form association, final
   control actionability through every outer ancestor, and CV receipt, then
   expose one opaque prepared final action. Disabled, ARIA-disabled, inert,
   hidden, CSS-non-actionable, detached, or zero-area controls stop closed.
   The bindings retain only hashes, field types, and bounded entry counts—not
   raw answers. It makes no LLM call.
5. The worker persists `committing` and consumes a one-use permit before the
   atomic commit primitive revalidates every binding. In one browser task, the
   primitive arms a single-use outbound gate and invokes the native form POST
   while bypassing page submit handlers. The gate permits only the exact
   retained action URL, POST method, main-frame document navigation, qualified
   transport, ordered successful-control payload, exact reviewed answer
   bindings, and routed CV content hash under the reviewed resume control.
   A named-submitter proxy is inserted before the final release boundary, then
   the exact ordered payload and unchanged form are rechecked with that proxy
   present. Immediately before native submit, with no intervening DOM mutation
   or await, the retained control rechecks disabled state,
   ARIA/inert/hidden/CSS state through ancestors outside the form, geometry,
   connectivity, and exact form association. Unknown, omitted, duplicated,
   defaulted, changed, or non-actionable controls abort before the request
   leaves. False results and exceptions during the final probe remove the proxy
   and restore the original form payload before returning `FORM_CHANGED`.
   Once the intrinsic submit method is invoked, the primitive preserves the
   proxy and reports invocation even if that call throws, because a request may
   already have started; the outbound gate and employer evidence then determine
   ambiguity and truth.
6. Once the intrinsic method is invoked, a missing gate event is still
   retry-unsafe and becomes typed `unknown` with
   `FINAL_ACTION_UNCONFIRMED`. Every atomic stage/reason combination maps to a
   domain-valid outcome; malformed combinations fail closed without leaking a
   validation exception. After gate arming, evaluation exceptions, context
   loss, `None`, malformed values, and unknown status strings are ambiguous;
   only exact `FORM_CHANGED` or `ATTACHMENT_UNVERIFIED` script returns prove a
   pre-request stop. A gate-observed request overrides any contradictory
   pre-request status and forces invoked/unknown semantics. Only a proven
   mismatch before intrinsic invocation stops for review or a definitive
   pre-commit failure without sending.
7. Only one fresh, visible, stable Greenhouse employer reference bound to the
   exact attempt and verified CV can become employer-confirmed. A redirect,
   HTTP success, generic thank-you text, hidden or pre-existing markup, and an
   email expectation are never proof. The evidence reference must equal the
   canonical digest of the exact post-action snapshot.

## Transport separation

The browser adapter never reads Chrome or Edge password stores. It does not
solve CAPTCHA, bypass MFA, escalate stealth, or rotate proxies. The retired
Harvest/browser hybrid is a network-incapable compatibility shim. A future
Greenhouse API transport would require legitimate employer-issued,
tenant-bound authorization and a separate qualification; a configured legacy
API key cannot activate it or change browser behavior.

## Qualification state

- Adapter: `greenhouse` `1.0.0`
- Selector: `greenhouse-candidate-v9`
- Contract: `two-phase-v2`
- Current tier: `fixture_qualified`
- Real-URL dry run: pending
- Live canary: pending explicit approval for one exact job
- Qualified live form scope: empty
- Final submission: disabled

Asynchronous resume-upload POSTs are not part of this fixture-qualified
contract. Supporting one later requires a new selector/protocol version with an
exact qualified endpoint, method, resume control, CV digest, upload receipt,
and adversarial regression set; broad path or substring upload allowances are
prohibited.

Any selector, protocol, field contract, form fingerprint, attachment proof, or
evidence-rule change invalidates the affected qualification. The checked-in
qualification report contains only sanitized fixture names, bounded state and
reason codes, and cryptographic digests.
