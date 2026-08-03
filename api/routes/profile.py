"""Profile + resume upload routes."""

from __future__ import annotations

import re
from profile.cv_intake import CVIngestError, ingest_cv_from_temp, stream_to_temp
from profile.loader import get_profile, load_profile_snapshot
from profile.models import Personal, ProfileEvidence, UserProfile
from profile.readiness import (
    email_is_valid,
    phone_is_valid,
    profile_discovery_readiness_issues,
    profile_preparation_readiness_issues,
    profile_submission_readiness_issues,
)
from profile.writer import profile_write_transaction, save_profile

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from core.config import Settings, get_settings
from db.session import get_db
from worker.rescore import auto_prepare_scored_jobs_if_ready, enqueue_pending_job_rescore

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/profile", tags=["profile"])

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# (payload field, evidence.user_confirmed key). Legal facts carry an explicit
# jurisdiction suffix so a resolver can never satisfy a question that names one
# country using a fact confirmed for another.
#
# The suffix is separated by an underscore, not a colon: canonical_fact_key in
# profile/models.py rewrites every non-alphanumeric character to "_", so a
# colon-suffixed key silently normalises to the underscore form anyway. The
# evidence key namespace is [a-z0-9_] only, and the matching evidence_ref is
# profile:user_confirmed:<canonical_key>.
_JURISDICTION_FACT_KEYS = (
    ("work_authorization_il", "work_authorization_il"),
    ("work_authorization_us", "work_authorization_us"),
    ("work_authorization_eu", "work_authorization_eu"),
    ("sponsorship_il", "visa_sponsorship_il"),
    ("sponsorship_us", "visa_sponsorship_us"),
    ("sponsorship_eu", "visa_sponsorship_eu"),
)

# Facts recurring on nearly every ATS application, stored under their own name.
# years_experience is handled separately: it derives two keys from one input.
_RECURRING_FACT_KEYS = (
    "security_clearance",
    "notice_period",
    "salary_expectation",
    "salary_currency",
    "availability_date",
    "languages",
    "relocation",
    "work_mode",
    "highest_degree",
    "how_did_you_hear",
    "demographic_disclosure",
)


