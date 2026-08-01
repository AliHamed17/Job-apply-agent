"""Build the deterministic, sanitized 240-case v5 fit/routing dataset."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tests" / "fixtures" / "v5" / "fit_routing_240.json"

FAMILIES = (
    {
        "id": "ai-ml",
        "en": "Machine Learning Engineer",
        "he": "מהנדס למידת מכונה",
        "en_skills": ["python", "pytorch"],
        "he_skills": ["פייתון", "למידה-עמוקה"],
    },
    {
        "id": "data-science",
        "en": "Data Scientist",
        "he": "מדען נתונים",
        "en_skills": ["python", "pandas"],
        "he_skills": ["פייתון", "סטטיסטיקה"],
    },
    {
        "id": "data-engineering",
        "en": "Data Engineer",
        "he": "מהנדס נתונים",
        "en_skills": ["python", "spark"],
        "he_skills": ["פייתון", "airflow"],
    },
    {
        "id": "backend-software",
        "en": "Backend Engineer",
        "he": "מפתח צד-שרת",
        "en_skills": ["java", "spring"],
        "he_skills": ["גאווה", "microservices"],
    },
    {
        "id": "fullstack",
        "en": "Fullstack Developer",
        "he": "מפתח פולסטאק",
        "en_skills": ["typescript", "react"],
        "he_skills": ["טייפסקריפט", "ריאקט"],
    },
    {
        "id": "qa-automation",
        "en": "QA Automation Engineer",
        "he": "בודק אוטומציה",
        "en_skills": ["python", "selenium"],
        "he_skills": ["פייתון", "playwright"],
    },
    {
        "id": "devops",
        "en": "DevOps Platform Engineer",
        "he": "מהנדס דבאופס פלטפורמה",
        "en_skills": ["kubernetes", "terraform"],
        "he_skills": ["קוברנטיס", "טרפורם"],
    },
    {
        "id": "cloud-infrastructure",
        "en": "Cloud Infrastructure Engineer",
        "he": "מהנדס תשתיות ענן",
        "en_skills": ["aws", "networking"],
        "he_skills": ["איי-דבליו-אס", "networking"],
    },
    {
        "id": "embedded-firmware",
        "en": "Embedded Firmware Engineer",
        "he": "מהנדס קושחה משובצת",
        "en_skills": ["c++", "rtos"],
        "he_skills": ["סי-פלוס-פלוס", "זמן-אמת"],
    },
    {
        "id": "telecom-networking",
        "en": "Telecom Networking Engineer",
        "he": "מהנדס רשתות תקשורת",
        "en_skills": ["5g", "sctp"],
        "he_skills": ["דור-חמישי", "פרוטוקולים"],
    },
    {
        "id": "junior-software",
        "en": "Junior Software Engineer",
        "he": "מפתח תוכנה מתחיל",
        "en_skills": ["python", "git"],
        "he_skills": ["פייתון", "גיט"],
        "seniority_en": "junior",
        "seniority_he": "מתחיל",
    },
    {
        "id": "internship",
        "en": "Software Intern",
        "he": "מתמחה תוכנה",
        "en_skills": ["python", "university"],
        "he_skills": ["פייתון", "אוניברסיטה"],
        "seniority_en": "internship",
        "seniority_he": "מתמחה",
        "employment": "Internship",
    },
)


def _case(family: dict, index: int) -> dict:
    hebrew = index >= 10
    language = "he" if hebrew else "en"
    skills = list(family[f"{language}_skills"])
    title = family[language]
    seniority = family.get(f"seniority_{language}", "בכיר" if hebrew else "senior")
    employment = family.get("employment", "Full-time")
    location = "תל אביב, ישראל" if hebrew else "Tel Aviv, Israel"
    requirements = (
        f"5 שנים {' '.join(skills)}. עברית חובה. אישור עבודה בישראל."
        if hebrew
        else f"5 years {' '.join(skills)}. Fluent English. Authorized to work in Israel."
    )
    profile = {
        "years_experience": "6 years",
        "work_authorization": "Authorized in Israel",
        "visa_sponsorship": "No",
        "languages": "English, Hebrew",
    }
    mode = index % 10
    disposition = "eligible"
    quality_eligible = True
    if mode == 3:
        unsupported_skill = "terraform" if "terraform" not in skills else "sctp"
        skills.append(unsupported_skill)
        requirements += f" {unsupported_skill} required."
        disposition, quality_eligible = "needs_review", False
    elif mode == 5:
        location = "Berlin, Germany"
        disposition, quality_eligible = "excluded", False
    elif mode == 6:
        location = "Remote - US only"
        disposition, quality_eligible = "excluded", False
    elif mode == 7:
        location = "עבודה מרחוק" if hebrew else "Remote"
        disposition, quality_eligible = "needs_review", False
    elif mode == 8:
        profile.pop("work_authorization")
        disposition, quality_eligible = "needs_review", False
    elif mode == 9:
        profile["years_experience"] = "2 years"
        disposition, quality_eligible = "excluded", False
    elif mode in {2, 4}:
        location = "עבודה מרחוק עולמי" if hebrew else "Remote Worldwide"

    return {
        "id": f"{family['id']}-{language}-{index:02d}",
        "family": family["id"],
        "language": language,
        "split": "holdout" if index in {3, 4, 17, 19} else "train",
        "job": {
            "title": title,
            "company": "Sanitized Employer",
            "location": location,
            "employment_type": employment,
            "seniority": seniority,
            "description": "Sanitized evidence-only role fixture.",
            "requirements": requirements,
            "source_url": "https://example.test/sanitized-job",
            "keywords": skills,
        },
        "confirmed_profile_facts": profile,
        "expected_cv_id": family["id"],
        "expected_disposition": disposition,
        "expected_quality_eligible": quality_eligible,
    }


def build_dataset() -> dict:
    cases = [_case(family, index) for family in FAMILIES for index in range(20)]
    return {
        "schema_version": "fit-routing-dataset.v1",
        "description": "Sanitized synthetic bilingual routing and fit qualification cases.",
        "families": [family["id"] for family in FAMILIES],
        "cases": cases,
    }


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(build_dataset(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
