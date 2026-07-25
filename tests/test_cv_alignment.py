from profile.cv_content_cache import clear_cv_text_cache, get_cv_text_by_id


def test_cv_content_cache_uses_configured_cv(tmp_path, monkeypatch):
    clear_cv_text_cache()
    cv_dir = tmp_path / "cvs"
    cv_dir.mkdir()
    pdf = cv_dir / "ai-engineer.pdf"
    pdf.write_bytes(b"%PDF-1.4 sanitized test fixture")
    routing = tmp_path / "cv_routing.yaml"
    routing.write_text(
        """
version: 1
minimum_confidence: 0.2
cvs:
  - id: ai-engineer
    file: ai-engineer.pdf
    role_families: [ai]
    skills: [python, pytorch]
fallback_cv_id: ai-engineer
overrides: []
""".strip()
        + "\n",
        encoding="utf-8",
    )
    extracted = "AI Engineer with Python and PyTorch experience. " * 20
    monkeypatch.setattr(
        "profile.cv_content_cache.extract_text_from_pdf",
        lambda _path: extracted,
    )

    text = get_cv_text_by_id(
        "ai-engineer",
        cv_routing_path=routing,
        cv_directory=cv_dir,
    )

    assert text == extracted
    assert len(text) > 500
