"""Probe an ATS application page's live transport shape before building for it.

Run this FIRST, before any selector or fixture work on an adapter. It answers the
only question that can invalidate an entire adapter design: does this ATS submit
through a native multipart form POST, or through client-side XHR?

Measured 2026-08-04 against real postings, rendered in Chromium:

    adapter          forms  method  enctype  controls(named)  submit  file
    greenhouse         1     get     none        24 (1)         1      2
    ashby              0      -        -         38 (35)        0      2
    smartrecruiters    0      -        -          1 (0)         0      -

Greenhouse has a form but almost no named controls; Ashby has named controls but
no form element at all. Those are mirror-image failures of the same assumption,
so any transport built on "find the form, build a payload from named fields, call
HTMLFormElement.prototype.submit" cannot work on either. SmartRecruiters renders
its form behind a further navigation not followed here. Lever was not reached —
the candidate tenants in TARGETS missed; add a known one before citing it.

Static HTML is not enough to answer this: fetched without a browser, Ashby and
SmartRecruiters return zero forms and zero controls because they are SPA shells.
That is why this probe renders.

Observational only. It navigates, waits for the app to boot, and reads the DOM.
It never fills a field and never clicks a submit control, so it costs no
application. Extend TARGETS to cover a new adapter or re-check an existing one
after an ATS redesign.
"""

import json
import sys

from playwright.sync_api import sync_playwright

TARGETS = [
    ("greenhouse", "https://job-boards.greenhouse.io/gitlab/jobs/8503792002"),
    ("ashby", "https://jobs.ashbyhq.com/ashby/7458d4e9-da2e-47bd-98cb-adfda43d42b2/application"),
    ("smartrecruiters", "https://jobs.smartrecruiters.com/Visa/744000133907678"),
]

DOM_STATS = """
() => {
  const inputs = Array.from(document.querySelectorAll('input, textarea, select'));
  const forms = Array.from(document.querySelectorAll('form'));
  return {
    forms: forms.length,
    form_methods: forms.map(f => (f.getAttribute('method') || '(none)').toLowerCase()).slice(0, 3),
    form_enctypes: forms.map(f => (f.getAttribute('enctype') || '(none)')).slice(0, 3),
    controls: inputs.length,
    controls_named: inputs.filter(e => e.getAttribute('name')).length,
    file_inputs: document.querySelectorAll('input[type=file]').length,
    labels_for: document.querySelectorAll('label[for]').length,
    data_field_id: document.querySelectorAll('[data-field-id]').length,
    data_qa: document.querySelectorAll('[data-qa]').length,
    submit_inputs: document.querySelectorAll('button[type=submit], input[type=submit]').length,
    button_type_button: document.querySelectorAll('button[type=button]').length,
  };
}
"""

rows = []
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    for label, url in TARGETS:
        page = browser.new_page(user_agent="Mozilla/5.0 (compatible; transport-probe)")
        reqs = []
        page.on("request", lambda r: reqs.append((r.method, r.resource_type)))
        rec = {"adapter": label, "url": url}
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            rec.update(page.evaluate(DOM_STATS))
            rec["title"] = (page.title() or "")[:60]
            rec["xhr_requests"] = sum(1 for m, t in reqs if t in ("xhr", "fetch"))
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {str(e)[:90]}"
        rows.append(rec)
        print(f"  {label:16} done", file=sys.stderr)
        page.close()
    browser.close()

print(json.dumps(rows, indent=1))
