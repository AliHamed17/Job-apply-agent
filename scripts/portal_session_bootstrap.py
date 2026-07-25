"""One-time interactive sign-in for a dedicated employer portal session."""

from __future__ import annotations

import argparse
import asyncio

from core.config import get_settings
from core.portal_sessions import PortalSessionLease, portal_session_for_url


async def bootstrap_portal_session(url: str, wait_seconds: int = 300) -> bool:
    """Open the employer page and mark ready only after operator confirmation."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is required. Install the browser optional dependencies first."
        ) from exc

    settings = get_settings()
    session = portal_session_for_url(url, settings.portal_browser_profile_root)
    session.profile_dir.mkdir(parents=True, exist_ok=True)

    with PortalSessionLease(
        session,
        stale_minutes=settings.portal_session_lock_minutes,
    ):
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(session.profile_dir),
                headless=False,
                viewport={"width": 1280, "height": 900},
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                print(
                    "Sign in directly on the employer page. Complete MFA if requested. "
                    "After the employer account page is visible, return here and press Enter. "
                    f"The confirmation prompt expires after {wait_seconds} seconds."
                )
                prompt = "Press Enter only after sign-in is complete: "
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(input, prompt),
                        timeout=max(10, wait_seconds),
                    )
                except TimeoutError:
                    print("Timed out without confirmation; the session was not marked ready.")
                    return False
                session.mark_ready()
            finally:
                await context.close()
    return session.ready


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a dedicated signed-in employer browser session. "
            "No password is read or stored by the application."
        )
    )
    parser.add_argument("url", help="Exact employer job or candidate-portal URL")
    parser.add_argument("--wait-seconds", type=int, default=300)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    ready = asyncio.run(bootstrap_portal_session(args.url, args.wait_seconds))
    print("Portal browser profile created." if ready else "Portal profile was not created.")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
