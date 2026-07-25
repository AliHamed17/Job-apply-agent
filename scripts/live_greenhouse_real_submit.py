"""Live Greenhouse Application Submitter with Real Email Confirmation Dispatch."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog
from playwright.async_api import async_playwright

from core.credentials import CredentialVault
from jobs.models import JobData
from profile.loader import get_profile

logger = structlog.get_logger(__name__)

# Target Live Greenhouse Application URL
LIVE_GREENHOUSE_JOB_URL = "https://boards.greenhouse.io/canonical/jobs/4253907"


async def execute_live_greenhouse_submission():
    print("\n" + "=" * 90)
    print("LIVE GREENHOUSE AUTOMATED APPLICATION FOR ALI HAMED")
    print("=" * 90)

    profile = get_profile()
    cv_path = Path("./cvs/Ali_Hamed_CV_AI_Engineer.pdf").resolve()

    print(f"\n[CANDIDATE EMAIL] {profile.personal.email}")
    print(f"[PHONE NUMBER]     {profile.personal.phone}")
    print(f"[CV FILE PATH]     {cv_path} (Exists: {cv_path.exists()})")
    print(f"[TARGET ATS URL]   {LIVE_GREENHOUSE_JOB_URL}")

    async with async_playwright() as pw:
        # Launch Chromium browser instance in visible mode
        browser = await pw.chromium.launch(headless=False, slow_mo=500)
        context = await browser.new_context(viewport={"width": 1280, "height": 850})
        page = await context.new_page()

        print(f"\n[STEP 1] Navigating to Live Greenhouse Application Page: {LIVE_GREENHOUSE_JOB_URL}")
        await page.goto(LIVE_GREENHOUSE_JOB_URL, timeout=60_000)
        await asyncio.sleep(3)

        title = await page.title()
        print(f"   * Page Title: {title}")

        # Fill First Name
        fn = page.locator('input[name="first_name"], input[id*="first_name"]').first
        if await fn.is_visible(timeout=5000):
            print("[STEP 2] Filling First Name (Ali)...")
            await fn.fill("Ali")

        # Fill Last Name
        ln = page.locator('input[name="last_name"], input[id*="last_name"]').first
        if await ln.is_visible(timeout=3000):
            print("[STEP 3] Filling Last Name (Hamed)...")
            await ln.fill("Hamed")

        # Fill Email
        em = page.locator('input[name="email"], input[type="email"]').first
        if await em.is_visible(timeout=3000):
            print(f"[STEP 4] Filling Email Address ({profile.personal.email})...")
            await em.fill(profile.personal.email)

        # Fill Phone
        ph = page.locator('input[name="phone"], input[type="tel"]').first
        if await ph.is_visible(timeout=3000):
            print(f"[STEP 5] Filling Phone Number ({profile.personal.phone})...")
            await ph.fill(profile.personal.phone)

        # Upload Resume PDF
        file_input = page.locator('input[type="file"]').first
        if await file_input.is_visible(timeout=3000) and cv_path.exists():
            print(f"[STEP 6] Uploading Aligned PDF Resume ({cv_path.name})...")
            await file_input.set_input_files(str(cv_path))

        print("\n" + "=" * 90)
        print("LIVE GREENHOUSE SESSION ACTIVE: FORM FILLED ON YOUR DESKTOP BROWSER")
        print("Click 'Submit Application' in the browser window to send the application live.")
        print("Greenhouse will immediately dispatch an official confirmation email to ali.h.10j@gmail.com!")
        print("Waiting 40 seconds before closing browser...")
        print("=" * 90 + "\n")

        await asyncio.sleep(40)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(execute_live_greenhouse_submission())
