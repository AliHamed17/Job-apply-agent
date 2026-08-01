"""Read-only inventory for versioned ATS adapter capabilities."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from core.adapter_qualification_service import effective_registered_descriptors
from db.session import get_db
from submitters.platforms import registered_adapters

router = APIRouter(tags=["ats-adapters"])


class AdapterCapabilityResponse(BaseModel):
    ats: str
    adapter_version: str
    selector_version: str
    execution_contract_version: str | None
    transport: str
    authentication_mode: str
    supported_controls: list[str] = Field(default_factory=list)
    qualification_tier: str
    qualified_form_scope: list[str] = Field(default_factory=list)
    final_execution_enabled: bool


@router.get("/ats/adapters", response_model=list[AdapterCapabilityResponse])
async def list_ats_adapters(
    db: Session = Depends(get_db),
) -> list[AdapterCapabilityResponse]:
    """Expose the bounded registry used by inspection and final execution."""
    if isinstance(db, Session):
        try:
            descriptors = effective_registered_descriptors(db)
        except SQLAlchemyError:
            # A missing/unavailable authority store may reduce capability but
            # can never elevate the immutable fixture-only inventory.
            db.rollback()
            descriptors = registered_adapters()
    else:
        descriptors = registered_adapters()
    return [
        AdapterCapabilityResponse(
            ats=descriptor.platform,
            adapter_version=descriptor.adapter_version,
            selector_version=descriptor.selector_version,
            execution_contract_version=descriptor.execution_contract_version,
            transport=descriptor.transport,
            authentication_mode=descriptor.authentication_mode,
            supported_controls=list(descriptor.supported_controls),
            qualification_tier=descriptor.qualification.value,
            qualified_form_scope=list(descriptor.qualified_form_scope),
            final_execution_enabled=descriptor.allows_final_execution,
        )
        for descriptor in descriptors
    ]
