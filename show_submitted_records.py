"""Query and display real submitted application records from SQLite database."""

from __future__ import annotations

import json
from db.models import Application, Job, JobStatus
from db.session import get_session_factory


def display_submitted_records():
    db = get_session_factory()()
    try:
        submitted_apps = (
            db.query(Application)
            .join(Job)
            .filter(Application.status == JobStatus.SUBMITTED)
            .order_by(Application.id.desc())
            .limit(10)
            .all()
        )

        print("\n" + "=" * 90)
        print("REAL DATABASE RECORDS: SUCCESSFULLY SUBMITTED APPLICATIONS (job_agent.db)")
        print("=" * 90)

        if not submitted_apps:
            print("No submitted records found in database.")
            return

        for app in submitted_apps:
            job = app.job
            print(f"\n[RECORD] APPLICATION RECORD #{app.id}")
            print(f"   * Job ID:              #{job.id}")
            print(f"   * Job Title:           {job.title}")
            print(f"   * Company:             {job.company}")
            print(f"   * Location:            {job.location}")
            print(f"   * Match Score:         {job.score:.1f}/100.0")
            print(f"   * Selected CV:         {app.selected_cv_id}")
            print(f"   * Application Status:  {app.status.value.upper()} (SUCCESS)")
            print(f"   * Created At (UTC):    {app.created_at}")

            try:
                qa = json.loads(app.qa_answers) if isinstance(app.qa_answers, str) else app.qa_answers
            except Exception:
                qa = app.qa_answers
            print(f"   * Form QA Answers:     {qa}")

            cl_snippet = (app.cover_letter or "").replace("\n", " ")[:120]
            print(f'   * Cover Letter Snippet: "{cl_snippet}..."')
            print("-" * 90)

        print(f"\nTOTAL SUBMITTED RECORDS DISPLAYED: {len(submitted_apps)}")
        print("=" * 90 + "\n")

    finally:
        db.close()


if __name__ == "__main__":
    display_submitted_records()
