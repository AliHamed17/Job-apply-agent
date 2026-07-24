# CV routing and application quality

Copy `cv_routing.yaml.example` to the ignored `cv_routing.yaml` file and place
the referenced PDFs in the ignored `cvs/` directory. CV ids are stable audit
identifiers; filenames and CV content are never committed.

Routing is deterministic. Ordered overrides run first, followed by weighted
title, required-skill, description, and seniority evidence. A route below
`minimum_confidence` either uses the configured fallback with an explicit
reason or abstains. An abstained application cannot be approved until the
operator previews the route or selects an override in the dashboard.

Every application and submission attempt records the selected CV id and profile
version. Applications also retain confidence, matched evidence, fallback
reason, and any operator override.

## Profile evidence

Profile facts are separated under `evidence`:

- `cv_extracted`: facts parsed from a CV, not independently confirmed;
- `user_confirmed`: explicit operator-confirmed facts;
- `inferred_preferences`: non-factual preferences inferred from behavior.

Authorization, visa, citizenship, clearance, certification, licensing, and
demographic questions only use matching `user_confirmed` evidence. Missing
confirmed evidence returns an unsupported answer and required fields move to
review rather than being guessed.

## Measurement

Run:

`python scripts/evaluate_cv_routing.py --config cv_routing.yaml.example`

The sanitized dataset spans AI/ML, data, software, QA, DevOps, infrastructure,
embedded, junior, and internship roles. The report includes routing accuracy,
abstention, unsupported required-field rate, and answer provenance. These are
dataset measurements, not a generalization or improvement claim.

Interview, rejection, offer, withdrawal, and user-correction outcomes can be
recorded through the application outcome API for reporting. Human-corrected
cover letters remain the only outcome material used as prompt examples.
Outcomes never mutate factual profile data or train a model automatically.
