from profile.cv_content_cache import clear_cv_text_cache, get_cv_text_by_id


def test_cv_content_cache():
    clear_cv_text_cache()
    # Test getting text by ID using real config
    text = get_cv_text_by_id("ai-engineer")
    assert "AI Engineer" in text or "Ali Hamed" in text
    assert len(text) > 500
