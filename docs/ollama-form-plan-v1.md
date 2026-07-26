# Local Ollama and Form Plan v1

This release keeps private application generation on the operator's Windows
machine. CV text, profile facts, observed questions, generated answers, and
cover-letter drafts are not sent to a cloud model.

## Local model setup

Install and start Ollama, then make the exact configured model available:

```powershell
$env:OLLAMA_NO_CLOUD = "1"
ollama pull qwen2.5:7b
ollama serve
```

Use these application settings:

```dotenv
LLM_PROVIDER=ollama
LLM_MODEL=qwen2.5:7b
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_NO_CLOUD=true
OLLAMA_NUM_CTX=16384
OLLAMA_REQUEST_TIMEOUT_SECONDS=120
OLLAMA_CONNECT_TIMEOUT_SECONDS=3
OLLAMA_LEASE_WAIT_SECONDS=10
OLLAMA_LEASE_TTL_SECONDS=130
OLLAMA_CIRCUIT_FAILURE_THRESHOLD=3
OLLAMA_CIRCUIT_RESET_SECONDS=30
LLM_MAX_PROMPT_CHARS=24000
LLM_GENERATION_MAX_HORIZON_SECONDS=120
TASKS_ALWAYS_EAGER=false
REDIS_URL=redis://127.0.0.1:6379/0
```

The application accepts loopback and the explicit Docker-to-host gateway only.
It rejects remote Ollama hosts and model tags that identify cloud execution.
Readiness checks `/api/tags` for the exact model name, a local artifact size and
format, and a 64-character model digest. A matching name without local artifact
evidence is not ready.

The qualified private runner uses CPython 3.13. The qualification artifact
binds the Python implementation and major/minor version, the exact committed
dependency graph in `config/qualified_runtime_packages.json`, Ollama server
`0.31.1` from `/api/version`, the model digest, context and prompt limits,
request and generation deadlines, connect and lease timings, circuit threshold
and reset, Redis lease mode, and the no-cloud policy. The dependency graph
includes Pydantic, pydantic-core, pydantic-settings, HTTPX, HTTPCore, AnyIO,
Certifi, h11, IDNA, sniffio, Redis, PyJWT, PyYAML, python-dotenv,
annotated-types, typing-extensions, and typing-inspection. Every graph member is
also an exact direct project pin; IDNA is qualified at `3.15`.

Production currentness captures the installed graph and requires exact equality
with both the report fingerprint and the committed manifest before admitting
generated material. A missing package, extra manifest key, version mismatch, or
malformed manifest fails closed. Live readiness also compares the observed
Ollama version. Any bound change requires a new qualification. Python patch
level and operating system remain outside this reset boundary.

Every typed attempt verifies the exact server version, tag, and digest before
and after inference. Qualification also records one bounded server-version
observation for every successful generation. A replaced model or server, an
excessive future deadline, or a request whose prompt, schema, retry margin, and
output budget cannot fit the configured context fails closed before its result
can be used.

Ollama supports passing a JSON schema through the `format` field. The complete
Pydantic schema remains the authoritative local validation contract and is
included in the request-size budget. Ollama's grammar parser cannot initialize
on selected regex and string-length hints, so the transport-only `format`
projection omits those unsupported hints. Every response is still validated
against the complete Pydantic schema; one bounded formatting retry is allowed,
and a second invalid result fails closed before use. See the official
[structured output documentation](https://docs.ollama.com/capabilities/structured-outputs)
and [model-list endpoint](https://docs.ollama.com/api/tags).

## Typed generation boundary

Every private inference declares:

- a bounded purpose and prompt version;
- a private-data classification;
- the exact response schema;
- an aware deadline;
- the expected local model identity.

Only malformed formatting gets one schema-format retry. Transport failures,
timeouts, a missing model, an open circuit, unsafe configuration, and a second
concurrent inference fail closed. There is no automatic OpenAI, Anthropic, or
other cloud fallback.

Prompts and responses are never written to logs. Operational status contains
only bounded provider, model, digest, readiness, and reason codes.

## Answer policy

Observed fields are resolved in this fixed order:

1. deterministic identity and link fields;
2. exact `evidence.user_confirmed` facts;
3. operator-approved reusable answers with an exact context key;
4. structured facts from the selected CV artifact;
5. local LLM synthesis for non-sensitive questions only;
6. abstain.

Authorization, sponsorship, nationality, citizenship, clearance, licensing,
certification, demographics, consent, and attestations never reach the LLM.
English and Hebrew labels use the same deterministic sensitive-field policy.
If confirmed evidence is unavailable, the answer becomes operator-required.
An unresolved required field adds `REQUIRED_FIELD_UNKNOWN` and the application
cannot be prepared for sending.

The old question-only answer cache is retained only for migration history. It
is never read by Form Plan v1. A reusable answer is usable only when all of
these still match:

- canonical field and field type;
- exact option-set digest and locale;
- profile version and selected CV id/hash;
- adapter, selector/form version, and form fingerprint;
- answer-policy version;
- active operator approval and evidence reference.

Revoked or legacy rows are ignored.

## Material evidence

The selected PDF is hashed before extraction. Routing, material generation,
form planning, attachment, and audit use the same CV id and SHA-256 identity.
Replacing a file under the same configured id produces a new artifact and
invalidates stale work.

Generated factual cover-letter segments must reference existing CV or profile
evidence. Missing references, fabricated metrics, degrees, certifications,
employers, projects, or technologies add `UNSUPPORTED_CLAIM`. The draft remains
reviewable, but it is not eligible for preparation.

Human correction examples influence style only. They do not become factual
evidence and never modify the private profile automatically.

## Final-action boundary

The final submission worker consumes only a persisted, reviewed `FormPlanV1`.
It does not call an LLM, generate new material, classify a question, or write
an answer. Changing the policy, profile, selected CV, form fingerprint, adapter
version, or application revision invalidates the permit before the external
action.

## Offline qualification

The sanitized fixtures under `tests/fixtures/v4/` contain:

- 120 CV-routing cases across nine role families;
- 240 English/Hebrew form-resolution cases;
- 40 cover-letter claim/evidence cases;
- 30 malformed-output and prompt-injection cases.

They contain synthetic identities and no private employer URL, answer, CV
content, or browser data. Reports must publish precision, coverage, and
abstention separately. No improvement claim is allowed unless the measured
threshold is met.

The deterministic contract baseline and real-model qualification are separate:

```powershell
python scripts/evaluate_v4_quality.py --check
python scripts/evaluate_v4_local_model_qualification.py --check
```

The second command refuses non-local or non-`qwen2.5:7b` configuration, requires
the qualified CPython 3.13 runtime, Ollama `0.31.1`, and a reachable private
Redis instance with `TASKS_ALWAYS_EAGER=false`. It binds the exact local digest
and inference configuration, serializes inference across processes, and
rechecks the server plus artifact around every typed attempt. It runs actual qwen fallback
routing, 80 bilingual form synthesis cases, and 40 complete material-package
tasks. The 30 malformed and prompt-injection fixtures must fail at their
production boundaries without invoking qwen. Its JSON and Markdown artifacts
contain aggregate metrics, bounded reason codes, prompt versions, runtime,
execution-environment fingerprint, and model identity only; model outputs and
source content are never persisted.

Both datasets are generated, synthetic, and co-designed with the contracts.
Their labels are not independent human annotations, and their percentages are
not estimates of production accuracy.
