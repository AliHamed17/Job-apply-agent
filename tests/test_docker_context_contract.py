import tomllib
from pathlib import Path


def test_docker_context_excludes_private_data_and_keeps_public_templates() -> None:
    rules = Path(".dockerignore").read_text(encoding="utf-8").splitlines()

    assert ".env" in rules
    assert "*.pdf" in rules
    assert "cvs/" in rules
    assert "user_profile.yaml" in rules
    assert "user_profile.yaml.*" in rules
    assert "cv_routing.yaml" in rules
    assert "cv_routing.yaml.*" in rules
    assert ".portal_profiles/" in rules
    assert ".linkedin_profile/" in rules

    assert rules.index("!user_profile.yaml.example") > rules.index("user_profile.yaml.*")
    assert rules.index("!cv_routing.yaml.example") > rules.index("cv_routing.yaml.*")


def test_web_image_bootstrap_uses_the_public_profile_template() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert "cp /app/user_profile.yaml.example /app/profile-data/user_profile.yaml" in dockerfile


def test_container_dependency_floor_covers_current_high_severity_fixes() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert '"setuptools>=78.1.1,<82"' in dockerfile
    assert "msgpack>=1.2.1,<2" in project["project"]["dependencies"]


def test_final_images_remove_build_only_python_packaging_tools() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert dockerfile.count("python -m pip uninstall -y setuptools wheel") == 2
    assert dockerfile.count("python -m pip uninstall -y pip") == 2

    web_image = dockerfile.split("FROM deps AS web-api", maxsplit=1)[1].split(
        "FROM deps AS celery-worker",
        maxsplit=1,
    )[0]
    worker_image = dockerfile.split("FROM deps AS celery-worker", maxsplit=1)[1]

    assert web_image.index('pip install -e ".[pdf,email,postgres]"') < web_image.index(
        "python -m pip uninstall -y pip"
    )
    assert worker_image.index("playwright install --with-deps chromium") < worker_image.index(
        "python -m pip uninstall -y pip"
    )
