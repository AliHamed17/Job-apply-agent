"""Best-effort operator alerts for automation-pausing events.

Kept separate from ``core.governor`` so the governor stays a pure
rate/cooldown store with no knowledge of WhatsApp — and separate from
``api.routes.webhook`` (imported lazily below) to avoid import cycles,
since ``worker.tasks`` and other worker modules import from both.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


async def notify_challenge(settings) -> None:
    """Send a best-effort WhatsApp alert that a LinkedIn challenge tripped
    the shared cooldown, so a human knows automation just paused itself.

    No-ops silently if no allowed sender is configured. Never raises —
    this is called from CAPTCHA/challenge-handling paths (discovery and
    the LinkedIn submitter) that must never fail because a notification
    failed.
    """
    if not settings.allowed_sender_list:
        return
    try:
        from api.routes.webhook import _send_whatsapp_message  # noqa: PLC0415

        await _send_whatsapp_message(
            settings.allowed_sender_list[0],
            "⚠️ LinkedIn challenge detected — automation paused (cooldown).",
            settings,
        )
    except Exception as exc:  # best-effort — alerting must never break the caller
        logger.warning("challenge_alert_failed", error=str(exc))
