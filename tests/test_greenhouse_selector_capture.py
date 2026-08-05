"""Guards for the capture tool. Its output is destined for committed fixtures,
so a sanitisation hole would commit the operator's personal data."""

from __future__ import annotations

from scripts.greenhouse_selector_capture import (
    RequestRecord,
    evaluate_tripwires,
    form_shape_tripwires,
    sanitize_html,
)

_FILLED_FORM = """
<form id="application_form" method="POST" enctype="multipart/form-data"
      action="/apply?token=SECRET">
  <script>var tracking = "beacon";</script>
  <!-- internal comment -->
  <label for="first_name">First Name *</label>
  <input id="first_name" name="first_name" type="text" value="Ali" required>
  <label for="email">Email</label>
  <input id="email" name="email" type="email" value="ahamed@parallelwireless.com">
  <label for="phone">Phone</label>
  <input id="phone" name="phone" type="tel" value="+972 50 123 4567">
  <label for="cover">Cover Letter</label>
  <textarea id="cover" name="cover">Dear hiring manager, I am Ali and my ID is 123456789.</textarea>
  <input id="resume" name="resume" type="file" accept=".pdf">
  <p>Reach the recruiter at recruiter@example.com or +1 (555) 867-5309.</p>
  <button type="submit">Submit Application</button>
</form>
"""


def test_sanitizer_drops_every_typed_value():
    out = sanitize_html(_FILLED_FORM)
    for leaked in ("Ali", "ahamed@parallelwireless.com", "972 50 123 4567", "123456789"):
        assert leaked not in out, f"sanitised output leaked {leaked!r}"
    assert "Dear hiring manager" not in out, "textarea body must be dropped"
    assert "SECRET" not in out, "action query string must be dropped"
    assert "tracking" not in out and "internal comment" not in out


def test_sanitizer_keeps_the_structure_a_selector_needs():
    out = sanitize_html(_FILLED_FORM)
    for kept in (
        'id="application_form"',
        'name="first_name"',
        'type="file"',
        'accept=".pdf"',
        "First Name",
        "Cover Letter",
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
        "url": "https://boards.greenhouse.io/x/apply",
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


def test_async_upload_trips():
    findings = evaluate_tripwires(
        [_req(), _req(phase="file_selected", url="https://x/upload", resource_type="xhr")],
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
    r = _req(url="https://boards.greenhouse.io/x/apply?token=SECRET&id=99")
    assert r.redacted()["url"] == "https://boards.greenhouse.io/x/apply"


def test_form_shape_tripwires_readable_from_blank_form_alone():
    """These three need no keystroke, upload or submit to detect."""
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


def test_form_shape_tripwires_matches_a_real_greenhouse_react_shell():
    """The exact shape curl-fetched from a live job-boards.greenhouse.io posting:
    method=get, no enctype, zero data-field-id. This is what the early-exit in
    main() must catch before the operator ever touches Step 2."""
    real_shape = {
        "found": True,
        "method": "get",
        "enctype": "",
        "file_input_count": 2,
        "submit_button_count": 1,
    }
    findings = [f["tripwire"] for f in form_shape_tripwires(real_shape)]
    assert "FORM_METHOD_NOT_POST" in findings
    assert "FORM_ENCTYPE_NOT_MULTIPART" in findings


def test_evaluate_tripwires_still_includes_form_shape_findings():
    """evaluate_tripwires (the end-of-run check) must not regress now that it
    delegates the form-shape portion to form_shape_tripwires."""
    findings = evaluate_tripwires([_req()], _shape(method="get", enctype=""))
    tripwires = {f["tripwire"] for f in findings}
    assert "FORM_METHOD_NOT_POST" in tripwires
    assert "FORM_ENCTYPE_NOT_MULTIPART" in tripwires
