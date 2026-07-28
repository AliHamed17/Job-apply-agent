# Employer application automation

## What the system now does

The agent continuously discovers jobs, scores them, selects a configured CV,
generates application material, and places complete applications in the
dashboard. An operator can approve one application or an exact reviewed batch.
Only then can a platform adapter perform an external action.

An application is recorded as submitted only when an official API returns a
successful candidate-creation response or a browser page displays explicit,
application-specific confirmation. A click, redirect, timeout, generic
“success” word, or exception is never treated as proof.

## Safety boundaries

- Browser and Edge/Chrome password stores are never read.
- Portal passwords are never accepted or persisted by this project.
- CAPTCHA, MFA, security challenges, and expired sessions stop for review.
- Sensitive answers require `evidence.user_confirmed` in the private profile.
- Generated Q&A cannot override confirmed-evidence rules.
- `DRY_RUN=true` prevents every platform adapter from being invoked.
- A score is eligibility for review, not approval.
- Unknown post-submit outcomes move to `NEEDS_REVIEW` and cannot retry until
  reconciled.

## Pipeline

1. Discovery deduplicates a job and stores its public URL.
2. Matching scores the role against the private profile.
3. CV routing selects one configured CV or abstains.
4. Generation produces a cover letter, recruiter message, and non-sensitive
   answer candidates.
5. The application remains a draft.
6. The operator reviews one item or selects an exact batch.
7. A submission attempt is committed as `running` before any external action.
8. The platform adapter fills known fields and stops on unknown required data.
9. The final state is one of `success`, `failed`, `draft_only`, or `unknown`.
10. A redacted event and attempt trace is displayed in Automation history.

## One-time employer sign-in

Authenticated Workday tenants use a dedicated Playwright profile per hostname.
Install the browser dependency and Chromium, then bootstrap the exact employer
portal once:

```powershell
pip install -e ".[browser]"
playwright install chromium
python -m scripts.portal_session_bootstrap "https://employer.wd5.myworkdayjobs.com/job/..."
```

Sign in and complete MFA directly on the employer page. When the account page
is visible, return to the terminal and press Enter; only that explicit
confirmation marks the session ready. Browser state is saved under
`.portal_profiles/<hostname>/`, which is ignored by Git and must be treated
like a secret. Run the bootstrap again only when the portal says the session
expired.

Do not point the application at an active Chrome or Edge profile and do not
copy their password database. The dedicated profile avoids password extraction
and prevents one employer from receiving another employer's cookies.

## Workday and NVIDIA flow

The reusable Workday adapter implements the sanitized flow qualified on the
NVIDIA tenant:

1. Open the exact job and click Apply.
2. Prefer **Use My Last Application** when the tenant offers it.
3. Reuse existing identity, experience, education, links, and resume.
4. For NVIDIA, answer the source hierarchy using
   **Website → NVIDIA.COM**.
5. Resolve remaining required questions through `FormBrain`.
6. Stop if confirmed evidence is missing.
7. Reach Review.
8. Click Submit only when `PORTAL_FINAL_SUBMIT_ENABLED=true` and the database
   application was explicitly approved.
9. Require Workday's submitted/already-applied confirmation.

The NVIDIA source path is public workflow metadata, not candidate data. Other
employers receive a generic Workday policy. Add known, non-personal tenant
details to a private `employer_workflows.yaml`, using
`employer_workflows.yaml.example` as the schema. An unknown source value causes
review rather than a guess.

## Confirmed profile evidence

Keep personal facts only in the ignored/private profile. Examples of keys a
portal may ask for are:

```yaml
evidence:
  user_confirmed:
    authorized to work: "<confirmed answer>"
    visa sponsorship: "<confirmed answer>"
    citizenship: "<confirmed answer>"
    nationality: "<confirmed answer>"
    gender: "<confirmed answer>"
    terms and conditions: "<confirmed answer>"
```

