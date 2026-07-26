"""Persistence helpers for CV-bound, evidence-validated application materials."""

from __future__ import annotations

import json
from profile.models import SelectedCVArtifact
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from db.models import Application
    from llm.generation import GeneratedApplication


_BLOCKER_ALIASES = {
    "UNSUPPORTED_FACTUAL_CLAIM": "UNSUPPORTED_CLAIM",
}


def invalidate_material_audit(application: Application) -> None:
    """Revoke material eligibility after the routed CV identity changes."""

    application.selected_cv_hash = None
    application.material_eligible = False
    application.material_blockers_json = '["MATERIAL_NOT_ELIGIBLE"]'
    application.material_claims_json = "[]"
    application.material_model_provider = None
    application.material_model_name = None
    application.material_model_digest = None
    application.material_prompt_version = None


def normalized_material_blockers(generated: GeneratedApplication) -> list[str]:
    """Return stable, de-duplicated blocker codes suitable for persistence."""

    blockers = list(
        dict.fromkeys(
            _BLOCKER_ALIASES.get(str(blocker), str(blocker))
            for blocker in generated.eligibility_blockers
            if str(blocker)
        )
    )
    if generated.has_placeholders and "UNFILLED_PLACEHOLDER" not in blockers:
        blockers.append("UNFILLED_PLACEHOLDER")
    return blockers


def persist_material_audit(
    application: Application,
    generated: GeneratedApplication,
    selected_cv: SelectedCVArtifact | None,
) -> list[str]:
    """Bind generated material audit data to the exact routed CV bytes."""

    from llm.contracts import is_qualified_material_identity
    from llm.generation import MaterialPackageV1

    blockers = normalized_material_blockers(generated)
    package = (
        generated.material_package
        if isinstance(generated.material_package, MaterialPackageV1)
        else None
    )
    qualified_identity = bool(
        package is not None
        and is_qualified_material_identity(
            provider=package.model_identity.provider,
            model=package.model_identity.model,
            local=package.model_identity.local,
            digest=package.model_identity.digest,
            prompt_version=package.prompt_version,
        )
    )
    if (
        package is not None
        and not qualified_identity
        and "MATERIAL_MODEL_NOT_QUALIFIED" not in blockers
    ):
        blockers.append("MATERIAL_MODEL_NOT_QUALIFIED")
    application.selected_cv_hash = selected_cv.pdf_sha256 if selected_cv is not None else None
    application.material_eligible = bool(
        selected_cv is not None
        and generated.eligible
        and generated.cv_sha256 == selected_cv.pdf_sha256
        and qualified_identity
    )
    if selected_cv is None and "MATERIAL_CV_ARTIFACT_REQUIRED" not in blockers:
        blockers.append("MATERIAL_CV_ARTIFACT_REQUIRED")
    if (
        selected_cv is not None
        and generated.cv_sha256 is not None
        and generated.cv_sha256 != selected_cv.pdf_sha256
        and "MATERIAL_CV_MISMATCH" not in blockers
    ):
        blockers.append("MATERIAL_CV_MISMATCH")
        application.material_eligible = False
    if not application.material_eligible and not blockers:
        blockers.append("MATERIAL_NOT_ELIGIBLE")

    claims = [claim.model_dump(mode="json") for claim in generated.claim_evidence]
    application.material_blockers_json = json.dumps(blockers, separators=(",", ":"))
    application.material_claims_json = json.dumps(claims, separators=(",", ":"))
    application.material_model_provider = (
        package.model_identity.provider if package is not None else None
    )
    application.material_model_name = package.model_identity.model if package is not None else None
    application.material_model_digest = (
        package.model_identity.digest if package is not None else None
    )
    application.material_prompt_version = package.prompt_version if package is not None else None
    return blockers


def material_review_reason(
    *,
    selected_cv_id: str | None,
    routing_fallback_reason: str | None,
    blockers: list[str],
    placeholder_fields: list[str],
) -> str | None:
    """Choose one bounded reason code for the application review queue."""

    if blockers:
        return blockers[0]
    if not selected_cv_id or routing_fallback_reason:
        return "CV_ROUTING_REVIEW_REQUIRED"
    if placeholder_fields:
        return "UNFILLED_PLACEHOLDERS"
    return None
