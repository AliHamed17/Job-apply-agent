# First-five ATS qualification matrix

This matrix is generated from the five committed sanitized report pairs and
the central adapter registry. It records offline fixture qualification only.

| Adapter | Version | Selector | Tier | Fixtures | Real URL dry run | Live canary | Qualified scopes | Final executor |
|---|---:|---|---|---:|---|---|---:|---|
| workday | 2.0.3 | workday-candidate-v2.4 | `fixture_qualified` | 9 | pending | pending | 0 | disabled |
| greenhouse | 1.0.0 | greenhouse-candidate-v9 | `fixture_qualified` | 22 | pending | pending | 0 | disabled |
| lever | 1.0.0 | lever-candidate-v2 | `fixture_qualified` | 28 | pending | pending | 0 | disabled |
| ashby | 1.0.0 | ashby-candidate-v1 | `fixture_qualified` | 13 | pending | pending | 0 | disabled |
| smartrecruiters | 1.0.0 | smartrecruiters-candidate-v1 | `fixture_qualified` | 15 | pending | pending | 0 | disabled |

## Current evidence boundary

- Sanitized fixtures: **87**.
- Real-URL dry runs completed: **0**.
- Live canaries completed: **0**.
- Qualified form scopes: **0**.
- Final executors enabled: **0**.

No row proves current tenant compatibility or authorizes an employer-side
submission. Any selector, protocol, form, attachment, request, or evidence
change requires a new sanitized fixture report and a later qualification cycle.

The machine-readable source of this table is
[`adapter-matrix.json`](adapter-matrix.json). Regenerate or validate both
artifacts with:

```powershell
python scripts/build_adapter_qualification_matrix.py --check
```
