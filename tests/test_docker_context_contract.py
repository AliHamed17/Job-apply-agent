import tomllib
from pathlib import Path

import yaml


def test_docker_context_excludes_private_data_and_keeps_public_templates() -> None:
    rules = Path(".dockerignore").read_text(encoding="utf-8").splitlines()

    assert ".env" in rules
    assert "*.pdf" in rules
    assert "cvs/" in rules
    assert "user_profile.yaml" in rules
    assert "user_profile.yaml.*" in rules
    assert "cv_routing.yaml" in rules
    assert "cv_routing.yaml.*" in rules
    assert "employer_catalog.yaml" in rules
    assert "employer_catalog.yaml.*" in rules
    assert ".gmail_oauth.json" in rules
    assert ".gmail_oauth.json.*" in rules
    assert ".job-alerts/" in rules
    assert ".portal_profiles/" in rules
    assert ".linkedin_profile/" in rules

    assert rules.index("!user_profile.yaml.example") > rules.index("user_profile.yaml.*")
    assert rules.index("!cv_routing.yaml.example") > rules.index("cv_routing.yaml.*")
    assert rules.index("!employer_catalog.yaml.example") > rules.index(
        "employer_catalog.yaml.*"
    )


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


def test_celery_worker_concurrency_is_bounded_within_its_memory_limit() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    environment_example = Path(".env.example").read_text(encoding="utf-8")
    worker = compose["services"]["celery-worker"]

    assert "--concurrency=2" in dockerfile
    assert "--concurrency=${CELERY_WORKER_CONCURRENCY:-2}" in worker["command"]
    assert worker["deploy"]["resources"]["limits"]["memory"] == "512M"
    assert "CELERY_WORKER_CONCURRENCY=2" in environment_example


def test_enterprise_ca_is_an_optional_buildkit_secret_without_tls_bypass() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    helper_path = Path("scripts/docker_build_with_optional_ca.sh")
    helper = helper_path.read_text(encoding="utf-8")
    marker = Path("config/empty-enterprise-ca.txt").read_text(encoding="utf-8")
    attributes = Path(".gitattributes").read_text(encoding="utf-8")

    assert "*.sh text eol=lf" in attributes
    assert b"\r\n" not in helper_path.read_bytes()
    assert dockerfile.count("--mount=type=secret,id=enterprise_ca,required=false") == 3
    assert dockerfile.count("docker_build_with_optional_ca.sh") == 3
    assert compose["secrets"]["job-agent-enterprise-ca"]["file"] == (
        "${JOB_AGENT_ENTERPRISE_CA_FILE:-./config/empty-enterprise-ca.txt}"
    )
    for service_name in ("web-api", "celery-worker", "celery-beat"):
        assert compose["services"][service_name]["build"]["secrets"] == [
            {"source": "job-agent-enterprise-ca", "target": "enterprise_ca"}
        ]

    assert "PIP_CERT" in helper
    assert "SSL_CERT_FILE" in helper
    assert "NODE_EXTRA_CA_CERTS" in helper
    assert "PRIVATE KEY" in helper
    assert "BEGIN CERTIFICATE" not in marker
    assert "PRIVATE KEY" not in marker
    combined = "\n".join((dockerfile, helper)).casefold()
    for forbidden in ("--trusted-host", "pip_trusted_host", "verify=false", "curl -k"):
        assert forbidden not in combined
