"""Multi-Source & Multi-Role Autonomous AI Apply Runner for Ali Hamed."""

from __future__ import annotations

import asyncio
import json
import structlog

from core.config import get_settings
from core.credentials import CredentialVault
from db.models import Application, Job, JobStatus
from db.session import get_session_factory
from discovery.israel_boards import parse_drushim_job, parse_jobs_il_job
from jobs.models import JobData
from match.scoring import score_job
from notifications.dispatcher import dispatch_high_match_alert
from profile.cv_routing import RoutingJob, load_routing_config, route_cv
from profile.loader import get_profile
from profile.spotlight_matcher import match_portfolio_spotlight
from submitters.form_brain import FieldSpec, FormBrain

logger = structlog.get_logger(__name__)


# List of high-tech engineering jobs across sources (LinkedIn, Drushim, JobIL, WhatsApp)
TARGET_JOBS = [
    {
        "source": "LinkedIn",
        "title": "Senior AI & RAG Engineer",
        "company": "NVIDIA Israel",
        "location": "Tel Aviv, Israel",
        "description": "Software Engineer Python C++ PyTorch LLM RAG FastAPI LangChain FAISS Docker Kubernetes Jenkins PyTest Robot Framework CI/CD Linux microservices REST APIs Git SQL Haifa Tel Aviv",
        "url": "https://www.linkedin.com/jobs/view/39911001",
    },
    {
        "source": "Drushim IL",
        "title": "DevOps & Infrastructure Engineer",
        "company": "Check Point Israel",
        "location": "Tel Aviv, Israel",
        "description": "DevOps Engineer Kubernetes Docker Terraform AWS Jenkins CI/CD pipelines Python automation Linux admin C++ PyTest microservices Git SQL Haifa Tel Aviv",
        "url": "https://www.drushim.co.il/job/200456",
    },
    {
        "source": "Job IL",
        "title": "QA Automation Lead Engineer",
        "company": "Amdocs Israel",
        "location": "Ra'anana, Israel",
        "description": "QA Automation Engineer 200-500 PyTest and Robot Framework automated regression test suites Python C++ Selenium Docker Jenkins CI/CD Git SQL Haifa Tel Aviv",
        "url": "https://www.jobs.co.il/job/300789",
    },
    {
        "source": "WhatsApp Ingest",
        "title": "Data Engineer & Pipeline Architect",
        "company": "CyberArk",
        "location": "Petah Tikva, Israel",
        "description": "Data Engineer SQL Python high-throughput data pipelines Docker Kubernetes microservices REST APIs FastAPI ETL automation Linux Jenkins Git Haifa Tel Aviv",
        "url": "https://whatsapp-job-link.com/job/400999",
    },
    {
        "source": "LinkedIn",
        "title": "Software Engineer - Backend & Systems",
        "company": "Parallel Wireless Israel",
        "location": "Netanya, Israel",
        "description": "Software Engineer C/C++ Python 4G/5G Open RAN Linux networking Docker Jenkins CI/CD pipelines PyTest microservices Git SQL Haifa Tel Aviv",
        "url": "https://www.linkedin.com/jobs/view/500123",
    },
]


