"""Private local Playwright transport for the Workday v2 adapter.

The transport reuses only an operator-bootstrapped, tenant-isolated Playwright
profile.  It never reads Chrome or Edge password stores.  Every page and
profile lease is owned by one async lifecycle and closed on that same event
loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
import zipfile
from collections.abc import Callable
from io import BytesIO
from secrets import token_bytes
from typing import Any
from urllib.parse import urlsplit

from core.config import Settings, get_settings
from core.portal_sessions import (
    PortalSessionError,
    PortalSessionLease,
    portal_session_for_url,
)
from core.submission_domain import (
    VERIFIED_ATTACHMENT_SENTINEL,
    AnswerDecisionV1,
    AnswerDisposition,
    FieldType,
    ReasonCode,
)
from submitters.workday_v2 import (
    WorkdayAdapterBlockedError,
    WorkdayAttachmentProof,
    WorkdayBrowserSnapshot,
    WorkdayCandidateSession,
    WorkdayPageState,
    assess_workday_v2_snapshot,
    observe_workday_v2_fields,
    workday_public_hostname,
)

_NAVIGATION_TIMEOUT_MS = 45_000
_ACTION_TIMEOUT_MS = 8_000
_UPLOAD_COMPLETE_SELECTORS = (
    '[data-automation-id="uploadCompleted"][data-upload-id]',
    '[data-automation-id="file-upload-success"][data-upload-id]',
    '[data-automation-id="attachmentStatus"][data-upload-id]',
)
_WORKDAY_STATIC_ASSET_DOMAINS = ("myworkdaycdn.com", "workdaycdn.com")


def _resume_payload_kind(payload: bytes) -> tuple[str, str]:
    if payload.startswith(b"%PDF-"):
        return "pdf", "application/pdf"
    try:
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile):
        names = set()
    if {"[Content_Types].xml", "word/document.xml"}.issubset(names):
        return (
            "docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)


class WorkdayNetworkGuard:
    """Exact-origin HTTPS and public-DNS policy for one candidate session."""

    def __init__(
        self,
        initial_url: str,
        *,
        resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
    ) -> None:
        self.expected_hostname = workday_public_hostname(initial_url)
        self._resolver = resolver
        self._dns_verified: set[str] = set()

    @staticmethod
    def _https_hostname(url: str) -> str:
        try:
            parsed = urlsplit((url or "").strip())
            port = parsed.port
        except ValueError as exc:
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        hostname = (parsed.hostname or "").lower()
        if (
            parsed.scheme.casefold() != "https"
            or not hostname
            or hostname != hostname.rstrip(".")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or port not in {None, 443}
            or any(ord(character) > 127 for character in hostname)
            or hostname == "localhost"
            or hostname.endswith((".localhost", ".local", ".internal"))
        ):
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            literal = None
        if literal is not None and not literal.is_global:
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        return hostname

    def require_allowed_url(self, url: str, *, main_frame: bool = True) -> None:
        hostname = self._https_hostname(url)
        if main_frame:
            workday_public_hostname(
                url,
                expected_hostname=self.expected_hostname,
            )
        elif hostname != self.expected_hostname and not any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in _WORKDAY_STATIC_ASSET_DOMAINS
        ):
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)

        if hostname in self._dns_verified:
            return
        try:
            answers = self._resolver(
                hostname,
                443,
                0,
                socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        addresses = {
            str(answer[4][0]).split("%", 1)[0]
            for answer in answers
            if len(answer) > 4 and answer[4]
        }
        if not addresses:
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        try:
            resolved = tuple(ipaddress.ip_address(address) for address in addresses)
        except ValueError as exc:
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        if any(not address.is_global for address in resolved):
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        self._dns_verified.add(hostname)


class PlaywrightWorkdayCandidateSession:
    """One lazily launched Workday page backed by a dedicated portal profile."""

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._lease: PortalSessionLease | None = None
        self._attachment: WorkdayAttachmentProof | None = None
        self._attachment_marker_id: str | None = None
        self._attachment_upload_name: str | None = None
        self._network_guard: WorkdayNetworkGuard | None = None
        self._clicked = False

    def _require_page(self) -> Any:
        if self._page is None:
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        return self._page

    async def navigate(self, url: str) -> None:
        if self._page is not None:
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        guard = WorkdayNetworkGuard(url)
        await asyncio.to_thread(guard.require_allowed_url, url)
        self._network_guard = guard
        try:
            portal = portal_session_for_url(
                url,
                self._settings.portal_browser_profile_root,
            )
        except PortalSessionError as exc:
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        if not portal.ready:
            raise WorkdayAdapterBlockedError(ReasonCode.SESSION_EXPIRED)

        lease = PortalSessionLease(
            portal,
            stale_minutes=self._settings.portal_session_lock_minutes,
        )
        try:
            lease.acquire()
        except PortalSessionError as exc:
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc
        self._lease = lease

        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            lease.release()
            self._lease = None
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc

        try:
            self._playwright = await async_playwright().start()
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(portal.profile_dir),
                headless=self._settings.portal_browser_headless,
                viewport={"width": 1280, "height": 900},
                service_workers="block",
            )
            await self._install_network_routes(self._context)
            self._page = (
                self._context.pages[0] if self._context.pages else await self._context.new_page()
            )
            await self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=_NAVIGATION_TIMEOUT_MS,
            )
            await self._assert_current_url()
        except Exception as exc:
            await self.close()
            if isinstance(exc, WorkdayAdapterBlockedError):
                raise
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY) from exc

    async def _install_network_routes(self, context: Any) -> None:
        route_web_socket = getattr(context, "route_web_socket", None)
        if not callable(route_web_socket):
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        await route_web_socket("**/*", self._block_web_socket)
        await context.route("**/*", self._guard_request)

    @staticmethod
    async def _block_web_socket(web_socket: Any) -> None:
        await web_socket.close(code=1008, reason="browser transport disabled")

    async def _guard_request(self, route: Any, request: Any) -> None:
        guard = self._network_guard
        if guard is None:
            await route.abort("blockedbyclient")
            return
        scheme = urlsplit(request.url).scheme.casefold()
        if scheme in {"data", "blob"} and not request.is_navigation_request():
            await route.continue_()
            return
        if scheme != "https":
            await route.abort("blockedbyclient")
            return
        try:
            await asyncio.to_thread(
                guard.require_allowed_url,
                request.url,
                main_frame=request.is_navigation_request(),
            )
        except WorkdayAdapterBlockedError:
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def _assert_current_url(self) -> None:
        page = self._require_page()
        guard = self._network_guard
        if guard is None:
            raise WorkdayAdapterBlockedError(ReasonCode.RUNTIME_NOT_READY)
        await asyncio.to_thread(guard.require_allowed_url, page.url)

    async def open_candidate_form(self) -> None:
        page = self._require_page()
        apply = page.locator('[data-automation-id="jobPostingApplyButton"]')
        if await apply.count() != 1 or not await apply.is_visible():
            raise WorkdayAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        await apply.click(timeout=_ACTION_TIMEOUT_MS)
        await page.wait_for_load_state("domcontentloaded")
        await self._assert_current_url()

        reuse = page.locator(
            '[data-automation-id="useMyLastApplication"], '
            'button:has-text("Use My Last Application")'
        )
        if await reuse.count() == 1 and await reuse.is_visible():
            await reuse.click(timeout=_ACTION_TIMEOUT_MS)
            await self._assert_current_url()
            return

        manual = page.locator(
            '[data-automation-id="applyManually"], button:has-text("Apply Manually")'
        )
        if await manual.count() == 1 and await manual.is_visible():
            await manual.click(timeout=_ACTION_TIMEOUT_MS)
            await self._assert_current_url()

    async def snapshot(self) -> WorkdayBrowserSnapshot:
        page = self._require_page()
        await self._assert_current_url()
        locale = await page.locator("html").get_attribute("lang") or "en"
        return WorkdayBrowserSnapshot(
            html=await page.content(),
            url=page.url,
            locale=locale,
        )

    async def _upload_marker_ids(self) -> set[str]:
        page = self._require_page()
        marker_ids: set[str] = set()
        for selector in _UPLOAD_COMPLETE_SELECTORS:
            nodes = page.locator(selector)
            count = await nodes.count()
            for index in range(count):
                node = nodes.nth(index)
                if not await node.is_visible():
                    continue
                marker_id = (await node.get_attribute("data-upload-id") or "").strip()
                if marker_id:
                    marker_ids.add(marker_id)
        return marker_ids

    async def _matching_upload_marker(
        self,
        *,
        expected_upload_name: str,
        expected_sha256: str,
    ) -> str | None:
        page = self._require_page()
        matches: list[str] = []
        for selector in _UPLOAD_COMPLETE_SELECTORS:
            nodes = page.locator(selector)
            for index in range(await nodes.count()):
                node = nodes.nth(index)
                if not await node.is_visible():
                    continue
                marker_id = (await node.get_attribute("data-upload-id") or "").strip()
                if not marker_id:
                    continue
                observed_digest = (
                    (await node.get_attribute("data-file-sha256") or "").strip().casefold()
                )
                observed_name = (await node.get_attribute("data-file-name") or "").strip()
                if not observed_name:
                    filename = node.locator('[data-automation-id="uploadedFileName"]')
                    if await filename.count() == 1 and await filename.is_visible():
                        observed_name = (await filename.inner_text()).strip()
                if (
                    observed_digest == expected_sha256
                    or observed_name.casefold() == expected_upload_name.casefold()
                ):
                    matches.append(marker_id)
        unique = tuple(dict.fromkeys(matches))
        return unique[0] if len(unique) == 1 else None

    async def ensure_resume_attachment(
        self,
        *,
        resume_bytes: bytes,
        cv_id: str,
        expected_sha256: str,
    ) -> WorkdayAttachmentProof:
        page = self._require_page()
        if hashlib.sha256(resume_bytes).hexdigest() != expected_sha256:
            raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        extension, mime_type = _resume_payload_kind(resume_bytes)
        local_name_digest = hashlib.sha256(
            token_bytes(32) + bytes.fromhex(expected_sha256)
        ).hexdigest()
        upload_name = f"resume-{local_name_digest[:24]}.{extension}"
        file_inputs = page.locator(
            '[data-automation-id="resumeUpload"] input[type="file"], '
            'input[data-automation-id="file-upload-input"][type="file"]'
        )
        if await file_inputs.count() != 1:
            raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)
        file_input = file_inputs.first
        before_marker_ids = await self._upload_marker_ids()
        try:
            await file_input.set_input_files(
                {
                    "name": upload_name,
                    "mimeType": mime_type,
                    "buffer": resume_bytes,
                },
                timeout=_ACTION_TIMEOUT_MS,
            )
            input_value = await file_input.input_value()
        except Exception as exc:
            raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED) from exc
        selected_basename = input_value.replace("\\", "/").rsplit("/", 1)[-1]
        if selected_basename.casefold() != upload_name.casefold():
            raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)

        marker_id: str | None = None
        for _poll in range(20):
            marker_id = await self._matching_upload_marker(
                expected_upload_name=upload_name,
                expected_sha256=expected_sha256,
            )
            if marker_id is not None and marker_id not in before_marker_ids:
                break
            marker_id = None
            await page.wait_for_timeout(100)
        if marker_id is None:
            raise WorkdayAdapterBlockedError(ReasonCode.ATTACHMENT_UNVERIFIED)

        receipt = hashlib.sha256(
            (
                f"{expected_sha256}|{marker_id}|{hashlib.sha256(upload_name.encode()).hexdigest()}"
            ).encode()
        ).hexdigest()
        self._attachment_marker_id = marker_id
        self._attachment_upload_name = upload_name
        self._attachment = WorkdayAttachmentProof(
            cv_id=cv_id,
            cv_sha256=expected_sha256,
            upload_complete=True,
            receipt_sha256=receipt,
        )
        return self._attachment

    async def verify_resume_attachment(
        self,
        *,
        cv_id: str,
        expected_sha256: str,
    ) -> WorkdayAttachmentProof:
        proof = self._attachment
        marker_id = self._attachment_marker_id
        upload_name = self._attachment_upload_name
        if (
            proof is None
            or marker_id is None
            or upload_name is None
            or not proof.matches(cv_id=cv_id, cv_sha256=expected_sha256)
            or await self._matching_upload_marker(
                expected_upload_name=upload_name,
                expected_sha256=expected_sha256,
            )
            != marker_id
        ):
            return WorkdayAttachmentProof(
                cv_id=cv_id,
                cv_sha256=expected_sha256,
                upload_complete=False,
            )
        return proof

    @staticmethod
    async def _radio_by_value(wrapper: Any, value: str) -> Any:
        radios = wrapper.locator('input[type="radio"]')
        matches = []
        for index in range(await radios.count()):
            candidate = radios.nth(index)
            if await candidate.get_attribute("value") == value:
                matches.append(candidate)
        if len(matches) != 1:
            raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
        return matches[0]

    async def fill(self, decisions: tuple[AnswerDecisionV1, ...]) -> None:
        page = self._require_page()
        fields = {
            field.field_id: field
            for field in observe_workday_v2_fields((await self.snapshot()).html)
        }
        for decision in decisions:
            if decision.disposition is not AnswerDisposition.RESOLVED:
                continue
            field = fields.get(decision.field_id)
            if field is None:
                raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
            if (
                field.field_type is FieldType.FILE
                and decision.value == VERIFIED_ATTACHMENT_SENTINEL
            ):
                continue
            wrapper = page.locator(
                f'[data-automation-id="formField"][data-field-id="{field.field_id}"]'
            )
            if await wrapper.count() != 1:
                raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
            control = wrapper.locator("input, textarea, select").first
            value = decision.value
            try:
                if field.field_type in {FieldType.SELECT, FieldType.MULTI_SELECT}:
                    await control.select_option(
                        value=list(value) if isinstance(value, tuple) else str(value)
                    )
                elif field.field_type is FieldType.RADIO:
                    await (await self._radio_by_value(wrapper, str(value))).check()
                elif field.field_type in {
                    FieldType.CHECKBOX,
                    FieldType.CONSENT,
                    FieldType.ATTESTATION,
                }:
                    await (control.check() if value is True else control.uncheck())
                else:
                    await control.fill(str(value))
            except WorkdayAdapterBlockedError:
                raise
            except Exception as exc:
                raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED) from exc

    async def advance_reversible_step(self) -> None:
        """Click exactly one non-final Next control after adapter verification."""

        page = self._require_page()
        assessment = assess_workday_v2_snapshot(
            (await self.snapshot()).html,
            page.url,
        )
        if assessment.reason_code is not None:
            raise WorkdayAdapterBlockedError(assessment.reason_code)
        if assessment.state not in {
            WorkdayPageState.FORM,
            WorkdayPageState.RESUME_UPLOAD,
        }:
            raise WorkdayAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        next_button = page.locator('button[data-automation-id="bottom-navigation-next-button"]')
        if await next_button.count() != 1 or not await next_button.is_visible():
            raise WorkdayAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        if "submit" in (await next_button.inner_text()).strip().casefold():
            raise WorkdayAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        await next_button.click(timeout=_ACTION_TIMEOUT_MS)
        await page.wait_for_timeout(300)
        await self._assert_current_url()

    async def click_final_action(self) -> None:
        page = self._require_page()
        if self._clicked:
            raise WorkdayAdapterBlockedError(ReasonCode.PERMIT_REPLAYED)
        assessment = assess_workday_v2_snapshot((await self.snapshot()).html, page.url)
        if assessment.state is not WorkdayPageState.REVIEW:
            raise WorkdayAdapterBlockedError(ReasonCode.FORM_CHANGED)
        submit = page.locator('button[data-automation-id="submitApplication"]')
        if await submit.count() != 1 or not await submit.is_visible():
            raise WorkdayAdapterBlockedError(ReasonCode.SELECTOR_DRIFT)
        self._clicked = True
        await submit.click(timeout=_ACTION_TIMEOUT_MS)

    async def confirmation_reference(self) -> str | None:
        """Require one stable, browser-visible nonblank employer reference."""

        page = self._require_page()
        await self._assert_current_url()
        locator = page.locator('main[data-automation-id="confirmationPage"][data-application-id]')
        if await locator.count() != 1 or not await locator.is_visible():
            return None
        first = (await locator.get_attribute("data-application-id") or "").strip()
        if not first:
            return None
        await page.wait_for_timeout(250)
        await self._assert_current_url()
        locator = page.locator('main[data-automation-id="confirmationPage"][data-application-id]')
        if await locator.count() != 1 or not await locator.is_visible():
            return None
        second = (await locator.get_attribute("data-application-id") or "").strip()
        return first if first == second and second else None

    async def close(self) -> None:
        context, playwright, lease = self._context, self._playwright, self._lease
        self._page = None
        self._context = None
        self._playwright = None
        self._lease = None
        self._network_guard = None
        self._attachment_marker_id = None
        self._attachment_upload_name = None
        try:
            if context is not None:
                await context.close()
        finally:
            try:
                if playwright is not None:
                    await playwright.stop()
            finally:
                if lease is not None:
                    lease.release()


def playwright_workday_browser_factory(_url: str) -> WorkdayCandidateSession:
    """Create a lazy local session; no browser starts until ``navigate``."""

    return PlaywrightWorkdayCandidateSession()
