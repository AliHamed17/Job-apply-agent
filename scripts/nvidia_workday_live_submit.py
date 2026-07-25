"""Interactive Live Playwright Submitter for NVIDIA Workday Application."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog
from playwright.async_api import async_playwright

from core.credentials import CredentialVault
from profile.loader import get_profile

logger = structlog.get_logger(__name__)

NVIDIA_JOB_URL = "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/Developer-Technology-Engineer--AI---New-College-Grad-2026_JR2014130-1"


async def run_visible_nvidia_workday_submit():
    print("\n" + "=" * 90)
    print("OPENING VISIBLE BROWSER WINDOW FOR NVIDIA WORKDAY APPLICATION")
    print("=" * 90)

    profile = get_profile()
    cred = CredentialVault.get_credential_for_url(NVIDIA_JOB_URL)
    cv_path = Path("./cvs/Ali_Hamed_CV_AI_Engineer.pdf").resolve()

    print(f"\nCandidate: {profile.personal.name} ({profile.personal.email})")
    print(f"Target URL: {NVIDIA_JOB_URL}")
    print(f"CV File Path: {cv_path}")

    async with async_playwright() as pw:
        # Launch non-headless browser so user sees the window on screen
        browser = await pw.chromium.launch(headless=False, slow_mo=1000)
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()

        print("\n1. Navigating to NVIDIA Workday Job Posting...")
        await page.goto(NVIDIA_JOB_URL, timeout=60_000)
        await asyncio.sleep(3)

        print("2. Clicking 'Apply' Button on Workday...")
        apply_btn = page.locator("a:has-text('Apply'), button:has-text('Apply')").first
        if await apply_btn.is_visible(timeout=10_000):
            await apply_btn.click()
            await asyncio.sleep(4)

        # Check if 'Apply Manually' option exists
        apply_manually = page.locator("a:has-text('Apply Manually'), button:has-text('Apply Manually')").first
        if await apply_manually.is_visible(timeout=5000):
            print("3. Clicking 'Apply Manually'...")
            await apply_manually.click()
            await asyncio.sleep(4)

        print("\n4. Checking Workday Account Authentication...")
        email_input = page.locator("input[type='email'], input[name='username']").first
        password_input = page.locator("input[type='password'], input[name='password']").first

        if await email_input.is_visible(timeout=5000):
            print(f"   Filling Email: {cred.username}")
            await email_input.fill(cred.username)

        if await password_input.is_visible(timeout=5000):
            print("   Filling Password...")
            await password_input.fill(cred.password)

        sign_in_btn = page.locator("button[type='submit'], button:has-text('Sign In'), button:has-text('Create Account')").first
        if await sign_in_btn.is_visible(timeout=3000):
            print("   Clicking Sign In / Account Button...")
            await sign_in_btn.click()
            await asyncio.sleep(5)

        print("\nBrowser is open. Waiting 15 seconds for Workday form interaction...")
        await asyncio.sleep(15)
        await browser.close()

    print("\n" + "=" * 90)
    print("NVIDIA WORKDAY SESSION FINISHED")
    print("=" * 90 + "\n")


if __name__ == "__main__":
    asyncio.run(run_visible_nvidia_workday_submit())
