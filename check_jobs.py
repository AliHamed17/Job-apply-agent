from db.session import get_session_factory
from db.models import Job, Application, JobStatus, ExtractedURL

def check_jobs():
    factory = get_session_factory()
    db = factory()
    
    print("\n--- URL Stats ---")
    urls = db.query(ExtractedURL).all()
    print(f"Total URLs: {len(urls)}")
    for u in urls:
        print(f"ID: {u.id} | Status: {u.status} | URL: {u.normalized_url[:60]}...")

    print("\n--- Job Stats ---")
    jobs = db.query(Job).all()
    print(f"Total Jobs: {len(jobs)}")
    for j in jobs:
        print(f"ID: {j.id} | Status: {j.status} | Title: {j.title}")
        
    print("\n--- Application Stats ---")
    apps = db.query(Application).all()
    print(f"Total Apps: {len(apps)}")
    for a in apps:
        print(f"ID: {a.id} | Status: {a.status} | Job ID: {a.job_id}")
        
    db.close()

if __name__ == "__main__":
    check_jobs()
