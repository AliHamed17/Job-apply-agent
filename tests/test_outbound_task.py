from datetime import datetime
from profile.models import UserProfile
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import Settings
from db.models import Base


def _fake_db():
    """A real SQLAlchemy session bound to a fresh in-memory SQLite DB.

    Lets can_contact/record_contact (worker.outbound_dedup) run for real
    against outbound_contacts, without touching the on-disk dev DB.
    """
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _settings(**overrides):
    return Settings(_env_file=None, wa_outbound_daily_cap=15, **overrides)


async def _gen(job, profile, client=None):
    return "Hello, I'm interested."


@pytest.mark.asyncio
async def test_whatsapp_outbound_requires_submit_permit(monkeypatch, tmp_path):
    from worker import outbound

    calls = {}

    async def fake_parse(text, client=None):
        return outbound.ParsedPost(
            is_job=True,
            title="RF Engineer",
            company="X",
            description="5g",
            contact_phone="+971500000000",
            contact_email="",
        )

    async def fake_bridge(to, text, pdf, settings, http=None):
        calls["wa"] = to
        return True

    async def fake_email(*a, **k):
        return False

    prof = UserProfile()
    prof.preferences.roles = ["RF Engineer"]
    prof.resume.pdf_path = ""
    deps = SimpleNamespace(
        parse=fake_parse,
        bridge=fake_bridge,
        email=fake_email,
        gen_msg=_gen,
        now=datetime(2026, 7, 20, 12, 0, 0),
    )

    class _Gov:
        def can_act(self):
            return (True, "ok")

        def wa_remaining(self):
            return 5

        def wa_record(self):
            calls["rec"] = True

    # draft_only=False reaches the final permit gate; no external sender runs.
    r = await outbound.process_text_post(
        "Hiring RF Engineer +971500000000",
        db=_fake_db(),
        settings=_settings(draft_only=False),
        profile=prof,
        governor=_Gov(),
        deps=deps,
    )
    assert r == "permit_required"
    assert calls == {}


@pytest.mark.asyncio
async def test_sender_fallback_still_requires_submit_permit():
    """A "DM me" post with no phone/email in the body must reply to the
    poster's own WhatsApp number (passed as `sender`), not return no_contact."""
    from worker import outbound

    calls = {}

    async def fake_parse(text, client=None):
        return outbound.ParsedPost(
            is_job=True,
            title="RF Engineer",
            company="X",
            description="5g",
            contact_phone="",
            contact_email="",
        )

    async def fake_bridge(to, text, pdf, settings, http=None):
        calls["wa"] = to
        return True

    async def fake_email(*a, **k):
        raise AssertionError("email should not be used when sender fallback applies")

    prof = UserProfile()
    prof.preferences.roles = ["RF Engineer"]
    prof.resume.pdf_path = ""
    deps = SimpleNamespace(
        parse=fake_parse,
        bridge=fake_bridge,
        email=fake_email,
        gen_msg=_gen,
        now=datetime(2026, 7, 20, 12, 0, 0),
    )

    class _Gov:
        def can_act(self):
            return (True, "ok")

        def wa_remaining(self):
            return 5

        def wa_record(self):
            calls["rec"] = True

    r = await outbound.process_text_post(
        "Hiring RF Engineer, interested? DM me",
        db=_fake_db(),
        settings=_settings(draft_only=False),
        profile=prof,
        governor=_Gov(),
        deps=deps,
        sender="+972500000123",
    )
    assert r == "permit_required"
    assert calls == {}


@pytest.mark.asyncio
async def test_no_contact_when_no_body_contact_and_no_sender():
    """Without a body contact AND without a sender, there's genuinely nobody
    to reach — must return no_contact (not crash, not misfire)."""
    from worker import outbound

    async def fake_parse(text, client=None):
        return outbound.ParsedPost(
            is_job=True,
            title="RF Engineer",
            company="X",
            description="5g",
            contact_phone="",
            contact_email="",
        )

    async def fake_bridge(*a, **k):
        raise AssertionError("bridge should not be called with no contact")

    prof = UserProfile()
    prof.preferences.roles = ["RF Engineer"]
    deps = SimpleNamespace(
        parse=fake_parse,
        bridge=fake_bridge,
        email=fake_bridge,
        gen_msg=_gen,
        now=datetime(2026, 7, 20, 12, 0, 0),
    )

    class _Gov:
        def can_act(self):
            return (True, "ok")

        def wa_remaining(self):
            return 5

    r = await outbound.process_text_post(
        "Hiring RF Engineer, apply on our site",
        db=_fake_db(),
        settings=_settings(draft_only=False),
        profile=prof,
        governor=_Gov(),
        deps=deps,
        sender=None,
    )
    assert r == "no_contact"


