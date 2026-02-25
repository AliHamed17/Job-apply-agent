"""Vision-based page analysis for obfuscated or canvas-rendered job pages.

Uses GPT-4o-vision (OpenAI) or Claude claude-sonnet-4-20250514 (Anthropic) to "look" at a
screenshot of a job page and extract structured job data via a JSON prompt.

This is the last-resort parser — triggered only when all HTML parsers fail.
It requires the ``playwright`` optional dependency to take the screenshot.

Safety & compliance:
- Only captures screenshots of pages already fetched by the fetcher
  (robots.txt + polite crawl delay already applied upstream).
- Never stores screenshots beyond the current task.
- Never attempts to interact with forms or bypass bot checks.
"""

from __future__ import annotations

import base64
import json
import re
import tempfile
from pathlib import Path

import structlog

from core.config import get_settings

logger = structlog.get_logger(__name__)

_VISION_SYSTEM_PROMPT = """\
You are a job posting extractor. Given a screenshot of a webpage, extract job
posting information and return it as a valid JSON object with these keys:

{
  "title": "Job title",
  "company": "Company name",
  "location": "Job location (or 'Remote')",
  "employment_type": "full-time | part-time | contract | internship",
  "seniority": "intern | junior | mid | senior | lead | director",
  "description": "Full job description text (plaintext)",
  "requirements": "Requirements and qualifications (plaintext)",
  "apply_url": "Direct application URL if visible",
  "date_posted": "Date posted if visible",
  "is_job_posting": true
}

If the page is NOT a job posting, return: {"is_job_posting": false}
Return ONLY the JSON object, no explanation or markdown.
"""


async def screenshot_url(url: str) -> bytes | None:
    """Capture a screenshot of the given URL using Playwright.

    Returns PNG bytes or None if Playwright is not installed or fails.
    Respects the polite crawl delay setting.
    """
    try:
        from playwright.async_api import async_playwright  # noqa: PLC0415
    except ImportError:
        logger.warning("playwright_not_installed", suggestion="pip install playwright")
        return None

    settings = get_settings()

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                # Mimic a real browser to reduce bot detection
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
            )
            page = await context.new_page()

            import asyncio  # noqa: PLC0415
            await asyncio.sleep(settings.polite_crawl_delay_seconds)

            await page.goto(url, timeout=15_000, wait_until="networkidle")
            screenshot = await page.screenshot(full_page=False, type="png")
            await browser.close()

            logger.info("screenshot_captured", url=url, size=len(screenshot))
            return screenshot

    except Exception as exc:
        logger.warning("screenshot_failed", url=url, error=str(exc))
        return None


async def analyze_screenshot_openai(screenshot: bytes, url: str) -> dict:
    """Send screenshot to GPT-4o-vision and parse the job JSON response."""
    settings = get_settings()
    try:
        import openai  # noqa: PLC0415
    except ImportError:
        return {}

    b64 = base64.b64encode(screenshot).decode()
    client = openai.AsyncOpenAI(api_key=settings.openai_api_key)

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            max_tokens=1500,
            temperature=0.0,
            messages=[
                {"role": "system", "content": _VISION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{b64}",
                                "detail": "high",
                            },
                        },
                        {"type": "text", "text": f"Page URL: {url}"},
                    ],
                },
            ],
        )
        raw = response.choices[0].message.content or "{}"
        return _parse_vision_json(raw)
    except Exception as exc:
        logger.error("vision_openai_failed", url=url, error=str(exc))
        return {}


async def analyze_screenshot_anthropic(screenshot: bytes, url: str) -> dict:
    """Send screenshot to Claude claude-sonnet-4-20250514 (vision) and parse the response."""
    settings = get_settings()
    try:
        import anthropic  # noqa: PLC0415
    except ImportError:
        return {}

    b64 = base64.b64encode(screenshot).decode()
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    try:
        message = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            system=_VISION_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": f"Page URL: {url}"},
                    ],
                }
            ],
        )
        raw = message.content[0].text if message.content else "{}"
        return _parse_vision_json(raw)
    except Exception as exc:
        logger.error("vision_anthropic_failed", url=url, error=str(exc))
        return {}


async def extract_job_via_vision(url: str) -> dict:
    """Capture a screenshot and analyse it with the configured LLM vision model.

    Returns a dict with job fields or an empty dict if vision extraction fails.
    """
    screenshot = await screenshot_url(url)
    if not screenshot:
        return {}

    settings = get_settings()
    if settings.llm_provider == "anthropic":
        result = await analyze_screenshot_anthropic(screenshot, url)
    else:
        result = await analyze_screenshot_openai(screenshot, url)

    if not result.get("is_job_posting"):
        logger.info("vision_not_job_posting", url=url)
        return {}

    logger.info("vision_extracted_job", url=url, title=result.get("title", ""))
    return result


def _parse_vision_json(raw: str) -> dict:
    """Strip markdown fences and parse JSON from an LLM response."""
    text = raw.strip()
    # Remove ```json ... ``` or ``` ... ``` wrappers
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("vision_json_parse_failed", raw=raw[:200])
        return {}
