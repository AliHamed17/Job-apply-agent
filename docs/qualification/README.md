# Qualification evidence

The first-five ATS adapters are **fixture-qualified only**. Their committed
evidence consists of 87 sanitized HTML fixtures:

- Workday: 9
- Greenhouse: 22
- Lever: 28
- Ashby: 13
- SmartRecruiters: 15

There have been zero real-URL dry runs, zero live canaries, zero qualified form
fingerprints/scopes, and zero enabled final executors. The presence of an
adapter or a confirmation fixture does not prove that a current employer page
can be submitted.

The paired platform JSON and Markdown reports are the source evidence.
[`adapter-matrix.json`](adapter-matrix.json) and
[`adapter-matrix.md`](adapter-matrix.md) are deterministic aggregate views.
Validate them without changing files:

```powershell
python scripts/build_adapter_qualification_matrix.py --check
```

To intentionally refresh the aggregate after a reviewed report change:

```powershell
python scripts/build_adapter_qualification_matrix.py --write
```

Qualification advances one exact adapter/version/form scope at a time:

`disabled → fixture_qualified → dry_run_qualified → live_canary_qualified`

Fixture qualification uses no employer network, candidate identity, CV
content, answers, cookies, or live application. A dry run must use one explicit
operator-selected URL and stop before the irreversible action. A live canary
requires separate explicit approval for that exact application. Selector,
protocol, form, attachment, request, or evidence drift resets the affected
scope to dry-run qualification.
