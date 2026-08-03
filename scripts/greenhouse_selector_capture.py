"""Capture real Greenhouse application markup while the operator applies by hand.

Why this exists
---------------
``tests/fixtures/greenhouse_v1/*.html`` are hand-written. They encode what we
*assumed* Greenhouse emits, and the adapter was built against them: every branch
of ``_FIELD_WRAPPER_SELECTOR`` requires ``data-field-id``, which real Greenhouse
never emits, so a real page yields zero wrappers and ``SELECTOR_DRIFT`` on the
first inspection. Twenty-two green fixtures cannot detect that. Only real markup
can.

What this does
--------------
Opens one job URL in a visible browser, then waits. **The operator applies by
hand.** This script never fills a field, never uploads a file and never clicks
submit — it only observes, and records:

* the application form subtree, sanitised (before anything is typed);
* the post-submit confirmation subtree, sanitised;
* a request transcript classifying the submit and any upload;
* candidate confirmation selectors.

Two tripwires are evaluated from the transcript, because either one invalidates
the P1 plan and must stop it rather than be worked around:

1. **The submit is XHR/JSON rather than a navigating form POST.**
   ``structureReady()`` demands a literal ``method=post``/``enctype=multipart``
   form and calls ``HTMLFormElement.prototype.submit``; the request guard aborts
   every non-GET pre-gate. An XHR submit means the adapter's transport model is
   wrong.
2. **The resume upload is asynchronous** (a request fires on file-input change).
   The adapter commits one multipart POST carrying the file; an async upload
   needs a separately gated, exactly-pinned endpoint allowance, which is a much
   larger and more dangerous change.

Privacy
-------
Output is destined for committed fixtures, so sanitisation is not cosmetic.
Every input ``value``, textarea body, ``contenteditable`` text, selected option,
file name, ``script``/``style``/``svg`` node and comment is dropped, and text
nodes are scrubbed of anything resembling an email, phone number or long digit
run. Structure, tag names, ``id``/``name``/``class``, ``data-*``, ``aria-*`` and
label text are kept — those are the parts a selector contract is written
against. Review the output before copying anything into ``tests/fixtures/``.

Usage
-----
    python scripts/greenhouse_selector_capture.py \
        --url https://boards.greenhouse.io/<tenant>/jobs/<id>

Pick a plain server-rendered ``boards.greenhouse.io`` posting with no US EEO
block, no consent checkbox and no cover-letter file input. Each of those costs
real applications to qualify later and none is needed for the first proof.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = ROOT / ".capture" / "greenhouse"
DEFAULT_PROFILE_DIR = ROOT / ".capture" / "profile"

# Structural attributes a selector contract is legitimately written against.
_KEEP_ATTR_PREFIXES = ("data-", "aria-")
_KEEP_ATTRS = {
    "id",
    "name",
    "class",
    "type",
    "for",
    "role",
    "required",
    "multiple",
    "accept",
    "method",
    "action",
    "enctype",
    "lang",
    "placeholder",
    "disabled",
    "checked",
    "selected",
}
# Attributes that can carry a value the operator typed.
_DROP_ATTRS = {"value", "src", "srcset", "href", "content", "title", "alt", "style"}

_EMAIL_RE = re.compile(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<![\w])\+?\d(?:[\d ()\-]{6,}\d)(?![\w])")
_LONG_DIGITS_RE = re.compile(r"\d{5,}")

_FORM_CANDIDATES = (
    "form#application_form",
    "form[data-greenhouse-application]",
    "form[enctype='multipart/form-data']",
    "main form",
    "form",
)
_CONFIRMATION_CANDIDATES = (
    "[data-qa='confirmation']",
    "#application_confirmation",
    ".application--confirmation",
    "[role='status']",
    "main h1",
    "main",
)


@dataclass
class RequestRecord:
    """One observed request, reduced to what the tripwires need."""

    url: str
    method: str
    resource_type: str
    is_navigation: bool
    content_type: str
    has_post_data: bool
    phase: str

    def redacted(self) -> dict[str, Any]:
        # Path only: a query string can carry a token or an identifier.
        without_query = self.url.split("?", 1)[0]
        return {
            "url": without_query,
            "method": self.method,
            "resource_type": self.resource_type,
            "is_navigation": self.is_navigation,
            "content_type": self.content_type,
            "has_post_data": self.has_post_data,
            "phase": self.phase,
        }


@dataclass
class Capture:
    transcript: list[RequestRecord] = field(default_factory=list)
    phase: str = "form_load"

    def record(self, request) -> None:
        try:
            headers = request.headers
            self.transcript.append(
                RequestRecord(
                    url=request.url,
                    method=request.method,
                    resource_type=request.resource_type,
                    is_navigation=bool(request.is_navigation_request()),
                    content_type=headers.get("content-type", ""),
                    has_post_data=request.post_data is not None,
                    phase=self.phase,
                )
            )
        except Exception:
            # An observation failure must never interrupt the operator.
            pass


def _scrub_text(text: str) -> str:
    text = _EMAIL_RE.sub("[email]", text)
    text = _PHONE_RE.sub("[phone]", text)
    return _LONG_DIGITS_RE.sub("[digits]", text)


def sanitize_html(html: str) -> str:
    """Keep structure and labels; drop every operator-supplied value."""
    from bs4 import BeautifulSoup, Comment

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    for node in soup.find_all(["script", "style", "svg", "noscript", "iframe"]):
        node.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    for tag in soup.find_all(True):
        kept: dict[str, Any] = {}
        for key, value in list(tag.attrs.items()):
            lowered = key.lower()
            if lowered in _DROP_ATTRS:
                continue
            if lowered in _KEEP_ATTRS or lowered.startswith(_KEEP_ATTR_PREFIXES):
                # form@action is kept because the transport shape depends on it,
                # but its query string can carry a token or an application id.
                if lowered == "action" and isinstance(value, str):
                    value = value.split("?", 1)[0].split("#", 1)[0]
                kept[key] = value
        tag.attrs = kept
        # A textarea's body and a contenteditable's text are typed content.
        if tag.name == "textarea" or tag.has_attr("contenteditable"):
            tag.string = ""

    for text_node in list(soup.find_all(string=True)):
        cleaned = _scrub_text(str(text_node))
        if cleaned != str(text_node):
            text_node.replace_with(cleaned)

    return soup.prettify()


def _first_present(page, selectors: tuple[str, ...]) -> tuple[str | None, str | None]:
    """Return (selector, outer_html) for the first selector that matches."""
    for selector in selectors:
        try:
            handle = page.query_selector(selector)
        except Exception:
            continue
        if handle is not None:
            try:
                return selector, handle.evaluate("el => el.outerHTML")
            except Exception:
                continue
    return None, None


def _observed_controls(page) -> list[dict[str, Any]]:
    """Structural description of every control, with no typed values."""
    script = """
    () => Array.from(document.querySelectorAll('input, textarea, select')).map(el => {
      const labels = [];
      if (el.id) {
        document.querySelectorAll(`label[for="${el.id}"]`).forEach(l =>
          labels.push((l.textContent || '').trim().slice(0, 120)));
      }
      let ancestor = el.closest('label');
      if (ancestor) labels.push((ancestor.textContent || '').trim().slice(0, 120));
      const described = el.getAttribute('aria-labelledby');
      if (described) described.split(/\\s+/).forEach(id => {
        const n = document.getElementById(id);
        if (n) labels.push((n.textContent || '').trim().slice(0, 120));
      });
      const wrapper = el.closest('[data-field-id], [data-gh-field], [data-qa], .field, fieldset');
      return {
        tag: el.tagName.toLowerCase(),
        type: (el.getAttribute('type') || '').toLowerCase(),
        name: el.getAttribute('name') || '',
        id: el.id || '',
        required: el.required === true || el.getAttribute('aria-required') === 'true',
        visually_required: /\\*|required/i.test(labels.join(' ')),
        option_count: el.tagName === 'SELECT' ? el.options.length : null,
        labels: labels.filter(Boolean),
        wrapper_selector: wrapper
          ? wrapper.tagName.toLowerCase() +
            (wrapper.getAttribute('data-field-id') ? '[data-field-id]' : '') +
            (wrapper.getAttribute('data-gh-field') ? '[data-gh-field]' : '') +
            (wrapper.getAttribute('data-qa')
              ? `[data-qa="${wrapper.getAttribute('data-qa')}"]` : '') +
            (wrapper.className
              ? '.' + String(wrapper.className).trim().split(/\\s+/).join('.') : '')
          : null,
        has_data_field_id: el.closest('[data-field-id]') !== null,
      };
    })
    """
    try:
        return page.evaluate(script)
    except Exception as exc:  # pragma: no cover - diagnostic only
        return [{"error": f"{type(exc).__name__}: {exc}"}]


def _form_shape(page) -> dict[str, Any]:
    script = """
    () => {
      const f = document.querySelector(
        'form#application_form, form[data-greenhouse-application], form[enctype], main form, form'
      );
      if (!f) return {found: false};
      return {
        found: true,
        method: (f.getAttribute('method') || '').toLowerCase(),
        enctype: (f.getAttribute('enctype') || '').toLowerCase(),
        action_path: (() => {
          try { return new URL(f.action, location.href).pathname; } catch (e) { return null; }
        })(),
        action_is_same_origin: (() => {
          try {
            return new URL(f.action, location.href).origin === location.origin;
          } catch (e) { return null; }
        })(),
        file_input_count: f.querySelectorAll('input[type=file]').length,
        submit_button_count: f.querySelectorAll('button[type=submit], input[type=submit]').length,
      };
    }
    """
    try:
        return page.evaluate(script)
    except Exception as exc:  # pragma: no cover
        return {"found": False, "error": f"{type(exc).__name__}: {exc}"}


def evaluate_tripwires(
    transcript: list[RequestRecord], form_shape: dict[str, Any]
) -> list[dict[str, str]]:
    """Return blocking findings. Empty means P1 may proceed as specified."""
    findings: list[dict[str, str]] = []

    submits = [
        r
        for r in transcript
        if r.method.upper() == "POST" and r.phase in {"submitting", "confirmation"}
    ]
    navigating = [r for r in submits if r.is_navigation]
    xhr = [r for r in submits if r.resource_type in {"xhr", "fetch"}]

    if not submits:
        findings.append(
            {
                "tripwire": "NO_SUBMIT_OBSERVED",
                "detail": (
                    "No POST was recorded during the submit phase. Either the capture "
                    "phases were advanced out of order, or the submit left no request "
                    "this observer can see. Re-run before drawing conclusions."
                ),
            }
        )
    elif not navigating and xhr:
        findings.append(
            {
                "tripwire": "SUBMIT_IS_XHR",
                "detail": (
                    "The submit is XHR/fetch, not a navigating form POST. "
                    "structureReady() requires a literal method=post form and the "
                    "request guard aborts non-GET pre-gate requests, so the adapter's "
                    "transport model does not apply. STOP and re-scope P1."
                ),
            }
        )

    uploads = [
        r
        for r in transcript
        if r.phase == "file_selected" and r.method.upper() in {"POST", "PUT", "PATCH"}
    ]
    if uploads:
        findings.append(
            {
                "tripwire": "ASYNC_UPLOAD",
                "detail": (
                    f"{len(uploads)} mutating request(s) fired when the file was chosen, "
                    "so the resume uploads asynchronously rather than inside the submit "
                    "POST. This needs a separately gated, exactly-pinned endpoint "
                    "allowance. STOP and re-scope before building it."
                ),
            }
        )

    if form_shape.get("found"):
        if form_shape.get("method") != "post":
            findings.append(
                {
                    "tripwire": "FORM_METHOD_NOT_POST",
                    "detail": (
                        f"form method is {form_shape.get('method')!r}; "
                        "structureReady() requires post."
                    ),
                }
            )
        if "multipart" not in (form_shape.get("enctype") or ""):
            findings.append(
                {
                    "tripwire": "FORM_ENCTYPE_NOT_MULTIPART",
                    "detail": (
                        f"form enctype is {form_shape.get('enctype')!r}; the adapter's "
                        "payload commitment assumes multipart/form-data."
                    ),
                }
            )
        if (form_shape.get("file_input_count") or 0) > 1:
            findings.append(
                {
                    "tripwire": "MULTIPLE_FILE_INPUTS",
                    "detail": (
                        f"{form_shape['file_input_count']} file inputs observed. The "
                        "single-file payload commitment is coupled in four places; pick "
                        "a posting with one."
                    ),
                }
            )
    else:
        findings.append(
            {
                "tripwire": "NO_FORM_FOUND",
                "detail": "No application form matched any candidate selector.",
            }
        )

    return findings


def _prompt(message: str) -> None:
    print(f"\n{'=' * 72}\n{message}\n{'=' * 72}")
    try:
        input("Press Enter when done... ")
    except EOFError:
        print("\nNo interactive stdin — this script must be run in a real terminal.")
        sys.exit(2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", required=True, help="One Greenhouse job URL")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.profile_dir.mkdir(parents=True, exist_ok=True)
    capture = Capture()

    print(__doc__)
    print(f"\nOutput: {args.output_dir}\nProfile: {args.profile_dir}")
    print(
        "\nThis script never fills a field, never uploads and never clicks submit.\n"
        "You do all of that by hand. It only watches."
    )

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(args.profile_dir),
            headless=False,
            accept_downloads=False,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.on("request", capture.record)

        page.goto(args.url, wait_until="domcontentloaded")

        capture.phase = "form_load"
        _prompt(
            "STEP 1 of 4 — form loaded.\n"
            "Do NOT type anything yet. Confirm the application form is visible,\n"
            "then press Enter to record the blank form structure."
        )
        form_selector, form_html = _first_present(page, _FORM_CANDIDATES)
        blank_controls = _observed_controls(page)
        form_shape = _form_shape(page)
        if form_html:
            (args.output_dir / "form_blank.html").write_text(
                sanitize_html(form_html), encoding="utf-8"
            )

        capture.phase = "file_selected"
        _prompt(
            "STEP 2 of 4 — attach your CV only.\n"
            "Choose the resume file, then STOP. Do not fill anything else and do\n"
            "not submit. Press Enter so I can see whether the upload fires its own\n"
            "request (an async upload changes the plan)."
        )

        capture.phase = "filled"
        _prompt(
            "STEP 3 of 4 — fill the rest of the form by hand, but DO NOT SUBMIT.\n"
            "Press Enter once every field is filled and you are about to submit."
        )
        filled_controls = _observed_controls(page)

        capture.phase = "submitting"
        _prompt(
            "STEP 4 of 4 — now click submit yourself and wait for the confirmation\n"
            "page to finish loading. Then press Enter to record the confirmation."
        )
        capture.phase = "confirmation"
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:
            pass
        confirmation_selector, confirmation_html = _first_present(page, _CONFIRMATION_CANDIDATES)
        if confirmation_html:
            (args.output_dir / "confirmation.html").write_text(
                sanitize_html(confirmation_html), encoding="utf-8"
            )

        confirmation_url_path = page.url.split("?", 1)[0]
        context.close()

    findings = evaluate_tripwires(capture.transcript, form_shape)
    report = {
        "form_selector": form_selector,
        "form_shape": form_shape,
        "confirmation_selector": confirmation_selector,
        "confirmation_url_path": confirmation_url_path,
        "controls_blank": blank_controls,
        "controls_filled": filled_controls,
        "any_control_has_data_field_id": any(
            c.get("has_data_field_id") for c in blank_controls if isinstance(c, dict)
        ),
        "transcript": [r.redacted() for r in capture.transcript],
        "tripwires": findings,
        "proceed": not findings,
    }
    (args.output_dir / "capture.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )

    print(f"\n{'=' * 72}")
    print(f"Wrote {args.output_dir / 'capture.json'}")
    print(f"  form selector:         {form_selector}")
    print(f"  confirmation selector: {confirmation_selector}")
    print(f"  form shape:            {form_shape}")
    print(f"  data-field-id present: {report['any_control_has_data_field_id']}")
    print(f"  requests observed:     {len(capture.transcript)}")
    if findings:
        print("\n  TRIPWIRES — do not proceed with P1 as specified:")
        for f in findings:
            print(f"    [{f['tripwire']}] {f['detail']}")
    else:
        print("\n  No tripwires. P1 may proceed against this markup.")
    print(f"\nReview the sanitised HTML before copying anything into tests/fixtures/.\n{'=' * 72}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
