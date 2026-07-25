"""Real Chrome Live Application Runner for Ali Hamed."""

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


async def run_real_chrome_application(job_url: str):
    print("\n" + "=" * 90)
    print("EXECUTING REAL LIVE APPLICATION SUBMISSION ON SYSTEM CHROME")
    print("=" * 90)

    profile = get_profile()
    cv_path = Path("./cvs/Ali_Hamed_CV_AI_Engineer.pdf").resolve()

    print(f"\n[CANDIDATE EMAIL] {profile.personal.email}")
    print(f"[CANDIDATE PHONE] {profile.personal.phone}")
    print(f"[CV FILE PATH]     {cv_path} (Exists: {cv_path.exists()})")
    print(f"[JOB ATS URL]      {job_url}")

    async with async_playwright() as pw:
        # Launch Google Chrome in visible mode with user arguments
        print("\n[STEP 1] Opening System Google Chrome browser instance...")
        try:
            browser = await pw.chromium.launch(headless=False, channel="chrome", slow_mo=300)
        except Exception:
            browser = await pw.chromium.launch(headless=False, slow_mo=300)

        context = await browser.new_context(viewport={"width": 1280, "height": 850})
        page = await context.new_page()

        print(f"[STEP 2] Navigating to target job URL: {job_url}")
        await page.goto(job_url, timeout=60_000)
        await asyncio.sleep(3)

        title = await page.title()
        print(f"   * Page Title: {title}")

        # Check for Apply button if on posting page
        apply_btn = page.locator("a:has-text('Apply'), button:has-text('Apply'), a:has-text('Apply for this job'), button:has-text('Apply for this job')").first
        if await apply_btn.is_visible(timeout=5000):
            print("[STEP 3] Clicking 'Apply' button...")
            await apply_btn.click()
            await asyncio.sleep(3)

        # Fill First Name
        fname = page.locator('input[name*="first"], input[id*="first"]').first
        if await fname.is_visible(timeout=3000):
            print("[STEP 4] Filling First Name: Ali")
            await fname.fill("Ali")

        # Fill Last Name
        lname = page.locator('input[name*="last"], input[id*="last"]').first
        if await lname.is_visible(timeout=3000):
            print("[STEP 5] Filling Last Name: Hamed")
            await lname.fill("Hamed")

        # Fill Email
        email_loc = page.locator('input[name*="email"], input[type="email"]').first
        if await email_loc.is_visible(timeout=3000):
            print(f"[STEP 6] Filling Candidate Email: {profile.personal.email}")
            await email_loc.fill(profile.personal.email)

        # Fill Phone
        phone_loc = page.locator('input[name*="phone"], input[type="tel"]').first
        if await phone_loc.is_visible(timeout=3000):
            print(f"[STEP 7] Filling Phone Number: {profile.personal.phone}")
            await phone_loc.fill(profile.personal.phone)

        # Upload Resume PDF
        file_input = page.locator('input[type="file"]').first
        if await file_input.is_visible(timeout=3000) and cv_path.exists():
            print(f"[STEP 8] Attaching Resume PDF ({cv_path.name})...")
            await file_input.set_input_files(str(cv_path))

        print("\n" + "=" * 90)
        print("LIVE FORM FILLED ON YOUR SYSTEM CHROME BROWSER WINDOW")
        print("The browser window will remain open for 45 seconds.")
        print("Click the final 'Submit' button in Chrome to send the live application!")
        print("=" * 90 + "\n")

        await asyncio.sleep(45)
        await browser.close()


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://boards.greenhouse.io/canonical/jobs/4253907"
    asyncio.run(run_real_chrome_application(url))
