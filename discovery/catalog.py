"""Local employer-catalog loading and ATS identifier learning."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import yaml

from db.models import EmployerCatalogEntryRecord
from discovery.contracts import EmployerCatalogEntry, stable_digest

_ATS_HOSTS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.eu.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
    "careers.smartrecruiters.com": "smartrecruiters",
    "jobs.smartrecruiters.com": "smartrecruiters",
}


def build_catalog_entry(
    *,
    company_name: str,
    ats: str,
    tenant_key: str,
    region: str = "global",
    base_url: str | None = None,
    enabled: bool = True,
    discovered_via: str = "config",
) -> EmployerCatalogEntry:
    clean_tenant = tenant_key.strip().strip("/")
    identity = {
        "ats": ats.strip().casefold(),
        "tenant_key": clean_tenant.casefold(),
        "region": region.strip().casefold(),
    }
    return EmployerCatalogEntry(
        catalog_key=stable_digest(identity),
        company_name=company_name.strip() or clean_tenant,
        ats=identity["ats"],
        tenant_key=clean_tenant,
        region=identity["region"],
        base_url=base_url,
        enabled=enabled,
        discovered_via=discovered_via,
    )


def load_catalog(path: str | Path) -> tuple[EmployerCatalogEntry, ...]:
    """Load a sanitized catalog; a missing personal file means no tenants."""

    resolved = Path(path)
    if not resolved.exists():
        return ()
    payload = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    rows = payload.get("employers", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        raise ValueError("employer catalog employers must be a list")
    entries = [build_catalog_entry(**row) for row in rows]
    if len({entry.catalog_key for entry in entries}) != len(entries):
        raise ValueError("employer catalog contains duplicate ATS tenants")
    return tuple(entries)


def catalog_entry_from_url(
    url: str,
    *,
    company_name: str = "",
    discovered_via: str = "alert",
) -> EmployerCatalogEntry | None:
    """Learn a supported tenant identifier from a validated alert URL."""

    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").casefold()
        parts = [part for part in parsed.path.split("/") if part]
    except (ValueError, UnicodeError):
        return None
    ats = _ATS_HOSTS.get(host)
    if parsed.scheme != "https" or ats is None or not parts:
        return None
    tenant_key = parts[0]
    region = "eu" if host == "jobs.eu.lever.co" else "global"
    base_url = f"https://{host}/{tenant_key}"
    return build_catalog_entry(
        company_name=company_name or tenant_key,
        ats=ats,
        tenant_key=tenant_key,
        region=region,
        base_url=base_url,
        discovered_via=discovered_via,
    )


def upsert_catalog_entries(db, entries: tuple[EmployerCatalogEntry, ...]) -> int:
    """Persist local catalog metadata without deleting learned entries."""

    changed = 0
    for entry in entries:
        row = (
            db.query(EmployerCatalogEntryRecord)
            .filter(EmployerCatalogEntryRecord.catalog_key == entry.catalog_key)
            .one_or_none()
        )
        values = {
            "company_name": entry.company_name,
            "ats": entry.ats,
            "tenant_key": entry.tenant_key,
            "region": entry.region,
            "base_url": str(entry.base_url) if entry.base_url else None,
            "enabled": entry.enabled,
            "discovered_via": entry.discovered_via,
        }
        if row is None:
            db.add(EmployerCatalogEntryRecord(catalog_key=entry.catalog_key, **values))
            changed += 1
            continue
        for key, value in values.items():
            if getattr(row, key) != value:
                setattr(row, key, value)
                changed += 1
    db.commit()
    return changed
