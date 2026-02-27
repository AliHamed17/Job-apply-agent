from db.session import get_session_factory
from db.models import ExtractedURL, Job, Message

def check():
    factory = get_session_factory()
    db = factory()
    
    print("--- Database Status ---")
    print(f"Messages: {db.query(Message).count()}")
    print(f"ExtractedURLs: {db.query(ExtractedURL).count()}")
    print(f"Jobs: {db.query(Job).count()}")
    
    print("\n--- URL Details ---")
    urls = db.query(ExtractedURL).all()
    for u in urls:
        print(f"ID: {u.id} | Status: {u.status} | URL: {u.normalized_url[:50]}...")
        if u.fetch_error:
            print(f"  Error: {u.fetch_error}")
            
    db.close()

if __name__ == "__main__":
    check()
