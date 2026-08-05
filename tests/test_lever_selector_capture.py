"""Guards for the Lever capture tool. Its output is destined for committed
fixtures, so a sanitisation hole would commit the operator's personal data."""

from __future__ import annotations

from scripts.lever_selector_capture import (
    RequestRecord,
    evaluate_tripwires,
    form_shape_tripwires,
    sanitize_html,
)

_FILLED_FORM = """
<form id="application-form" enctype="multipart/form-data" method="POST">
  <script>var tracking = "beacon";</script>
  <!-- internal comment -->
  <li class="application-question">
    <label>
      <div class="application-label">Full name<span class="required">*</span></div>
      <div class="application-field">
        <input type="text" data-qa="name-input" name="name" value="Ali Hamed" required>
      </div>
    </label>
  </li>
  <li class="application-question">
    <label>
      <div class="application-label">Email</div>
      <div class="application-field">
        <input name="email" data-qa="email-input" type="email" value="ali.h.10j@gmail.com">
      </div>
    </label>
  </li>
  <li class="application-question">
    <label>
      <div class="application-label">Phone</div>
      <div class="application-field">
        <input name="phone" data-qa="phone-input" type="tel" value="+972 53 339 2826">
      </div>
    </label>
  </li>
  <p>Reach the recruiter at recruiter@example.com or +1 (555) 867-5309.</p>
  <button type="submit" data-qa="btn-submit">Submit application</button>
</form>
"""


def test_sanitizer_drops_every_typed_value():
    out = sanitize_html(_FILLED_FORM)
    for leaked in ("Ali Hamed", "ali.h.10j@gmail.com", "972 53 339 2826"):
        assert leaked not in out, f"sanitised output leaked {leaked!r}"
    assert "tracking" not in out and "internal comment" not in out


def test_sanitizer_keeps_the_structure_a_selector_needs():
    out = sanitize_html(_FILLED_FORM)
    for kept in (
        'id="application-form"',
        'data-qa="name-input"',
        'data-qa="email-input"',
        'class="application-question"',
        'data-qa="btn-submit"',
        "multipart/form-data",
    ):
        assert kept in out, f"sanitiser destroyed structure: {kept!r}"


def test_sanitizer_scrubs_pii_from_visible_prose():
    out = sanitize_html(_FILLED_FORM)
    assert "recruiter@example.com" not in out
    assert "[email]" in out and "[phone]" in out


def _shape(**over):
    base = {
        "found": True,
        "method": "post",
        "enctype": "multipart/form-data",
        "file_input_count": 1,
        "submit_button_count": 1,
    }
    return base | over


def _req(**over):
    base = {
        "url": "https://jobs.lever.co/x/apply",
        "method": "POST",
        "resource_type": "document",
        "is_navigation": True,
        "content_type": "multipart/form-data",
        "has_post_data": True,
        "phase": "submitting",
    }
    return RequestRecord(**(base | over))


def test_navigating_multipart_post_has_no_tripwires():
    assert evaluate_tripwires([_req()], _shape()) == []


def test_xhr_submit_trips():
    findings = evaluate_tripwires(
        [_req(resource_type="xhr", is_navigation=False, content_type="application/json")],
        _shape(),
    )
    assert [f["tripwire"] for f in findings] == ["SUBMIT_IS_XHR"]


def test_async_resume_upload_trips():
    findings = evaluate_tripwires(
        [_req(), _req(phase="file_selected", url="https://x/parse-resume", resource_type="xhr")],
        _shape(),
    )
    assert "ASYNC_UPLOAD" in [f["tripwire"] for f in findings]


def test_multiple_file_inputs_trips():
    findings = evaluate_tripwires([_req()], _shape(file_input_count=2))
    assert "MULTIPLE_FILE_INPUTS" in [f["tripwire"] for f in findings]


def test_missing_submit_is_reported_rather_than_assumed_fine():
    findings = evaluate_tripwires([], _shape())
    assert "NO_SUBMIT_OBSERVED" in [f["tripwire"] for f in findings]


def test_redacted_request_drops_the_query_string():
    r = _req(url="https://jobs.lever.co/x/apply?token=SECRET&id=99")
    assert r.redacted()["url"] == "https://jobs.lever.co/x/apply"


def test_form_shape_tripwires_readable_from_blank_form_alone():
    assert form_shape_tripwires(_shape()) == []
    assert [f["tripwire"] for f in form_shape_tripwires(_shape(found=False))] == ["NO_FORM_FOUND"]
    assert [f["tripwire"] for f in form_shape_tripwires(_shape(method="get"))] == [
        "FORM_METHOD_NOT_POST"
    ]
    assert [f["tripwire"] for f in form_shape_tripwires(_shape(enctype=""))] == [
        "FORM_ENCTYPE_NOT_MULTIPART"
    ]
    assert [f["tripwire"] for f in form_shape_tripwires(_shape(file_input_count=2))] == [
        "MULTIPLE_FILE_INPUTS"
    ]


def test_form_shape_tripwires_matches_the_real_lever_markup_confirmed_by_curl():
    """The exact shape curl-fetched from two live jobs.lever.co postings:
    method=post, enctype=multipart/form-data, no tripwires. This is the
    real-markup baseline the earlier ATS transport probe measured."""
    real_shape = {
        "found": True,
        "method": "post",
        "enctype": "multipart/form-data",
        "file_input_count": 1,
        "submit_button_count": 1,
    }
    assert form_shape_tripwires(real_shape) == []


def test_evaluate_tripwires_still_includes_form_shape_findings():
    findings = evaluate_tripwires([_req()], _shape(method="get", enctype=""))
    tripwires = {f["tripwire"] for f in findings}
    assert "FORM_METHOD_NOT_POST" in tripwires
    assert "FORM_ENCTYPE_NOT_MULTIPART" in tripwires
