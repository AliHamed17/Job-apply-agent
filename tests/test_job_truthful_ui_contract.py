"""Static browser contracts for truthful job-level submission styling."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "api/static/js/app.js").read_text(encoding="utf-8")


def test_job_table_green_style_requires_exact_employer_verified_flag() -> None:
    assert "job.employer_verified === true ? 'submitted'" in APP_JS
    assert "job.status === 'submitted' ? 'unverified'" in APP_JS
    assert "job.display_status || 'unverified'" in APP_JS


def test_job_csv_exports_truth_derived_status_and_verification() -> None:
    assert "'Employer Verified'" in APP_JS
    assert "j.display_status || (j.status === 'submitted' ? 'unverified'" in APP_JS
    assert "j.employer_verified === true ? 'yes' : 'no'" in APP_JS
