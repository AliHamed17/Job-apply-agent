"""Capture real Lever application markup while the operator applies by hand.

Why this exists
----------------
The existing Lever adapter (``submitters/lever_v1.py``) was built against assumed
markup: ``LEVER_FORM_SELECTOR`` requires ``form[data-qa="application-form"]
[data-posting-id][data-site]``, and field extraction requires wrappers matching
``[data-qa="application-field"][data-field-id]``. A plain ``curl`` against two
independent real Lever tenants (``jobs.lever.co/gopuff``,
``jobs.lever.co/shieldai``) showed neither exists in real markup:

    real form:   <form id="application-form" enctype="multipart/form-data" method="POST">
    real fields: <li class="application-question"><label>
                   <div class="application-label">Full name<span class="required">*</span></div>
                   <div class="application-field"><input data-qa="name-input" name="name"></div>
                 </label></li>

So the form-detection selector alone fails to match (``SELECTOR_DRIFT``) on real
Lever pages, before field extraction is even attempted. Unlike Greenhouse, the
underlying *transport* is already confirmed correct on both tenants (native
``method=post``/``enctype=multipart/form-data``, every control named) — this is
a selector-contract problem, not a transport rebuild.

Three distinct field shapes exist on a real Lever form, and this capture only
observes them; it does not resolve which selector contract to write:

1. **Simple identity fields** (name, email, phone, location) follow the pattern
   shown above — one ``data-qa`` value per field, stable across postings.
2. **Resume upload** is not a plain file input. It sits inside
   ``<a class="invisible-resume-upload" data-qa="input-resume">`` with visible
   "Analyzing resume...", "Couldn't auto-read resume" and success states — a
   strong signal the file is parsed asynchronously the moment it is chosen,
   not uploaded inside the final submit POST. The ``ASYNC_UPLOAD`` tripwire
   below exists specifically to confirm or refute this before any adapter code
   is written against it.
3. **Survey/EEO questions** (age range, race, veteran status, ...) use
   ``<ul data-qa="multiple-choice">``/``<ul data-qa="checkboxes">`` with
   ``name="surveysResponses[<uuid>][responses][fieldN]"`` — a per-posting
   random UUID in the field name, not a stable identifier. A selector contract
   keyed on ``name`` will not generalise across postings for these; note this
   when reviewing captured output.

What this does
---------------
Opens one job URL in a visible browser, then waits. **The operator applies by
hand.** This script never fills a field, never uploads a file and never clicks
submit — it only observes, and records:

* the application form subtree, sanitised (before anything is typed);
* the post-submit confirmation subtree, sanitised;
* a request transcript classifying the submit and any upload;
* candidate confirmation selectors.

Two tripwires are readable from the blank form alone, before a single
keystroke, and the script aborts right there if either trips — a real
application, once submitted, cannot be un-submitted, and most ATSs bar
reapplying to the same posting for 6-12 months, so an early, free abort is not
an optimisation, it is what keeps this script from costing exactly what it
exists to avoid:

* the form is not a navigating ``method=post``/``enctype=multipart`` form
  (would mean the transport probe's finding does not hold for this posting);
* more than one file input (the single-file payload commitment is coupled in
  four places elsewhere in the codebase).

One further tripwire can only be evaluated from the full transcript:

* **the resume upload fires a request on file-input change** (``ASYNC_UPLOAD``).
  If confirmed, the adapter needs a separately gated, exactly-pinned endpoint
  allowance before any submit-time file handling is built — a materially
  larger and more dangerous change than a selector-contract update.

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
    python scripts/lever_selector_capture.py \
        --url https://jobs.lever.co/<tenant>/<posting-id>/apply

Pick a posting with one resume upload and, ideally, no survey/EEO block for
the first proof — each extra field shape is real coverage work, not free.
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
DEFAULT_OUTPUT_DIR = ROOT / ".capture" / "lever"
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
    "form#application-form",
    "form[enctype='multipart/form-data']",
    "main form",
    "form",
)
_CONFIRMATION_CANDIDATES = (
    "[data-qa='confirmation']",
    ".confirmation",
    ".application-confirmation",
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
      const wrapper = el.closest('li.application-question, [data-qa], .field, fieldset');
      return {
        tag: el.tagName.toLowerCase(),
        type: (el.getAttribute('type') || '').toLowerCase(),
        name: el.getAttribute('name') || '',
        id: el.id || '',
        data_qa: el.getAttribute('data-qa') || (el.closest('[data-qa]') || {}).getAttribute
          ? (el.getAttribute('data-qa') || el.closest('[data-qa]')?.getAttribute('data-qa') || '')
          : '',
        required: el.required === true || el.getAttribute('aria-required') === 'true',
        visually_required: /\\*|required/i.test(labels.join(' ')),
        option_count: el.tagName === 'SELECT' ? el.options.length : null,
        labels: labels.filter(Boolean),
        wrapper_selector: wrapper
          ? wrapper.tagName.toLowerCase() +
            (wrapper.getAttribute('data-qa')
              ? `[data-qa="${wrapper.getAttribute('data-qa')}"]` : '') +
            (wrapper.className
              ? '.' + String(wrapper.className).trim().split(/\\s+/).join('.') : '')
          : null,
        looks_like_dynamic_survey_name: /\\[[0-9a-f-]{20,}\\]/.test(el.getAttribute('name') || ''),
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
        'form#application-form, form[enctype], main form, form'
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


def form_shape_tripwires(form_shape: dict[str, Any]) -> list[dict[str, str]]:
    """Findings determinable from the blank form alone.

    None of these need a keystroke, an upload or a submit — they read only the
    just-loaded form's own attributes. Checking them before Steps 2-4 lets the
    operator abort before spending a real, irreversible application on a
    transport that provably cannot work, instead of learning that only after
    a real submit.
    """
    if not form_shape.get("found"):
        return [
            {
                "tripwire": "NO_FORM_FOUND",
                "detail": "No application form matched any candidate selector.",
            }
        ]

    findings: list[dict[str, str]] = []
    if form_shape.get("method") != "post":
        findings.append(
            {
                "tripwire": "FORM_METHOD_NOT_POST",
                "detail": (
                    f"form method is {form_shape.get('method')!r}; the transport probe's "
                    "finding (native multipart POST) does not hold for this posting."
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
    return findings


def evaluate_tripwires(
    transcript: list[RequestRecord], form_shape: dict[str, Any]
) -> list[dict[str, str]]:
    """Return blocking findings. Empty means field-selector work may proceed."""
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
                    "The submit is XHR/fetch, not a navigating form POST. This "
                    "contradicts the transport probe's finding for this tenant — "
                    "STOP and re-measure before building against it."
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
                    f"{len(uploads)} mutating request(s) fired when the resume was chosen, "
                    "confirming the 'Analyzing resume...' UI parses it asynchronously "
                    "rather than inside the submit POST. This needs a separately gated, "
                    "exactly-pinned endpoint allowance. STOP and re-scope before building "
                    "submit-time file handling."
                ),
            }
        )

    findings.extend(form_shape_tripwires(form_shape))

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
    parser.add_argument("--url", required=True, help="One Lever job apply URL")
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

        early_findings = form_shape_tripwires(form_shape)
        if early_findings:
            print(f"\n{'=' * 72}")
            print(
                "STOP — the blank form already trips a tripwire that no amount of\n"
                "typing, uploading or submitting can fix. Aborting now, before any of\n"
                "that, so this doesn't cost a real application to learn."
            )
            for f in early_findings:
                print(f"    [{f['tripwire']}] {f['detail']}")
            print(f"{'=' * 72}")
            context.close()
            report = {
                "form_selector": form_selector,
                "form_shape": form_shape,
                "controls_blank": blank_controls,
                "tripwires": early_findings,
                "proceed": False,
                "aborted_before_submit": True,
            }
            (args.output_dir / "capture.json").write_text(
                json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
            )
            print(f"Wrote {args.output_dir / 'capture.json'}")
            return 1

        capture.phase = "file_selected"
        _prompt(
            "STEP 2 of 4 — attach your CV only.\n"
            "Choose the resume file, then STOP. Do not fill anything else and do\n"
            "not submit. Watch for the 'Analyzing resume...' indicator — press\n"
            "Enter once it settles (success or failure) so I can see whether the\n"
            "upload fired its own request (an async upload changes the plan)."
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
    dynamic_name_fields = [
        c.get("name", "")
        for c in filled_controls
        if isinstance(c, dict) and c.get("looks_like_dynamic_survey_name")
    ]
    report = {
        "form_selector": form_selector,
        "form_shape": form_shape,
        "confirmation_selector": confirmation_selector,
        "confirmation_url_path": confirmation_url_path,
        "controls_blank": blank_controls,
        "controls_filled": filled_controls,
        "dynamic_survey_name_fields": dynamic_name_fields,
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
    print(f"  requests observed:     {len(capture.transcript)}")
    if dynamic_name_fields:
        print(
            f"  {len(dynamic_name_fields)} field(s) have survey-style dynamic "
            "names (e.g. surveysResponses[<uuid>]...) — these will not have a "
            "stable name-based selector across postings."
        )
    if findings:
        print("\n  TRIPWIRES — do not build the field selector contract yet:")
        for f in findings:
            print(f"    [{f['tripwire']}] {f['detail']}")
    else:
        print("\n  No tripwires. Field-selector work may proceed against this markup.")
    print(f"\nReview the sanitised HTML before copying anything into tests/fixtures/.\n{'=' * 72}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
