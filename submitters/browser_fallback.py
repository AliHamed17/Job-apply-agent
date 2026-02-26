"""Shared browser fallback utilities for ATS form filling."""

from __future__ import annotations

from submitters.base import SubmissionResult


_PLATFORM_PROFILES: dict[str, dict[str, list[str]]] = {
    "default": {
        "first_name": ['input[name*="first" i]', 'input[id*="first" i]'],
        "last_name": ['input[name*="last" i]', 'input[id*="last" i]'],
        "email": ['input[type="email"]', 'input[name*="email" i]'],
        "phone": ['input[type="tel"]', 'input[name*="phone" i]'],
        "cover": ['textarea[name*="cover" i]', 'textarea[id*="cover" i]'],
    },
    "smartrecruiters": {
        "cover": ['textarea[aria-label*="Cover" i]', 'textarea[name*="cover" i]'],
    },
    "jobvite": {
        "cover": ['textarea[name*="message" i]', 'textarea[name*="cover" i]'],
    },
}


def _selectors(platform_name: str, key: str) -> str:
    default = _PLATFORM_PROFILES["default"].get(key, [])
    specific = _PLATFORM_PROFILES.get(platform_name, {}).get(key, [])
    return ", ".join(specific + default)


async def run_browser_form_fill(
    *,
    platform_name: str,
    apply_url: str,
    application,
    user_profile: dict,
    resume_path: str | None = None,
    safe_mode: bool = True,
) -> SubmissionResult:
    """Best-effort Playwright form fill; leaves final submit to human confirmation."""
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except Exception as exc:
        return SubmissionResult(
            success=False,
            platform=platform_name,
            status="failed",
            error=f"Playwright not available: {exc}",
        )

    personal = user_profile.get("personal", {})
    full_name = personal.get("name", "")
    names = full_name.split()

    def _qa_value(label: str) -> str:
        qa = getattr(application, "qa_answers", {}) or {}
        low = label.lower()
        for key, value in qa.items():
            if key.lower().replace("_", " ") in low:
                return str(value)
        if "authoriz" in low:
            return "Yes"
        if "sponsor" in low or "visa" in low:
            return "No"
        if "experience" in low or "years" in low:
            return "0"
        return ""

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(apply_url, wait_until="domcontentloaded", timeout=30000)

            first = page.locator(_selectors(platform_name, "first_name")).first
            if await first.count():
                await first.fill(names[0] if names else "")

            last = page.locator(_selectors(platform_name, "last_name")).first
            if await last.count():
                await last.fill(" ".join(names[1:]))

            email = page.locator(_selectors(platform_name, "email")).first
            if await email.count():
                await email.fill(personal.get("email", ""))

            phone = page.locator(_selectors(platform_name, "phone")).first
            if await phone.count():
                await phone.fill(personal.get("phone", ""))

            if resume_path:
                upload = page.locator('input[type="file"]').first
                if await upload.count():
                    await upload.set_input_files(resume_path)

            if getattr(application, "cover_letter", ""):
                cover = page.locator(_selectors(platform_name, "cover")).first
                if await cover.count():
                    await cover.fill(application.cover_letter[:4000])

            if not safe_mode:
                text_inputs = page.locator('input[type="text"], textarea')
                for idx in range(await text_inputs.count()):
                    inp = text_inputs.nth(idx)
                    if not await inp.is_visible():
                        continue
                    val = await inp.input_value()
                    if val:
                        continue
                    label = (
                        (await inp.get_attribute("aria-label"))
                        or (await inp.get_attribute("name"))
                        or (await inp.get_attribute("id"))
                        or ""
                    )
                    mapped = _qa_value(label)
                    if mapped:
                        await inp.fill(mapped)

                checkboxes = page.locator('input[type="checkbox"]')
                for idx in range(await checkboxes.count()):
                    cb = checkboxes.nth(idx)
                    if await cb.is_visible() and not await cb.is_checked():
                        await cb.check(force=True)

            selects = page.locator("select")
            for idx in range(await selects.count()):
                sel = selects.nth(idx)
                label = ((await sel.get_attribute("name")) or "").lower()
                if "authoriz" in label:
                    await sel.select_option(label="Yes")
                elif "sponsor" in label or "visa" in label:
                    await sel.select_option(label="No")

            await browser.close()

        return SubmissionResult(
            success=False,
            platform=platform_name,
            status="requires_human_confirmation",
            error="Browser form filled (submission left for final human confirmation)",
        )
    except PlaywrightTimeoutError:
        return SubmissionResult(
            success=False,
            platform=platform_name,
            status="failed",
            error=f"Timed out while loading {platform_name} application form",
        )
    except Exception as exc:
        return SubmissionResult(
            success=False,
            platform=platform_name,
            status="failed",
            error=str(exc),
        )
