# v5 Calibrated Fit and 12-CV Routing

Release 3 adds an immutable `JobFitDecisionV1` beside the legacy numeric job score. The old
score remains available for compatibility, but it cannot establish quality eligibility.

## Safety contract

- Routing is deterministic first and records the runner-up CV and confidence margin.
- A configured CV and its current SHA-256 digest must be present.
- Fallback routes, unsupported required skills, low confidence, small margins, `NaN`, prompt
  injection, and missing or mismatched qualification artifacts require review.
- A remote role must explicitly name Israel, worldwide/global, EMEA, or the Middle East.
  Plain `Remote` is quarantined and incompatible regional scopes are excluded.
- Authorization, sponsorship, clearance, and language constraints use only user-confirmed
  evidence. They are never inferred by the LLM.
- The general software fallback remains useful for preparation, but can never be quality
  eligible.
- A quality-eligible decision is not permission to submit. The API therefore always reports
  `submission_authorized=false`; signed autopilot authority is introduced separately.

Every decision stores only bounded evidence codes and hashes. It does not store job text,
profile values, CV content, company URLs, or candidate identity.

## Qualification

The committed fixture contains 240 English/Hebrew cases across 12 CV families. Thresholds are
selected using 192 training cases and checked once against 48 held-out cases. The generated
report records routing precision, fit precision, auto-eligible coverage, abstention,
unsupported-field rate, and confusion matrices.

Regenerate the sanitized artifacts with:

```powershell
python scripts/build_v5_fit_routing_dataset.py
python scripts/evaluate_v5_fit_routing.py
```

The committed report qualifies only the sanitized fixture. It deliberately cannot match a
private CV manifest. To create a local artifact, use a reviewed dataset whose 12 expected CV
IDs exactly match the private routing configuration:

```powershell
python scripts/qualify_local_fit_routing.py `
  --config cv_routing.yaml `
  --cv-directory cvs `
  --dataset path\to\reviewed-private-fit-cases.json
```

The resulting `fit_routing_qualification.json` and `routing-evaluation-report.json` are ignored
by Git. A failed or mismatched qualification leaves all decisions in review. Even a passing
private qualification grants quality eligibility only; it does not activate submission.

## API

`GET /api/jobs/{id}/automation-decision` returns the latest schema-validated decision,
decision digest, and bounded evidence. A missing decision returns 404. Historical decisions
are insert-only and remain available for audit when a job, profile, routing config, or CV hash
changes.
