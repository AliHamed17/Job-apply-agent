"""Israeli job board parsing — Drushim, AllJobs, JobMaster (jobs/parsers/).

Ali is based in Haifa and these are where most Israeli engineering roles are
posted, so the agent could not see the majority of its own market without
them.

The fixtures are Hebrew on purpose. Encoding is the risk worth pinning: this
project already had a real cp1252 mangling incident, and a description that
silently becomes mojibake would poison both scoring and CV routing while
still looking like a successful parse. So these assert on actual Hebrew
substrings rather than on lengths or truthiness.
"""

from __future__ import annotations

import pytest

from jobs.extractor import extract_jobs
from jobs.parsers.israeli_boards import is_israeli_board, parse_israeli_board

DRUSHIM_URL = "https://www.drushim.co.il/job/12345/"
ALLJOBS_URL = "https://www.alljobs.co.il/SearchResultsGuest.aspx?page=1&position=1712"
JOBMASTER_URL = "https://www.jobmaster.co.il/jobs/98765"

DRUSHIM_POSTING = """
<html lang="he" dir="rtl"><body>
  <h1 class="job-title">מהנדס תוכנה Backend</h1>
  <div class="company-name">אלביט מערכות</div>
  <div class="job-location">חיפה</div>
  <div class="job-body">
    <h3>תיאור התפקיד</h3>
    <p>פיתוח שירותי Backend בפייתון עבור מערכות זמן אמת.</p>
    <h3>דרישות התפקיד</h3>
    <p>ניסיון של 3 שנים בפייתון, ידע ב-Docker ו-Kubernetes.</p>
  </div>
  <a class="apply-button" href="/apply/12345">הגשת מועמדות</a>
</body></html>
"""

ALLJOBS_POSTING = """
<html lang="he" dir="rtl"><body>
  <h1 class="jobTitle">מפתח/ת AI</h1>
  <span class="jobCompany">חברת הייטק מובילה</span>
  <span class="jobLocation">תל אביב</span>
  <div>
    <div>תיאור המשרה</div>
    <div>בניית מודלים ו-RAG pipelines בסביבת ענן.</div>
  </div>
  <div>
    <div>דרישות</div>
    <div>תואר ראשון במדעי המחשב, ניסיון ב-PyTorch.</div>
  </div>
</body></html>
"""

JOBMASTER_POSTING = """
<html lang="he" dir="rtl"><body>
  <h1>בודק/ת תוכנה אוטומציה</h1>
  <div class="employer-name">מטריקס</div>
  <div class="location">הרצליה</div>
  <main>
    <h2>תיאור התפקיד</h2>
    <p>כתיבת תשתיות בדיקה אוטומטיות ב-Pytest.</p>
  </main>
</body></html>
"""

DRUSHIM_RESULTS = """
<html lang="he" dir="rtl"><body>
  <div class="job-item">
    <a href="/job/111/"><h2 class="job-title">מהנדס DevOps</h2></a>
    <div class="company-name">אמדוקס</div>
    <div class="job-location">רעננה</div>
  </div>
  <div class="job-item">
    <a href="/job/222/"><h2 class="job-title">מהנדס נתונים</h2></a>
    <div class="company-name">אינטל</div>
    <div class="job-location">חיפה</div>
  </div>
</body></html>
"""


# ── routing ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [DRUSHIM_URL, ALLJOBS_URL, JOBMASTER_URL, "https://drushim.co.il/x"],
)
def test_recognises_israeli_boards(url):
    assert is_israeli_board(url) is True


@pytest.mark.parametrize(
    "url",
    ["https://boards.greenhouse.io/acme/jobs/1", "https://www.linkedin.com/jobs/view/1"],
)
def test_does_not_claim_other_boards(url):
    assert is_israeli_board(url) is False


# ── posting pages ─────────────────────────────────────────────────────


def test_drushim_posting_parsed_with_hebrew_intact():
    jobs = parse_israeli_board(DRUSHIM_POSTING, DRUSHIM_URL)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "מהנדס תוכנה Backend"
    assert job.company == "אלביט מערכות"
    assert job.location == "חיפה"
    # The description/requirements split is the whole reason this parser
    # exists — the generic heuristic collapses them into one blob.
    assert "פיתוח שירותי Backend" in job.description
    assert "ניסיון של 3 שנים" in job.requirements
    assert job.apply_url.endswith("/apply/12345")


def test_alljobs_posting_parsed():
    jobs = parse_israeli_board(ALLJOBS_POSTING, ALLJOBS_URL)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "מפתח/ת AI"
    assert job.company == "חברת הייטק מובילה"
    assert "RAG pipelines" in job.description
    assert "PyTorch" in job.requirements


def test_jobmaster_posting_parsed_without_requirements_block():
    """A posting with no requirements section must still yield a usable job."""
    jobs = parse_israeli_board(JOBMASTER_POSTING, JOBMASTER_URL)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "בודק/ת תוכנה אוטומציה"
    assert "Pytest" in job.description
    assert job.requirements == ""


def test_results_page_yields_every_card():
    jobs = parse_israeli_board(DRUSHIM_RESULTS, DRUSHIM_URL)
    assert len(jobs) == 2
    assert {j.title for j in jobs} == {"מהנדס DevOps", "מהנדס נתונים"}
    assert {j.company for j in jobs} == {"אמדוקס", "אינטל"}
    # Relative hrefs must be absolutised or the job cannot be fetched later.
    assert all(j.apply_url.startswith("https://www.drushim.co.il/job/") for j in jobs)


