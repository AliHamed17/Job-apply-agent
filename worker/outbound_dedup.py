"""Dedup + record for outbound recruiter contacts."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta

from db.models import OutboundContact


def normalize_contact(value: str) -> str:
    v = (value or "").strip()
    if "@" in v:
        return v.lower()
    return re.sub(r"[^\d]", "", v)


def contact_hash(value: str) -> str:
    return hashlib.sha256(normalize_contact(value).encode()).hexdigest()


def can_contact(db, value: str, dedup_days: int, now: datetime) -> bool:
    ch = contact_hash(value)
    row = db.query(OutboundContact).filter(OutboundContact.contact_hash == ch).first()
    if not row:
        return True
    return row.last_contacted_at < now - timedelta(days=dedup_days)


def record_contact(db, value: str, channel: str, job_id, now: datetime) -> None:
    ch = contact_hash(value)
    row = db.query(OutboundContact).filter(OutboundContact.contact_hash == ch).first()
    if row:
        row.last_contacted_at = now
        row.channel = channel
    else:
        db.add(OutboundContact(contact_hash=ch, channel=channel,
                               last_contacted_at=now, job_id=job_id))
    db.commit()
