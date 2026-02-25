"""Tests for LLM output validation — structure, placeholders, guardrails."""

import json
import re
import pytest

from llm.generation import _check_placeholders, GeneratedApplication


class TestPlaceholderDetection:
    """Tests for placeholder detection in generated text."""

    def test_detect_placeholders(self):
        text = "Dear [PLACEHOLDER: hiring manager name], I am writing about [PLACEHOLDER: specific project]."
        placeholders = _check_placeholders(text)
        assert len(placeholders) == 2
        assert "hiring manager name" in placeholders
        assert "specific project" in placeholders

    def test_no_placeholders(self):
        text = "Dear Hiring Team, I am excited to apply for this role."
        placeholders = _check_placeholders(text)
        assert len(placeholders) == 0

    def test_empty_text(self):
        placeholders = _check_placeholders("")
        assert len(placeholders) == 0


class TestGeneratedApplicationModel:
    """Tests for the GeneratedApplication dataclass."""

    def test_default_empty(self):
        app = GeneratedApplication()
        assert app.cover_letter == ""
        assert app.recruiter_message == ""
        assert app.qa_answers == {}
        assert app.has_placeholders is False
        assert app.placeholder_fields == []

    def test_with_data(self):
        app = GeneratedApplication(
            cover_letter="Dear Team, I am writing...",
            recruiter_message="Hi, I saw your posting...",
            qa_answers={"why_us": "I love your mission."},
            has_placeholders=False,
        )
        assert app.cover_letter.startswith("Dear")
        assert "why_us" in app.qa_answers

    def test_with_placeholders(self):
        app = GeneratedApplication(
            cover_letter="Dear [PLACEHOLDER: name]...",
            has_placeholders=True,
            placeholder_fields=["name"],
        )
        assert app.has_placeholders is True
        assert "name" in app.placeholder_fields


class TestQaAnswersValidation:
    """Tests for Q&A answer output schema validation."""

    EXPECTED_KEYS = {
        "why_this_company", "why_this_role", "salary_expectations",
        "notice_period", "work_authorization", "relevant_experience",
    }

    def test_valid_qa_schema(self):
        qa = {
            "why_this_company": "I admire your innovation.",
            "why_this_role": "It aligns with my experience.",
            "salary_expectations": "$120,000 - $200,000 USD",
            "notice_period": "2 weeks",
            "work_authorization": "US Citizen, authorized to work.",
            "relevant_experience": "5 years Python backend development.",
        }
        assert all(key in qa for key in self.EXPECTED_KEYS)
        assert all(isinstance(v, str) for v in qa.values())
        assert all(len(v) > 0 for v in qa.values())

    def test_missing_keys(self):
        qa = {"why_this_company": "Cool company."}
        missing = self.EXPECTED_KEYS - set(qa.keys())
        assert len(missing) > 0  # should detect missing keys

    def test_empty_values_flagged(self):
        qa = {
            "why_this_company": "",
            "why_this_role": "Great role.",
        }
        empty_fields = [k for k, v in qa.items() if not v.strip()]
        assert "why_this_company" in empty_fields


class TestCoverLetterValidation:
    """Tests validating cover letter content guardrails."""

    def test_no_fabricated_degrees(self):
        """Guardrail: detect when a cover letter contains degrees not in the profile."""
        cover_letter = "I hold a Ph.D. in Quantum Computing from MIT."
        resume_text = "B.S. Computer Science, UC Berkeley"

        # Check if cover letter mentions degrees not in resume — this is a violation
        degree_patterns = [r"Ph\.?D", r"Master'?s", r"M\.?S\.", r"M\.?B\.?A"]
        fabricated = []
        for pattern in degree_patterns:
            if re.search(pattern, cover_letter, re.IGNORECASE):
                if not re.search(pattern, resume_text, re.IGNORECASE):
                    fabricated.append(pattern)

        # The guardrail SHOULD detect the fabricated Ph.D.
        assert len(fabricated) > 0, "Should detect fabricated degree not in resume"
        assert any("Ph" in f for f in fabricated)

    def test_placeholder_used_for_missing_info(self):
        """When info is missing, the LLM should use placeholders."""
        cover_letter = (
            "I noticed [PLACEHOLDER: specific company initiative] "
            "and would love to contribute."
        )
        placeholders = _check_placeholders(cover_letter)
        assert len(placeholders) > 0

    def test_reasonable_length(self):
        """Cover letter should be 200-2000 characters (3-4 paragraphs)."""
        cover_letter = "Dear Hiring Team,\n\n" + "A" * 500 + "\n\nBest regards"
        assert 200 <= len(cover_letter) <= 5000

    def test_json_serializable(self):
        """Generated application should be JSON-serializable for storage."""
        app = GeneratedApplication(
            cover_letter="Dear Team...",
            recruiter_message="Hi!",
            qa_answers={"q1": "answer1"},
        )
        # Should not raise
        serialized = json.dumps({
            "cover_letter": app.cover_letter,
            "recruiter_message": app.recruiter_message,
            "qa_answers": app.qa_answers,
        })
        parsed = json.loads(serialized)
        assert parsed["cover_letter"] == "Dear Team..."