Legal, authorization, clearance, certification, nationality, demographic,
terms, consent, and attestation questions never use CV extraction, an LLM, or
generated Q&A as factual authority.

## Review and batch approval

In the Applications view:

1. Open each draft, verify the selected CV and application content.
2. For Workday, confirm that the card says **Session ready**.
3. Approve individually, or select multiple reviewed cards and choose
   **Approve selected**.
4. The batch API accepts only exact IDs plus the acknowledgement value
   `APPROVE_SELECTED_APPLICATIONS`.

The score-filtered compatibility endpoint `/api/control/batch-apply` is now
read-only. It returns candidates but never changes their state. Exact approval
uses:

```http
POST /api/applications/batch-approve
{
  "application_ids": [12, 15],
  "acknowledgement": "APPROVE_SELECTED_APPLICATIONS"
}
```

## Platform coverage

The detector recognizes Workday, Greenhouse, Lever, Ashby, Workable,
SmartRecruiters, Jobvite, iCIMS, Comeet, LinkedIn, and Indeed. Detection is not
submission qualification. Workday v2, Greenhouse v1, Lever v1, and Ashby v1 have
versioned candidate-browser implementations, but all remain fixture-qualified
with empty live form scopes. Their real-URL inspection and final action are
disabled.
Employer API transports are separate and disabled unless legitimate,
tenant-bound authorization is explicitly implemented and qualified.
Candidate-browser and API transports never silently switch between each other.

Every candidate-browser adapter shares the same rules:

- required unknown field → `REQUIRED_FIELD_UNKNOWN`;
- security challenge → review/cooldown;
- missing selector before submit → `SELECTOR_DRIFT`;
- possible final action without exact employer confirmation →
  `FINAL_ACTION_UNCONFIRMED` and `unknown`;
- unsupported portal → reviewable draft.

Employer pages change. A fixture-qualified adapter proves only its sanitized
offline contract. It does not claim current tenant support, real-URL
qualification, or permission to submit.

## Configuration

Safe defaults:

```dotenv
DRAFT_ONLY=true
AUTO_APPLY=false
DRY_RUN=true
PORTAL_FINAL_SUBMIT_ENABLED=false
PORTAL_BROWSER_PROFILE_ROOT=.portal_profiles
PORTAL_REUSE_LAST_APPLICATION=true
```

For a controlled approved submission worker:

```dotenv
DRAFT_ONLY=false
DRY_RUN=false
PORTAL_FINAL_SUBMIT_ENABLED=true
LIVE_AUTOMATION_ACKNOWLEDGED=true
```

Production startup rejects contradictory or unacknowledged live settings.
Keep the final-submit flag false until fixture tests and a review-only run pass
for the employer tenant.

## Audit and reconciliation

Every approval and outcome has durable provenance (`manual`, `batch`,
`whatsapp`, or `retry`). Attempt history records sequence, idempotency key,
platform, selected CV identifier, profile version, reason code, timestamps,
confirmation, and a bounded redacted trace.

Traces contain only selector version, transitions, field types, resolver
sources, terminal reason, and timestamps. They do not retain answers, names,
email addresses, phone numbers, CV text, cookies, page HTML, or job URLs.

If a final click has no confirmation:

1. The attempt becomes `unknown`.
2. The application becomes `NEEDS_REVIEW`.
3. Inspect the employer candidate dashboard or confirmation email.
4. Call `/api/applications/{id}/reconcile` with
   `confirmed_submitted` or `confirmed_not_submitted`.
5. Only a reconciled, definitively not-submitted application may be retried.

## Deployment note

The browser worker needs a long-lived machine with Chromium and persistent,
encrypted profile storage. A serverless Vercel function has ephemeral
filesystem/process limits and must not be used for the signed-in browser
worker. Vercel may host a separately configured dashboard/control plane, while
PostgreSQL, Redis, Celery, and browser workers run on a private persistent host.
