# Job Apply Agent v4 Offline Quality Baseline

Sanitized generated-fixture contract baseline only. This report makes no improvement, independent-label, Ollama-accuracy, live-submission, or real-world generalization claim.

## Summary

| Task | Cases | Precision | Coverage | Abstention | Fixture gate |
|---|---:|---:|---:|---:|:---:|
| CV routing | 120 | 97.96% | 81.67% | 18.33% | PASS |
| Form resolution | 240 | 100.00% | 66.67% | 33.33% | PASS |
| Claim evidence | 40 | 100.00% | 50.00% | 50.00% | PASS |
| Production output boundary | 30 | 100.00% | 0.00% | 100.00% | PASS |

Overall offline fixture gate: **PASS**.

## Thresholds

| Task | Requirement | Result |
|---|---|:---:|
| Claim Unsafe Eligibility | zero unsafe eligibility and exact expected blocker sets | PASS |
| Dataset Case Counts | exact case counts are 120, 240, 40, and 30 | PASS |
| Form Non Sensitive Precision | non-sensitive precision >= 0.95, all expected decisions and typed-local cases exercised, and zero unsupported or sensitive eligibility | PASS |
| Form Unsafe Eligibility | zero unsupported answers, automatic sensitive answers, or sensitive LLM calls | PASS |
| Malformed Fail Closed | all 30 actual production-schema cases fail closed with exact reasons, and every prompt-injection case reaches and fails semantic eligibility | PASS |
| Routing High Confidence Precision | precision >= 0.95 for confidence >= 0.75 with at least 24 predictions | PASS |

## Confusion counts

- CV routing: {"expected_abstained": {"abstained": 14, "selected": 2}, "expected_selected": {"abstained": 8, "correct_selected": 96, "incorrect_selected": 0}, "high_confidence": {"correct": 77, "incorrect": 0}}
- Form resolution: {"abstained": {}, "operator_required": {"operator_required": 80}, "resolved": {"resolved": 160}}
- Claim evidence: {"false_blocked": 0, "false_eligible": 0, "true_blocked": 20, "true_eligible": 20}
- Production output boundary: {"correctly_blocked": 30, "incorrectly_accepted_or_misclassified": 0, "semantic_blocked": 18, "typed_rejected": 12}

## Safety observations

- Actual routing high-confidence cutoff: 0.75; coverage: 64.17%.
- Typed local form cases exercised: 80 of 80.
- Sensitive-field typed-provider calls: 0.
- Unsupported resolved form answers eligible: 0.
- Claim blocker-set mismatches: 0.
- Semantic prompt-injection cases blocked: 18 of 18.

## Method and limitations

- The evaluator calls the production deterministic CV router, form-answer policy, claim-to-evidence validator, and actual form, routing, and material typed response schemas.
- Claim fixtures bind each rendered factual clause to a literal span in one authorized evidence item; denials, uncertainty, other subjects, sensitive facts, and unsupported additions must abstain.
- A deterministic local fixture provider exercises schema validation and semantic eligibility without making network or Ollama calls.
- English and Hebrew label-only sensitive controls are evaluated without canonical-name hints; non-sensitive synthesis uses bounded evidence citations.
- Fixtures are generated, synthetic, and co-designed with these contracts. Their rows are not independent labels and the percentages are not an estimate of production accuracy.
- Coverage is the fraction allowed to proceed; abstention is the fraction withheld. For production-boundary fixtures, safe rejection is the positive class.
- A passing fixture gate does not prove behavior on changed employer forms, unseen jobs, a real Ollama model, live ATS pages, or private candidate data.

## Dataset integrity

| Dataset | Cases | SHA-256 |
|---|---:|---|
| cover_letter_claims_40.json | 40 | `e325dd1af21181aa8b128563bca5cfc13233d1c1adf5fb53d48126d4aef03fef` |
| cv_routing_120.json | 120 | `e9d4fd6bc011e77e8e162eb963d48199ec45ae31ec25eda64fa5b942c031e2e8` |
| form_resolution_bilingual_240.json | 240 | `1f003a31408eba068ad40987d32e4e2004c1222f3c5e9b84f99aa8777c1b3034` |
| malformed_prompt_injection_30.json | 30 | `70b644c6e47fd84f1d49095f04763f362f9c5f3a063559c2d51377445e71309a` |
