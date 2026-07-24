# LinkedIn dry-run qualification

The smoke command accepts one explicit LinkedIn job URL and is intentionally
incapable of qualifying a real submission. It refuses to start unless:

- `DRY_RUN=true`;
- `SECRET_KEY` is non-default; and
- `JOB_AGENT_OPERATOR_TOKEN` exactly matches `SECRET_KEY`.

Run:

`python scripts/linkedin_dry_run_smoke.py --url "https://www.linkedin.com/jobs/view/..." --report linkedin-smoke-qualification.json`

The operator token is read from the environment so it is not placed in command
history. The command uses the existing authenticated browser profile, walks the
Easy Apply form, and must successfully discard the modal immediately before
the final submit action. A discard failure fails qualification.

The report and database record contain only selector version, step transitions,
field types, resolver-source categories, terminal reason, timestamps, and the
qualification result. They never retain the job URL, names, field labels,
answers, CV text, cookies, page HTML, emails, or phone numbers.

Sanitized fixtures cover common Easy Apply, required-field refusal, resume
upload, session expiry, selector drift, CAPTCHA, and missing confirmation.
Failure clusters appear in the dashboard by selector version and stable reason.

CAPTCHA solving, stealth escalation, proxy rotation, and real submission are
prohibited. A CAPTCHA trips the existing cooldown and fails qualification.
