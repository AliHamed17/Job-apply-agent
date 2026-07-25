"""Display only employer-verified submissions, with private content omitted."""

from __future__ import annotations

from core.submission_truth import latest_employer_verified_query
from db.models import Application, Job, Submission
from db.session import get_session_factory


def display_submitted_records():
    db = get_session_factory()()
    try:
        verified_rows = (
            latest_employer_verified_query(db)
            .join(Application, Application.id == Submission.application_id)
            .join(Job, Job.id == Application.job_id)
            .order_by(Submission.submitted_at.desc())
            .limit(10)
            .all()
        )

        print("\n" + "=" * 90)
        print("EMPLOYER-VERIFIED SUBMISSION RECORDS (job_agent.db)")
        print("=" * 90)

        if not verified_rows:
            print("No employer-verified submission records found.")
            return

        for attempt in verified_rows:
            app = attempt.application
            job = app.job
            print(f"\n[VERIFIED] APPLICATION RECORD #{app.id}")
            print(f"   * Job ID:              #{job.id}")
            print(f"   * Job Title:           {job.title}")
            print(f"   * Company:             {job.company}")
            print(f"   * Location:            {job.location}")
            score = f"{job.score:.1f}/100.0" if job.score is not None else "not scored"
            print(f"   * Match Score:         {score}")
            print(f"   * Attempt:             {attempt.attempt_number}")
            print(f"   * ATS Adapter:         {attempt.submitter_name}")
            print(f"   * Verification Code:   {attempt.reason_code}")
            print(f"   * Verified At (UTC):   {attempt.submitted_at}")
            print("-" * 90)

        print(f"\nTOTAL EMPLOYER-VERIFIED RECORDS DISPLAYED: {len(verified_rows)}")
        print("=" * 90 + "\n")

    finally:
        db.close()


if __name__ == "__main__":
    display_submitted_records()
