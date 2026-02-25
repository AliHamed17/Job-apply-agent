"""Tests for job parsing — JSON-LD, HTML heuristic, and platform parsers."""

import json
import pytest

from jobs.models import JobData
from jobs.parsers.jsonld import parse_jsonld
from jobs.parsers.html_heuristic import parse_html_heuristic


class TestJsonLdParser:
    """Tests for JSON-LD (Schema.org JobPosting) parser."""

    def _make_html(self, jsonld_data: dict | list) -> str:
        """Wrap JSON-LD data in a minimal HTML page."""
        return f"""
        <html><head>
        <script type="application/ld+json">{json.dumps(jsonld_data)}</script>
        </head><body></body></html>
        """

    def test_single_job_posting(self):
        data = {
            "@context": "https://schema.org/",
            "@type": "JobPosting",
            "title": "Senior Python Developer",
            "hiringOrganization": {"@type": "Organization", "name": "TechCorp"},
            "jobLocation": {
                "@type": "Place",
                "address": {
                    "@type": "PostalAddress",
                    "addressLocality": "San Francisco",
                    "addressRegion": "CA",
                    "addressCountry": "US",
                }
            },
            "employmentType": "FULL_TIME",
            "datePosted": "2024-01-15",
            "description": "<p>We are looking for a Senior Python Developer.</p>",
        }
        html = self._make_html(data)
        jobs = parse_jsonld(html, "https://example.com/job/123")

        assert len(jobs) == 1
        job = jobs[0]
        assert job.title == "Senior Python Developer"
        assert job.company == "TechCorp"
        assert "San Francisco" in job.location
        assert job.employment_type == "full-time"
        assert job.seniority == "senior"
        assert "Senior Python Developer" in job.description or "Python Developer" in job.description
        assert job.date_posted == "2024-01-15"

    def test_graph_with_multiple_postings(self):
        data = {
            "@context": "https://schema.org/",
            "@graph": [
                {"@type": "JobPosting", "title": "Frontend Dev",
                 "hiringOrganization": {"name": "Co1"}},
                {"@type": "JobPosting", "title": "Backend Dev",
                 "hiringOrganization": {"name": "Co1"}},
                {"@type": "WebPage", "name": "Careers Page"},  # non-job
            ]
        }
        html = self._make_html(data)
        jobs = parse_jsonld(html, "https://example.com/careers")

        assert len(jobs) == 2
        titles = {j.title for j in jobs}
        assert "Frontend Dev" in titles
        assert "Backend Dev" in titles

    def test_remote_job(self):
        data = {
            "@type": "JobPosting",
            "title": "Remote Engineer",
            "hiringOrganization": {"name": "RemoteCo"},
            "jobLocationType": "TELECOMMUTE",
        }
        html = self._make_html(data)
        jobs = parse_jsonld(html, "https://example.com/remote-job")

        assert len(jobs) == 1
        assert "Remote" in jobs[0].location

    def test_no_jsonld(self):
        html = "<html><body><h1>Regular page</h1></body></html>"
        jobs = parse_jsonld(html, "https://example.com")
        assert jobs == []

    def test_invalid_jsonld(self):
        html = '<html><head><script type="application/ld+json">not valid json</script></head></html>'
        jobs = parse_jsonld(html, "https://example.com")
        assert jobs == []

    def test_jsonld_without_jobposting(self):
        data = {"@type": "Organization", "name": "SomeCo"}
        html = self._make_html(data)
        jobs = parse_jsonld(html, "https://example.com")
        assert jobs == []

    def test_employment_type_mapping(self):
        for schema_type, expected in [
            ("FULL_TIME", "full-time"),
            ("PART_TIME", "part-time"),
            ("CONTRACT", "contract"),
            ("INTERN", "internship"),
        ]:
            data = {
                "@type": "JobPosting",
                "title": "Test Role",
                "employmentType": schema_type,
            }
            html = self._make_html(data)
            jobs = parse_jsonld(html, "https://example.com/job")
            assert len(jobs) == 1
            assert jobs[0].employment_type == expected

    def test_multiple_locations(self):
        data = {
            "@type": "JobPosting",
            "title": "Multi-location Role",
            "jobLocation": [
                {"@type": "Place", "address": {"addressLocality": "NYC"}},
                {"@type": "Place", "address": {"addressLocality": "LA"}},
            ],
        }
        html = self._make_html(data)
        jobs = parse_jsonld(html, "https://example.com/job")

        assert len(jobs) == 1
        assert "NYC" in jobs[0].location
        assert "LA" in jobs[0].location

    def test_seniority_detection(self):
        for title, expected_seniority in [
            ("Junior Developer", "junior"),
            ("Senior Software Engineer", "senior"),
            ("Lead Backend Engineer", "lead"),
            ("Principal Architect", "lead"),
            ("Staff Engineer", "senior"),
            ("Engineering Manager", "manager"),
            ("VP of Engineering", "director"),
            ("Software Developer", ""),  # no seniority
        ]:
            data = {"@type": "JobPosting", "title": title}
            html = self._make_html(data)
            jobs = parse_jsonld(html, "https://example.com/job")
            if jobs:
                assert jobs[0].seniority == expected_seniority, \
                    f"Title '{title}' => expected '{expected_seniority}', got '{jobs[0].seniority}'"

    def test_salary_extraction(self):
        data = {
            "@type": "JobPosting",
            "title": "Dev",
            "baseSalary": {
                "@type": "MonetaryAmount",
                "currency": "USD",
                "value": {"minValue": "100000", "maxValue": "150000"},
            },
        }
        html = self._make_html(data)
        jobs = parse_jsonld(html, "https://example.com")
        assert len(jobs) == 1
        assert any("salary" in kw.lower() or "USD" in kw for kw in jobs[0].keywords)

    def test_html_stripped_from_description(self):
        data = {
            "@type": "JobPosting",
            "title": "Dev",
            "description": "<p>Build <b>awesome</b> stuff.</p><ul><li>Python</li></ul>",
        }
        html = self._make_html(data)
        jobs = parse_jsonld(html, "https://example.com")
        assert len(jobs) == 1
        assert "<p>" not in jobs[0].description
        assert "<b>" not in jobs[0].description
        assert "Python" in jobs[0].description


