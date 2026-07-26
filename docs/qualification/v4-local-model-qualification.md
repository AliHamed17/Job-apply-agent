# Job Apply Agent v4 Local qwen Qualification

This is a real local-model run over generated synthetic, contract-co-designed labels. The labels are not independent human annotations, so percentages are regression measurements, not estimates of production accuracy or live ATS performance. Coverage and abstention must be read separately from precision.

Qualification status: **PASSED**.

## Aggregate measurements

| Boundary | Cases | Precision | Coverage | Abstention | Gate |
|---|---:|---:|---:|---:|:---:|
| CV routing | 120 | 98.11% | 88.33% | 11.67% | PASS |
| Form resolution | 240 | 100.00% | 57.08% | 42.92% | PASS |
| Full material packages | 40 | 100.00% | 100.00% | 0.00% | PASS |
| Malformed boundaries | 30 | 100.00% | 0.00% | 100.00% | PASS |

## Safety and provenance

- Exact model: `qwen2.5:7b` at `sha256:845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e`.
- Qualified runner: `CPython 3.13`; Pydantic `2.13.4`; pydantic-settings `2.14.2`; httpx `0.28.1`; Redis `5.3.1`; Ollama `0.31.1`.
- Bound inference configuration: context `16384`; prompt characters `24000`; request timeout `120.0s`; lease `redis`.
- Real qwen typed calls: 142; provider attempts: 142.
- Form unsupported eligible: 0; automatic sensitive eligible: 0; sensitive provider attempts: 0.
- Material unsupported eligible: 0; sensitive eligible: 0.
- Material supported-claim precision: 195/195 audited claim units; package coverage is reported separately.
- Malformed-boundary provider attempts: 0.

## Bounded reason counts

- Routing: `{"llm_abstained": 14}`
- Forms: `{"REQUIRED_FIELD_UNKNOWN": 40, "UNSUPPORTED_CLAIM": 63}`
- Materials: `{}`
- Malformed boundaries: `{"LLM_OUTPUT_INVALID": 12, "REQUIRED_FIELD_UNKNOWN": 6, "UNTRUSTED_INPUT_BLOCKED": 6, "llm_input_rejected": 6}`

## Dataset integrity

| Dataset | Cases | SHA-256 |
|---|---:|---|
| form_resolution_bilingual_240.json | 240 | `2741cbb63ac67995963fb64b39b223fea37b38dc85f92c3ef8cd53682270f91e` |
| malformed_prompt_injection_30.json | 30 | `14d3a1094be7f4ae31cd282dba11a2cab3ab0e1651d2fd860cac138e14bdc09c` |
| local_model_full_material_40.json | 40 | `8493bdf1f6b535c148410ce174b3315c323c71c660f718d8b6434d8f90423686` |
| cv_routing_120.json | 120 | `cb6497a20c7e82db9bbce172fb3a755a093e9d7a39086f113c9ffbdf33e657f3` |
| cv_routing_eval_config.yaml | 1 | `862bf65397bed0b64a0bd219d82a0646db8ac1163cc11950ded5611df68e095d` |

## Limitations

- Model outputs and source content are never persisted in this report.
- Synthetic labels are generated and co-designed with the contracts.
- The measurements do not establish real-job, private-profile, or ATS accuracy.
- A blocked run is never converted into a passing qualification.
