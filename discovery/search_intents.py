"""Deterministic search-intent derivation and immutable activation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from profile.cv_routing import CVRoutingConfig

from sqlalchemy import func

from db.models import DiscoveryCursorState, DiscoverySourceState, SearchIntentRevision
from discovery.contracts import SearchIntentV1, stable_digest


def _ordered_unique(values) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = " ".join(str(value).split()).strip()
        key = clean.casefold()
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
    return tuple(result)


def derive_search_intents(
    routing: CVRoutingConfig,
    *,
    profile_locations: list[str] | tuple[str, ...] = (),
) -> tuple[SearchIntentV1, ...]:
    """Build one stable role-family intent for every configured CV."""

    locations = _ordered_unique([*profile_locations, "Israel", "Worldwide Remote"])
    overrides_by_cv: dict[str, list[str]] = {}
    for override in routing.overrides:
        overrides_by_cv.setdefault(override.cv_id, []).extend(override.title_contains)

    intents: list[SearchIntentV1] = []
    for cv in routing.cvs:
        titles = _ordered_unique([*overrides_by_cv.get(cv.id, []), *cv.title_terms])
        if not titles:
            titles = (cv.id.replace("-", " "),)
        skills = _ordered_unique(cv.skills)
        seniority = _ordered_unique(cv.seniority)
        identity_payload = {
            "cv_id": cv.id,
            "titles": titles,
            "skills": skills,
            "seniority": seniority,
            "locations": locations,
            "remote_regions": ("worldwide", "emea", "israel"),
        }
        intents.append(
            SearchIntentV1(
                intent_id=stable_digest(identity_payload),
                cv_id=cv.id,
                titles=titles,
                skills=skills,
                seniority=seniority,
                locations=locations,
            )
        )
    return tuple(sorted(intents, key=lambda item: item.cv_id.casefold()))


def search_intent_payload(intents: tuple[SearchIntentV1, ...]) -> tuple[str, str]:
    """Return canonical JSON and its digest for an exact set of intents."""

    payload = [intent.model_dump(mode="json") for intent in intents]
    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return payload_json, stable_digest(payload)


def activate_search_intents(db, intents: tuple[SearchIntentV1, ...]) -> SearchIntentRevision:
    """Activate an immutable revision, reusing an identical current revision."""

    if not intents:
        raise ValueError("SEARCH_INTENTS_EMPTY")
    payload_json, payload_digest = search_intent_payload(intents)
    current = (
        db.query(SearchIntentRevision)
        .filter(SearchIntentRevision.active.is_(True))
        .order_by(SearchIntentRevision.version.desc())
        .first()
    )
    if current is not None and current.payload_digest == payload_digest:
        return current

    now = datetime.now(UTC).replace(tzinfo=None)
    latest_version = db.query(func.max(SearchIntentRevision.version)).scalar() or 0
    db.query(SearchIntentRevision).filter(SearchIntentRevision.active.is_(True)).update(
        {"active": False}
    )
    revision = SearchIntentRevision(
        version=int(latest_version) + 1,
        payload_digest=payload_digest,
        payload_json=payload_json,
        active=True,
        activated_at=now,
    )
    db.add(revision)
    # An unchanged feed may return 304, but a new role scope still needs a
    # complete local re-evaluation. Reset only discovery checkpoints; source
    # occurrences and canonical jobs remain immutable/preserved.
    db.query(DiscoveryCursorState).delete(synchronize_session=False)
    db.query(DiscoverySourceState).filter(DiscoverySourceState.enabled.is_(True)).update(
        {
            DiscoverySourceState.next_poll_at: None,
            DiscoverySourceState.health_status: "unknown",
            DiscoverySourceState.last_error_code: None,
        },
        synchronize_session=False,
    )
    db.commit()
    db.refresh(revision)
    return revision


def active_search_intents(db) -> tuple[int | None, tuple[SearchIntentV1, ...]]:
    """Load the exact active revision without falling back to mutable files."""

    revision = (
        db.query(SearchIntentRevision)
        .filter(SearchIntentRevision.active.is_(True))
        .order_by(SearchIntentRevision.version.desc())
        .first()
    )
    if revision is None:
        return None, ()
    payload = json.loads(revision.payload_json)
    if not isinstance(payload, list):
        raise ValueError("SEARCH_INTENT_REVISION_INVALID")
    return revision.version, tuple(SearchIntentV1.model_validate(item) for item in payload)