# ── robustness ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "html",
    [
        "",
        "   ",
        "<html><body><p>לא נמצאו משרות</p></body></html>",
        "<html><body><div><span>unclosed",  # malformed
        "<html><body><h1></h1></body></html>",  # empty title
    ],
)
def test_unrecognised_or_broken_html_returns_empty_not_raises(html):
    assert parse_israeli_board(html, DRUSHIM_URL) == []


def test_placeholder_titles_are_rejected():
    """JobData.is_complete guards template leakage; confirm we honour it."""
    html = "<html><body><h1 class='job-title'>{{position.name}}</h1></body></html>"
    assert parse_israeli_board(html, DRUSHIM_URL) == []


# ── dispatch wiring ───────────────────────────────────────────────────


def test_extractor_routes_israeli_urls_to_this_parser():
    result = extract_jobs(DRUSHIM_POSTING, DRUSHIM_URL)
    assert result.parser_used == "israeli_board"
    assert result.jobs[0].title == "מהנדס תוכנה Backend"


def test_extractor_still_prefers_jsonld_when_present():
    """The board parser must not shadow structured data if a board emits it."""
    jsonld = """
    <html><body>
    <script type="application/ld+json">
    {"@type":"JobPosting","title":"Data Scientist","hiringOrganization":{"name":"Acme"}}
    </script>
    <h1 class="job-title">משהו אחר</h1>
    </body></html>
    """
    result = extract_jobs(jsonld, DRUSHIM_URL)
    assert result.parser_used == "jsonld"


def test_hebrew_survives_the_full_extractor_path():
    """Guards the cp1252 class of bug end to end, not just in the parser."""
    result = extract_jobs(ALLJOBS_POSTING, ALLJOBS_URL)
    job = result.jobs[0]
    assert "מפתח" in job.title
    # Mojibake signature: Hebrew mis-decoded as latin-1 produces these.
    assert "×" not in job.title
    assert "Ã" not in job.description


# ── Fail closed on non-posting pages ────────────────────────────────────────
# _parse_detail accepted any non-empty title, _TITLE_SELECTORS ends in bare
# h1/h2/h3, and JobData.is_complete requires only a 3+ character title that is
# not a template placeholder. A CAPTCHA interstitial therefore produced a
# confident JobData(title="Are you a robot?"), which was then scored, generated
# for, and applied to.

_DETAIL_URL = "https://www.drushim.co.il/job/12345/"


@pytest.mark.parametrize(
    "html,label",
    [
        ("<html><body><h1>Are you a robot?</h1><p>Please verify.</p></body></html>", "captcha"),
        (
            "<html><head><title>Access Denied</title></head><body><h1>Forbidden</h1></body></html>",
            "403",
        ),
        ("<html><body><h1>Page not found</h1></body></html>", "soft404"),
        ("<html><body><h1>אין הרשאה</h1></body></html>", "hebrew_forbidden"),
        ("<html><body><h1>הדף לא נמצא</h1></body></html>", "hebrew_not_found"),
        ("<html><body><h1>Please enable JavaScript</h1></body></html>", "js_wall"),
    ],
)
def test_challenge_and_error_pages_yield_no_jobs(html, label):
    assert parse_israeli_board(html, _DETAIL_URL) == [], f"{label} page must not yield a job"


def test_bare_heading_with_no_job_signal_yields_no_jobs():
    """A generic heading alone is site chrome, not a posting."""
    html = "<html><body><h1>Drushim</h1><p>Welcome to our site.</p></body></html>"
    assert parse_israeli_board(html, _DETAIL_URL) == []


def test_real_posting_still_parses():
    html = """
    <html><body>
      <h1 class="job-title">מהנדס תוכנה משובץ</h1>
      <div class="company-name">Parallel Wireless</div>
      <div class="job-location">פתח תקווה</div>
      <div>תיאור התפקיד: פיתוח קושחה בסביבת לינוקס.</div>
      <div>דרישות התפקיד: C, C++, לינוקס.</div>
    </body></html>
    """
    jobs = parse_israeli_board(html, _DETAIL_URL)
    assert len(jobs) == 1
    assert jobs[0].title == "מהנדס תוכנה משובץ"
    assert jobs[0].company == "Parallel Wireless"


def test_posting_with_only_a_bare_title_but_real_company_still_parses():
    """Corroboration, not a specific selector, is what makes a page a posting."""
    html = """
    <html><body>
      <h1>Embedded Software Engineer</h1>
      <div class="company-name">Parallel Wireless</div>
      <div>תיאור התפקיד: firmware work.</div>
    </body></html>
    """
    jobs = parse_israeli_board(html, _DETAIL_URL)
    assert len(jobs) == 1
    assert jobs[0].title == "Embedded Software Engineer"


def test_marker_in_description_does_not_discard_a_real_posting():
    """The marker scan is head-only, so body prose cannot cause a false reject."""
    html = """
    <html><head><title>מהנדס תוכנה - דרושים</title></head><body>
      <h1 class="job-title">Embedded Software Engineer</h1>
      <div class="company-name">Parallel Wireless</div>
      <div>תיאור התפקיד: debug why a device reports "page not found" and
      handle access denied responses from the CAPTCHA service.</div>
    </body></html>
    """
    jobs = parse_israeli_board(html, _DETAIL_URL)
    assert len(jobs) == 1
    assert jobs[0].company == "Parallel Wireless"
