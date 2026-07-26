"""Exact selected-CV evidence seam for versioned adapter inspectors."""

from __future__ import annotations

from dataclasses import dataclass
from profile.cv_content_cache import (
    CVArtifactBindingError,
    get_selected_cv_artifact_by_id,
    require_current_selected_cv_artifact,
)
from profile.cv_facts import build_selected_cv_fact_catalog
from profile.models import SelectedCVArtifact, SelectedCVFactCatalog, UserProfile

from core.form_planning import AnswerPolicyContext


@dataclass(frozen=True, slots=True)
class SelectedCVPlanningBinding:
    """Private in-memory binding shared by form policy and an inspector."""

    selected_cv: SelectedCVArtifact
    fact_catalog: SelectedCVFactCatalog

    def __post_init__(self) -> None:
        if self.fact_catalog.pdf_sha256 != self.selected_cv.pdf_sha256:
            raise CVArtifactBindingError("CV_FACT_CATALOG_BINDING_MISMATCH")

    @property
    def resume_path(self) -> str:
        """Local path for the adapter; never include it in events or responses."""

        return self.selected_cv.resolved_path

    def require_current(self) -> SelectedCVPlanningBinding:
        require_current_selected_cv_artifact(
            self.selected_cv,
            expected_sha256=self.fact_catalog.pdf_sha256,
        )
        return self

    def redacted_diagnostics(self) -> dict[str, str | int]:
        """Return bounded hashes/counts only, never facts, text, or a local path."""

        return {
            "selected_cv_hash": self.selected_cv.pdf_sha256,
            "fact_catalog_schema_version": self.fact_catalog.schema_version,
            "fact_catalog_digest": self.fact_catalog.catalog_sha256,
            "fact_count": len(self.fact_catalog.facts),
        }

    def answer_policy_context(
        self,
        *,
        profile: UserProfile,
        profile_version: int,
        adapter_name: str,
        adapter_version: str,
        selector_version: str,
        form_fingerprint: str,
        locale: str = "en",
        attached_cv_id: str | None = None,
        attached_cv_hash: str | None = None,
        attachment_verified: bool = False,
    ) -> AnswerPolicyContext:
        """Construct the only context an adapter should use for selected-CV facts."""

        self.require_current()
        return AnswerPolicyContext(
            profile=profile,
            profile_version=profile_version,
            selected_cv_id=self.selected_cv.cv_id,
            selected_cv_hash=self.selected_cv.pdf_sha256,
            adapter_name=adapter_name,
            adapter_version=adapter_version,
            selector_version=selector_version,
            form_fingerprint=form_fingerprint,
            locale=locale,
            attached_cv_id=attached_cv_id,
            attached_cv_hash=attached_cv_hash,
            attachment_verified=attachment_verified,
            selected_cv_fact_catalog=self.fact_catalog,
        )


def resolve_selected_cv_planning_binding(
    *,
    selected_cv_id: str,
    expected_cv_hash: str,
    cv_routing_path: str,
    cv_directory: str,
) -> SelectedCVPlanningBinding:
    """Resolve, prove, extract, and re-prove one reviewed selected CV."""

    selected = get_selected_cv_artifact_by_id(
        selected_cv_id,
        cv_routing_path=cv_routing_path,
        cv_directory=cv_directory,
    )
    if selected is None:
        raise CVArtifactBindingError("CV_ARTIFACT_UNAVAILABLE")
    require_current_selected_cv_artifact(
        selected,
        expected_sha256=expected_cv_hash,
    )
    catalog = build_selected_cv_fact_catalog(selected.artifact)
    require_current_selected_cv_artifact(
        selected,
        expected_sha256=expected_cv_hash,
    )
    return SelectedCVPlanningBinding(
        selected_cv=selected,
        fact_catalog=catalog,
    )
