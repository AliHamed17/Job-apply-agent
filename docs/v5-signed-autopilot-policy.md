# v5 Signed Qualified-Autopilot Policy

Release 4 adds the authority boundary for unattended submission. It does not
qualify an ATS or enable live submission by itself. The checked-in adapter
matrix remains fixture-only, so the current repository cannot activate a live
autopilot policy.

## Authority invariant

An environment flag, fit score, prepared draft, queue message, or Vercel
command cannot authorize submission. Authority requires all of the following:

1. an authenticated local operator activation;
2. a private local Ed25519 signature over one immutable policy revision;
3. a current profile, CV manifest, routing policy, confirmed-answer revision,
   and held-out fit qualification;
4. at least one exact live-canary-qualified adapter/version/selector/semantic
   form-contract scope;
5. a matching eligible application and immutable `FormPlanV1`;
6. a policy decision reserved under PostgreSQL locking and bounded limits;
7. a five-minute, one-use permit bound to the decision digest; and
8. a final recheck immediately before the irreversible action.

Every failed or uncertain check quarantines the application. A crash or timeout
after the irreversible boundary remains `unknown` and is never retried
automatically. Only ATS-confirmed employer evidence can produce green.

## Local signing identity

Create the key once on the private Windows runner:

```powershell
python scripts/automation_policy_key.py init
python scripts/automation_policy_key.py status
```

The default path is `.job-agent/automation-policy-ed25519.pem`. The path and key
are ignored by Git and Docker. On Windows, the initialization command removes
inherited ACLs and grants the current user read/write access. It refuses to
overwrite an existing key. Back up the key with the same protection as browser
state and candidate profile data; never copy it to Vercel or CI.

Changing or losing the key invalidates existing signed policies. Generate a new
identity only after revoking/quarantining the old runtime state and completing
the recovery review.

## Policy bounds

`AutoSubmitPolicyV1` is frozen and schema-strict. Its hard ceilings are:

- validity of 1–30 days;
- active hours of 08:00–21:00 `Asia/Jerusalem`;
- minimum fit score of 85/100;
- at most 25 reserved submissions per local day;
- at most five reserved submissions in a rolling hour;
- at most two reserved applications per company in 14 days;
- explicitly listed CV role families, geographies, adapters, and qualified
  semantic form contracts.

The limit is reserved before a submission attempt and database command are
created. Repeated dispatches for the same exact decision replay the original
attempt and command. They do not consume another slot or create another
external action.

## Inspection and commit flow

When preparation creates an eligible draft, the worker records one durable
inspection run for the exact application revision and policy revision. Celery
is only a wake-up mechanism; PostgreSQL is authoritative. A 15-minute lease
prevents concurrent browser inspection. A stale lease can be reclaimed with a
new token, while completion from the expired token is rejected.

The reversible inspection observes the live form, resolves only supported
answers, uploads the routed CV, and records attachment evidence. The policy is
then evaluated against the exact semantic form-contract digest. Successful
evaluation creates one decision, attempt, permit, and outbox command in the
same protected path. Policy expiry, revocation, kill-switch state, form drift,
answer revision, attachment identity, adapter qualification, active hours, and
limits are rechecked at the final commit boundary.

No LLM call occurs during the irreversible action.

## Local API

The following endpoints belong only on the private loopback dashboard and use
the configured operator bearer token:

- `GET /api/automation/policy`
- `POST /api/automation/policy/activate`
- `POST /api/automation/policy/revoke`
- `POST /api/automation/kill-switch`
- `GET /api/automation/status`

Activation requires the literal acknowledgement
`ACTIVATE_QUALIFIED_AUTOPILOT`. Revocation requires
`REVOKE_QUALIFIED_AUTOPILOT`. Local emergency-stop changes require an
acknowledgement matching the requested state. The service rejects a missing or
unqualified form scope even if every environment variable requests live mode.

## Vercel emergency stop

The protected redacted control plane exposes an activation-only emergency
stop. It creates a separate Ed25519-signed command that expires after five
minutes and is bound to the current runner boot. The private runner polls this
channel before review-grant publication or application-command polling.

The command contains only an opaque command ID and stable stop reason. It
contains no job URL, company, candidate identity, CV, question, answer, or form
content. Exact envelope-digest replay is audited and idempotent. A remote
command can activate the local stop but can never clear it. Clearing requires
the authenticated local endpoint.

## Recovery

If the policy, signing key, form, CV, profile, qualification artifact, browser
session, or runtime release changes:

1. activate the local kill switch;
2. stop the inspection and submission workers;
3. reconcile any `committing`, `verifying`, or `unknown` attempt manually;
4. revoke the active policy;
5. repair and requalify the changed scope;
6. verify migrations, readiness, and the exact runtime release; and
7. activate a new signed policy revision only after the operator reviews its
   complete scope.

Never convert restored or legacy rows into authority. Restored queued/running
work remains quarantined until its exact private state is reviewed.
