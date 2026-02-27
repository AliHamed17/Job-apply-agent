from db.session import get_session_factory
from db.models import ExtractedURL, Job, Message, Application, Submission

def reset_db():
    factory = get_session_factory()
    db = factory()
    
    # Order matters for foreign keys
    print("Clearing submissions...")
    db.query(Submission).delete()
    print("Clearing applications...")
    db.query(Application).delete()
    print("Clearing jobs...")
    db.query(Job).delete()
    print("Clearing extracted_urls...")
    db.query(ExtractedURL).delete()
    print("Clearing messages...")
    db.query(Message).delete()
    
    db.commit()
    print("Database cleared!")
    db.close()

if __name__ == "__main__":
    reset_db()