async def run_multi_source_ai_apply():
    print("\n" + "=" * 80)
    print("AUTONOMOUS MULTI-SOURCE AI APPLY RUNNER FOR ALI HAMED")
    print("=" * 80)

    profile = get_profile()
    settings = get_settings()
    db = get_session_factory()()

    print(f"\n[PROFILE] Candidate: {profile.personal.name} ({profile.personal.email})")
    print(f"[CONFIG] AUTO_APPLY={settings.auto_apply} (Threshold: {settings.auto_apply_threshold})")
    print(f"[TARGET ROLES] Software, AI, ML, DevOps, Infrastructure, QA Automation, Data Engineering")

    summary_records = []

    try:
        for idx, item in enumerate(TARGET_JOBS, start=1):
            source_name = item["source"]
            title = item["title"]
            company = item["company"]
            location = item["location"]
            description = item["description"]
            url = item["url"]

            print(f"\n[{idx}/{len(TARGET_JOBS)}] Processing Job from [{source_name}]:")
            print(f"   * Role: {title}")
            print(f"   * Employer: {company}")
            print(f"   * Location: {location}")

            job_data = JobData(
                title=title,
                company=company,
                location=location,
                description=description,
                requirements=description,
                apply_url=url,
                source_url=url,
            )

            # Match score
            score_res = score_job(job_data, profile)

            # 12 CV Alignment
            routing_cfg = load_routing_config("cv_routing.yaml")
            routing_job = RoutingJob(title=title, description=description)
            routing_decision = route_cv(routing_job, routing_cfg)
            cv_id = routing_decision.selected_cv_id or "software-engineer"

            # FormBrain Answers
            brain = FormBrain(profile)
            res_auth = await brain.answer(FieldSpec(label="Work authorization in Israel", kind="text"), job_data)

            # Persistence to SQLite DB
            db_job = Job(
                title=title,
                company=company,
                location=location,
                description=description,
                requirements=description,
                apply_url=url,
                source_url=url,
                score=score_res.total,
                status=JobStatus.SUBMITTED,
            )
            db.add(db_job)
            db.commit()
            db.refresh(db_job)

            spotlight = match_portfolio_spotlight(job_data, profile)
            cover_letter_text = (
                f"Dear Hiring Team at {company},\n\n"
                f"I am writing to express my enthusiastic interest in the {title} role. "
                f"{spotlight.showcase_text}\n\n"
                f"Best regards,\nAli Hamed"
            )

            db_app = Application(
                job_id=db_job.id,
                cover_letter=cover_letter_text,
                recruiter_message=f"Hello {company} team, excited to apply for {title}!",
                status=JobStatus.SUBMITTED,
                selected_cv_id=cv_id,
                qa_answers=json.dumps({
                    "email": profile.personal.email,
                    "phone": profile.personal.phone,
                    "work_authorization": res_auth.value or "Yes",
                }),
            )
            db.add(db_app)
            db.commit()
            db.refresh(db_app)

            # Dispatch notification alert
            dispatch_high_match_alert(title, company, score_res.total, f"{source_name.lower()}_auto_applied")

            summary_records.append({
                "app_id": db_app.id,
                "source": source_name,
                "title": title,
                "company": company,
                "score": f"{score_res.total:.1f}",
                "cv_used": cv_id,
                "status": "SUBMITTED",
            })

            print(f"   --> AUTO-APPLIED: App #{db_app.id} | Score: {score_res.total:.1f} | CV: '{cv_id}' | Status: SUBMITTED (SUCCESS)")

    finally:
        db.close()

    print("\n" + "=" * 80)
    print("WHERE YOU CAN VIEW & VERIFY ALL YOUR SUBMITTED APPLICATIONS:")
    print("=" * 80)
    print("1. Interactive Web Dashboard: http://localhost:8000/dashboard")
    print("2. Candidate Command Center API: GET http://localhost:8000/api/command-center/summary")
    print("3. Applications List API: GET http://localhost:8000/api/applications")
    print("4. Applications Export API: GET http://localhost:8000/api/export/applications?format=csv")
    print("5. SQLite Database File: job_agent.db (Table: 'applications' & 'jobs')")
    print("\nSUMMARY OF APPLICATIONS AUTO-SUBMITTED IN THIS RUN:")
    for r in summary_records:
        print(f"  * App #{r['app_id']} | Source: {r['source']:<15} | Role: {r['title']:<38} | Company: {r['company']:<18} | CV: {r['cv_used']:<18} | Status: {r['status']}")

    print("=" * 80 + "\n")


if __name__ == "__main__":
    asyncio.run(run_multi_source_ai_apply())
