"""IMPORTANT #2 — worker.alerts.notify_challenge: the WhatsApp alert fired
when a LinkedIn challenge trips the shared cooldown. Must no-op when no
allowed sender is configured, must send when one is, and must never raise
(best-effort) even if the send itself blows up.
"""

from __future__ import annotations

import pytest

from core.config import Settings
from worker.alerts import notify_challenge


@pytest.mark.asyncio
async def test_notify_challenge_noop_without_allowed_sender(monkeypatch):
    settings = Settings(_env_file=None, allowed_senders="")

    calls = []

    async def fake_send(*a, **k):
        calls.append((a, k))

    from api.routes import webhook
    monkeypatch.setattr(webhook, "_send_whatsapp_message", fake_send)

    await notify_challenge(settings)

    assert calls == []


@pytest.mark.asyncio
async def test_notify_challenge_sends_to_first_allowed_sender(monkeypatch):
    settings = Settings(_env_file=None, allowed_senders="+971500000001,+971500000002")

    calls = {}

    async def fake_send(phone, text, settings_arg):
        calls["phone"] = phone
        calls["text"] = text

    from api.routes import webhook
    monkeypatch.setattr(webhook, "_send_whatsapp_message", fake_send)

    await notify_challenge(settings)

    assert calls["phone"] == "+971500000001"
    assert "challenge" in calls["text"].lower()
    assert "cooldown" in calls["text"].lower()


@pytest.mark.asyncio
async def test_notify_challenge_is_best_effort_and_never_raises(monkeypatch):
    settings = Settings(_env_file=None, allowed_senders="+971500000001")

    async def boom(*a, **k):
        raise RuntimeError("network exploded")

    from api.routes import webhook
    monkeypatch.setattr(webhook, "_send_whatsapp_message", boom)

    await notify_challenge(settings)  # must not raise
