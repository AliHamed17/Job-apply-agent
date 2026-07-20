from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from db.models import Base
from worker.outbound_dedup import normalize_contact, can_contact, record_contact


def _db(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path/'o.db'}")
    Base.metadata.create_all(e)
    return sessionmaker(bind=e)()


def test_normalize():
    assert normalize_contact("+971 50-000 0000") == "971500000000"
    assert normalize_contact("HR@Example.com ") == "hr@example.com"


def test_dedup_window(tmp_path):
    db = _db(tmp_path)
    now = datetime(2026, 7, 20, 12, 0, 0)
    assert can_contact(db, "+971500000000", 30, now) is True
    record_contact(db, "+971500000000", "whatsapp_dm", job_id=None, now=now)
    assert can_contact(db, "+971 50 000 0000", 30, now + timedelta(days=5)) is False
    assert can_contact(db, "+971500000000", 30, now + timedelta(days=31)) is True
