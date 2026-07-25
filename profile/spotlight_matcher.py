"""Ali Hamed's Autonomous Portfolio & Project Spotlight Matcher."""

from __future__ import annotations

from dataclasses import dataclass, field
import structlog

from jobs.models import JobData
from profile.models import UserProfile

logger = structlog.get_logger(__name__)


@dataclass
class ProjectSpotlightMatch:
    spotlight_title: str
    relevant_keywords: list[str] = field(default_factory=list)
    showcase_text: str = ""


def match_portfolio_spotlight(job: JobData, profile: UserProfile) -> ProjectSpotlightMatch:
    """Match job technical requirements to Ali's specific project spotlights."""
    text = (f"{job.title} {job.description} {job.requirements}").lower()

    if any(kw in text for kw in ["ai", "llm", "rag", "pytorch", "langchain", "faiss", "agent"]):
        return ProjectSpotlightMatch(
            spotlight_title="Production AI Agent Tools",
            relevant_keywords=["AI", "LLM", "RAG", "PyTorch", "Python"],
            showcase_text=(
                "Architected and deployed 3 production AI agent tools using Python, PyTorch, and RAG architectures, "
                "improving automated candidate-job alignment accuracy and data processing efficiency."
            ),
        )

    if any(kw in text for kw in ["devops", "docker", "kubernetes", "k8s", "aws", "ci/cd", "pipeline", "jenkins"]):
        return ProjectSpotlightMatch(
            spotlight_title="75% Build-to-Deploy CI/CD Acceleration",
            relevant_keywords=["Docker", "Kubernetes", "CI/CD", "Jenkins", "AWS"],
            showcase_text=(
                "Engineered containerized build-to-deploy pipelines with Docker and Kubernetes, achieving a "
                "75% speedup in deployment cycles and ensuring robust infrastructure scalability."
            ),
        )

    return ProjectSpotlightMatch(
        spotlight_title="200-500 PyTest & Robot Test Suites",
        relevant_keywords=["PyTest", "Robot Framework", "Python", "Automation", "QA"],
        showcase_text=(
            "Designed and implemented automated test suites encompassing 200-500 PyTest and Robot Framework test cases, "
            "guaranteeing continuous software reliability and zero-regression releases."
        ),
    )
