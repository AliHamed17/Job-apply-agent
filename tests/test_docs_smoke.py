from pathlib import Path
import yaml


def test_compose_has_beat_service():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    assert "celery-beat" in compose["services"]


def test_readme_documents_full_auto():
    txt = Path("README.md").read_text(encoding="utf-8").lower()
    assert "full_auto" in txt or "full-auto" in txt
    assert "discovery.login" in txt
    assert "min_apply_score" in txt
