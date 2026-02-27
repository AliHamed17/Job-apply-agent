from db.session import get_session_factory
from db.models import ExtractedURL, URLStatus
from worker.tasks import process_url_task

def retrigger():
    factory = get_session_factory()
    db = factory()
    
    retriggerable = [URLStatus.PENDING, URLStatus.BLOCKED, URLStatus.FAILED]
    pending_urls = db.query(ExtractedURL).filter(ExtractedURL.status.in_(retriggerable)).all()
    print(f"Retriggering {len(pending_urls)} URLs (Pending/Blocked/Failed)...")
    
    for u in pending_urls:
        print(f"Processing ID: {u.id} | {u.normalized_url[:80]}")
        try:
            # We call apply() to run it synchronously here
            process_url_task.apply(args=[u.id])
        except Exception as e:
            print(f"  Error: {e}")
            
    db.close()

if __name__ == "__main__":
    retrigger()