class OnboardingProfileUpdate(BaseModel):
    """Exact operator-confirmed identity and legal facts; never LLM-derived."""

    model_config = ConfigDict(extra="forbid")

    legal_name: str = Field(min_length=2, max_length=200)
    primary_email: str = Field(min_length=3, max_length=320)
    phone: str = Field(min_length=7, max_length=40)
    location: str = Field(min_length=2, max_length=200)
    search_locations: list[str] = Field(min_length=1, max_length=20)
    work_authorization: str = Field(min_length=1, max_length=300)
    sponsorship: str = Field(min_length=1, max_length=300)
    citizenship: str = Field(default="", max_length=200)
    nationality: str = Field(default="", max_length=200)
    gender: str | None = Field(default=None, max_length=100)
    disability: str | None = Field(default=None, max_length=100)
    ethnicity: str | None = Field(default=None, max_length=100)
    veteran_status: str | None = Field(default=None, max_length=100)

    # ── Jurisdiction-scoped legal facts ─────────────────────────────────
    # An Israeli authorisation fact must never answer a United States
    # authorisation question. The flat work_authorization/sponsorship fields
    # above remain the jurisdiction-*unspecified* answer and therefore satisfy
    # no jurisdiction-named question; these suffixed keys are what a resolver
    # matches against a label that names a country. A jurisdiction with no
    # confirmed fact abstains, which is the safe outcome.
    work_authorization_il: str = Field(default="", max_length=300)
    work_authorization_us: str = Field(default="", max_length=300)
    work_authorization_eu: str = Field(default="", max_length=300)
    sponsorship_il: str = Field(default="", max_length=300)
    sponsorship_us: str = Field(default="", max_length=300)
    sponsorship_eu: str = Field(default="", max_length=300)

    # ── Recurring non-legal facts ATS forms ask on nearly every application ──
    security_clearance: str = Field(default="", max_length=200)
    notice_period: str = Field(default="", max_length=100)
    salary_expectation: str = Field(default="", max_length=50)
    salary_currency: str = Field(default="", max_length=10)
    availability_date: str = Field(default="", max_length=40)
    years_experience: str = Field(default="", max_length=10)
    languages: str = Field(default="", max_length=300)
    relocation: str = Field(default="", max_length=100)
    work_mode: str = Field(default="", max_length=100)
    highest_degree: str = Field(default="", max_length=200)
    how_did_you_hear: str = Field(default="", max_length=200)
    demographic_disclosure: str = Field(default="", max_length=20)

    @field_validator("years_experience")
    @classmethod
    def validate_years_experience(cls, value: str) -> str:
        if value and not re.fullmatch(r"\d{1,2}(?:\.\d)?", value):
            raise ValueError("years_experience must be a number such as '2' or '2.5'")
        return value

    @field_validator("demographic_disclosure")
    @classmethod
    def validate_demographic_disclosure(cls, value: str) -> str:
        if value and value not in {"decline", "disclose"}:
            raise ValueError("demographic_disclosure must be 'decline' or 'disclose'")
        return value

    @field_validator("*", mode="before")
    @classmethod
    def strip_text(cls, value):
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("*")
    @classmethod
    def reject_control_characters(cls, value):
        if isinstance(value, str) and _CONTROL_RE.search(value):
            raise ValueError("control characters are not allowed")
        return value

    @field_validator("primary_email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if not email_is_valid(value):
            raise ValueError("enter a valid email address")
        return value

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        if not phone_is_valid(value):
            raise ValueError("enter a valid international phone number")
        return value

    @field_validator("search_locations")
    @classmethod
    def validate_search_locations(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            clean = str(value).strip()
            if not 2 <= len(clean) <= 200:
                raise ValueError("each search location must contain 2 to 200 characters")
            if _CONTROL_RE.search(clean):
                raise ValueError("control characters are not allowed")
            if clean not in normalized:
                normalized.append(clean)
        if not normalized:
            raise ValueError("confirm at least one search location")
        return normalized

    @model_validator(mode="after")
    def require_citizenship_or_nationality(self):
        if not self.citizenship and not self.nationality:
            raise ValueError("confirm citizenship or nationality")
        return self


def _stage_readiness(profile: UserProfile) -> dict[str, dict[str, object]]:
    def stage(reasons: list[str]) -> dict[str, object]:
        return {"ready": not reasons, "reason_codes": reasons}

    return {
        "discovery": stage(profile_discovery_readiness_issues(profile)),
        "preparation": stage(profile_preparation_readiness_issues(profile)),
        "submission": stage(profile_submission_readiness_issues(profile)),
    }


def _onboarding_payload(profile: UserProfile) -> dict[str, object]:
    confirmed = profile.evidence.user_confirmed
    payload: dict[str, object] = {
        "legal_name": profile.personal.name,
        "primary_email": profile.personal.email,
        "phone": profile.personal.phone,
        "location": profile.personal.location,
        "search_locations": list(profile.preferences.locations),
        "work_authorization": confirmed.get("work_authorization", ""),
        "sponsorship": confirmed.get("visa_sponsorship", ""),
        "citizenship": confirmed.get("citizenship", ""),
        "nationality": confirmed.get("nationality", ""),
        "gender": confirmed.get("gender"),
        "disability": confirmed.get("disability"),
        "ethnicity": confirmed.get("ethnicity"),
        "veteran_status": confirmed.get("veteran_status"),
        "readiness": _stage_readiness(profile),
    }
    for field_name, key in _JURISDICTION_FACT_KEYS:
        payload[field_name] = confirmed.get(key, "")
    for key in _RECURRING_FACT_KEYS:
        payload[key] = confirmed.get(key, "")
    # Echoed back in the operator's input shape ("2"), not the derived one.
    payload["years_experience"] = confirmed.get("years_experience_number", "")
    # The exact stored facts, so the operator can see what a form will resolve
    # against without inferring it from the fields above.
    payload["confirmed"] = dict(confirmed)
    return payload


@router.post("/resume")
async def upload_resume(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Accept a CV PDF, rebuild the profile, re-score queued jobs."""
    try:
        tmp = await stream_to_temp(
            lambda: file.read(64 * 1024),
            settings.profile_path.parent,
            settings.max_resume_bytes,
        )
        result = await ingest_cv_from_temp(
            tmp, settings=settings, db=db, max_bytes=settings.max_resume_bytes
        )
    except CVIngestError as exc:
        raise HTTPException(
            status_code=413 if exc.code == "TOO_LARGE" else 422,
            detail={"code": exc.code, "message": exc.message},
        ) from exc

    logger.info("resume_uploaded", version=result["version"], rescored=result["rescored"])
    return result


@router.get("")
async def get_profile_summary():
    """Full profile summary — used by the dashboard and application form-filling."""
    p = get_profile()
    return {
        "name": p.personal.name,
        "email": p.personal.email,
        "phone": p.personal.phone,
        "location": p.personal.location,
        "linkedin": p.links.linkedin,
        "github": p.links.github,
        "portfolio": p.links.portfolio,
        "resume_pdf": p.resume.pdf_path,
        "roles": p.preferences.roles,
        "keywords": p.preferences.keywords,
        "skills": p.preferences.keywords[:20],
        "has_resume_pdf": bool(p.resume.pdf_path),
        "readiness": _stage_readiness(p),
    }


@router.get("/onboarding")
async def get_onboarding_profile(response: Response):
    """Return the private local onboarding form without any inferred facts."""

    response.headers["Cache-Control"] = "no-store"
    return _onboarding_payload(get_profile())


@router.put("/onboarding")
async def update_onboarding_profile(
    payload: OnboardingProfileUpdate,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Persist exact operator-confirmed facts as a new immutable profile version."""

    try:
        with profile_write_transaction(db):
            current = load_profile_snapshot(settings.profile_path)
            evidence = ProfileEvidence.model_validate(current.evidence.model_dump())
            confirmed = dict(evidence.user_confirmed)
            confirmed.update(
                {
                    "work_authorization": payload.work_authorization,
                    "visa_sponsorship": payload.sponsorship,
                }
            )
            for key in (
                "citizenship",
                "nationality",
                "gender",
                "disability",
                "ethnicity",
                "veteran_status",
            ):
                value = getattr(payload, key)
                if value is not None:
                    if value:
                        confirmed[key] = value
                    else:
                        confirmed.pop(key, None)

            # Jurisdiction-scoped legal facts. An empty value *removes* the key
            # rather than storing "", so an unset jurisdiction abstains instead
            # of resolving to an empty answer.
            for field_name, key in _JURISDICTION_FACT_KEYS:
                value = getattr(payload, field_name)
                if value:
                    confirmed[key] = value
                else:
                    confirmed.pop(key, None)

            for key in _RECURRING_FACT_KEYS:
                value = getattr(payload, key)
                if value:
                    confirmed[key] = value
                else:
                    confirmed.pop(key, None)

            # One operator input, two shapes: free-text fields want "2 years",
            # NUMBER controls want "2". Deriving both here keeps them from
            # drifting apart across thousands of applications.
            if payload.years_experience:
                confirmed["years_experience_number"] = payload.years_experience
                confirmed["years_experience"] = f"{payload.years_experience} years"
            else:
                confirmed.pop("years_experience_number", None)
                confirmed.pop("years_experience", None)

            evidence.user_confirmed = confirmed
            preferences = current.preferences.model_copy(deep=True)
            preferences.locations = payload.search_locations
            updated = UserProfile.model_validate(
                {
                    **current.model_dump(),
                    "personal": Personal(
                        name=payload.legal_name,
                        email=payload.primary_email,
                        phone=payload.phone,
                        location=payload.location,
                    ).model_dump(),
                    "preferences": preferences.model_dump(),
                    "evidence": evidence.model_dump(),
                }
            )
            onboarding_issues = [
                reason
                for reason in profile_preparation_readiness_issues(updated)
                if reason
                in {
                    "PROFILE_NAME_PLACEHOLDER",
                    "PROFILE_EMAIL_PLACEHOLDER",
                    "PROFILE_CURRENT_LOCATION_MISSING",
                    "PROFILE_CURRENT_LOCATION_PLACEHOLDER",
                    "PROFILE_SEARCH_LOCATIONS_MISSING",
                    "PROFILE_SEARCH_LOCATIONS_PLACEHOLDER",
                }
            ]
            if onboarding_issues:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": onboarding_issues[0],
                        "message": "Replace placeholder onboarding values with confirmed values.",
                    },
                )
            version = save_profile(updated, settings.profile_path, db=db)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("profile_onboarding_save_failed")
        raise HTTPException(
            status_code=500,
            detail={
                "code": "PROFILE_SAVE_FAILED",
                "message": "The confirmed profile could not be saved.",
            },
        ) from exc

    rescore_queued = (
        enqueue_pending_job_rescore(
            db,
            settings,
            expected_profile_version=version,
        )
        if db is not None
        else 0
    )
    auto_prepared = auto_prepare_scored_jobs_if_ready(db, settings) if db is not None else 0
    logger.info(
        "profile_onboarding_saved",
        profile_version=version,
        rescore_queued=rescore_queued,
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "profile_version": version,
        "rescored": 0,
        "rescore_queued": rescore_queued,
        "auto_prepared": auto_prepared,
        **_onboarding_payload(updated),
    }
