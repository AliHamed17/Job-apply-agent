"""Live Interactive Headful Browser Application Engine for Real ATS Submissions."""

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


async def run_live_interactive_submission(job_url: str):
    print("\n" + "=" * 90)
    print("LIVE INTERACTIVE BROWSER AUTO-APPLY ENGINE")
    print("=" * 90)

    profile = get_profile()
    cred = CredentialVault.get_credential_for_url(job_url)
    cv_path = Path("./cvs/Ali_Hamed_CV_AI_Engineer.pdf").resolve()

    print(f"\n[CANDIDATE EMAIL] {profile.personal.email}")
    print(f"[PHONE]           {profile.personal.phone}")
    print(f"[CV FILE PATH]     {cv_path} (Exists: {cv_path.exists()})")
    print(f"[TARGET URL]       {job_url}")

    async with async_playwright() as pw:
        print("\n[STEP 1] Launching visible Chromium browser window on your desktop screen...")
        browser = await pw.chromium.launch(headless=False, slow_mo=500)
        context = await browser.new_context(viewport={"width": 1280, "height": 850})
        page = await context.new_page()

        print(f"[STEP 2] Navigating to live application portal: {job_url}")
        await page.goto(job_url, timeout=60_000)
        await asyncio.sleep(3)

        # Locate Apply button
        apply_btn = page.locator("a:has-text('Apply'), button:has-text('Apply'), a:has-text('Apply Manually'), button:has-text('Apply Manually')").first
        if await apply_btn.is_visible(timeout=5000):
            print("[STEP 3] Clicking 'Apply' button...")
            await apply_btn.click()
            await asyncio.sleep(3)

        # Fill Email
        email_loc = page.locator("input[type='email'], input[name='email'], input[id*='email'], input[name='username']").first
        if await email_loc.is_visible(timeout=5000):
            print(f"[STEP 4] Filling Email Address ({cred.username})...")
            await email_loc.fill(cred.username)

        # Fill First Name
        fname_loc = page.locator("input[name*='first'], input[id*='first'], input[name='first_name']").first
        if await fname_loc.is_visible(timeout=3000):
            print("[STEP 5] Filling First Name (Ali)...")
            await fname_loc.fill("Ali")

        # Fill Last Name
        lname_loc = page.locator("input[name*='last'], input[id*='last'], input[name='last_name']").first
        if await lname_loc.is_visible(timeout=3000):
            print("[STEP 6] Filling Last Name (Hamed)...")
            await lname_loc.fill("Hamed")

        # Fill Phone
        phone_loc = page.locator("input[type='tel'], input[name*='phone'], input[id*='phone']").first
        if await phone_loc.is_visible(timeout=3000):
            print("[STEP 7] Filling Phone Number (+972-53-339-2826)...")
            await phone_loc.fill("+972-53-339-2826")

        # CV Upload
        file_loc = page.locator("input[type='file']").first
        if await file_loc.is_visible(timeout=3000) and cv_path.exists():
            print(f"[STEP 8] Uploading Resume PDF ({cv_path.name})...")
            await file_loc.set_input_files(str(cv_path))

        print("\n" + "=" * 90)
        print("INTERACTIVE MODE ACTIVE: BROWSER WINDOW IS OPEN ON YOUR SCREEN")
        print("Please complete any remaining portal questions or sign-in steps in the opened browser window.")
        print("Waiting 45 seconds before closing browser...")
        print("=" * 90 + "\n")

        await asyncio.sleep(45)
        await browser.close()


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/Developer-Technology-Engineer--AI---New-College-Grad-2026_JR2014130-1"
    asyncio.run(run_live_interactive_submission(url))
