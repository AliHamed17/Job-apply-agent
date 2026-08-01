# Discovery mesh

The v5 discovery mesh is an authorized, read-only ingestion layer. It discovers
and canonicalizes job metadata; it cannot approve, prepare, or submit an
application.

## Source policy

| Source | Transport | Default cadence | Notes |
| --- | --- | ---: | --- |
| Greenhouse | Public tenant board API | 10 minutes + jitter | Board token required in the local catalog |
| Lever | Public tenant postings API | 10 minutes + jitter | Site slug and region required |
| Ashby | Public tenant job-board API | 10 minutes + jitter | Job-board name required |
| SmartRecruiters | Public tenant postings API | 10 minutes + jitter | Company identifier required |
| Remotive | Public API | 6 hours + jitter | Existing provider limit is preserved |
| Gmail alerts | Local read-only OAuth | 1 minute | Only a dedicated label is read |
| Generic feed/JSON-LD | Permitted HTTPS source | 10 minutes + jitter | Robots rules and public DNS are mandatory |
| LinkedIn crawler | Disabled | n/a | Alerts or approved partner access only |

Public ATS feeds are tenant scoped. The mesh evolves an employer catalog from
operator configuration and validated alert URLs; it does not claim that a
single endpoint enumerates every employer.

## Local employer catalog

Copy `employer_catalog.yaml.example` to the ignored
`employer_catalog.yaml`. Configure only employers and feeds you are permitted
to poll. The file remains local and must not contain credentials.

Each enabled source uses HTTPS, one concurrent request per host, bounded
pagination, conditional requests where supported, exponential backoff, and
`Retry-After`. Redirects are rejected so a source cannot bypass its host
allowlist. Generic sources additionally require public DNS and an allowed
robots policy. Generic feed indexes are reloaded on every scan because an
unchanged sitemap or feed validator cannot prove that linked job pages are
unchanged.

## Gmail alert onboarding

1. Create a dedicated Gmail label, default `JobApplyAgent`.
2. Create a local OAuth client with Gmail read-only scope.
3. Complete OAuth locally and save the resulting token state as the ignored
   `.gmail_oauth.json`.
4. Set `GMAIL_ALERT_ENABLED=true` and, if needed,
   `GMAIL_ALERT_LABEL=<label>`.

The runtime performs only Gmail `GET` operations. OAuth state and raw messages
remain local. Parsing persists only validated job metadata and opaque digests;
recipient addresses, message bodies, and message identifiers are not stored.
Never commit `.gmail_oauth.json`.

## Search-intent activation

`POST /api/search-intent/preview` derives one immutable intent from every
configured CV route. It always adds Israel and Worldwide Remote to the selected
locations. Activate the exact preview digest with
`POST /api/search-intent/activate`. Digest binding prevents an edited CV routing
file from silently changing an active search scope.

Activating a new scope clears only source cursors and due times so unchanged
feeds are re-evaluated against the new roles. Existing jobs and source
occurrences are preserved. A source version, host, authentication mode, or
configuration-digest change also forces a clean source snapshot.

The first worker run may activate the initial CV-derived scope when no revision
exists. Later changes require explicit activation.

## Operations

- `GET /api/discovery/sources` returns source versions, health, and next polls.
- `GET /api/discovery/runs` returns bounded run history and insert, update,
  duplicate, and closure counts.
- `POST /api/discovery/run` queues a run for all due sources or one known opaque
  source key.

The scheduler wakes every minute. PostgreSQL advisory locking prevents
overlapping mesh runs. A run abandoned for 30 minutes is marked
`STALE_RUN_RECOVERED`; durable cursors resume from the last committed page.
An origin watermark remains in the durable cursor across bounded worker runs.
Closure reconciliation occurs only when every page in that exact snapshot has
been observed, preventing both false closure after a partial scan and missed
closure for sources larger than one run's page budget.

Source occurrences are preserved even when multiple sources resolve to the
same normalized URL. Distinct posting IDs with distinct URLs remain distinct,
even when title, company, and location happen to match.

## Privacy boundary

Discovery state is authoritative only in the private local PostgreSQL database.
CV contents, profile facts, mailbox OAuth state, email content, employer
tenants, job URLs, and job titles are not control-plane or metrics dimensions.
The remote control plane receives no discovery payloads.

Discovery does not weaken submission gates. Unsupported ATS families and
unqualified form contracts can be discovered and prepared later, but remain
in quarantine for final submission.