@pytest.mark.asyncio
async def test_not_job_when_parser_says_no():
    from worker import outbound

    async def fake_parse(text, client=None):
        return outbound.ParsedPost(is_job=False)

    deps = SimpleNamespace(
        parse=fake_parse, bridge=None, email=None, gen_msg=_gen, now=datetime(2026, 7, 20, 12, 0, 0)
    )
    prof = UserProfile()

    r = await outbound.process_text_post(
        "just chatting", db=_fake_db(), settings=_settings(), profile=prof, governor=None, deps=deps
    )
    assert r == "not_job"


@pytest.mark.asyncio
async def test_low_score_blocks_send():
    from worker import outbound

    async def fake_parse(text, client=None):
        return outbound.ParsedPost(
            is_job=True,
            title="Barista",
            company="Y",
            description="coffee",
            contact_phone="+971500000001",
            contact_email="",
        )

    deps = SimpleNamespace(
        parse=fake_parse, bridge=None, email=None, gen_msg=_gen, now=datetime(2026, 7, 20, 12, 0, 0)
    )
    prof = UserProfile()
    prof.preferences.roles = ["RF Engineer"]
    prof.preferences.locations = ["Dubai"]
    prof.preferences.seniority = ["senior"]

    r = await outbound.process_text_post(
        "Hiring Barista +971500000001",
        db=_fake_db(),
        settings=_settings(min_apply_score=90.0),
        profile=prof,
        governor=None,
        deps=deps,
    )
    assert r == "low_score"


@pytest.mark.asyncio
async def test_capped_when_governor_denies():
    from worker import outbound

    async def fake_parse(text, client=None):
        return outbound.ParsedPost(
            is_job=True,
            title="RF Engineer",
            company="X",
            description="5g",
            contact_phone="+971500000002",
            contact_email="",
        )

    deps = SimpleNamespace(
        parse=fake_parse, bridge=None, email=None, gen_msg=_gen, now=datetime(2026, 7, 20, 12, 0, 0)
    )
    prof = UserProfile()
    prof.preferences.roles = ["RF Engineer"]

    class _Gov:
        def can_act(self):
            return (True, "ok")

        def wa_remaining(self):
            return 0

        def wa_record(self):
            raise AssertionError("should not be called")

    r = await outbound.process_text_post(
        "Hiring RF Engineer +971500000002",
        db=_fake_db(),
        settings=_settings(),
        profile=prof,
        governor=_Gov(),
        deps=deps,
    )
    assert r == "capped"


@pytest.mark.asyncio
async def test_no_contact_when_no_phone_or_email():
    from worker import outbound

    async def fake_parse(text, client=None):
        return outbound.ParsedPost(
            is_job=True,
            title="RF Engineer",
            company="X",
            description="5g",
            contact_phone="",
            contact_email="",
        )

    deps = SimpleNamespace(
        parse=fake_parse, bridge=None, email=None, gen_msg=_gen, now=datetime(2026, 7, 20, 12, 0, 0)
    )
    prof = UserProfile()
    prof.preferences.roles = ["RF Engineer"]

    class _Gov:
        def can_act(self):
            return (True, "ok")

        def wa_remaining(self):
            return 5

        def wa_record(self):
            pass

    r = await outbound.process_text_post(
        "Hiring RF Engineer, no contact info",
        db=_fake_db(),
        settings=_settings(),
        profile=prof,
        governor=_Gov(),
        deps=deps,
    )
    assert r == "no_contact"


@pytest.mark.asyncio
async def test_duplicate_within_dedup_window():
    from worker import outbound
    from worker.outbound_dedup import record_contact

    async def fake_parse(text, client=None):
        return outbound.ParsedPost(
            is_job=True,
            title="RF Engineer",
            company="X",
            description="5g",
            contact_phone="+971500000003",
            contact_email="",
        )

    now = datetime(2026, 7, 20, 12, 0, 0)
    deps = SimpleNamespace(parse=fake_parse, bridge=None, email=None, gen_msg=_gen, now=now)
    prof = UserProfile()
    prof.preferences.roles = ["RF Engineer"]

    class _Gov:
        def can_act(self):
            return (True, "ok")

        def wa_remaining(self):
            return 5

        def wa_record(self):
            pass

    db = _fake_db()
    record_contact(db, "+971500000003", "whatsapp_dm", job_id=None, now=now)

    r = await outbound.process_text_post(
        "Hiring RF Engineer +971500000003",
        db=db,
        settings=_settings(),
        profile=prof,
        governor=_Gov(),
        deps=deps,
    )
    assert r == "duplicate"