class TestHtmlHeuristicParser:
    """Tests for the generic HTML heuristic fallback parser."""

    def test_basic_job_page(self):
        html = """
        <html><body>
        <h1>Python Developer</h1>
        <div class="company-name">TechCorp</div>
        <div class="location">San Francisco, CA</div>
        <div class="job-description">
            We are looking for a Python Developer to join our team.
            Responsibilities: Build APIs. Requirements: 3+ years experience.
            Qualifications: BS in CS. Apply now to this exciting position.
        </div>
        <a href="https://example.com/apply">Apply</a>
        </body></html>
        """
        jobs = parse_html_heuristic(html, "https://example.com/jobs/python-dev")
        assert len(jobs) == 1
        assert jobs[0].title == "Python Developer"

    def test_non_job_page(self):
        html = """
        <html><body>
        <h1>Welcome to Our Blog</h1>
        <p>This is a blog post about cooking recipes.</p>
        </body></html>
        """
        jobs = parse_html_heuristic(html, "https://example.com/blog")
        assert jobs == []

    def test_empty_html(self):
        jobs = parse_html_heuristic("", "https://example.com")
        assert jobs == []

    def test_page_with_job_indicators(self):
        html = """
        <html><body>
        <h1>DevOps Engineer</h1>
        <div class="job-description">
            Position: DevOps Engineer.
            Responsibilities include managing infrastructure.
            Requirements: experience with AWS.
            Benefits include health insurance and stock options.
            Salary competitive. Apply through our portal.
        </div>
        </body></html>
        """
        jobs = parse_html_heuristic(html, "https://example.com/devops")
        assert len(jobs) == 1
        assert "DevOps" in jobs[0].title
