"""Amazon Israel Jobs Auto-Apply Submitter for Ali Hamed."""

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
from match.scoring import score_job
from profile.cv_routing import RoutingJob, load_routing_config, route_cv
from profile.loader import get_profile
from profile.spotlight_matcher import match_portfolio_spotlight

logger = structlog.get_logger(__name__)

AMAZON_ISRAEL_JOBS_URL = "https://www.amazon.jobs/en/locations/tel-aviv-israel"


async def run_amazon_israel_application():
    print("\n" + "=" * 90)
    print("AMAZON ISRAEL JOBS: AUTONOMOUS APPLICATION FOR ALI HAMED")
    print("=" * 90)

    profile = get_profile()
    cred = CredentialVault.get_credential_for_url(AMAZON_ISRAEL_JOBS_URL)

    job_title = "Software Development Engineer II - Amazon Israel (Tel Aviv)"
    job_description = (
        "Build scalable cloud infrastructure, AWS microservices, Python/C++ backends, "
        "distributed data pipelines, and automated test frameworks."
    )

    # Align CV across 12 specialized resumes
    routing_cfg = load_routing_config("cv_routing.yaml")
    routing_job = RoutingJob(title=job_title, description=job_description)
    routing_decision = route_cv(routing_job, routing_cfg)
    selected_cv_id = routing_decision.selected_cv_id or "software-engineer"

    cv_filename = f"Ali_Hamed_CV_{selected_cv_id.replace('-', '_').title()}.pdf"
    cv_path = Path(f"./cvs/{cv_filename}").resolve()
    if not cv_path.exists():
        cv_path = Path("./cvs/Ali_Hamed_CV_Software_Engineer.pdf").resolve()

    print(f"\n[CANDIDATE EMAIL] {profile.personal.email}")
    print(f"[LOGIN USERNAME]  {cred.username}")
    print(f"[ALIGNED CV]       '{selected_cv_id}' ({cv_path.name})")
    print(f"[TARGET URL]       {AMAZON_ISRAEL_JOBS_URL}")

    async with async_playwright() as pw:
        print("\n[STEP 1] Launching visible Chromium browser window for Amazon Jobs...")
        browser = await pw.chromium.launch(headless=False, slow_mo=500)
        context = await browser.new_context(viewport={"width": 1280, "height": 850})
        page = await context.new_page()

        print(f"[STEP 2] Navigating to Amazon Israel Jobs Portal: {AMAZON_ISRAEL_JOBS_URL}")
        await page.goto(AMAZON_ISRAEL_JOBS_URL, timeout=60_000)
        await asyncio.sleep(3)

        title = await page.title()
        print(f"   * Page Title: {title}")

        # Search for Software Engineer jobs in Israel
        search_input = page.locator("input[id*='search'], input[name*='query'], input[placeholder*='Search']").first
        if await search_input.is_visible(timeout=5000):
            print("[STEP 3] Searching for Software Engineer roles in Tel Aviv...")
            await search_input.fill("Software Engineer")
            await page.keyboard.press("Enter")
            await asyncio.sleep(3)

        # Locate Apply button
        apply_btn = page.locator("a:has-text('Apply now'), button:has-text('Apply now'), a:has-text('Apply'), button:has-text('Apply')").first
        if await apply_btn.is_visible(timeout=5000):
            print("[STEP 4] Clicking Amazon 'Apply now' button...")
            await apply_btn.click()
            await asyncio.sleep(3)

        # Authenticate email
        email_input = page.locator("input[type='email'], input[name='email'], input[id*='email']").first
        if await email_input.is_visible(timeout=5000):
            print(f"[STEP 5] Authenticating Candidate Email ({cred.username})...")
            await email_input.fill(cred.username)

        # CV Upload
        file_input = page.locator("input[type='file']").first
        if await file_input.is_visible(timeout=3000) and cv_path.exists():
            print(f"[STEP 6] Uploading Aligned PDF Resume ({cv_path.name})...")
            await file_input.set_input_files(str(cv_path))

        print("\n" + "=" * 90)
        print("AMAZON ISRAEL APPLICATION SESSION IN PROGRESS")
        print("Waiting 30 seconds for browser session completion...")
        print("=" * 90 + "\n")

        await asyncio.sleep(30)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(run_amazon_israel_application())
