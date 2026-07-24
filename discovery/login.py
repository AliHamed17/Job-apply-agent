"""One-time LinkedIn login into a persistent browser profile."""

from __future__ import annotations

import asyncio

import structlog

logger = structlog.get_logger(__name__)


def _is_logged_in(url: str) -> bool:
    return "feed" in url and "login" not in url and "checkpoint" not in url


async def open_login(profile_dir: str, timeout_s: int = 180) -> None:
    from playwright.async_api import async_playwright  # noqa: PLC0415

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(profile_dir, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.linkedin.com/login")
        logger.info("login_waiting", hint="Complete login + 2FA in the opened window")
        for _ in range(timeout_s):
            if _is_logged_in(page.url):
                logger.info("login_detected")
                break
            await asyncio.sleep(1)
        await ctx.close()


if __name__ == "__main__":
    from core.config import get_settings
    asyncio.run(open_login(get_settings().linkedin_browser_profile_dir))