@pytest.mark.asyncio
async def test_permit_gate_does_not_record_contact_or_burn_governor():
    """The hard permit gate precedes all external IO and bookkeeping."""
    from db.models import OutboundContact
    from worker import outbound

    async def fake_parse(text, client=None):
        return outbound.ParsedPost(
            is_job=True,
            title="RF Engineer",
            company="X",
            description="5g",
            contact_phone="+971500000099",
            contact_email="",
        )

    async def fake_bridge(to, text, pdf, settings, http=None):
        return False  # bridge/API call failed

    deps = SimpleNamespace(
        parse=fake_parse,
        bridge=fake_bridge,
        email=None,
        gen_msg=_gen,
        now=datetime(2026, 7, 20, 12, 0, 0),
    )
    prof = UserProfile()
    prof.preferences.roles = ["RF Engineer"]

    class _Gov:
        def can_act(self):
            return (True, "ok")

        def wa_remaining(self):
            return 5

        def wa_record(self):
            raise AssertionError("wa_record must not be called on send failure")

    db = _fake_db()
    # draft_only=False reaches the final permit gate.
    r = await outbound.process_text_post(
        "Hiring RF Engineer +971500000099",
        db=db,
        settings=_settings(draft_only=False),
        profile=prof,
        governor=_Gov(),
        deps=deps,
    )
    assert r == "permit_required"
    assert db.query(OutboundContact).count() == 0


@pytest.mark.asyncio
async def test_email_outbound_requires_submit_permit():
    from worker import outbound

    calls = {}

    async def fake_parse(text, client=None):
        return outbound.ParsedPost(
            is_job=True,
            title="RF Engineer",
            company="X",
            description="5g",
            contact_phone="",
            contact_email="hr@example.com",
        )

    async def fake_email(to_addr, subject, body, pdf_path, settings, sender=None):
        calls["email"] = to_addr
        return True

    deps = SimpleNamespace(
        parse=fake_parse,
        bridge=None,
        email=fake_email,
        gen_msg=_gen,
        now=datetime(2026, 7, 20, 12, 0, 0),
    )
    prof = UserProfile()
    prof.preferences.roles = ["RF Engineer"]

    class _Gov:
        def can_act(self):
            return (True, "ok")

        def wa_remaining(self):
            return 5

        def wa_record(self):
            calls["rec"] = True

    r = await outbound.process_text_post(
        "Hiring RF Engineer hr@example.com",
        db=_fake_db(),
        settings=_settings(draft_only=False),
        profile=prof,
        governor=_Gov(),
        deps=deps,
    )
    assert r == "permit_required"
    assert calls == {}


@pytest.mark.asyncio
async def test_draft_only_master_switch_blocks_send():
    """DRAFT_ONLY=true (the default) must block outbound sends entirely —
    an above-threshold job with a phone number must NOT reach bridge/email,
    must NOT record_contact, and must NOT burn the governor's wa budget."""
    from db.models import OutboundContact
    from worker import outbound

    async def fake_parse(text, client=None):
        return outbound.ParsedPost(
            is_job=True,
            title="RF Engineer",
            company="X",
            description="5g",
            contact_phone="+971500000123",
            contact_email="",
        )

    async def fake_bridge(to, text, pdf, settings, http=None):
        raise AssertionError("bridge must not be called when draft_only is True")

    async def fake_email(*a, **k):
        raise AssertionError("email must not be called when draft_only is True")

    deps = SimpleNamespace(
        parse=fake_parse,
        bridge=fake_bridge,
        email=fake_email,
        gen_msg=_gen,
        now=datetime(2026, 7, 20, 12, 0, 0),
    )
    prof = UserProfile()
    prof.preferences.roles = ["RF Engineer"]

    class _Gov:
        def can_act(self):
            return (True, "ok")

        def wa_remaining(self):
            return 5

        def wa_record(self):
            raise AssertionError("wa_record must not be called when draft_only is True")

    db = _fake_db()
    r = await outbound.process_text_post(
        "Hiring RF Engineer +971500000123",
        db=db,
        settings=_settings(draft_only=True),
        profile=prof,
        governor=_Gov(),
        deps=deps,
    )
    assert r == "draft_only"
    assert db.query(OutboundContact).count() == 0
