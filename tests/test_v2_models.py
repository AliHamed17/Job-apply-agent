"""Tests for Task 3.1: AnswerCache + OutboundContact models + v2 columns."""


def test_new_models_and_columns_import():
    from db.models import AnswerCache, OutboundContact, Job, Application, JobStatus
    assert hasattr(Job, "discovery_source")
    assert hasattr(Job, "easy_apply")
    assert hasattr(Application, "submission_channel")
    assert JobStatus.NEEDS_REVIEW.value == "needs_review"
    assert AnswerCache.__tablename__ == "answer_cache"
    assert OutboundContact.__tablename__ == "outbound_contacts"


def test_answer_cache_roundtrip(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from db.models import Base, AnswerCache
    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(AnswerCache(question_hash="h1", question_text="Years of Python?",
                       answer="5", source="llm"))
    db.commit()
    assert db.query(AnswerCache).filter_by(question_hash="h1").one().answer == "5"
